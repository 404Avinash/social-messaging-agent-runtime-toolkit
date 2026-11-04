# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Phone numbers routes: bind, unbind, list, update SNS destination."""
from __future__ import annotations

import re
from typing import NamedTuple

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import get_user_groups, has_permission, require_group
from shared.aws import REGION
from shared.http import error_response as _error, failure_response, json_response as _json
from shared.registry import get_registry as _get_registry, put_registry as _put_registry, get_all_registries as _get_all_registries
from shared.registry import read_all_registries as _read_all_registries
from shared.resolver import app, current_body, from_legacy

tracer = Tracer(service="admin-console-backend")
logger = Logger(service="admin-console-backend", child=True)

# The error EUM Social answers for a phone it does not know in the account and
# Region asked: a phone on none of the WABAs linked there.
_NOT_LINKED = "ResourceNotFoundException"


def _eum_failure(event: str, exc: Exception, stack_id: str) -> str:
    """Log *exc*, a failed EUM Social lookup of ``stack_id``'s phone, and
    return its error code, or its type when it has none.

    Call from an ``except`` block. An EUM Social error message can quote the
    phone's ARN, so a botocore error is a warning with the stack id, the
    error type and code, without the message or a traceback. Anything else,
    a fault in this code, keeps its traceback.
    """
    # stacklevel=3: the record names the function that handled the failure.
    if not isinstance(exc, (ClientError, BotoCoreError)):
        logger.exception(event, extra={"stack_id": stack_id}, stacklevel=3)
        return type(exc).__name__
    extra = {"stack_id": stack_id, "error_type": type(exc).__name__}
    code = None
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code:
            extra["error_code"] = code
    logger.warning(event, extra=extra, stacklevel=3)
    return code or type(exc).__name__


def _phone_region(phone_id: str, config: dict) -> str:
    """The Region of a registered phone: its ARN's, or else its stack's."""
    parts = phone_id.split(":")
    if len(parts) >= 6 and parts[0] == "arn" and parts[3]:
        return parts[3]
    return config.get("region") or REGION


class _WabaStacks(NamedTuple):
    """The registered stacks with a phone on a WABA, and those not checked.

    ``siblings`` pairs each stack's id and registry entry with its phone's
    EUM Social record. ``unchecked`` lists ``{stack, reason}`` for each
    registered stack that may be on the WABA but could not be checked.
    """

    siblings: list[tuple[str, dict, dict]]
    unchecked: list[dict]


def _stacks_on_waba(eum, region: str, waba_id: str, stack_id: str) -> tuple[dict[str, dict], _WabaStacks]:
    """Every registry entry, and the registered stacks other than ``stack_id``
    with a phone on ``waba_id``.

    A stack whose registry entry could not be read, whose phone EUM Social
    could not look up (access denied, throttled), or whose phone's record
    names no WABA is listed as unchecked, with the reason. These lookups
    used to skip such a stack, so a bind took a shared WABA for an exclusive
    one and replaced the event destination its sibling reads. A caller that
    may change the WABA's destination refuses while any stack is unchecked.

    ``region`` is the WABA's: the Region of the phone that named it (see
    _phone_region). A phone in another Region is on another WABA, since a
    WABA's link to EUM Social and its event destination are per Region, so
    it is not looked up. A phone EUM Social
    does not know (``ResourceNotFoundException``) is on none of the WABAs
    linked in the account and Region, so it is no sibling either. The
    registry listing failing raises.
    """
    registries, unread = _read_all_registries()
    siblings: list[tuple[str, dict, dict]] = []
    unchecked = [{"stack": sid, "reason": "registry entry not read"} for sid in unread if sid != stack_id]
    for sid, cfg in registries.items():
        if sid == stack_id:
            continue
        other_pid = cfg.get("phone_number_id", "")
        if not other_pid or _phone_region(other_pid, cfg) != region:
            continue
        try:
            detail = eum.get_linked_whatsapp_business_account_phone_number(id=other_pid)
        except Exception as e:
            reason = _eum_failure("sibling_phone_lookup_failed", e, sid)
            if reason != _NOT_LINKED:
                unchecked.append({"stack": sid, "reason": reason})
            continue
        linked = detail.get("linkedWhatsAppBusinessAccountId", "")
        if not linked:
            unchecked.append({"stack": sid, "reason": "no WABA in its EUM Social record"})
        elif linked == waba_id:
            siblings.append((sid, cfg, detail))
    return registries, _WabaStacks(siblings, unchecked)


