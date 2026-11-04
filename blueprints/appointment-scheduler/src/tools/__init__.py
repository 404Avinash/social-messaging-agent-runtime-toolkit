# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tools for the appointment-scheduler agent."""

from __future__ import annotations

import re
import time
import unicodedata

from strands import tool, ToolContext

import socialagent_runtime.state as state
import socialagent_runtime.scheduler as scheduler
import socialagent_runtime.notify as notify
import socialagent_runtime.messages as messages

from src.catalog import (
    find_slot,
    resolve_property_by_name,
    resolve_slot_by_datetime,
)
from src.skills import load_skill as _load_skill
from src.tools.rich_messages import build_rich_message as _build_rich_message


# Word lists, not a language detector. They decide only the cancel card's labels and
# the reminder template. Each list is deliberately small: a message with no marker
# keeps the booking's language. Words that are also English ("agenda", "favor") are
# left out of the Spanish markers; common es-MX confirmations ("dale", "listo") are in,
# since the message that books a visit is often just the confirmation itself.
_SPANISH_MARKERS = frozenset(
    "hola buenas quiero cancela cancelar cancélala cancelala cancélalo cancelalo visita "
    "cita agendar mi sí si gracias mejor horario propiedad casa departamento claro "
    "confirmo mañana tarde dale okey perfecto listo va sale acuerdo déjala dejala espera "
    "mantenla mantener puedo porfa adelante hay problema nel".split()
)
_ENGLISH_MARKERS = frozenset(
    "yes yeah yep sure please cancel the my visit viewing booking book i need want to "
    "hi hello thanks keep wait schedule time slot it".split()
)
# The cancel card's own button titles, distinct from the booking card's `Yes`/`No`:
# a tap echoes the title back, so the title alone tells a yes to this card from a yes
# to anything else. WhatsApp caps a reply-button title at 20 characters.
CANCEL_LABELS = {
    "en": ("Yes, cancel", "No, keep it"),
    "es": ("Sí, cancelar", "No, mantener"),
}
_CANCEL_YES = frozenset(yes for yes, _ in CANCEL_LABELS.values())
_CANCEL_NO = frozenset(no for _, no in CANCEL_LABELS.values())
# A typed answer cancels only when the whole message, folded by _typed_answer, is one
# of these. The list is closed because the ways a yes can be phrased cannot be
# enumerated, so a grammar (a yes word plus allowed fillers) also accepts replies that
# are not a yes ("yes I want to keep it", "sí va la cita"), and a false yes loses a
# booking while a missed one only costs the user a tap on the card. Nothing here names
# the booking, because a yes next to the booking ("yes, cancel my visit") is a request
# about it, not an answer; nothing here is a bare "ok", "dale", "confirmo" or "va",
# because in Spanish and English alike those also confirm attendance. Anything else
# re-asks with the buttons.
_TYPED_YES = frozenset(
    (
        # English
        "yes",
        "yeah",
        "yep",
        "yup",
        "yes please",
        "yes thanks",
        "yes im sure",
        "yes confirm",
        "yes go ahead",
        "ok go ahead",
        "ok cancel it",
        "cancel it",
        "cancel it please",
        "please cancel it",
        "yes cancel it",
        "yes cancel it please",
        "yes please cancel it",
        # Spanish (accents already folded: "sí" is "si", "cancélala" is "cancelala")
        "si",
        "si por favor",
        "si porfa",
        "si gracias",
        "si confirmo",
        "si dale",
        "si de acuerdo",
        "si adelante",
        "si cancela",
        "si cancelala",
        "si cancelalo",
        "si cancelala por favor",
        "cancelala",
        "cancelalo",
        "cancelala por favor",
    )
)
_NO = frozenset("no nope nah not don't dont never nevermind nunca tampoco".split())
# Matched on word boundaries in the folded text (accents dropped, apostrophes straight).
_NO_PHRASES = (
    "keep it",
    "keep my booking",
    "mantenla",
    "mantener",
    "please don't cancel",
    "don't cancel",
    "nel",
    "cambie de opinion",
    "changed my mind",
    "mejor no",
    "dejala",
    "no cancel",
    "no canceles",
)
# Searched on the folded text with apostrophes removed, so "dont cancel" matches too.
_NO_PHRASES_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w.replace("'", "")) for w in _NO_PHRASES) + r")\b"
)
# What a no may carry and still ask for nothing (is_bare_no): courtesy words, the
# booking it keeps and the verbs that keep it ("No, mantener mi cita", "no thanks,
# keep my appointment", "no gracias, la mantengo", "no, leave it as is").
_NO_FILLER = frozenset(
    "thanks thank you gracias please por favor porfa ok okay it ill "
    "mi la el my the cita visita reserva booking visit viewing appointment "
    "keep leave as is asi manten mantenga mantenlo mantengo conserva conservala dejala".split()
)
# A cancel verb in either language ("cancel", "cancela", "cancélala", "cancelen",
# "cancelling"): the mark of a request to cancel in a typed yes ("yes, cancel it"
# asks; "yes" alone answers or, with nothing live, means nothing here).
_CANCEL_VERB_RE = re.compile(r"\bcancel[a-z]*")
_THUMBS_DOWN_RE = re.compile("\U0001f44e[\U0001f3fb-\U0001f3ff]?\ufe0f?")
_TAP_RE = re.compile(r"^\s*\[button reply:\s*(.*?)\]\s*$", re.I)
# Two or more dots or commas, with or without spaces between ("...", ". . .", ",,,").
_TRAILING_OFF_RE = re.compile(r"[.,](\s*[.,])+")
# Combining overlay marks: strikethrough and the tilde, bar and ring overlays.
_OVERLAY_RE = re.compile("[\u0334-\u0338\u20d2\u20d3\u20e5\u20e6\u20eb]")
_APOSTROPHES = str.maketrans(dict.fromkeys("\u2019\u2018\u00b4`\u02bc\u2032", "'"))
_PUNCTUATION = str.maketrans(dict.fromkeys(".,!\u00a1", " "))
_SPANISH_DAYS = {
    "monday": "lunes",
    "tuesday": "martes",
    "wednesday": "miércoles",
    "thursday": "jueves",
    "friday": "viernes",
    "saturday": "sábado",
    "sunday": "domingo",
}
_SPANISH_MONTHS = {
    "january": "enero",
    "february": "febrero",
    "march": "marzo",
    "april": "abril",
    "may": "mayo",
    "june": "junio",
    "july": "julio",
    "august": "agosto",
    "september": "septiembre",
    "october": "octubre",
    "november": "noviembre",
    "december": "diciembre",
}
PENDING_MINUTES = 30
# How many asking message ids a pending question remembers (see _pending_applies).
ASKED_BY_LIMIT = 20
# How long after booking the visit reminder fires. The booking records the moment,
# so cancel_visit can tell a typed yes to a question asked before the reminder from
# one asked after it (see _pending_applies).
REMINDER_DELAY_MINUTES = 2
# The reminder handler records when it delivered (the `reminder_sent` state key),
# and the tool records when the turn that asked began (`asked_turn_at`, taken by
# agent.py before it reads anything). A delivery at or after that moment found the
# question open, or was landing while it was being asked, so a typed yes afterwards
# is a reply to the reminder. The handler's Lambda and the runtime keep separate
# clocks; this is an allowance for their skew, not a delivery window: a delivery up
# to this many seconds before the turn began still counts as inside it. The safe
# failure is to decline a typed yes, since the card re-asks on a request.
CLOCK_SKEW_SECONDS = 5
# The fallback when no delivery marker can be read: how long after its scheduled
# moment the reminder is assumed to still arrive. Without the state table the
# handler cannot write the marker, and this window after reminder_at stands in.
# EventBridge fires a one-shot up to about a minute late, and the handler then runs
# and deletes `cancel_pending` while a turn may be writing the record: in a live run
# the reminder came 34 seconds late, inside the turn that asked, so the record the
# tool wrote a few seconds after the delete looked like a question asked after the
# reminder, and the user's "Sí, gracias" to the reminder cancelled the visit.
REMINDER_GRACE_SECONDS = 120


