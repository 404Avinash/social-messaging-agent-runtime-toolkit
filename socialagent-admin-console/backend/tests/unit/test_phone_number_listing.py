# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Phone numbers page lists every linked number, and says what a bind changes.

The list was built from the registry alone, so every row already had a bound
stack, the "Bind to stack" button (shown only for unbound rows) never appeared,
and an unbound number left the page for good. Numbers now come from each linked
WABA (``GetLinkedWhatsAppBusinessAccount`` returns ``account.phoneNumbers``),
and a number is bound only where a registry entry names it.

Bind and unbind edit the console's registry copy of ``socialagent.yaml``. The
stack's Lambdas send from ``whatsapp.phone_number_id`` in the project config,
and a deploy rewrites the registry entry from that file, so the responses say
so and name the keys to set instead of a ``make tf-apply`` that reads neither.
"""

import json
import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

ACCOUNT = "111122223333"


def _arn(suffix):
    return f"arn:aws:social-messaging:us-east-1:{ACCOUNT}:phone-number-id/{suffix}"


def _number(suffix, display):
    return {
        "arn": _arn(suffix),
        "phoneNumber": display,
        "phoneNumberId": f"phone-number-id-{suffix}",
        "metaPhoneNumberId": f"meta-{suffix}",
        "displayPhoneNumberName": f"Bot {suffix}",
        "displayPhoneNumber": display,
        "qualityRating": "GREEN",
    }


TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:dev-bot-whatsapp"
WABAS = {
    "waba-1": {
        "id": "waba-1",
        "wabaName": "Shop",
        "eventDestinations": [{"eventDestinationArn": TOPIC}],
        "phoneNumbers": [_number("a1", "+1 555-0100"), _number("b2", "+1 555-0101")],
    },
    "waba-2": {
        "id": "waba-2",
        "wabaName": "Clinic",
        "eventDestinations": [],
        "phoneNumbers": [_number("c3", "+1 555-0102")],
    },
}


@pytest.fixture
def eum(monkeypatch):
    """A fake EUM Social client: two WABAs, the second on a later list page."""
    from routes import phone_numbers

    client = MagicMock()

    def list_accounts(**kwargs):
        summaries = [{k: v for k, v in w.items() if k != "phoneNumbers"} for w in WABAS.values()]
        if kwargs.get("nextToken") == "page-2":
            return {"linkedAccounts": summaries[1:]}
        return {"linkedAccounts": summaries[:1], "nextToken": "page-2"}

    def phone_detail(id):
        for waba_id, waba in WABAS.items():
            for n in waba["phoneNumbers"]:
                if id in (n["arn"], n["phoneNumberId"]):
                    return {"phoneNumber": n, "linkedWhatsAppBusinessAccountId": waba_id}
        raise RuntimeError("ResourceNotFoundException")

    client.list_linked_whatsapp_business_accounts.side_effect = list_accounts
    client.get_linked_whatsapp_business_account.side_effect = lambda id: {"account": WABAS[id]}
    client.get_linked_whatsapp_business_account_phone_number.side_effect = phone_detail
    monkeypatch.setattr(phone_numbers.boto3, "client", lambda *a, **k: client)
    return client


def _list(monkeypatch, registries):
    from routes import phone_numbers

    monkeypatch.setattr(phone_numbers, "_get_all_registries", lambda: registries)
    resp = phone_numbers.handle_list_all_phone_numbers({}, {})
    assert resp["statusCode"] == 200
    return {n["displayPhone"]: n for n in json.loads(resp["body"])}


def test_unbound_numbers_on_every_linked_waba_are_listed(eum, monkeypatch):
    rows = _list(monkeypatch, {"bot-dev": {"phone_number_id": _arn("a1")}})

    assert sorted(rows) == ["+1 555-0100", "+1 555-0101", "+1 555-0102"]
    assert rows["+1 555-0100"]["boundStack"] == "bot-dev"
    assert rows["+1 555-0101"]["boundStack"] == ""
    assert rows["+1 555-0102"]["boundStack"] == ""


def test_an_unbound_row_carries_what_a_bind_needs(eum, monkeypatch):
    rows = _list(monkeypatch, {"bot-dev": {"phone_number_id": _arn("a1")}})

    unbound = rows["+1 555-0101"]
    assert unbound["phoneNumberId"] == _arn("b2")
    assert unbound["wabaId"] == "waba-1" and unbound["wabaName"] == "Shop"
    assert unbound["snsTopic"] == TOPIC
    assert unbound["metaPhoneNumberId"] == "meta-b2"
    # Binding it would share waba-1 with bot-dev; the bind modal warns on this.
    assert unbound["wabaSharing"] == ["bot-dev"]
    assert rows["+1 555-0102"]["wabaSharing"] == []
    assert rows["+1 555-0100"]["wabaSharing"] == []


def test_a_registry_entry_holding_the_short_id_still_binds_its_row(eum, monkeypatch):
    rows = _list(monkeypatch, {"bot-dev": {"phone_number_id": "phone-number-id-c3"}})

    assert rows["+1 555-0102"]["boundStack"] == "bot-dev"
    # The row keeps the registry's value, which unbind and bind compare against.
    assert rows["+1 555-0102"]["phoneNumberId"] == "phone-number-id-c3"
    assert len(rows) == 3


def test_an_unbound_number_is_listed_again(eum, monkeypatch):
    """Unbind used to drop the number from the page, so it could not be re-bound."""
    rows = _list(monkeypatch, {"bot-dev": {"project": "bot", "environment": "dev"}})

    assert len(rows) == 3 and all(not n["boundStack"] for n in rows.values())


def test_a_bound_number_no_waba_lists_is_still_shown(eum, monkeypatch):
    """A WABA whose details cannot be read keeps its registered numbers on the page."""
    eum.get_linked_whatsapp_business_account.side_effect = RuntimeError("AccessDeniedException")

    rows = _list(monkeypatch, {"bot-dev": {"phone_number_id": _arn("a1")}})

    assert list(rows) == ["+1 555-0100"]
    assert rows["+1 555-0100"]["boundStack"] == "bot-dev"
    assert rows["+1 555-0100"]["wabaName"] == "Shop"


def test_backend_role_may_read_a_wabas_numbers():
    """GetLinkedWhatsAppBusinessAccount is authorized on the waba resource."""
    tf = (Path(__file__).resolve().parents[3] / "infra-terraform" / "modules" / "api" / "main.tf").read_text()
    start = tf.index('Sid    = "EUMSocialWaba"')
    statement = tf[start : tf.index("\n      },", start)]
    assert '"social-messaging:GetLinkedWhatsAppBusinessAccount"' in statement
    assert re.search(r'Resource = "arn:aws:social-messaging:\*:\$\{[^}]+\}:waba/\*"', statement)


# ── bind / unbind say what they change ─────────────────────────────────────────

PHONE = _arn("a1")
SIBLING_PHONE = _arn("b2")
SIBLING_TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:dev-leader-whatsapp"


def _stack(**extra):
    return {"project": "bot", "environment": "dev", "outputs": {"sns_topic_arn": TOPIC}, **extra}


def _call(monkeypatch, handler, body, registries, unread=()):
    from routes import phone_numbers

    written = {}
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: registries.get(sid))
    monkeypatch.setattr(phone_numbers, "_read_all_registries", lambda: (registries, list(unread)))
    monkeypatch.setattr(phone_numbers, "_put_registry", lambda sid, cfg: written.setdefault(sid, cfg) or True)
    resp = getattr(phone_numbers, handler)({}, body)
    return resp["statusCode"], json.loads(resp["body"]), written


def test_exclusive_bind_names_the_project_config_key(eum, monkeypatch):
    status, body, _ = _call(
        monkeypatch, "handle_bind_phone_number", {"phoneNumberId": PHONE, "stackId": "bot-dev"}, {"bot-dev": _stack()}
    )

    assert status == 200 and body["mode"] == "exclusive_waba"
    assert body["projectConfig"] == {"whatsapp.phone_number_id": PHONE}
    assert "socialagent.dev.yaml" in body["nextStep"] and "socialagent deploy" in body["nextStep"]
    assert "registry" in body["nextStep"]
    # The one change outside the registry is the WABA's event destination.
    assert body["eventDestinationUpdated"] is True
    assert "event destination" in body["nextStep"]


def test_exclusive_bind_does_not_claim_a_destination_it_failed_to_set(eum, monkeypatch):
    eum.put_whatsapp_business_account_event_destinations.side_effect = RuntimeError("AccessDenied")

    _, body, _ = _call(
        monkeypatch, "handle_bind_phone_number", {"phoneNumberId": PHONE, "stackId": "bot-dev"}, {"bot-dev": _stack()}
    )

    assert body["eventDestinationUpdated"] is False
    assert "was not changed" in body["nextStep"]
    # The page shows a success flash unless the response warns: inbound
    # messages on the WABA still go to the old destination.
    assert "event destination" in body["warning"]


# ── an exclusive bind that takes a WABA away from another workload ────────────

FOREIGN_TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:crm-inbound"
OLD_STACK_TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:dev-old-whatsapp"


def _waba_destinations(eum, *topics):
    account = {**WABAS["waba-1"], "eventDestinations": [{"eventDestinationArn": t} for t in topics]}
    eum.get_linked_whatsapp_business_account.side_effect = lambda id: {"account": account}


def _signed_in_as(monkeypatch, *groups):
    from routes import phone_numbers

    monkeypatch.setattr(phone_numbers, "get_user_groups", lambda: list(groups))


def _bind(monkeypatch, registries=None):
    return _call(
        monkeypatch,
        "handle_bind_phone_number",
        {"phoneNumberId": PHONE, "stackId": "bot-dev"},
        registries or {"bot-dev": _stack()},
    )


def test_an_operator_cannot_take_a_waba_from_a_topic_no_stack_uses(eum, monkeypatch):
    # An exclusive bind re-points the WABA's event destination, the change
    # update-sns-destination makes for admins only. Moved off a topic no
    # registered stack consumes, it takes another workload's inbound messages.
    _waba_destinations(eum, FOREIGN_TOPIC)
    _signed_in_as(monkeypatch, "operator")

    status, body, written = _bind(monkeypatch)

    assert status == 403
    assert body["requiredGroup"] == "admin"
    assert FOREIGN_TOPIC in body["message"] and "'admin'" in body["message"]
    assert written == {}
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_an_admin_can_take_a_waba_from_a_topic_no_stack_uses(eum, monkeypatch):
    _waba_destinations(eum, FOREIGN_TOPIC)
    _signed_in_as(monkeypatch, "admin")

    status, body, written = _bind(monkeypatch)

    assert status == 200 and body["eventDestinationUpdated"] is True
    assert set(written) == {"bot-dev"}


@pytest.mark.parametrize("current", [(), (TOPIC,), (OLD_STACK_TOPIC,)], ids=["unset", "own", "registered"])
def test_an_operator_may_bind_a_waba_no_other_workload_reads(eum, monkeypatch, current):
    # No destination, the stack's own topic, or a registered stack's topic,
    # such as the stack the number was unbound from.
    _waba_destinations(eum, *current)
    _signed_in_as(monkeypatch, "operator")
    registries = {
        "bot-dev": _stack(),
        "old-dev": {"project": "old", "environment": "dev", "outputs": {"sns_topic_arn": OLD_STACK_TOPIC}},
    }

    status, body, _ = _bind(monkeypatch, registries)

    assert status == 200 and body["eventDestinationUpdated"] is True


def test_the_wabas_destination_is_not_read_when_the_bind_leaves_it(eum, monkeypatch):
    # A stack with no topic yet changes no destination, so nothing is guarded.
    _signed_in_as(monkeypatch, "operator")

    status, body, _ = _bind(monkeypatch, {"bot-dev": {"project": "bot", "environment": "dev", "outputs": {}}})

    assert status == 200 and body["eventDestinationUpdated"] is False
    eum.get_linked_whatsapp_business_account.assert_not_called()


def test_shared_bind_names_every_key_instead_of_tf_apply(eum, monkeypatch):
    registries = {
        "bot-dev": _stack(),
        "leader-dev": {
            "project": "leader",
            "environment": "dev",
            "phone_number_id": SIBLING_PHONE,
            "outputs": {"sns_topic_arn": SIBLING_TOPIC},
        },
    }

    status, body, written = _call(
        monkeypatch, "handle_bind_phone_number", {"phoneNumberId": PHONE, "stackId": "bot-dev"}, registries
    )

    assert status == 200 and body["mode"] == "shared_waba"
    assert body["projectConfig"] == {
        "whatsapp.phone_number_id": PHONE,
        "whatsapp.meta_phone_number_id": "meta-a1",
        "terraform.shared_sns_topic_arn": SIBLING_TOPIC,
    }
    assert body["siblingConfig"] == {"leader-dev": {"whatsapp.meta_phone_number_id": "meta-b2"}}
    assert "tf-apply" not in body["nextStep"]
    assert "socialagent deploy" in body["nextStep"]
    assert written["bot-dev"]["shared_sns_topic_arn"] == SIBLING_TOPIC
    # The sibling's entry was written too, and the response says so.
    assert set(written) == {"bot-dev", "leader-dev"}
    assert body["siblingsAutoUpdated"] == ["leader-dev"]
    assert "only" not in body["nextStep"]
    assert "registry entries for bot-dev and leader-dev" in body["nextStep"]


# ── a bind that cannot check every registered stack changes nothing ────────
#
# The bind looked up each registered stack's phone to find the stacks already
# on the WABA, and skipped a stack whose lookup failed. With every sibling
# skipped, a shared WABA read as exclusive, and the bind pointed it at this
# stack's topic, so the sibling stopped receiving its messages.


def _leader(**extra):
    return {
        "project": "leader",
        "environment": "dev",
        "phone_number_id": SIBLING_PHONE,
        "outputs": {"sns_topic_arn": SIBLING_TOPIC},
        **extra,
    }


def _client_error(code):
    # An EUM Social error message can quote the phone's ARN.
    return ClientError({"Error": {"Code": code, "Message": f"{code} for {SIBLING_PHONE}"}}, "GetLinkedWhatsAppBusinessAccountPhoneNumber")


def _sibling_lookup(eum, outcome):
    """Make the sibling's phone lookup raise ``outcome``, or answer it."""
    found = eum.get_linked_whatsapp_business_account_phone_number.side_effect

    def lookup(id):
        if id != SIBLING_PHONE:
            return found(id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    eum.get_linked_whatsapp_business_account_phone_number.side_effect = lookup


def _nothing_changed(eum, written):
    assert written == {}
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException"])
@pytest.mark.parametrize("force", [False, True])
def test_a_sibling_lookup_that_fails_refuses_the_bind(eum, monkeypatch, caplog, code, force):
    _sibling_lookup(eum, _client_error(code))
    caplog.set_level(logging.WARNING)

    status, body, written = _call(
        monkeypatch,
        "handle_bind_phone_number",
        {"phoneNumberId": PHONE, "stackId": "bot-dev", "force": force},
        {"bot-dev": _stack(), "leader-dev": _leader()},
    )

    assert status == 502
    assert body["uncheckedStacks"] == [{"stack": "leader-dev", "reason": code}]
    assert f"leader-dev ({code})" in body["error"] and "Nothing was bound" in body["error"]
    _nothing_changed(eum, written)
    # Nor was the WABA's destination read, as an exclusive bind reads it.
    eum.get_linked_whatsapp_business_account.assert_not_called()
    assert SIBLING_PHONE not in json.dumps(body)
    logged = [(r.getMessage(), getattr(r, "stack_id", None), getattr(r, "error_code", None)) for r in caplog.records]
    assert logged == [("sibling_phone_lookup_failed", "leader-dev", code)]
    for record in caplog.records:
        assert record.exc_info is None and SIBLING_PHONE not in str(record.__dict__)


def test_a_registry_entry_that_cannot_be_read_refuses_the_bind(eum, monkeypatch):
    # The listing named leader-dev, but its entry could not be read, so its
    # phone, and whether that is on the WABA, is unknown.
    status, body, written = _call(
        monkeypatch,
        "handle_bind_phone_number",
        {"phoneNumberId": PHONE, "stackId": "bot-dev"},
        {"bot-dev": _stack()},
        unread=["leader-dev"],
    )

    assert status == 502
    assert body["uncheckedStacks"] == [{"stack": "leader-dev", "reason": "registry entry not read"}]
    _nothing_changed(eum, written)


def test_a_registry_listing_that_fails_refuses_the_bind(eum, monkeypatch):
    from routes import phone_numbers

    written = {}
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: _stack())
    monkeypatch.setattr(phone_numbers, "_put_registry", lambda sid, cfg: written.setdefault(sid, cfg) or True)

    def unlisted():
        raise _client_error("AccessDenied")

    monkeypatch.setattr(phone_numbers, "_read_all_registries", unlisted)
    resp = phone_numbers.handle_bind_phone_number({}, {"phoneNumberId": PHONE, "stackId": "bot-dev"})

    assert resp["statusCode"] == 500
    _nothing_changed(eum, written)