def _unchecked_response(unchecked: list[dict], unchanged: str) -> dict:
    """The 502 of a WABA change refused because a registered stack that may
    share the WABA could not be checked. It names each such stack and why,
    never its phone."""
    names = ", ".join(f"{u['stack']} ({u['reason']})" for u in unchecked)
    return failure_response(
        502,
        f"Could not check whether these registered stacks share the WABA: {names}. {unchanged} "
        "Try again once that is fixed.",
        uncheckedStacks=unchecked,
    )


def _registry_only(stack_id: str, config: dict, siblings: list[str] | None = None) -> tuple[str, str]:
    """What a bind or unbind changed, and the project config file that decides.

    Both edit the registry copy of socialagent.yaml, which only the console
    reads: the stack's entry, and on a shared WABA the ``siblings`` whose
    entries a bind gave a Meta phone filter. The stack's Lambdas send from
    PHONE_NUMBER_ID, which Terraform takes from the project config, and a
    deploy writes the registry entry from that same config, replacing what the
    console wrote.
    """
    project_file = f"socialagent.{config.get('environment') or '<env>'}.yaml"
    if siblings:
        names = [stack_id, *siblings]
        entries = f"registry entries for {', '.join(names[:-1])} and {names[-1]}"
        return (
            f"This changed the console's {entries}. The bot sends from "
            f"whatsapp.phone_number_id in the project's {project_file}, and a deploy of each "
            f"stack can rewrite its registry entry from its own project config."
        ), project_file
    return (
        f"This changed only the console's registry entry for {stack_id}. The bot sends from "
        f"whatsapp.phone_number_id in the project's {project_file}, and a deploy can rewrite "
        f"the registry entry from that file."
    ), project_file


