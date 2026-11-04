# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Safety hardening regression tests for agent-supplied input: the media-upload
bucket/key allowlist, the schedule-expression allowlist, the recurring-cron
minimum interval, scheduler object-level authorization (the exact owner field,
the operation allowlist, explicit ids and a phone-less cancel) and schedule-name
prefix matching."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from socialagent.providers.eum_social import EUMSocialProvider


# ── upload_media_from_s3 bucket/key allowlist ───────────────────────────────────


def _media_provider() -> EUMSocialProvider:
    p = EUMSocialProvider.__new__(EUMSocialProvider)
    p._conversation_logs_bucket = "my-stack-media"
    p._media_prefix = "stacks/my-stack"
    p._phone_number_id = "pid"
    p._client = MagicMock()
    return p


@pytest.mark.asyncio
async def test_upload_media_rejects_foreign_bucket():
    p = _media_provider()
    out = await p.upload_media_from_s3("attacker-bucket", "stacks/my-stack/voice/a.mp3")
    assert out == ""
    p._client.post_whatsapp_message_media.assert_not_called()


@pytest.mark.asyncio
async def test_upload_media_rejects_out_of_prefix_key():
    p = _media_provider()
    out = await p.upload_media_from_s3("my-stack-media", "secrets/creds.txt")
    assert out == ""
    p._client.post_whatsapp_message_media.assert_not_called()


@pytest.mark.asyncio
async def test_upload_media_rejects_path_traversal():
    p = _media_provider()
    out = await p.upload_media_from_s3(
        "my-stack-media", "stacks/my-stack/../../etc/passwd"
    )
    assert out == ""
    p._client.post_whatsapp_message_media.assert_not_called()


@pytest.mark.asyncio
async def test_upload_media_allows_legit_voice_and_image_keys():
    p = _media_provider()
    p._client.post_whatsapp_message_media.return_value = {"mediaId": "m-1"}
    for key in (
        "stacks/my-stack/voice/a.mp3",
        "stacks/my-stack/images/b.jpg",
        "stacks/my-stack/media/c",
    ):
        assert await p.upload_media_from_s3("my-stack-media", key) == "m-1"


@pytest.mark.parametrize(
    "key",
    [
        # Under the stack prefix but outside the three media roots: the rest of
        # the bucket is conversation data and audit trails, not media.
        "stacks/my-stack/conversations/+525555550100.json",
        "stacks/my-stack/audit-trails/cloudtrail/x.json.gz",
        "stacks/my-stack/voice.mp3",
        "stacks/my-stack/voicemail/a.mp3",
        # A media root that is not under this stack's prefix.
        "voice/a.mp3",
        "other-stack/images/b.jpg",
    ],
)
@pytest.mark.asyncio
async def test_upload_media_rejects_keys_outside_the_media_roots(key):
    """Only voice/, images/ and media/ under MEDIA_PREFIX are uploadable.

    The allowlist used to accept any key under the prefix, so a prompt-injected
    agent could link stored conversation data and have it delivered as a
    document. The roots match the notification role's read grant.
    """
    p = _media_provider()
    p._client.post_whatsapp_message_media.return_value = {"mediaId": "m-1"}
    assert await p.upload_media_from_s3("my-stack-media", key) == ""
    p._client.post_whatsapp_message_media.assert_not_called()


@pytest.mark.asyncio
async def test_upload_media_roots_apply_without_a_prefix():
    """A dedicated bucket (empty MEDIA_PREFIX) still restricts to the roots."""
    p = _media_provider()
    p._media_prefix = ""
    p._client.post_whatsapp_message_media.return_value = {"mediaId": "m-1"}
    for key in ("voice/a.mp3", "images/b.jpg", "media/c"):
        assert await p.upload_media_from_s3("my-stack-media", key) == "m-1"
    for key in ("conversations/x.json", "secrets/creds.txt", "voicex/a.mp3"):
        assert await p.upload_media_from_s3("my-stack-media", key) == ""
    assert p._client.post_whatsapp_message_media.call_count == 3


@pytest.mark.asyncio
async def test_webhook_drain_rejects_an_agent_link_outside_the_media_roots():
    """The drain turns agent S3 links into uploads; a log link must not upload.

    The message keeps its link (WhatsApp cannot fetch a private S3 object), so
    nothing from the bucket reaches the user.
    """
    from socialagent.handlers.webhook import _upload_media_if_needed

    p = _media_provider()
    rich = {
        "type": "document",
        "payload": {
            "document": {
                "link": "https://my-stack-media.s3.us-east-1.amazonaws.com/"
                "stacks/my-stack/conversations/%2B525555550100.json"
            }
        },
    }
    assert await _upload_media_if_needed(rich, p) is rich
    p._client.post_whatsapp_message_media.assert_not_called()