def test_a_phone_record_that_names_no_waba_refuses_the_bind(eum, monkeypatch):
    _sibling_lookup(eum, {"phoneNumber": {}})

    status, body, written = _call(
        monkeypatch,
        "handle_bind_phone_number",
        {"phoneNumberId": PHONE, "stackId": "bot-dev"},
        {"bot-dev": _stack(), "leader-dev": _leader()},
    )

    assert status == 502
    assert body["uncheckedStacks"] == [{"stack": "leader-dev", "reason": "no WABA in its EUM Social record"}]
    _nothing_changed(eum, written)


def test_a_sibling_whose_filter_cannot_be_written_refuses_the_bind(eum, monkeypatch):
    from routes import phone_numbers

    written = {}
    registries = {"bot-dev": _stack(), "leader-dev": _leader()}
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: registries.get(sid))
    monkeypatch.setattr(phone_numbers, "_read_all_registries", lambda: (registries, []))
    monkeypatch.setattr(
        phone_numbers, "_put_registry", lambda sid, cfg: sid != "leader-dev" and (written.setdefault(sid, cfg) or True)
    )

    resp = phone_numbers.handle_bind_phone_number({}, {"phoneNumberId": PHONE, "stackId": "bot-dev"})
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 502
    assert body["siblingsNotUpdated"] == ["leader-dev"]
    assert body["siblingsAutoUpdated"] == []
    assert "bot-dev was not bound" in body["error"]
    # The write records the sibling's Meta id. It is not the sibling's filter,
    # which comes from its own project config, so the error does not say so.
    assert "Meta phone number id is not recorded there" in body["error"]
    assert "filter" not in body["error"]
    # This stack's entry is not written, so the console never records the
    # WABA as shared with a sibling whose Meta id it did not record.
    _nothing_changed(eum, written)