def handle_bind_phone_number(_params, body):
    """Bind a phone number to a stack with WABA-sharing awareness.

    Behavior:
    - If another stack already binds a phone from the same WABA, this is a
      SHARED WABA scenario (EUM allows one SNS destination per WABA). We:
        1. Update the new stack's registry with phone_number_id +
           meta_phone_number_id + shared_sns_topic_arn.
        2. Do NOT call put_whatsapp_business_account_event_destinations
           (that would break the sibling stack).
        3. Also set meta_phone_number_id on the sibling stack's registry
           entry. That is the console's record of the Meta id the sibling
           filters on, not the filter: the sibling's webhook filters on
           SOCIALAGENT_META_PHONE_NUMBER_ID, which Terraform sets from its own
           project config, so the response names the value in
           ``siblingConfig``.
    - Otherwise (exclusive WABA), fall back to the original behavior:
      set the SNS destination on the WABA.

    Neither mode changes the number the bot sends from (see _registry_only), so
    the response names the project config keys to set, in ``projectConfig``.

    The bind fails closed. While any registered stack that may be on the WABA
    could not be checked (see _stacks_on_waba), it answers 502 naming those
    stacks and changes nothing, ``force`` or not: taken for an exclusive bind,
    it would replace the destination such a stack reads. A shared bind also
    answers 502, before it writes this stack's entry, when it cannot record a
    sibling's Meta id in that sibling's entry: EUM Social gave none, so
    ``siblingConfig`` could not name it, or the write failed, which the bind
    used to report as done. An entry written for an earlier sibling stays
    written, and the 502 lists it in ``siblingsAutoUpdated``.

    The WABA is the one EUM Social links the phone to, never the body's: a
    trusted wabaId let a caller bind their own phone and still re-point some
    other WABA's event destination at this stack's topic.

    An exclusive bind that moves the WABA off a destination no registered
    stack consumes takes the WABA's inbound messages away from whatever reads
    that destination, as update-sns-destination would, so it needs the same
    group, admin.

    Body:
        phoneNumberId: EUM phone ARN (required)
        stackId: target stack id (required)
        wabaId: optional; rejected unless it matches the phone's WABA
        force: bool — skip the warning for a stack with no SNS topic yet
    """
    phone_arn = body.get("phoneNumberId", "")
    stack_id = body.get("stackId", "")
    claimed_waba = body.get("wabaId", "")
    force = bool(body.get("force", False))
    if not phone_arn or not stack_id:
        return _json(400, {"error": "phoneNumberId and stackId are required"})

    # Load target stack config
    config = _get_registry(stack_id)
    if not config:
        return _json(404, {"error": f"Stack {stack_id} not found"})

    region = config.get("region") or REGION
    try:
        eum = boto3.client("socialmessaging", region_name=region)

        # Fetch phone details to capture metaPhoneNumberId for filtering
        phone_detail = eum.get_linked_whatsapp_business_account_phone_number(id=phone_arn)
        meta_phone_id = phone_detail.get("phoneNumber", {}).get("metaPhoneNumberId", "")
        waba_id = phone_detail.get("linkedWhatsAppBusinessAccountId", "")
        if not waba_id:
            return _json(409, {"error": "EUM Social reports no WABA for this phone number"})
        if claimed_waba and claimed_waba != waba_id:
            return _json(400, {"error": "wabaId does not match the phone number's WABA", "wabaId": waba_id})

        # Check if any OTHER stack already binds a phone on the same WABA
        registries, found = _stacks_on_waba(eum, _phone_region(phone_arn, config), waba_id, stack_id)
        if found.unchecked:
            return _unchecked_response(
                found.unchecked, "Nothing was bound, and the WABA's event destination was not changed."
            )
        sibling_stacks = found.siblings

        # The destinations an exclusive bind would replace. A shared bind, or
        # a stack with no topic yet, leaves the WABA's destination as it is.
        sns_topic = config.get("outputs", {}).get("sns_topic_arn", "")
        replaced: list[str] = []
        if not sibling_stacks and sns_topic:
            account = eum.get_linked_whatsapp_business_account(id=waba_id).get("account", {})
            replaced = [
                d["eventDestinationArn"]
                for d in account.get("eventDestinations", [])
                if d.get("eventDestinationArn") and d["eventDestinationArn"] != sns_topic
            ]
    except Exception:
        return _error(500, "Failed to inspect WABA")

    consumed = {t for cfg in [config, *registries.values()] for t in _stack_topics(cfg)}
    unconsumed = [arn for arn in replaced if arn not in consumed]
    if unconsumed and not has_permission(get_user_groups(), "admin"):
        return _json(403, {
            "error": "Forbidden",
            "message": (
                f"WABA {waba_id} sends its events to {', '.join(unconsumed)}, which no registered stack "
                f"uses. Pointing it at {stack_id}'s topic instead requires 'admin' group membership."
            ),
            "requiredGroup": "admin",
        })

    # Update this stack's registry
    config["phone_number_id"] = phone_arn
    if meta_phone_id:
        config["meta_phone_number_id"] = meta_phone_id

    if sibling_stacks:
        # SHARED WABA: register this stack as a follower of the sibling's SNS
        sibling_sns = ""
        sibling_sids = [s[0] for s in sibling_stacks]
        for _, scfg, _ in sibling_stacks:
            sibling_sns = scfg.get("outputs", {}).get("sns_topic_arn", "") or sibling_sns
            if sibling_sns:
                break
        if not sibling_sns:
            return _json(500, {
                "error": "Shared WABA detected but could not resolve sibling SNS topic. "
                         "Run 'socialagent deploy' on the sibling stack first.",
                "siblingStacks": sibling_sids,
            })
        config["shared_sns_topic_arn"] = sibling_sns

        # Record each sibling's Meta id, from the record _stacks_on_waba read,
        # in its registry entry, and name it in siblingConfig. The entry only
        # records it: the sibling's webhook filters on
        # SOCIALAGENT_META_PHONE_NUMBER_ID, which Terraform sets from the
        # sibling's own project config, and until a deploy sets that, the
        # sibling answers messages sent to any number on the WABA, whatever its
        # entry says. A sibling with no id, or whose write fails, stops the
        # bind before this stack's entry is written. The bind used to bind
        # anyway: it left a sibling with no id out of siblingConfig, and listed
        # one whose write failed in siblingsAutoUpdated.
        pending = [
            (sid, scfg, detail.get("phoneNumber", {}).get("metaPhoneNumberId", ""))
            for sid, scfg, detail in sibling_stacks
            if not scfg.get("meta_phone_number_id")
        ]
        no_meta = [sid for sid, _, meta in pending if not meta]
        if no_meta:
            return failure_response(
                502,
                f"EUM Social gave no Meta phone number id for the number of {', '.join(no_meta)}, which shares "
                f"the WABA, so it cannot be recorded in the registry entry or named in siblingConfig. "
                f"Nothing was bound.",
                siblingsNotUpdated=no_meta,
            )
        updated_siblings = []
        for sid, scfg, meta in pending:
            scfg["meta_phone_number_id"] = meta
            if not _put_registry(sid, scfg):
                done = f" The registry entries of {', '.join(updated_siblings)} were updated." if updated_siblings else ""
                return failure_response(
                    502,
                    f"Could not write the registry entry of {sid}, which shares the WABA, so its Meta phone "
                    f"number id is not recorded there. {stack_id} was not bound.{done} Try again.",
                    siblingsNotUpdated=[sid],
                    siblingsAutoUpdated=updated_siblings,
                )
            updated_siblings.append(sid)

        # Persist target config
        if not _put_registry(stack_id, config):
            return _json(500, {"error": "Failed to update registry"})

        # The keys that make the deployed stacks match the registry. The webhook
        # filter only reaches a sibling's Lambdas through its own project config.
        project_config = {
            "whatsapp.phone_number_id": phone_arn,
            "whatsapp.meta_phone_number_id": meta_phone_id,
            "terraform.shared_sns_topic_arn": sibling_sns,
        }
        sibling_config = {
            sid: {"whatsapp.meta_phone_number_id": scfg["meta_phone_number_id"]}
            for sid, scfg, _ in sibling_stacks
            if scfg.get("meta_phone_number_id")
        }
        changed, project_file = _registry_only(stack_id, config, updated_siblings)
        return _json(200, {
            "bound": True,
            "stackId": stack_id,
            "phoneNumberId": phone_arn,
            "mode": "shared_waba",
            "sharedSnsTopicArn": sibling_sns,
            "metaPhoneNumberId": meta_phone_id,
            "siblingStacks": sibling_sids,
            "siblingsAutoUpdated": updated_siblings,
            "projectConfig": project_config,
            "siblingConfig": sibling_config,
            "nextStep": (
                f"{changed} The WABA's event destination was not changed. Set "
                f"{', '.join(project_config)} in {stack_id}'s {project_file} to the values in "
                f"projectConfig, set whatsapp.meta_phone_number_id in each sibling stack's project "
                f"config to the value in siblingConfig, then run 'socialagent deploy' for each stack."
            ),
        })

    # EXCLUSIVE WABA: set SNS destination on the WABA as before
    if not _put_registry(stack_id, config):
        return _json(500, {"error": "Failed to update registry"})
    changed, project_file = _registry_only(stack_id, config)

    destination_updated = False
    if sns_topic:
        try:
            eum.put_whatsapp_business_account_event_destinations(
                id=waba_id,
                eventDestinations=[{"eventDestinationArn": sns_topic}],
            )
            destination_updated = True
        except Exception:
            logger.exception("handler_error")
    if destination_updated:
        destination = (
            f"It also pointed WABA {waba_id}'s event destination at {sns_topic}, so inbound "
            f"messages on that WABA reach this stack."
        )
    elif sns_topic:
        destination = "The WABA's event destination was not changed: setting it failed."
    else:
        destination = "The WABA's event destination was not changed: the stack has no deployed SNS topic yet."
    response = {
        "bound": True,
        "stackId": stack_id,
        "phoneNumberId": phone_arn,
        "mode": "exclusive_waba",
        "snsTopicArn": sns_topic,
        "eventDestinationUpdated": destination_updated,
        "projectConfig": {"whatsapp.phone_number_id": phone_arn},
        "nextStep": (
            f"{changed} {destination} To make the bot send from this number, set "
            f"whatsapp.phone_number_id to {phone_arn} in {project_file} and run 'socialagent deploy'."
        ),
    }
    if not sns_topic and not force:
        response["warning"] = (
            "Stack has no deployed SNS topic yet (run 'socialagent deploy' first). "
            "Re-bind after deploy, or pass 'force=true' to skip SNS wiring."
        )
    elif sns_topic and not destination_updated:
        response["warning"] = (
            f"Pointing WABA {waba_id}'s event destination at {sns_topic} failed, so inbound "
            "messages on it still go where they went before and do not reach this stack. Bind "
            "again, or set it with Update SNS event destination on the stack's Phone Numbers tab."
        )
    return _json(200, response)