def _fold(text: str) -> str:
    """Apostrophe look-alikes made straight, NFKC, lower-case, accents dropped (á→a, ñ→n).

    The apostrophes go first because NFKC turns "´" into a space and a combining
    accent; NFKC then reads a full-width "ｎｏ" or "＇" as its ASCII form.
    """
    t = unicodedata.normalize("NFKC", str(text or "").translate(_APOSTROPHES)).lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn"
    )


def _words(text: str) -> list[str]:
    """The words of *text* as typed, lower-cased, apostrophes straight, accents kept."""
    t = str(text or "").translate(_APOSTROPHES).lower()
    return re.findall(r"[a-záéíóúñü']+", t)


def _typed_answer(text: str) -> str:
    """The message as _TYPED_YES spells it, or "" when it carries anything else.

    After _fold, apostrophes go, ". , ! ¡" become spaces and runs of spaces collapse.
    Any character left that is not a letter or a space ("?", "¿", an emoji, a digit,
    a newline, a zero-width joiner) means the message is more than a yes, and so does
    a trailing off: "Sí...", "Sí. . .", "Sí,,," or "…", which NFKC spells as three
    dots. A combining overlay mark anywhere ("y̶e̶s̶", struck through) is not a yes
    either; _fold would drop it with the accents, so it is checked on the text as
    typed.
    """
    if _OVERLAY_RE.search(str(text or "")):
        return ""
    t = _fold(text).replace("'", "")
    if _TRAILING_OFF_RE.search(t):
        return ""
    t = t.translate(_PUNCTUATION)
    t = re.sub(r"[ \t]+", " ", t).strip(" ")
    return t if re.fullmatch(r"[a-z ]+", t) else ""