@pytest.mark.asyncio
async def test_upload_media_fails_closed_when_no_bucket_configured():
    """No media bucket configured: fail closed. This provider is only built
    for real deployments — the socialagent dev REPL uses _DevProvider — so a missing
    bucket is a misconfiguration, not a dev session. Reject rather than skip the
    allowlist and let any bucket/key through."""
    p = EUMSocialProvider.__new__(EUMSocialProvider)
    p._conversation_logs_bucket = ""
    p._media_prefix = ""
    p._phone_number_id = "pid"
    p._client = MagicMock()
    p._client.post_whatsapp_message_media.return_value = {"mediaId": "dev"}
    assert await p.upload_media_from_s3("anything", "anykey") == ""
    p._client.post_whatsapp_message_media.assert_not_called()


# ── schedule expression allowlist ───────────────────────────────────────────────

_AGENT_DIR = str(Path(__file__).parent.parent / "packages" / "socialagent-runtime")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)


def test_cron_validator_accepts_valid_6field():
    from scheduler import _validate_cron_inner

    assert _validate_cron_inner("0 12 * * ? *") == "0 12 * * ? *"


@pytest.mark.parametrize(
    "bad",
    [
        "0 12 * * ?",  # only 5 fields
        "0 12 * * ? * extra",  # 7 fields
        "0 12 * * ? *; rm -rf /",  # injection
        "$(whoami)",
    ],
)
def test_cron_validator_rejects_bad(bad):
    from scheduler import _validate_cron_inner

    with pytest.raises(ValueError):
        _validate_cron_inner(bad)


def test_at_validator():
    from scheduler import _validate_at_inner

    assert _validate_at_inner("2026-07-13T12:00:00") == "2026-07-13T12:00:00"
    with pytest.raises(ValueError):
        _validate_at_inner("2026-07-13 12:00")
    with pytest.raises(ValueError):
        _validate_at_inner("tomorrow")


# ── recurring cron minimum-interval floor ───────────────────────────────────────


@pytest.mark.parametrize(
    "ok",
    [
        "30 14 * * ? *",  # fixed minute, once/hour
        "*/15 * * * ? *",  # 15-min step == floor
        "*/30 * * * ? *",  # coarser step
        "0,30 * * * ? *",  # comma list, 30 apart
        "0,15,30,45 * * * ? *",  # 15 apart
        "5 9 * * ? *",  # single fixed minute
    ],
)
def test_cron_floor_accepts_coarse(ok):
    from scheduler import _validate_cron_inner

    assert _validate_cron_inner(ok) == ok


@pytest.mark.parametrize(
    "bad",
    [
        "* * * * ? *",  # every minute
        "*/1 * * * ? *",  # every minute (step 1)
        "*/5 * * * ? *",  # every 5 min < floor
        "0-30 * * * ? *",  # range fires every minute
        "0,10,20 * * * ? *",  # 10 apart < floor
        "0,5 * * * ? *",  # 5 apart < floor
        # EventBridge's start/step form: N/step fires at N, N+step, ... to :59,
        # so these fire every 1, 5, 10 and 14 minutes, not once an hour.
        "0/1 * * * ? *",
        "0/5 * * * ? *",
        "5/10 * * * ? *",
        "0/14 * * * ? *",
    ],
)
def test_cron_floor_rejects_frequent(bad):
    from scheduler import _validate_cron_inner

    with pytest.raises(ValueError):
        _validate_cron_inner(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "*/1,*/15 * * * ? *",  # comma list of steps — the fine one still fires
        "*,*/15 * * * ? *",  # bare * in a comma list is still every minute
        "0,1/20 * * * ? *",  # :00 and the :01 of 1/20 are a minute apart
        "*/15,*/1 * * * ? *",  # order must not matter
        "0-5,30 * * * ? *",  # a range inside a comma list fires every minute
        "*/59 * * * ? *",  # :00 and :59 are one minute apart across the hour
    ],
)
def test_cron_floor_rejects_frequent_comma_combinations(bad):
    """A comma list must be judged by when it actually fires.

    The floor was checked by inspecting syntax one branch at a time, and the
    step branch ran before the comma branch while reading only the text after
    the *last* slash. So "*/1,*/15" was judged on the 15 and accepted, while the
    "*/1" half fired every minute — bypassing the minimum-interval control.
    """
    from scheduler import _validate_cron_inner

    with pytest.raises(ValueError):
        _validate_cron_inner(bad)