def handle_get_phone_numbers(params, _body):
    """Get phone numbers linked to the WABA with their SNS topic configuration."""
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    stack_id = f"{config.get('project', 'unknown')}-{config.get('environment', 'unknown')}"
    phone_number_id = config.get("phone_number_id", "")
    if not phone_number_id:
        return _json(200, [])
    try:
        eum = boto3.client("socialmessaging", region_name=config.get("region", REGION))
        # Get phone number details
        phone_resp = eum.get_linked_whatsapp_business_account_phone_number(id=phone_number_id)
        phone_data = phone_resp.get("phoneNumber", {})
        waba_id_from_resp = phone_resp.get("linkedWhatsAppBusinessAccountId", "")
        # Get WABA info for SNS topic
        waba_resp = eum.list_linked_whatsapp_business_accounts(maxResults=10)
        sns_topic = config.get("outputs", {}).get("sns_topic_arn", "")
        waba_name = ""
        waba_id = waba_id_from_resp
        for acct in waba_resp.get("linkedAccounts", []):
            if acct.get("id") == waba_id_from_resp or not waba_id_from_resp:
                waba_name = acct.get("wabaName", "")
                waba_id = acct.get("id", waba_id)
                for dest in acct.get("eventDestinations", []):
                    arn = dest.get("eventDestinationArn", "")
                    if arn.startswith("arn:aws:sns"):
                        sns_topic = arn
                        break
                break
        numbers = [{
            "phoneNumberId": phone_number_id,
            "displayPhone": phone_data.get("displayPhoneNumber", ""),
            "displayName": phone_data.get("displayPhoneNumberName", ""),
            "qualityRating": phone_data.get("qualityRating", "UNKNOWN"),
            "metaPhoneNumberId": phone_data.get("metaPhoneNumberId", ""),
            "wabaName": waba_name,
            "wabaId": waba_id,
            "snsTopic": sns_topic,
            "stack": stack_id,
        }]
        return _json(200, numbers)
    except Exception:
        return _error(500, "Phone numbers error")