def guess_language(text: str, default: str = "en") -> str:
    """``"es"`` or ``"en"`` when one language's markers outnumber the other's, else *default*.

    One message at a time, so a loanword alone ("Yes, gracias!") does not switch; the
    default is the booking's language, or English for a first booking. The markers are
    counted on the words as typed: nothing is rewritten first.
    """
    words = _words(text)
    es = sum(w in _SPANISH_MARKERS for w in words)
    en = sum(w in _ENGLISH_MARKERS for w in words)
    if es > en:
        return "es"
    if en > es:
        return "en"
    return default


def tapped_title(text: str, message_type: str) -> str | None:
    """The title of the button the user tapped, or None for anything typed.

    Only the webhook sets ``message_type`` to ``button_reply``, for a message WhatsApp
    delivered as a tap, and it never buffers one: a tap arrives alone, with its own
    message id, as ``[Button reply: <title>]``. A buffered burst is reported as one
    ``text`` message carrying the last message's id, so a typed
    ``[Button reply: Yes, cancel]`` is text and stays None here. A tap whose title
    cannot be read returns "", which is neither a yes nor a no.
    """
    if message_type != "button_reply":
        return None
    m = _TAP_RE.match(str(text or ""))
    return unicodedata.normalize("NFC", m.group(1).strip()) if m else ""


def is_affirmative(text: str, message_type: str = "") -> bool:
    """The message is a yes to the cancel card.

    A tap counts when its title is the card's own Yes (`Yes, cancel`, `Sí, cancelar`);
    the booking card's `Yes`/`Sí` is a yes to something else. Typed text counts only
    when the whole message, folded, is one of _TYPED_YES: "yes", "sí, cancélala por
    favor" and "Yes, cancel it." do; "yes 👍", "yeah yeah", "ok", "sí va la cita" and
    "yes?" do not, and re-ask.
    """
    title = tapped_title(text, message_type)
    if title is not None:
        return title in _CANCEL_YES
    return _typed_answer(text) in _TYPED_YES


def is_negative(text: str, message_type: str = "") -> bool:
    """The message answers the live question with a no.

    A tap on the card's own No, a typed reply that opens with a no word ("no", "nope",
    "don´t", at any length), one that says keep it in either language, or a typed 👎
    on its own. A reaction arrives as ``[User reacted 👎 to message …]``, which is
    neither a yes nor a no.
    """
    title = tapped_title(text, message_type)
    if title is not None:
        return title in _CANCEL_NO
    folded = _fold(text)
    words = re.findall(r"[a-z']+", folded)
    if not words:
        return bool(_THUMBS_DOWN_RE.fullmatch(folded.strip()))
    return words[0] in _NO or bool(_NO_PHRASES_RE.search(folded.replace("'", "")))