def test_a_second_sibling_whose_write_fails_lists_the_first_as_written(eum, monkeypatch):
    """With two siblings, the first one's entry is written before the
    second's write fails. That entry stays written, so the 502 is not a
    bind that changed nothing: it lists the entry in siblingsAutoUpdated."""
    from routes import phone_numbers

    helper_phone = _arn("d4")
    found = eum.get_linked_whatsapp_business_account_phone_number.side_effect

    def lookup(id):
        if id == helper_phone:
            return {"phoneNumber": _number("d4", "+1 555-0103"), "linkedWhatsAppBusinessAccountId": "waba-1"}
        return found(id)

    eum.get_linked_whatsapp_business_account_phone_number.side_effect = lookup
    registries = {
        "bot-dev": _stack(),
        "leader-dev": _leader(),
        "helper-dev": _leader(project="helper", phone_number_id=helper_phone),
    }
    written = {}
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: registries.get(sid))
    monkeypatch.setattr(phone_numbers, "_read_all_registries", lambda: (registries, []))
    monkeypatch.setattr(
        phone_numbers, "_put_registry", lambda sid, cfg: sid != "helper-dev" and (written.setdefault(sid, cfg) or True)
    )

    resp = phone_numbers.handle_bind_phone_number({}, {"phoneNumberId": PHONE, "stackId": "bot-dev"})
    body = json.loads(resp["body"])

    assert resp["statusCode"] == 502
    assert body["siblingsNotUpdated"] == ["helper-dev"]
    assert body["siblingsAutoUpdated"] == ["leader-dev"]
    assert "The registry entries of leader-dev were updated." in body["error"]
    assert set(written) == {"leader-dev"}
    assert written["leader-dev"]["meta_phone_number_id"] == "meta-b2"
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_a_sibling_with_no_meta_phone_number_id_refuses_the_bind(eum, monkeypatch):
    _sibling_lookup(eum, {"phoneNumber": {"arn": SIBLING_PHONE}, "linkedWhatsAppBusinessAccountId": "waba-1"})

    status, body, written = _call(
        monkeypatch,
        "handle_bind_phone_number",
        {"phoneNumberId": PHONE, "stackId": "bot-dev"},
        {"bot-dev": _stack(), "leader-dev": _leader()},
    )

    assert status == 502
    assert body["siblingsNotUpdated"] == ["leader-dev"]
    assert "cannot be recorded in the registry entry or named in siblingConfig" in body["error"]
    assert "filter" not in body["error"]
    _nothing_changed(eum, written)