def _linked_accounts(eum) -> list[dict]:
    """Every WABA linked in the client's region, across list pages."""
    accounts: list[dict] = []
    kwargs: dict = {"maxResults": 100}
    for _ in range(10):
        resp = eum.list_linked_whatsapp_business_accounts(**kwargs)
        accounts.extend(resp.get("linkedAccounts", []))
        if not resp.get("nextToken"):
            break
        kwargs["nextToken"] = resp["nextToken"]
    return accounts


def _sns_destination(account: dict) -> str:
    """The SNS topic a WABA delivers its events to, or ``""``."""
    for dest in account.get("eventDestinations", []):
        arn = dest.get("eventDestinationArn", "")
        if arn.startswith("arn:aws:sns"):
            return arn
    return ""


def _phone_row(phone_id: str, detail: dict, waba_id: str, waba: dict, bound_stack: str) -> dict:
    return {
        "phoneNumberId": phone_id,
        "displayPhone": detail.get("displayPhoneNumber", ""),
        "displayName": detail.get("displayPhoneNumberName", ""),
        "qualityRating": detail.get("qualityRating", "UNKNOWN"),
        "metaPhoneNumberId": detail.get("metaPhoneNumberId", ""),
        "wabaName": waba.get("name", ""),
        "wabaId": waba_id,
        "snsTopic": waba.get("sns", ""),
        "boundStack": bound_stack,
    }


