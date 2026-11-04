# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Property Viewing Scheduler agent — deployed to AgentCore Runtime via Terraform.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeModelCallEvent
from socialagent_runtime import (
    create_session_manager,
    socialagent_notify_context,
    SocialAgentResponse,
)
from socialagent_runtime.config import config

import socialagent_runtime.state as state
from src.catalog import AGENCY_ADDRESS, LISTINGS, SLOTS, format_slots
from src.model import MODEL_ID
from src.skills import available_skills_block
from src.tools import tools

app = BedrockAgentCoreApp()
logger = logging.getLogger(__name__)


def _model_kwargs() -> dict:
    """BedrockModel kwargs, adding the Bedrock Guardrail when one is configured.

    When BEDROCK_GUARDRAIL_ID is set (Terraform sets it from terraform.guardrail
    in socialagent.<env>.yaml: the stack's guardrail, or one of your own; see
    "Guardrails" in docs/configuration.md in the SMART repo), the guardrail is
    enforced on every model call. Without it (terraform.guardrail: none) no
    guardrail arg is passed.
    """
    kwargs = {"model_id": MODEL_ID}
    if config.guardrail_enabled:
        kwargs["guardrail_id"] = config.guardrail_id
        kwargs["guardrail_version"] = config.guardrail_version or "DRAFT"
    return kwargs


model = BedrockModel(**_model_kwargs())

_SYSTEM_PROMPT_TEMPLATE = """\
You are a WhatsApp assistant for Example Realty, helping users schedule property \
viewings. You can respond with text or with interactive messages via the \
`attach_interactive` tool.

{time_context}

## Current User Status
{context}

> *IMPORTANT*: The status above is fetched live from the database on every \
message and is the *single source of truth*. If it contradicts anything in the \
conversation history, the status above is correct. Never rely on previous \
conversation turns to determine booking status.
>
> If the conversation history appears to be missing or starts abruptly, a \
technical reset may have occurred. Greet the user naturally and continue from \
the current status — do not mention the reset.

## Listings & Viewing Slots
{slots_section}

## Agency Office
{AGENCY_ADDRESS}

Each listing has its own address; a viewing happens at the property, not at the \
office.

## Rules
1. ALWAYS call load_skill before responding to any request
2. book_visit only after the user's yes: echo back the details in a `Yes`/`No` \
buttons message (`Sí`/`No` in Spanish), wait for the yes, then execute. The yes to \
book is a tap on that card's `Yes`/`Sí`, which arrives as `[Button reply: Yes]` or \
`[Button reply: Sí]`, or a typed reply that plainly says yes to booking that slot \
("yes, book it", "sí, agéndala"). A bare "ok", "dale" or "confirmo", a yes to some \
other question, or anything you cannot read as a yes to the booking is not one: send \
the buttons again. Never re-ask after a yes. That confirmation is always buttons, \
never a plain-text question. cancel_visit is the exception: it asks for its own \
confirmation with its own buttons (`Yes, cancel` / `No, keep it`; `Sí, cancelar` / \
`No, mantener` in Spanish) and judges the answer itself. Call it as soon as the user \
asks to cancel; while its question is open, ask the user nothing else, and call it \
again only on the user's reply to that card, once per message. A reply to any other \
question is not a cancel answer; never send your own Yes/No for a cancellation
3. NEVER mention internal IDs to the user — refer to listings by name and slots \
by date and time only. Book by passing the listing name plus the slot's date and \
time to execute_action; there are no ids to track.
4. Be friendly and concise. Reply in the language the user writes in; the \
listing names and this prompt are in English, the user may not be.
4a. Whenever you list the available properties, ALWAYS include each one's price \
(e.g. "Downtown Loft — $420,000"), even in a quick buttons prompt — the price is \
part of the listing the user is entitled to see. With buttons or a list, the \
prices go in the body; a label or row title carries the listing name alone, \
since WhatsApp caps them at 20 and 24 characters.
5. Your FINAL text output is the ONLY thing sent to the user, the intermediate \
text between tool calls is not visible to the user.
6. Earlier turns are restored as text only; the tool calls behind them are not \
shown. A past "booked" or "cancelled" was true when sent. Never conclude an \
earlier action did not happen because its call is missing, and never apologize \
for actions you did take: check the Current User Status above and answer from \
that.

## Sending Messages — CRITICAL

### Rich messages
Load the `rich_messages` skill for full examples and parameter details.

When you call `attach_interactive`, the *attachment is sent FIRST, then your \
final text*.

*The `body` parameter of `attach_interactive` is your FULL reply* — not a \
label. Write your greeting, context, reasoning, and the question that ends in \
a choice, all inside `body` (up to 1024 chars).

After the attachment, your final text output should be:
- *Empty* in most cases, OR
- *≤12 words* of warmth / follow-up tip (e.g. "I'll confirm as soon as you \
pick 👍")

NEVER use the final text to introduce what's in the attachment — the user \
already saw it. Don't repeat text between attachment body and final response.

### FORMATTING RULES (WhatsApp, NOT standard markdown):
- Bold: *text* (single asterisk, NOT **text**)
- Italic: _text_ (underscore)
- Strikethrough: ~text~
- Monospace: ```text```
- NEVER use **double asterisks**, # headers, [links](url), or any standard markdown
- Use emojis naturally
- Break long responses into short paragraphs

{skills_section}"""