def is_bare_no(text: str, message_type: str = "") -> bool:
    """A no that asks for nothing: what counts as a no when no question is live.

    The card's No, or a typed no (is_negative) with nothing left once the no words,
    the no phrases and the filler a no may carry (courtesy words, the booking it
    keeps) are taken out: "no", "No thanks, keep my booking", "please keep it", "no no
    no", "No, mantener mi cita", a typed 👎. A no that says anything else is a request
    about the booking, with a cancel verb ("No puedo ir, cancela mi visita", "no,
    cancel it") or without one ("No puedo ir", "Nope, can't make it"), and gets the
    card. Punctuation and emoji are not words: "no?" and "no 🙏" are bare.
    """
    title = tapped_title(text, message_type)
    if title is not None:
        return title in _CANCEL_NO
    if not is_negative(text):
        return False
    rest = _NO_PHRASES_RE.sub(" ", _fold(text).replace("'", ""))
    return all(w in _NO or w in _NO_FILLER for w in re.findall(r"[a-z]+", rest))


def cancel_labels(language: str) -> tuple[str, str]:
    """The cancel card's (yes, no) button titles for *language*."""
    return CANCEL_LABELS["es" if language == "es" else "en"]


def spanish_date(date: str) -> str:
    """The catalog's English date ("Thursday, October 29") in Spanish ("jueves 29 de octubre")."""
    m = re.match(r"\s*([A-Za-z]+),?\s+([A-Za-z]+)\s+(\d{1,2})\s*$", str(date or ""))
    if not m:
        return str(date or "")
    day, month, n = m.group(1).lower(), m.group(2).lower(), m.group(3)
    if day not in _SPANISH_DAYS or month not in _SPANISH_MONTHS:
        return str(date or "")
    return f"{_SPANISH_DAYS[day]} {int(n)} de {_SPANISH_MONTHS[month]}"


def _booking_key(booking: dict) -> list:
    return [
        booking.get("property") or booking.get("car", ""),
        booking.get("date", ""),
        booking.get("time", ""),
    ]


def _asked_by(pending, booking: dict) -> list[str]:
    """The message ids that asked about this same booking, carried over on a re-ask."""
    if (
        isinstance(pending, dict)
        and pending.get("booking") == _booking_key(booking)
        and isinstance(pending.get("asked_by"), list)
    ):
        return [str(i) for i in pending["asked_by"] if isinstance(i, str) and i]
    return []


def _delivered_at(reminder_sent) -> float | None:
    """When the `reminder_sent` marker says a visit reminder was delivered, or None.

    The reminder handler writes the marker the moment the template is out: ``at``,
    the moment, and ``booking``, the key of the booking it was about (see
    _marker_about). A marker without a numeric ``at`` is no marker.
    """
    if not isinstance(reminder_sent, dict):
        return None
    at = reminder_sent.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return float(at)


def _marker_about(reminder_sent: dict, booking: dict) -> bool:
    """The marker is about *booking*: its ``booking`` is the booking's key, or None.

    A reminder scheduled before the date travelled in its payload stores None, which
    is read as the current booking (one reminder per phone; a rebook drops the
    marker).
    """
    marker_booking = reminder_sent.get("booking")
    return marker_booking is None or marker_booking == _booking_key(booking)