def handle_list_all_phone_numbers(_params, _body):
    """List ALL EUM Social phone numbers with their bound stack info.

    Numbers come from each linked WABA's details (``account.phoneNumbers``), so
    a number no stack holds is listed too and can be bound. A registered number
    no WABA listing returned (another region, or a WABA whose details could not
    be read) is looked up on its own, so it keeps its row.

    Returns list of phone numbers with:
        - phoneNumberId, displayPhone, displayName, qualityRating, metaPhoneNumberId
        - wabaName, wabaId, snsTopic (from EUM Social)
        - boundStack: the SMART stack whose registry entry names this phone, or ""
        - wabaSharing: list of other SMART stacks that share this WABA (for
          detecting when shared SNS + phone filtering is needed)
    """
    try:
        eum = boto3.client("socialmessaging", region_name=REGION)
        # Registry value -> stack. An entry may hold the phone's ARN or its
        # phone-number-id-... id; the row keeps the value the entry holds.
        phone_to_stack = {
            cfg["phone_number_id"]: sid for sid, cfg in _get_all_registries().items() if cfg.get("phone_number_id")
        }

        numbers = []
        waba_map = {}  # waba_id -> {name, sns}
        listed = set()  # registry values some WABA listing returned
        for acct in _linked_accounts(eum):
            waba_id = acct.get("id", "")
            waba_map[waba_id] = {"name": acct.get("wabaName", ""), "sns": _sns_destination(acct)}
            try:
                account = eum.get_linked_whatsapp_business_account(id=waba_id).get("account", {})
            except Exception:
                logger.exception("get_linked_waba_failed", extra={"waba_id": waba_id})
                continue
            for pd in account.get("phoneNumbers", []):
                registered = next((i for i in (pd.get("arn"), pd.get("phoneNumberId")) if i in phone_to_stack), "")
                listed.add(registered)
                phone_id = registered or pd.get("arn") or pd.get("phoneNumberId", "")
                numbers.append(_phone_row(phone_id, pd, waba_id, waba_map[waba_id], phone_to_stack.get(registered, "")))

        for phone_arn, sid in phone_to_stack.items():
            if phone_arn in listed:
                continue
            try:
                detail = eum.get_linked_whatsapp_business_account_phone_number(id=phone_arn)
            except Exception:
                continue
            linked_waba = detail.get("linkedWhatsAppBusinessAccountId", "")
            numbers.append(_phone_row(phone_arn, detail.get("phoneNumber", {}), linked_waba, waba_map.get(linked_waba, {}), sid))

        # Annotate each number with list of OTHER stacks that share its WABA
        waba_to_stacks: dict[str, list[str]] = {}  # waba_id -> [stack_ids]
        for n in numbers:
            if n["boundStack"] and n["wabaId"]:
                waba_to_stacks.setdefault(n["wabaId"], []).append(n["boundStack"])
        for n in numbers:
            stacks_on_waba = [s for s in waba_to_stacks.get(n["wabaId"], []) if s != n["boundStack"]]
            n["wabaSharing"] = stacks_on_waba  # e.g. ['socialagent-dev'] when multiparty-dev shares WABA with socialagent-dev
        return _json(200, numbers)
    except Exception:
        return _error(500, "List phone numbers failed")