def _build_context(booking: dict | None) -> str:
    """Infer user state from data shape — no stored status field."""
    if booking:
        return (
            # A booking stored before the rename carries "car"; read both
            # for one release so a user mid-booking at deploy time is not stuck.
            f"Active booking: {booking.get('property') or booking.get('car', '')} on {booking['date']} "
            f"at {booking['time']} at {booking['location']}"
        )
    return (
        "No active bookings. If the user believes they have a booking, "
        "it may have been cancelled — trust this status over conversation history."
    )


def _slots_section() -> str:
    return "\n\n".join(
        f"*{p['name']}* — {p['price']} ({p['address']}):\n{format_slots(p['id'])}"
        for p in LISTINGS
    )


class ContextRefresher(HookProvider):
    """Builds the system prompt with fresh state, once per invocation.

    One refresher is created per invocation, so the prompt built at the first
    model call is kept for the rest of the turn. Newer Claude models reject a
    thinking block sent back after the system prompt changed, and a turn with
    several tool calls can cross a minute (the time line) or midnight (the slot
    dates) between model calls. The booking comes from invocation_state, which
    is fixed for the turn, so nothing fresher is lost.
    """

    def __init__(self) -> None:
        self._prompt: str | None = None

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeModelCallEvent, self._refresh)

    def _refresh(self, event: BeforeModelCallEvent) -> None:
        if self._prompt is None:
            booking = event.invocation_state.get("booking")
            now = datetime.now(timezone.utc).astimezone()
            time_context = f"Current date/time: {now.strftime('%A, %Y-%m-%d %H:%M %Z')}"
            self._prompt = _SYSTEM_PROMPT_TEMPLATE.format(
                time_context=time_context,
                context=_build_context(booking),
                slots_section=_slots_section(),
                AGENCY_ADDRESS=AGENCY_ADDRESS,
                skills_section=available_skills_block(),
            )
        event.agent.system_prompt = self._prompt


async def _drop_unasked_question(phone: str, message_id: str) -> None:
    """Forget a cancel question this message asked but the user never received.

    The queued buttons are dropped when the turn raises, so a `cancel_pending` whose
    ``asked_by`` holds this message id would be a live question nobody saw, and the
    user's next "yes" would answer it. A record whose ``asked_by`` does not hold this
    id stays; a re-ask that failed drops the whole question, which only costs the user
    one more card.
    """
    try:
        pending = await state.get(phone, "cancel_pending")
        asked_by = pending.get("asked_by") if isinstance(pending, dict) else None
        if message_id and isinstance(asked_by, list) and message_id in asked_by:
            await state.delete(phone, "cancel_pending")
    except Exception:  # the turn's own error is the one worth raising
        logger.warning("could not drop the undelivered cancel question", exc_info=True)


@app.entrypoint
async def invoke(payload: dict, context: object = None) -> dict:
    """Handle an inbound WhatsApp message."""
    # Taken before anything is read from state: cancel_visit records it as when
    # the asking turn began, so a visit reminder delivered after this moment (its
    # handler may run inside the turn, before or after the tool reads state) is
    # one the question was open for, whatever order the two landed in.
    turn_started_at = time.time()
    phone = payload.get("phone", "")
    booking = await state.get(phone, "booking")

    sm = create_session_manager(payload)
    agent = Agent(
        model=model,
        system_prompt="",  # set by ContextRefresher before first model call
        session_manager=sm,
        tools=tools,
        hooks=[ContextRefresher()],
    )
    meta = payload.get("socialagent") or {}
    invocation_state = {
        "phone": phone,
        "listings": LISTINGS,
        "slots": SLOTS,
        "booking": booking,
        # The current message, so a tool can judge the user's answer itself instead
        # of trusting the model to ask first: the text, whether WhatsApp delivered
        # it as a tap ("button_reply") or typed, and which message it is.
        "prompt": payload["prompt"],
        "message_type": meta.get("message_type", ""),
        "message_id": meta.get("message_id", ""),
        "turn_started_at": turn_started_at,
        "language": (booking or {}).get("language", "en"),
    }
    try:
        with socialagent_notify_context() as outbound:
            result = agent(payload["prompt"], invocation_state=invocation_state)
    except Exception:
        await _drop_unasked_question(phone, invocation_state["message_id"])
        raise
    return SocialAgentResponse(result=str(result), outbound_messages=outbound)


app.run()