@pytest.mark.parametrize(
    "ok",
    [
        "0,20,40 * * * ? *",  # 20 apart, wraps to 20
        "*/20 * * * ? *",  # {0,20,40}: last-to-next-hour gap is 20
        "0-45/15 * * * ? *",  # range with a coarse step
        "10 * * * ? *",  # single fixed minute
        "0/15 * * * ? *",  # start/step form: {0,15,30,45}
        "10/20 * * * ? *",  # {10,30,50}: wraps to :10 after 20
        "45/15 * * * ? *",  # only :45 is left before the hour ends
    ],
)
def test_cron_floor_accepts_coarse_combinations(ok):
    """The stricter check must not reject legitimately coarse schedules."""
    from scheduler import _validate_cron_inner

    assert _validate_cron_inner(ok) == ok


# ── scheduler object-level authorization (BOLA) ──────────────────────────────────


def test_owns_schedule_matches_only_owner():
    from scheduler import _auto_schedule_id, _owns_schedule

    name = _auto_schedule_id("+525555550100", "reminder", {"message": "hi"})
    assert _owns_schedule(name, "+525555550100") is True  # owner
    assert _owns_schedule(name, "+525555550199") is False  # other user
    assert _owns_schedule(name, "") is False  # empty phone owns nothing


def test_owns_schedule_rejects_substring_phone():
    """A shorter phone that is a substring of a longer one must NOT match — the
    digits are matched as a whole hyphen-delimited token, not a raw substring."""
    from scheduler import _auto_schedule_id, _owns_schedule

    long_name = _auto_schedule_id("+525555550100", "reminder", {"m": "x"})
    assert _owns_schedule(long_name, "5550100") is False  # substring, not owner
    # Ownership holds across all name shapes for the true owner.
    assert _owns_schedule("reminder-525555550100", "+525555550100") is True
    assert (
        _owns_schedule(
            "myprefix-525555550100-reminder-abc", "+525555550100", prefix="myprefix"
        )
        is True
    )
    assert _owns_schedule("reminder-525555550100", "5550100") is False


class _ProdConfig:
    """Non-dev config stub (config is a frozen dataclass with a computed
    is_dev_mode property, so we swap the module reference instead of mutating)."""

    is_dev_mode = False
    has_scheduler_lambda = True
    scheduler_prefix = ""
    region = "us-east-1"


@pytest.mark.asyncio
async def test_list_schedules_fails_closed_without_phone(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _ProdConfig())
    # No phone -> empty, and no AWS client is ever built.
    assert await sched.list_schedules("") == []


@pytest.mark.asyncio
async def test_list_schedules_returns_only_owned(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _ProdConfig())

    mine = "525555550100-reminder-aaa"
    theirs = "525555550199-reminder-bbb"

    class _Client:
        def list_schedules(self, **kwargs):
            return {"Schedules": [{"Name": mine}, {"Name": theirs}]}

        def get_schedule(self, Name):
            return {
                "State": "ENABLED",
                "ScheduleExpression": "at(2026-01-01T00:00:00)",
                "Target": {
                    "Input": '{"operation":"reminder","data":{"phone":"x","message":"m"}}'
                },
            }

    monkeypatch.setattr(sched, "_scheduler_client", lambda: _Client())
    results = await sched.list_schedules("+525555550100")
    names = [r["name"] for r in results]
    assert names == [mine]  # the other user's schedule is filtered out


@pytest.mark.asyncio
async def test_cancel_refuses_foreign_schedule(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _ProdConfig())
    called = {"delete": False}

    class _Client:
        class exceptions:
            class ResourceNotFoundException(Exception): ...

        def delete_schedule(self, **kwargs):
            called["delete"] = True

    monkeypatch.setattr(sched, "_scheduler_client", lambda: _Client())
    # A schedule owned by someone else: caller phone digits are not in the name.
    await sched.cancel("525555550199-reminder-abc", phone="+525555550100")
    assert called["delete"] is False


@pytest.mark.asyncio
async def test_cancel_allows_own_schedule(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _ProdConfig())
    called = {"name": None}

    class _Client:
        class exceptions:
            class ResourceNotFoundException(Exception): ...

        def delete_schedule(self, **kwargs):
            called["name"] = kwargs.get("Name")

    monkeypatch.setattr(sched, "_scheduler_client", lambda: _Client())
    await sched.cancel("525555550100-reminder-abc", phone="+525555550100")
    assert called["name"] == "525555550100-reminder-abc"