def handle_unbind_phone_number(_params, body):
    """Unbind a phone number from a stack.

    If the stack is part of a shared-WABA group, also removes the filter
    (meta_phone_number_id and shared_sns_topic_arn) from the registry so
    subsequent rebind semantics work correctly.
    """
    stack_id = body.get("stackId", "")
    if not stack_id:
        return _json(400, {"error": "stackId is required"})
    config = _get_registry(stack_id)
    if not config:
        return _json(404, {"error": f"Stack {stack_id} not found"})

    was_shared = bool(config.get("shared_sns_topic_arn"))
    config.pop("phone_number_id", None)
    config.pop("meta_phone_number_id", None)
    config.pop("shared_sns_topic_arn", None)

    if not _put_registry(stack_id, config):
        return _json(500, {"error": "Failed to update registry"})

    changed, project_file = _registry_only(stack_id, config)
    response = {
        "unbound": True,
        "stackId": stack_id,
        "nextStep": (
            f"{changed} The WABA's event destination was not changed, so the bot keeps the number. "
            f"To stop it using the number, clear whatsapp.phone_number_id (and "
            f"whatsapp.meta_phone_number_id and terraform.shared_sns_topic_arn, if set) in "
            f"{project_file} and run 'socialagent deploy'."
        ),
    }
    if was_shared:
        response["warning"] = (
            "Stack was using a shared SNS topic from a sibling stack. "
            "The sibling's phone filtering (meta_phone_number_id) was left in place — "
            "you may want to remove it if no other stacks share the WABA anymore."
        )
    return _json(200, response)


def _stack_topics(config: dict) -> list[str]:
    """SNS topics a stack's registry entry vouches for, the one it consumes first.

    A shared-WABA follower carries ``shared_sns_topic_arn`` (set by bind) and,
    once redeployed, the same ARN as ``outputs.sns_topic_arn``; an exclusive
    stack carries only its own topic.
    """
    topics = [config.get("shared_sns_topic_arn") or "", config.get("outputs", {}).get("sns_topic_arn") or ""]
    return [t for t in dict.fromkeys(topics) if t]


def _is_sns_topic_arn(arn: str, region: str) -> bool:
    """A standard SNS topic ARN in ``region``. EUM Social delivers in-region."""
    return bool(re.fullmatch(rf"arn:aws:sns:{re.escape(region)}:\d{{12}}:[A-Za-z0-9_-]+", arn))