def _reminder_closed(
    pending, booking: dict, reminder_sent=None, now: float | None = None
) -> bool:
    """The visit reminder closed this question for typed text.

    The reminder invites "Sí, gracias" or "Yes, thanks", which are on the yes list, so
    a question the reminder found open is over. With a delivery marker
    (_delivered_at) that is any question whose asking turn began at or before the
    delivery, less CLOCK_SKEW_SECONDS: the record carries when the turn began
    (``asked_turn_at``, taken before the turn read anything), so it does not matter
    whether the handler's delete and marker landed before or after the tool read
    state inside that turn, or what either clock said (EventBridge Scheduler has no
    delivery SLA). A record written before the field existed uses ``asked_at``. Such
    a delivery closes the question whichever booking the marker names: a reminder
    for the booking the user has just replaced can land inside the turn asking about
    the new one, and the reply it invites reads the same. A marker from before the
    turn began speaks for the question only when it is about this booking
    (_marker_about): the question then came after the reminder and is live. Another
    booking's earlier marker is no marker. Without one (the state table is off, so
    the handler could not write it), the fallback is any question asked at or before
    ``booking["reminder_at"]`` plus REMINDER_GRACE_SECONDS, once the reminder has
    come due.
    """
    if not isinstance(pending, dict):
        return False
    try:
        asked_at = float(pending.get("asked_at", 0))
    except (TypeError, ValueError):
        return False
    delivered = _delivered_at(reminder_sent)
    if delivered is not None:
        try:
            asked_turn_at = float(pending.get("asked_turn_at", asked_at))
        except (TypeError, ValueError):
            asked_turn_at = asked_at
        if delivered >= asked_turn_at - CLOCK_SKEW_SECONDS:
            return True
        if _marker_about(reminder_sent, booking):
            return False
    try:
        reminder_at = float(booking.get("reminder_at", 0))
    except (TypeError, ValueError):
        return False
    if now is None:
        now = time.time()
    return asked_at <= reminder_at + REMINDER_GRACE_SECONDS and now >= reminder_at


def _pending_applies(
    pending, booking: dict, message_id: str, tapped: bool = False, reminder_sent=None
) -> bool:
    """The recorded question still stands: recent, about this booking, not asked by this message.

    ``asked_by`` holds every message that asked or re-asked the question, so a message
    redelivered after a re-ask ("yes" → asked, "ok wait" → re-asked, the first "yes"
    again) cannot answer a question it asked itself. A typed answer with no message id
    cannot be told from the message that asked, so it never answers: a tool call on a
    message without an id ("cancel it" twice in one turn, or a caller that sends no
    metadata) re-asks instead of cancelling before the card was seen.

    For a typed answer (*tapped* False) the visit reminder must not have closed the
    question (_reminder_closed): the reminder's recorded delivery (*reminder_sent*)
    is at or after the moment the asking turn began, or, without that record, the
    question was asked before ``booking["reminder_at"]`` or within the grace window
    after it, and answered once the reminder was due. A tap on the card's own button
    is not subject to this rule, with or without an id; it answers while the record
    stands, and the reminder handler deletes the record, so after that the tap asks
    afresh.
    """
    if not isinstance(pending, dict):
        return False
    try:
        asked_at = float(pending.get("asked_at", 0))
    except (TypeError, ValueError):
        return False
    now = time.time()
    if now - asked_at > PENDING_MINUTES * 60:
        return False
    if pending.get("booking") != _booking_key(booking):
        return False
    asked_by = pending.get("asked_by")
    if not isinstance(asked_by, list):
        return False
    if message_id and message_id in asked_by:
        return False
    if not tapped:
        if not message_id:
            return False
        if _reminder_closed(pending, booking, reminder_sent, now):
            return False
    return True


@tool
async def load_skill(skill_name: str) -> dict:
    """Load detailed instructions for a specific skill.

    Args:
        skill_name: Name of the skill to load (slot_selection, status_check, cancellation, rich_messages)
    """
    return {"instructions": _load_skill(skill_name)}