# ── schedule ownership: exact owner field, not "any token" ───────────────────────

_VICTIM = "+15551234567"
_ATTACKER = "+15550000000"


class _PrefixedProdConfig:
    """Non-dev config stub with a scheduler prefix, as Terraform sets one."""

    is_dev_mode = False
    scheduler_prefix = "dev-bot"
    scheduler_lambda_arn = "arn:aws:lambda:us-east-1:123:function:scheduler"
    has_scheduler_lambda = True
    scheduler_role_arn = "arn:aws:iam::123:role/scheduler"
    region = "us-east-1"


class _RecordingClient:
    """Scheduler client fake: lists ``names``, records deletes."""

    class exceptions:
        class ResourceNotFoundException(Exception): ...

    def __init__(self, names=()):
        self.names = list(names)
        self.deleted: list[str] = []

    def list_schedules(self, **kwargs):
        return {"Schedules": [{"Name": n} for n in self.names]}

    def get_schedule(self, Name):
        return {
            "State": "ENABLED",
            "ScheduleExpression": "cron(0 9 * * ? *)",
            "Target": {"Input": '{"operation":"reminder","data":{"message":"m"}}'},
        }

    def delete_schedule(self, Name):
        self.deleted.append(Name)


def test_second_digits_field_does_not_confer_ownership():
    """The spoof: an operation of the victim's digits used to make the
    attacker's auto id count as the victim's too. Names like this may already
    be live, so the parser must read only the owner field."""
    from scheduler import _owns_schedule

    spoof = "15550000000-15551234567-abcd1234"  # {attacker}-{victim}-{hash}
    assert _owns_schedule(spoof, _VICTIM, prefix="") is False
    assert _owns_schedule(spoof, _ATTACKER, prefix="") is True
    assert _owns_schedule(f"dev-bot-{spoof}", _VICTIM, prefix="dev-bot") is False
    assert _owns_schedule(f"dev-bot-{spoof}", _ATTACKER, prefix="dev-bot") is True
    # A trailing-owner name with two digit fields belongs to nobody.
    two = "reminder-15550000000-15551234567"
    assert _owns_schedule(two, _VICTIM, prefix="") is False
    assert _owns_schedule(two, _ATTACKER, prefix="") is False
    # The 8-char hash can be all digits; it is never the owner.
    assert (
        _owns_schedule("15550000000-reminder-12345678", "12345678", prefix="") is False
    )