def handle_update_sns_destination(params, body):
    """Point the WABA of a stack's bound phone at an SNS topic the registry vouches for.

    Neither the WABA nor the topic is taken from the caller. The WABA is the one
    the stack's bound phone belongs to, and the destination defaults to the topic
    the stack consumes. A caller may name another topic, but only one published
    by this stack or by a registered stack on the same WABA (the shared-WABA
    case, where one topic feeds every stack on it). Taking both from the body let
    a caller send any WABA's inbound traffic, end-user phone numbers and message
    text included, to a topic of their choosing.

    Guard: if other SMART stacks currently bind phones on this WABA, changing
    the SNS destination would silently break them. The request is rejected
    unless body.force is true, in which case the caller must understand
    they're orphaning sibling stacks. A registered stack that could not be
    checked (see _stacks_on_waba) refuses the change with a 502, ``force``
    or not: it may be a sibling, and its topic is not in the allowed list.

    Body (all optional):
        snsTopicArn: destination; defaults to the stack's own topic
        wabaId: accepted for older clients, rejected unless it matches
        force: true to override the shared-WABA guard
    """
    stack_id = params["id"]
    config = _get_registry(stack_id)
    if not config:
        return _json(404, {"error": "Stack not found"})
    region = config.get("region") or REGION
    phone_arn = config.get("phone_number_id", "")
    if not phone_arn:
        return _json(400, {"error": "Stack has no bound phone number; bind one before changing its SNS destination"})
    requested = body.get("snsTopicArn") or ""
    claimed_waba = body.get("wabaId") or ""
    force = body.get("force") is True
    if not isinstance(requested, str) or not isinstance(claimed_waba, str):
        return _json(400, {"error": "snsTopicArn and wabaId must be strings"})

    try:
        eum = boto3.client("socialmessaging", region_name=region)
        waba_id = eum.get_linked_whatsapp_business_account_phone_number(id=phone_arn).get(
            "linkedWhatsAppBusinessAccountId", ""
        )
        if not waba_id:
            return _json(409, {"error": "Could not resolve the WABA of this stack's bound phone"})
        if claimed_waba and claimed_waba != waba_id:
            return _json(400, {
                "error": "wabaId does not match the WABA of this stack's bound phone; omit it",
                "wabaId": waba_id,
            })

        # Registered stacks with a phone on this WABA, and the topics they publish.
        _, found = _stacks_on_waba(eum, _phone_region(phone_arn, config), waba_id, stack_id)
        if found.unchecked:
            return _unchecked_response(found.unchecked, "The WABA's event destination was not changed.")
        siblings = [sid for sid, _, _ in found.siblings]
        own_topics = _stack_topics(config)
        allowed = list(own_topics)
        for _, cfg, _ in found.siblings:
            allowed.extend(_stack_topics(cfg))

        new_sns_arn = requested or (own_topics[0] if own_topics else "")
        if not new_sns_arn:
            return _json(400, {"error": "Stack has no deployed SNS topic yet (run terraform apply first)"})
        if not _is_sns_topic_arn(new_sns_arn, region):
            return _json(400, {"error": f"snsTopicArn must be an SNS topic ARN in {region}"})
        if new_sns_arn not in allowed:
            return _json(400, {
                "error": "snsTopicArn is not a topic of this stack or of a registered stack on the same WABA",
                "allowedTopics": list(dict.fromkeys(allowed)),
            })

        if siblings and not force:
            return _json(409, {
                "error": "WABA is shared with other stacks — changing its SNS destination "
                         "would silently break them. Re-run with force=true to override.",
                "siblingStacks": siblings,
                "suggestion": (
                    f"Stacks {siblings} currently receive messages for this WABA via the "
                    "existing SNS topic. If you want them to share a new topic, update the "
                    "shared_sns_topic_arn in each stack's registry and re-deploy."
                ),
            })

        eum.put_whatsapp_business_account_event_destinations(
            id=waba_id,
            eventDestinations=[{"eventDestinationArn": new_sns_arn}],
        )
        return _json(200, {"updated": True, "wabaId": waba_id, "snsTopicArn": new_sns_arn, "force": force})
    except Exception:
        return _error(500, "Failed to update SNS destination")



@app.get("/api/stacks/<id>/phone-numbers")
@require_group("operator")
def get_phone_numbers(id: str):
    return from_legacy(handle_get_phone_numbers({"id": id}, {}))


@app.get("/api/phone-numbers")
@require_group("operator")
def list_all_phone_numbers():
    return from_legacy(handle_list_all_phone_numbers({}, {}))


# admin, not operator: this redirects every inbound message on the WABA, and
# force=true orphans the stacks sharing it.
@app.put("/api/stacks/<id>/update-sns-destination")
@require_group("admin")
def update_sns_destination(id: str):
    return from_legacy(handle_update_sns_destination({"id": id}, current_body()))


@app.put("/api/phone-numbers/bind")
@require_group("operator")
def bind_phone_number():
    return from_legacy(handle_bind_phone_number({}, current_body()))


@app.put("/api/phone-numbers/unbind")
@require_group("operator")
def unbind_phone_number():
    return from_legacy(handle_unbind_phone_number({}, current_body()))