@tool(context=True)
async def execute_action(
    action_name: str, params: dict, tool_context: ToolContext
) -> dict:
    """Execute a state-changing action. book_visit only after the user's yes to your buttons;
    cancel_visit asks for its own confirmation, so call it on the request and again on the answer.

    Actions:
        book_visit: params = {property_name, date, time} — the human-readable slot the
            user picked (e.g. date="Friday, July 17", time="2:00 PM"). slot_id is
            accepted as a fallback but is optional; the agent never sees slot ids.
        cancel_visit: params = {} — call it as soon as the user asks to cancel. The
            tool sends its own confirmation buttons ('Yes, cancel' / 'No, keep it')
            and cancels only when the user's current message answers them: a tap on
            'Yes, cancel', or one of a few short typed yeses; call it again only on
            the user's reply to that card, once per message. Anything else re-asks
            or, on a no, returns declined.

    Args:
        action_name: The action to execute
        params: Action parameters — use the listing name and the slot's date + time
            exactly as shown to the user
        tool_context: Injected by Strands — not provided by the LLM
    """
    phone: str = tool_context.invocation_state.get("phone", "")
    if not phone:
        return {"status": "error", "message": "No user context available"}

    if action_name == "book_visit":
        property_name: str = params.get("property_name", "")
        slot_id: str = params.get("slot_id", "")

        listing = resolve_property_by_name(property_name)
        if not listing:
            return {
                "status": "error",
                "message": f"Property '{property_name}' not found",
            }
        # Resolve the slot by the human-readable date+time the agent actually has
        # (slots are shown to the user without internal ids). Fall back to slot_id
        # for backward compatibility if a caller still passes one.
        slot = resolve_slot_by_datetime(
            listing["id"], params.get("date", ""), params.get("time", "")
        )
        if not slot and slot_id:
            slot = find_slot(listing["id"], slot_id)
        if not slot:
            return {
                "status": "error",
                "message": f"No matching slot for {listing['name']} at {params.get('date', '')} {params.get('time', '')}",
            }

        language = guess_language(
            tool_context.invocation_state.get("prompt", ""),
            default=tool_context.invocation_state.get("language", "en"),
        )
        booking = {
            "property_id": listing["id"],
            "property": listing["name"],
            "date": slot["date"],
            "time": slot["time"],
            "location": listing["address"],
            "language": language,
            # When the visit reminder fires, so cancel_visit can tell a typed yes to
            # a question asked before it from one asked after (_pending_applies).
            "reminder_at": int(time.time()) + REMINDER_DELAY_MINUTES * 60,
        }
        await state.put(phone, "booking", booking)
        # A cancellation question about the previous booking must not be answered
        # by a later yes about this one, and the previous reminder's delivery marker
        # is not this reminder's.
        await state.delete(phone, "cancel_pending")
        await state.delete(phone, "reminder_sent")
        digits = "".join(c for c in phone if c.isdigit())
        await scheduler.schedule(
            operation="visit_reminder",
            phone=phone,
            delay_minutes=REMINDER_DELAY_MINUTES,
            # The reminder runs in the scheduler Lambda, which does not ship
            # src/, so everything it needs travels in the payload. The date is
            # there for the handler's delivery marker, keyed as _booking_key is.
            data={
                "property": listing["name"],
                "date": slot["date"],
                "time": slot["time"],
                "location": listing["address"],
                "location_name": listing["name"],
                "lat": listing["lat"],
                "lng": listing["lng"],
                "language": language,
            },
            schedule_id=f"reminder-{digits}",
        )
        return {
            "status": "booked",
            "property": listing["name"],
            "date": slot["date"],
            "time": slot["time"],
        }

    if action_name == "cancel_visit":
        booking = tool_context.invocation_state.get("booking")
        if not booking:
            return {"status": "error", "message": "No active booking found"}
        # The confirmation lives here, not in the prompt, as a two-step state machine:
        # the first call sends the card and records that it asked; a later call
        # cancels only if the question is live and the user's current message answers
        # it with a yes: a tap on the card's own Yes button, or one of a few short
        # typed yeses. "ok cancel" before any question is a request and gets the card,
        # never a cancellation; so does "yes, cancel my visit" after it.
        prompt = tool_context.invocation_state.get("prompt", "")
        message_type = str(tool_context.invocation_state.get("message_type", "") or "")
        message_id = str(tool_context.invocation_state.get("message_id", "") or "")
        tapped = tapped_title(prompt, message_type) is not None
        # When the turn began, taken by agent.py before it read anything, so a
        # reminder delivered inside this turn is one the question was open for
        # whether its handler ran before or after the reads below. Without it (dev
        # mode, an agent.py from before the field) the tool's own read stands in.
        turn_started_at = tool_context.invocation_state.get("turn_started_at")
        if isinstance(turn_started_at, bool) or not isinstance(
            turn_started_at, (int, float)
        ):
            turn_started_at = time.time()
        recorded = await state.get(phone, "cancel_pending")
        # When the reminder handler delivered, and for which booking (None if the
        # state table is off and the handler could not write it).
        reminder_sent = await state.get(phone, "reminder_sent")
        pending = _pending_applies(recorded, booking, message_id, tapped, reminder_sent)
        if pending and is_negative(prompt, message_type):
            await state.delete(phone, "cancel_pending")
            return {
                "status": "declined",
                "message": "The user said no. Keep the booking; say so in one short line.",
            }
        if not pending and is_bare_no(prompt, message_type):
            # A no with no question live (a late tap on an expired card, a bare "no")
            # changes nothing and must not open a new question. A record about this
            # booking that only an id-less reply could not answer is still a no.
            if isinstance(recorded, dict) and recorded.get("booking") == _booking_key(
                booking
            ):
                await state.delete(phone, "cancel_pending")
            return {
                "status": "declined",
                "message": "The user said no and nothing was pending, so nothing "
                "changes. Reply in one short line; do not ask about cancelling.",
            }
        asked_this_booking = isinstance(recorded, dict) and recorded.get(
            "booking"
        ) == _booking_key(booking)
        fresh = (
            isinstance(recorded, dict)
            and isinstance(recorded.get("asked_at"), (int, float))
            and time.time() - recorded["asked_at"] <= PENDING_MINUTES * 60
        )
        # Only a fresh question the reminder closed is answered with a no-op; a stale
        # one gets the card again whatever the reminder did.
        closed_by_reminder = (
            asked_this_booking
            and fresh
            and not tapped
            and _reminder_closed(recorded, booking, reminder_sent)
        )
        if (
            (not asked_this_booking or closed_by_reminder)
            and not tapped
            and _typed_answer(prompt) in _TYPED_YES
            and not _CANCEL_VERB_RE.search(_fold(prompt))
            and await state.get(phone, "booking") is not None
        ):
            # A bare yes with no question live about this booking is not a request to
            # cancel: no question about this booking was asked ("sí" to something the
            # model asked), or the visit reminder closed the one that was ("Yes,
            # thanks!" to the reminder). Opening a card would ask a user who just
            # confirmed attendance whether to cancel, so nothing changes; a record the
            # reminder closed is dropped, since the next typed yes would otherwise
            # re-send the card too. A record this reply cannot answer for another
            # reason (asked by this same message, over 30 minutes old) still gets the
            # card again below; with no booking left in state the ask branch reports
            # that instead.
            if closed_by_reminder:
                await state.delete(phone, "cancel_pending")
            return {
                "status": "declined",
                "message": "Nothing was pending and the message does not ask to cancel, "
                "so nothing changes. Reply in one short line; do not ask about cancelling.",
            }
        if not (pending and is_affirmative(prompt, message_type)):
            # The booking in invocation_state was read when the turn began. A second
            # cancel_visit call in the turn that cancelled (the model re-checking) has
            # nothing left to ask about, and must not send a card for a visit that is
            # gone or record a question nobody can answer.
            if await state.get(phone, "booking") is None:
                return {"status": "error", "message": "No active booking found"}
            language = guess_language(prompt, default=booking.get("language", "en"))
            yes_label, no_label = cancel_labels(language)
            asked_by = _asked_by(recorded, booking)
            hint = (
                f"Toca '{yes_label}' para confirmar."
                if language == "es"
                else f"Tap '{yes_label}' to confirm."
            )
            requested = {
                "status": "confirmation_requested",
                "message": f"'{yes_label}' / '{no_label}' buttons were sent to the user. "
                f"Reply with exactly one short line in the user's language (for example "
                f"'{hint}'), never nothing and never the question again. Call cancel_visit "
                "again when the user answers.",
            }
            if message_id and message_id in asked_by and fresh:
                # This message already asked about this booking: the model calling the
                # tool twice in one turn, or the webhook redelivering the message. One
                # card per message; the record stands as it is. An expired record gets
                # a fresh card instead of pointing the user at a stale one.
                return requested
            name = booking.get("property") or booking.get("car", "")
            if language == "es":
                body = (
                    f"Tienes esta visita agendada:\n{name}\n"
                    f"{spanish_date(booking.get('date', ''))} {booking.get('time', '')}\n"
                    f"{booking.get('location', '')}\n\n¿Cancelo esta visita?"
                )
            else:
                body = (
                    f"Your current viewing:\n{name}\n"
                    f"{booking.get('date', '')} {booking.get('time', '')}\n{booking.get('location', '')}\n\n"
                    "Cancel this viewing?"
                )
            # Built directly: the labels carry commas, which build_rich_message's
            # comma-separated `options` cannot.
            await notify.send_message(
                phone, messages.buttons(body, [yes_label, no_label])
            )
            # The question is recorded with when, about which booking and in reply to
            # which messages, and judged on read (_pending_applies): the TTL below is
            # only housekeeping, since DynamoDB expires items lazily and dev mode not at
            # all. ``asked_turn_at`` is when the asking turn began, which a reminder
            # delivered at or after it closes (_reminder_closed). A re-ask about the
            # same booking keeps the earlier asking ids, so a redelivered earlier
            # message cannot answer the question it asked. A message without an id is
            # not recorded: _pending_applies takes no typed answer from one.
            if message_id:
                asked_by.append(message_id)
            await state.put(
                phone,
                "cancel_pending",
                {
                    "asked_at": int(time.time()),
                    "asked_turn_at": int(turn_started_at),
                    "booking": _booking_key(booking),
                    "asked_by": asked_by[-ASKED_BY_LIMIT:],
                },
                ttl_minutes=PENDING_MINUTES,
            )
            return requested
        await state.delete(phone, "cancel_pending")
        await state.delete(phone, "booking")
        digits = "".join(c for c in phone if c.isdigit())
        # Pass the caller's phone so the cancel stays scoped to their own
        # schedule (object-level authorization).
        await scheduler.cancel(f"reminder-{digits}", phone=phone)
        return {
            "status": "cancelled",
            "property": booking.get("property") or booking.get("car", ""),
        }

    return {"status": "error", "message": f"Unknown action '{action_name}'"}


