# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""PUT /api/stacks/<id>/update-sns-destination must not take its target on trust.

It used to read both the WABA id and the topic ARN from the request body and
pass them straight to ``PutWhatsAppBusinessAccountEventDestinations``. Any
operator could send any WABA's inbound traffic (end-user phone numbers and
message text) to any topic, and ``force=true`` skipped the only guard. Now the
WABA comes from the stack's bound phone, the topic must be one the registry
vouches for, and the route needs the admin group.
"""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

# Route modules read shared.config at import time, which needs the env vars the
# autouse aws_env fixture provides — so import inside each test.

ACCOUNT = "111122223333"
PHONE = f"arn:aws:social-messaging:us-east-1:{ACCOUNT}:phone-number-id/p1"
SIBLING_PHONE = f"arn:aws:social-messaging:us-east-1:{ACCOUNT}:phone-number-id/p2"
OTHER_PHONE = f"arn:aws:social-messaging:us-east-1:{ACCOUNT}:phone-number-id/p3"
OWN_TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:dev-bot-whatsapp"
SIBLING_TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:dev-leader-whatsapp"
UNRELATED_TOPIC = f"arn:aws:sns:us-east-1:{ACCOUNT}:dev-other-whatsapp"

STACK = {
    "project": "bot",
    "environment": "dev",
    "region": "us-east-1",
    "phone_number_id": PHONE,
    "outputs": {"sns_topic_arn": OWN_TOPIC},
}
WABA_OF = {PHONE: "waba-1", SIBLING_PHONE: "waba-1", OTHER_PHONE: "waba-2"}


def _registries(*extra):
    regs = {"bot-dev": STACK}
    regs.update(dict(extra))
    return regs


@pytest.fixture
def eum(monkeypatch):
    """A fake EUM Social client; phones resolve to WABAs through WABA_OF."""
    from routes import phone_numbers

    client = MagicMock()
    client.get_linked_whatsapp_business_account_phone_number.side_effect = lambda id: {
        "linkedWhatsAppBusinessAccountId": WABA_OF[id]
    }
    # No event destination yet, so a bind replaces none.
    client.get_linked_whatsapp_business_account.return_value = {"account": {"eventDestinations": []}}
    monkeypatch.setattr(phone_numbers.boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: _registries().get(sid))
    monkeypatch.setattr(phone_numbers, "_read_all_registries", lambda: (_registries(), []))
    return client


def _update(body, registries=None, monkeypatch=None):
    from routes import phone_numbers

    if registries is not None:
        monkeypatch.setattr(phone_numbers, "_read_all_registries", lambda: (registries, []))
    resp = phone_numbers.handle_update_sns_destination({"id": "bot-dev"}, body)
    return resp["statusCode"], json.loads(resp["body"])


def _destination_set(eum):
    call = eum.put_whatsapp_business_account_event_destinations.call_args
    return call.kwargs["id"], call.kwargs["eventDestinations"][0]["eventDestinationArn"]


def test_defaults_to_the_stacks_own_topic_on_its_phones_waba(eum):
    status, body = _update({})

    assert status == 200
    assert _destination_set(eum) == ("waba-1", OWN_TOPIC)
    assert body["wabaId"] == "waba-1"


def test_waba_comes_from_the_bound_phone_not_the_body(eum):
    """A wabaId naming another WABA is refused, not acted on."""
    status, body = _update({"wabaId": "waba-2"})

    assert status == 400
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_matching_waba_id_from_an_older_client_is_accepted(eum):
    status, _ = _update({"wabaId": "waba-1", "snsTopicArn": OWN_TOPIC})

    assert status == 200
    assert _destination_set(eum) == ("waba-1", OWN_TOPIC)


@pytest.mark.parametrize(
    "arn",
    [
        f"arn:aws:sns:eu-west-1:{ACCOUNT}:dev-bot-whatsapp",  # another region
        f"arn:aws:sqs:us-east-1:{ACCOUNT}:dev-bot-whatsapp",  # not SNS
        f"arn:aws:sns:us-east-1:{ACCOUNT}:dev-bot-whatsapp.fifo",
        f"arn:aws:sns:us-east-1:{ACCOUNT}:a b",
        f"arn:aws:sns:us-east-1:{ACCOUNT}:topic\n",
        "arn:aws:sns:us-east-1:123:short-account",
        "not-an-arn",
    ],
)
def test_malformed_topic_arn_is_refused(eum, arn):
    status, _ = _update({"snsTopicArn": arn})

    assert status == 400
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_well_formed_topic_no_registered_stack_publishes_is_refused(eum):
    """The heart of the finding: a well-formed ARN of an arbitrary topic."""
    status, body = _update({"snsTopicArn": "arn:aws:sns:us-east-1:999999999999:attacker"})

    assert status == 400
    assert OWN_TOPIC in body["allowedTopics"]
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_topic_of_a_stack_on_another_waba_is_refused(eum, monkeypatch):
    """Registered is not enough: that stack's topic would get this WABA's traffic."""
    other = {"phone_number_id": OTHER_PHONE, "outputs": {"sns_topic_arn": UNRELATED_TOPIC}}
    status, _ = _update(
        {"snsTopicArn": UNRELATED_TOPIC, "force": True},
        registries=_registries(("other-dev", other)),
        monkeypatch=monkeypatch,
    )

    assert status == 400
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_sibling_topic_on_the_same_waba_needs_force(eum, monkeypatch):
    """A registered sibling's topic is legitimate, and the shared-WABA guard still holds."""
    leader = {"phone_number_id": SIBLING_PHONE, "outputs": {"sns_topic_arn": SIBLING_TOPIC}}
    registries = _registries(("leader-dev", leader))

    status, body = _update({"snsTopicArn": SIBLING_TOPIC}, registries, monkeypatch)
    assert status == 409
    assert body["siblingStacks"] == ["leader-dev"]
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()

    status, _ = _update({"snsTopicArn": SIBLING_TOPIC, "force": True}, registries, monkeypatch)
    assert status == 200
    assert _destination_set(eum) == ("waba-1", SIBLING_TOPIC)