def test_a_sibling_that_already_filters_needs_no_write(eum, monkeypatch):
    status, body, written = _call(
        monkeypatch,
        "handle_bind_phone_number",
        {"phoneNumberId": PHONE, "stackId": "bot-dev"},
        {"bot-dev": _stack(), "leader-dev": _leader(meta_phone_number_id="meta-b2")},
    )

    assert status == 200 and body["mode"] == "shared_waba"
    assert set(written) == {"bot-dev"}
    assert body["siblingsAutoUpdated"] == []
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_a_phone_eum_social_does_not_know_is_on_no_waba(eum, monkeypatch):
    # A stale entry whose phone was unlinked: on none of this account's WABAs.
    _sibling_lookup(eum, _client_error("ResourceNotFoundException"))

    status, body, written = _call(
        monkeypatch,
        "handle_bind_phone_number",
        {"phoneNumberId": PHONE, "stackId": "bot-dev"},
        {"bot-dev": _stack(), "leader-dev": _leader()},
    )

    assert status == 200 and body["mode"] == "exclusive_waba"
    assert set(written) == {"bot-dev"}
    assert body["eventDestinationUpdated"] is True


def test_a_phone_in_another_region_is_on_another_waba(eum, monkeypatch):
    # A WABA's link and its event destination are per Region, so a phone
    # elsewhere is not looked up, and cannot fail the lookup either.
    elsewhere = SIBLING_PHONE.replace(":us-east-1:", ":eu-west-1:")

    status, body, _ = _call(
        monkeypatch,
        "handle_bind_phone_number",
        {"phoneNumberId": PHONE, "stackId": "bot-dev"},
        {"bot-dev": _stack(), "west-dev": _leader(phone_number_id=elsewhere, region="eu-west-1")},
    )

    assert status == 200 and body["mode"] == "exclusive_waba"
    looked_up = [c.kwargs["id"] for c in eum.get_linked_whatsapp_business_account_phone_number.call_args_list]
    assert elsewhere not in looked_up


def test_unbind_says_the_bot_keeps_its_number(eum, monkeypatch):
    status, body, written = _call(
        monkeypatch, "handle_unbind_phone_number", {"stackId": "bot-dev"}, {"bot-dev": _stack(phone_number_id=PHONE)}
    )

    assert status == 200 and body["unbound"] is True
    assert "phone_number_id" not in written["bot-dev"]
    assert "whatsapp.phone_number_id" in body["nextStep"]
    assert "socialagent.dev.yaml" in body["nextStep"] and "socialagent deploy" in body["nextStep"]