@tool(context=True)
async def attach_interactive(
    message_type: str,
    tool_context: ToolContext,
    body: str = "",
    options: str = "",
    url: str = "",
    caption: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
    name: str = "",
    address: str = "",
    button_text: str = "",
) -> dict:
    """Send an interactive WhatsApp message (buttons, list, media, location).

    CRITICAL MENTAL MODEL:
    `body` is your FULL reply — the complete conversational message the user
    reads first. Write your greeting, context, reasoning, and the question
    that ends in a choice, all inside `body` (up to 1024 chars).

    After this, you may produce a brief final text output (≤12 words) OR
    leave it empty. Your MAIN MESSAGE lives in `body`, not in the final
    text.

    WRONG:
        body="Pick one:"
        final text: "Here are the listings! Which one interests you?"

    RIGHT:
        body="Here are the homes available to view — which one would you like to see? 🏠"
        final text: ""  (or "Happy to share more details on any of them 👍")

    Args:
        message_type: buttons | list_menu | location | image | video | link_button | location_request
        body: YOUR FULL REPLY — not a label. Write the complete message here.
        options: Comma-separated for buttons; "Section|row~desc;row~desc" for list_menu.
            Use `~` to separate row title and description (never `:`, times contain `:`).
        tool_context: Injected by Strands — not provided by the LLM
    """
    result = _build_rich_message(
        message_type,
        body=body,
        options=options,
        url=url,
        caption=caption,
        latitude=latitude,
        longitude=longitude,
        name=name,
        address=address,
        button_text=button_text,
    )
    if "error" in result:
        raise ValueError(result["error"])
    phone = tool_context.invocation_state.get("phone", "")
    await notify.send_message(phone, result["message"])
    return {"sent": True}


tools = [load_skill, execute_action, attach_interactive]