def test_spoofed_name_counts_only_against_its_creator(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    client = _RecordingClient(
        [
            "dev-bot-15550000000-15551234567-abcd1234",  # attacker's, victim digits inside
            "dev-bot-15551234567-reminder-aaaa1111",  # victim's own
        ]
    )
    monkeypatch.setattr(sched, "_scheduler_client", lambda: client)
    assert sched._count_user_schedules(_VICTIM) == 1
    assert sched._count_user_schedules(_ATTACKER) == 1


@pytest.mark.asyncio
async def test_spoofed_name_is_not_listed_or_cancellable_by_the_victim(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    spoof = "dev-bot-15550000000-15551234567-abcd1234"
    client = _RecordingClient([spoof])
    monkeypatch.setattr(sched, "_scheduler_client", lambda: client)
    assert await sched.list_schedules(_VICTIM) == []
    await sched.cancel(spoof, phone=_VICTIM)
    assert client.deleted == []


@pytest.mark.parametrize(
    "operation",
    [
        "15551234567",  # the spoof: a phone number as the operation
        "reminder-15551234567",  # a hyphen splits it into extra fields
        "reminder1",
        "Reminder",
        "_reminder",
        "",
        "a" * 33,
        "reminder\n",
        "remind me",
        None,
    ],
)
@pytest.mark.asyncio
async def test_schedule_rejects_unsafe_operation(monkeypatch, operation):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    created = MagicMock()
    monkeypatch.setattr(sched, "_create_schedule", created)
    monkeypatch.setattr(sched, "_count_user_schedules", lambda phone: 0)
    with pytest.raises(ValueError, match="invalid operation"):
        await sched.schedule(operation, _ATTACKER, cron="0 9 * * ? *")
    created.assert_not_called()


@pytest.mark.parametrize(
    "operation", ["reminder", "invoke_agent", "visit_reminder", "a" * 32]
)
def test_validate_operation_accepts_shipped_names(operation):
    from scheduler import _validate_operation

    assert _validate_operation(operation) == operation


@pytest.mark.asyncio
async def test_schedule_rejects_unsafe_operation_in_dev_mode_too():
    """`socialagent dev` must refuse what the deployed stack would refuse."""
    import scheduler as sched

    assert sched.config.is_dev_mode
    with pytest.raises(ValueError, match="invalid operation"):
        await sched.schedule("15551234567", _ATTACKER, cron="0 9 * * ? *")


@pytest.mark.asyncio
async def test_auto_id_is_owned_by_the_scheduling_phone(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    created = MagicMock()
    monkeypatch.setattr(sched, "_create_schedule", created)
    monkeypatch.setattr(sched, "_count_user_schedules", lambda phone: 0)
    await sched.schedule("invoke_agent", _VICTIM, 5, {"prompt": "weather"})
    name = created.call_args[0][0]
    assert name.startswith("dev-bot-15551234567-invoke-agent-")
    assert sched._owns_schedule(name, _VICTIM) is True
    assert sched._owns_schedule(name, _ATTACKER) is False


@pytest.mark.parametrize(
    "schedule_id",
    [
        "reminder-15551234567",  # the appointment blueprint's id
        "dev-bot-reminder-15551234567",  # already in the prefix namespace
        "visit_reminder-15551234567",  # "_" becomes "-" in the name
        "daily-checkin-15551234567",
        "15551234567-weekly_report",
    ],
)
@pytest.mark.asyncio
async def test_schedule_accepts_explicit_id_owned_by_the_phone(
    monkeypatch, schedule_id
):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    created = MagicMock()
    monkeypatch.setattr(sched, "_create_schedule", created)
    monkeypatch.setattr(sched, "_count_user_schedules", lambda phone: 0)
    sid = await sched.schedule(
        "visit_reminder",
        _VICTIM,
        2,
        {"property": "Downtown Loft"},
        schedule_id=schedule_id,
    )
    assert sid == schedule_id
    assert sched._owns_schedule(created.call_args[0][0], _VICTIM) is True


@pytest.mark.parametrize(
    "schedule_id",
    [
        "reminder-15551234567",  # another user's appointment reminder (upsert = overwrite)
        "15551234567-reminder-abcd1234",  # another user's auto id
        "my-id",  # nobody's: escapes list, cancel and the cap
        "reminder-order-123",
        "reminder-15550000000-15551234567",
    ],
)
@pytest.mark.asyncio
async def test_schedule_refuses_explicit_id_not_owned_by_the_phone(
    monkeypatch, schedule_id
):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    created = MagicMock()
    monkeypatch.setattr(sched, "_create_schedule", created)
    monkeypatch.setattr(sched, "_count_user_schedules", lambda phone: 0)
    with pytest.raises(ValueError, match="does not belong"):
        await sched.schedule("reminder", _ATTACKER, 5, {}, schedule_id=schedule_id)
    created.assert_not_called()


@pytest.mark.parametrize("phone", ["", "+", None])
@pytest.mark.asyncio
async def test_schedule_requires_a_phone_outside_dev_mode(monkeypatch, phone):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    created = MagicMock()
    monkeypatch.setattr(sched, "_create_schedule", created)
    monkeypatch.setattr(sched, "_count_user_schedules", lambda phone: 0)
    with pytest.raises(ValueError, match="phone is required"):
        await sched.schedule("reminder", phone, 5, {"message": "hi"})
    created.assert_not_called()


@pytest.mark.parametrize("phone", ["", "+", None])
@pytest.mark.asyncio
async def test_cancel_fails_closed_without_phone(monkeypatch, phone):
    """An empty phone used to skip the ownership check and delete any name."""
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    client = _RecordingClient()
    monkeypatch.setattr(sched, "_scheduler_client", lambda: client)
    with pytest.raises(ValueError, match="phone is required"):
        await sched.cancel("15551234567-reminder-abcd1234", phone=phone)
    with pytest.raises(ValueError, match="phone is required"):
        await sched.cancel("15551234567-reminder-abcd1234")
    assert client.deleted == []


@pytest.mark.asyncio
async def test_cancel_fails_closed_without_phone_in_dev_mode(capsys):
    import scheduler as sched

    assert sched.config.is_dev_mode
    with pytest.raises(ValueError, match="phone is required"):
        await sched.cancel("15551234567-reminder-abcd1234")
    assert "CANCEL" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "prefix", "owned"),
    [
        ("dev-bot-15551234567-reminder-abcd1234", "dev-bot", True),
        # A different stack whose prefix starts with ours: NamePrefix returns
        # it, but it is not in our namespace.
        ("dev-bot2-15551234567-reminder-abcd1234", "dev-bot", False),
        # A prefix with a digit field is stripped whole before the owner is read.
        ("dev-bot2-15551234567-reminder-abcd1234", "dev-bot2", True),
        ("dev-bot2-reminder-15551234567", "dev-bot2", True),
        # Outside the configured prefix entirely.
        ("other-15551234567-reminder-abcd1234", "dev-bot", False),
        ("15551234567-reminder-abcd1234", "dev-bot", False),
        # No prefix configured: the name is read from its first field.
        ("15551234567-reminder-abcd1234", "", True),
        ("reminder-15551234567", "", True),
    ],
)
def test_owner_is_read_after_the_configured_prefix(name, prefix, owned):
    from scheduler import _owns_schedule

    assert _owns_schedule(name, _VICTIM, prefix=prefix) is owned


def test_owner_prefix_defaults_to_config(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    assert sched._owns_schedule("dev-bot-reminder-15551234567", _VICTIM) is True
    assert sched._owns_schedule("reminder-15551234567", _VICTIM) is False


def _live_names() -> list[str]:
    """Every name shape the shipped code has created, under prefix dev-bot."""
    import scheduler as sched

    digits = "15551234567"
    return [
        # personal-assistant: schedule_reminder / schedule_recurring
        f"dev-bot-{sched._auto_schedule_id(_VICTIM, 'reminder', {'message': 'hi'})}",
        # personal-assistant: schedule_task ("invoke_agent" becomes "invoke-agent")
        f"dev-bot-{digits}-invoke-agent-0a1b2c3d",
        # appointment-scheduler: book_visit
        f"dev-bot-reminder-{digits}",
    ]


@pytest.mark.asyncio
async def test_existing_name_formats_still_list_for_their_owner(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    names = _live_names()
    client = _RecordingClient([*names, "dev-bot-15550000000-reminder-ffff0000"])
    monkeypatch.setattr(sched, "_scheduler_client", lambda: client)
    listed = [s["name"] for s in await sched.list_schedules(_VICTIM)]
    assert listed == names
    assert sched._count_user_schedules(_VICTIM) == len(names)


@pytest.mark.asyncio
async def test_existing_name_formats_still_cancel_for_their_owner(monkeypatch):
    import scheduler as sched

    monkeypatch.setattr(sched, "config", _PrefixedProdConfig())
    client = _RecordingClient()
    monkeypatch.setattr(sched, "_scheduler_client", lambda: client)
    names = _live_names()
    for name in names:
        await sched.cancel(name, phone=_VICTIM)
    # The unprefixed id a tool returned resolves into the namespace too.
    await sched.cancel("reminder-15551234567", phone=_VICTIM)
    assert client.deleted == [*names, "dev-bot-reminder-15551234567"]
    # None of them can be cancelled by anyone else.
    client.deleted.clear()
    for name in names:
        await sched.cancel(name, phone=_ATTACKER)
    assert client.deleted == []


# ── scheduler name/prefix and cron token robustness ────────────────────────────


def test_schedule_name_respects_prefix_boundary():
    """A prefix must match on the hyphen boundary, not a bare substring.

    Otherwise prefix "socialagent-dev" treats id "socialagent-devious" as already-prefixed and
    leaves it outside the NamePrefix namespace, invisible to listing and the cap.
    """
    import scheduler as sch

    original = sch.config.scheduler_prefix
    object.__setattr__(sch.config, "scheduler_prefix", "socialagent-dev")
    try:
        assert sch._schedule_name("devious-thing") == "socialagent-dev-devious-thing"
        # An id already in the namespace is not double-prefixed.
        assert (
            sch._schedule_name("socialagent-dev-reminder-5")
            == "socialagent-dev-reminder-5"
        )
    finally:
        object.__setattr__(sch.config, "scheduler_prefix", original)


@pytest.mark.parametrize(
    "cron",
    ["0 9 ? * MON *", "0 9 1 JAN ? *", "0 9 ? * MON-FRI *"],
)
def test_cron_accepts_alphabetic_day_month_tokens(cron):
    """EventBridge accepts MON-SUN / JAN-DEC; the allowlist rejected them."""
    from scheduler import _validate_cron_inner

    assert _validate_cron_inner(cron) == cron