def test_force_must_be_a_real_boolean(eum, monkeypatch):
    """The string "false" is truthy; it must not skip the guard."""
    leader = {"phone_number_id": SIBLING_PHONE, "outputs": {"sns_topic_arn": SIBLING_TOPIC}}
    status, _ = _update(
        {"force": "false"}, registries=_registries(("leader-dev", leader)), monkeypatch=monkeypatch
    )

    assert status == 409


def test_shared_follower_defaults_to_the_shared_topic(eum, monkeypatch):
    """A follower bound before its redeploy consumes shared_sns_topic_arn, not its old topic."""
    from routes import phone_numbers

    follower = {**STACK, "shared_sns_topic_arn": SIBLING_TOPIC}
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: follower)
    status, _ = _update({}, registries={"bot-dev": follower}, monkeypatch=monkeypatch)

    assert status == 200
    assert _destination_set(eum) == ("waba-1", SIBLING_TOPIC)


def test_stack_without_a_bound_phone_is_refused(eum, monkeypatch):
    from routes import phone_numbers

    unbound = {k: v for k, v in STACK.items() if k != "phone_number_id"}
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: unbound)
    status, _ = _update({"snsTopicArn": OWN_TOPIC})

    assert status == 400
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_stack_without_a_topic_is_refused(eum, monkeypatch):
    from routes import phone_numbers

    undeployed = {**STACK, "outputs": {}}
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: undeployed)
    status, _ = _update({}, registries={"bot-dev": undeployed}, monkeypatch=monkeypatch)

    assert status == 400
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


@pytest.mark.parametrize("field", ["snsTopicArn", "wabaId"])
def test_non_string_fields_are_refused(eum, field):
    status, _ = _update({field: ["arn:aws:sns:us-east-1:111122223333:x"]})

    assert status == 400
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException"])
def test_a_registered_stack_that_cannot_be_checked_refuses_the_change(eum, monkeypatch, force, code):
    """A stack whose phone lookup fails may share the WABA, so its guard cannot be skipped.

    The lookup skipped it, so the shared-WABA 409 never fired and the change
    replaced the destination that stack reads. force=true does not cover it:
    its topic is not even in the allowed list.
    """
    leader = {"phone_number_id": SIBLING_PHONE, "outputs": {"sns_topic_arn": SIBLING_TOPIC}}

    def lookup(id):
        if id == SIBLING_PHONE:
            raise ClientError({"Error": {"Code": code, "Message": f"{code} for {id}"}}, "Op")
        return {"linkedWhatsAppBusinessAccountId": WABA_OF[id]}

    eum.get_linked_whatsapp_business_account_phone_number.side_effect = lookup
    status, body = _update({"force": force}, registries=_registries(("leader-dev", leader)), monkeypatch=monkeypatch)

    assert status == 502
    assert body["uncheckedStacks"] == [{"stack": "leader-dev", "reason": code}]
    assert "destination was not changed" in body["error"]
    assert SIBLING_PHONE not in json.dumps(body)
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


def test_a_registry_entry_that_cannot_be_read_refuses_the_change(eum, monkeypatch):
    from routes import phone_numbers

    monkeypatch.setattr(phone_numbers, "_read_all_registries", lambda: (_registries(), ["leader-dev"]))
    status, body = _update({"force": True})

    assert status == 502
    assert body["uncheckedStacks"] == [{"stack": "leader-dev", "reason": "registry entry not read"}]
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()


# ── bind: the same WABA trust problem ──────────────────────────────────────────


def _bind(body, monkeypatch):
    from routes import phone_numbers

    writes = []
    monkeypatch.setattr(phone_numbers, "_put_registry", lambda sid, cfg: writes.append(sid) or True)
    unbound = {k: v for k, v in STACK.items() if k != "phone_number_id"}
    monkeypatch.setattr(phone_numbers, "_get_registry", lambda sid: unbound)
    monkeypatch.setattr(phone_numbers, "_read_all_registries", lambda: ({"bot-dev": unbound}, []))
    resp = phone_numbers.handle_bind_phone_number({}, body)
    return resp["statusCode"], writes


def test_bind_wires_the_waba_the_phone_belongs_to(eum, monkeypatch):
    status, writes = _bind({"phoneNumberId": PHONE, "stackId": "bot-dev"}, monkeypatch)

    assert status == 200
    assert writes == ["bot-dev"]
    assert _destination_set(eum) == ("waba-1", OWN_TOPIC)


def test_bind_refuses_a_waba_id_the_phone_is_not_on(eum, monkeypatch):
    """Binding your own phone must not re-point some other WABA at this stack."""
    status, writes = _bind(
        {"phoneNumberId": PHONE, "stackId": "bot-dev", "wabaId": "waba-2"}, monkeypatch
    )

    assert status == 400
    assert writes == []
    eum.put_whatsapp_business_account_event_destinations.assert_not_called()
