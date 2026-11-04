# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The appointment scheduler's cancellation confirmation is owned by the tool.

Two live rounds showed the model answering "Confirm cancellation?" in prose, so the
user had nothing to tap, a typed yes was not accepted, the booking survived and a
rebook made a second one. The buttons now come from ``execute_action`` itself, with
their own titles, and the cancel happens only on an answer to a live question: a tap
on the card's `Yes, cancel`, or a typed message that is exactly one of a short closed
list. Free-text rules were tried first and kept cancelling on replies that were not a
yes; ``tests/fixtures/cancel_answer_replies.json`` holds the replies that showed it.
The tests drive the tool in a subprocess with the blueprint on the path, the way
``tests/test_blueprint_scheduler_tools.py`` does, because ``agent.py`` starts a
server on import.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("strands")

REPO_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = REPO_ROOT / "blueprints" / "appointment-scheduler"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "cancel_answer_replies.json"
PHONE = "+15551234567"
# A reply that names the booking is a request, not an answer, whatever its label.
_BOOKING_NOUN = re.compile(r"\b(cita|visita|reserva|booking|visit|appointment)\b", re.I)
# Labelled Y, but they open with a no word, so the tool reads them as a no and
# declines. Keeping the booking on "no problem" is the safe side of a judgement
# call; a rule would hide the list, so it is spelled out.
_Y_DECLINED = {"no problem", "no hay problema", "No, go ahead", "Not a problem"}

_DRIVER = r"""
import asyncio, json, sys, time
import socialagent_runtime.notify as notify
import socialagent_runtime.scheduler as scheduler
import socialagent_runtime.state as state

sent, deleted, cancelled, store, puts, scheduled = [], [], [], {}, [], []

async def fake_send(phone, msg, **kw):
    assert not kw.get("immediate"), "the confirmation must go through the queue"
    sent.append({"phone": phone, "msg": msg})
async def fake_get(phone, key):
    return store.get((phone, key))
async def fake_put(phone, key, data, ttl_minutes=None):
    store[(phone, key)] = data; puts.append((key, ttl_minutes))
async def fake_delete(phone, key):
    deleted.append((phone, key)); store.pop((phone, key), None)
async def fake_cancel(schedule_id, phone=None):
    cancelled.append(schedule_id)
async def fake_schedule(**kw):
    scheduled.append(kw)

notify.send_message = fake_send
state.get = fake_get
state.put = fake_put
state.delete = fake_delete
scheduler.cancel = fake_cancel
scheduler.schedule = fake_schedule

from src import tools as t
t.resolve_property_by_name = lambda name: {"id": "p-2", "name": "Garden House", "address": "48 Elm Ave, Your City", "lat": 1.0, "lng": 2.0}
t.resolve_slot_by_datetime = lambda pid, d, tm: {"date": "Thursday, October 29", "time": "1:00 PM"}

# The blueprint's visit_reminder handler, loaded as the scheduler Lambda would, so the
# reminder that fires between the question and the answer is the real one.
import importlib.util, os
for k, v in {"AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x", "MEMORY_ID": "m", "PHONE_NUMBER_ID": "p"}.items():
    os.environ.setdefault(k, v)
_spec = importlib.util.spec_from_file_location("_sched_probe", "handlers/scheduler.py")
_sched = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sched)
async def _no_sleep(_s):
    return None
_sched.asyncio.sleep = _no_sleep
reminder_handler = _sched.app._schedule_handlers["visit_reminder"]
reminder_calls = []

class ReminderState:
    # EventContext.state is the stack's table: the same store the tool reads
    async def delete(self, phone, key):
        reminder_calls.append(("delete", key)); store.pop((phone, key), None)
    async def put(self, phone, key, data, ttl_minutes=None):
        reminder_calls.append(("put", key, ttl_minutes)); store[(phone, key)] = data

class ReminderCtx:
    state = ReminderState()
    async def send_template(self, phone, name, params, language="en"):
        reminder_calls.append(("template", name))
    async def send_message(self, phone, msg, memory_text=""):
        reminder_calls.append(("message",))
    async def write_to_memory(self, phone, text):
        reminder_calls.append(("memory",))

class _StillSleeping(Exception):
    pass

async def _stop_at_pin(_s):
    # the handler is in the ten-second pause before the location pin: whatever
    # it has done so far is all the answer turn can see
    raise _StillSleeping()

def run_reminder_handler(payload, delivered_at=None, stop_at_pin=False):
    # the handler stamps the marker with its own clock; *delivered_at* moves it.
    # *stop_at_pin* leaves the handler mid-way, still asleep before the pin
    reminder_calls.clear()
    real_time, real_sleep = _sched.time.time, _sched.asyncio.sleep
    if delivered_at is not None:
        _sched.time.time = lambda: delivered_at
    if stop_at_pin:
        _sched.asyncio.sleep = _stop_at_pin
    try:
        asyncio.run(reminder_handler(payload, ReminderCtx()))
    except _StillSleeping:
        reminder_calls.append(("sleeping",))
    finally:
        _sched.time.time, _sched.asyncio.sleep = real_time, real_sleep

def reminder_payload(booking, last_scheduled):
    # after a book_visit the payload is the one it scheduled, so the marker's key
    # is whatever the payload lets the handler compute; otherwise it is built
    # from the booking as book_visit would have
    if last_scheduled is not None:
        return {"phone": PHONE, **last_scheduled["data"]}
    return {"phone": PHONE, "property": booking["property"], "date": booking["date"], "time": booking["time"],
            "location": booking["location"], "lat": 1.0, "lng": 2.0, "language": booking.get("language", "en")}

class Ctx:
    def __init__(self, s): self.invocation_state = s

PHONE = "+15551234567"
BOOKING = {"property": "Downtown Loft", "date": "Thursday, October 29", "time": "9:00 AM",
           "location": "12 River St, Your City", "language": "en"}

def tap(title):
    # what the webhook sends for a real button tap: the prompt and the type together
    return {"prompt": f"[Button reply: {title}]", "message_type": "button_reply"}

# the turn start the next turn() hands the tool, as agent.py would: set by a
# handler_first step, so the asking turn began before the handler ran
next_turn_start = {"at": None}

def turn(step, booking, message_id):
    if isinstance(step, str):
        step = {"prompt": step}
    s = {"phone": PHONE, "booking": dict(booking), "prompt": step["prompt"],
         "message_type": step.get("message_type", "text"),
         "message_id": step.get("message_id", message_id),
         "language": booking.get("language", "en")}
    if "turn_started_at" in step:
        # the raw value, numeric or not: the tool must cope with either
        s["turn_started_at"] = step["turn_started_at"]
    elif "turn_offset" in step:
        s["turn_started_at"] = time.time() + step["turn_offset"]
    elif next_turn_start["at"] is not None:
        s["turn_started_at"] = next_turn_start["at"]
        next_turn_start["at"] = None
    ctx = Ctx(s)
    return asyncio.run(t.execute_action(action_name="cancel_visit", params={}, tool_context=ctx))

def sequence(steps, booking=BOOKING, same_message=False, age_seconds=0):
    sent.clear(); deleted.clear(); cancelled.clear(); store.clear(); puts.clear(); scheduled.clear()
    next_turn_start["at"] = None
    # the booking is in state too: cancel_visit re-reads it before asking
    store[(PHONE, "booking")] = dict(booking)
    outs = []
    last_scheduled = None
    for n, step in enumerate(steps):
        if step == "<swap>":
            # the stored booking changes under the question without book_visit (a handler wrote it)
            booking = {**booking, "property": "Parkview Apartment"}
            store[(PHONE, "booking")] = dict(booking)
            continue
        if step == "<retime>":
            # same property, another slot: still another booking
            booking = {**booking, "time": "11:00 AM"}
            store[(PHONE, "booking")] = dict(booking)
            continue
        if step == "<old-shape>":
            # a record written before asked_by existed: fresh, but not a live question
            store[(PHONE, "cancel_pending")] = {"asked_at": int(time.time()), "booking": t._booking_key(booking), "message_id": "m-old"}
            continue
        if step == "<rebook>":
            ctx = Ctx({"phone": PHONE, "booking": dict(booking), "prompt": "[Button reply: Yes]", "message_type": "button_reply", "message_id": "m-rebook", "language": "en"})
            outs.append(asyncio.run(t.execute_action(action_name="book_visit", params={"property_name": "Garden House"}, tool_context=ctx)))
            booking = store[(PHONE, "booking")]
            last_scheduled = scheduled[-1]
            continue
        if step == "<reminder-same-second>":
            # the question and the reminder share a second: the reminder wins
            shared_second = int(time.time()) - 30
            store[(PHONE, "cancel_pending")]["asked_at"] = shared_second
            booking = {**booking, "reminder_at": shared_second}
            store[(PHONE, "booking")] = dict(booking)
            continue
        if step in ("<reminder-fired>", "<reminder-handler>"):
            # time passes: the question was asked a minute ago and the visit reminder
            # fired half a minute ago. The booking knows when (reminder_at); with the
            # state table the handler has also run, and deleted the question.
            store[(PHONE, "cancel_pending")]["asked_at"] = int(time.time()) - 60
            booking = {**booking, "reminder_at": int(time.time()) - 30}
            store[(PHONE, "booking")] = dict(booking)
            if step == "<reminder-handler>":
                run_reminder_handler(reminder_payload(booking, last_scheduled))
            continue
        if isinstance(step, dict) and "clock" in step:
            # the question and the reminder at set offsets from now: the live false
            # cancel had the reminder fire late, inside the turn that asked, so the
            # record was written after the handler's delete and after reminder_at
            now = int(time.time())
            store[(PHONE, "cancel_pending")]["asked_at"] = now + step["clock"]["asked"]
            booking = {**booking, "reminder_at": now + step["clock"]["reminder"]}
            store[(PHONE, "booking")] = dict(booking)
            continue
        if isinstance(step, dict) and "late-handler" in step:
            # the reminder arrives well after the moment it was scheduled for: the
            # scheduled moment (reminder) is set, then the real handler runs with its
            # clock at `delivered`, all offsets from now. Two orders inside a turn:
            #   handler_first: the handler runs BEFORE the asking turn's state read,
            #     so the next turn() (the ask) begins at `turn`, finds no record and
            #     writes its own after the marker (the live incident)
            #   inside_turn: the tool read state BEFORE the handler ran, so the record
            #     (asked at `asked`, its turn begun at `turn`) survives the delete
            # Without either the handler runs after the asking turn and deletes
            # the record.
            now = int(time.time())
            c = step["late-handler"]
            booking = {**booking, "reminder_at": now + c["reminder"]}
            store[(PHONE, "booking")] = dict(booking)
            # With `mid_sleep` the handler has sent the template and stopped in the
            # pause before the location pin, where it still is when the reply runs.
            if c.get("handler_first"):
                run_reminder_handler(reminder_payload(booking, last_scheduled), delivered_at=now + c["delivered"], stop_at_pin=c.get("mid_sleep", False))
                next_turn_start["at"] = now + c["turn"]
                continue
            record = {**store[(PHONE, "cancel_pending")], "asked_at": now + c["asked"],
                      "asked_turn_at": now + c.get("turn", c["asked"])}
            store[(PHONE, "cancel_pending")] = record
            run_reminder_handler(reminder_payload(booking, last_scheduled), delivered_at=now + c["delivered"], stop_at_pin=c.get("mid_sleep", False))
            if c.get("inside_turn"):
                store[(PHONE, "cancel_pending")] = record
            continue
        if isinstance(step, dict) and "marker" in step:
            # a delivery marker written by hand: `delivered` is its `at`, `booking`
            # names which booking it is about ("this", "other" or None, the shape a
            # reminder scheduled without a date leaves). The record is re-timed:
            # `asked` is its asked_at and `turn` (default `asked`) when its turn
            # began; `no_turn` leaves the field out, as a record written before it
            now = int(time.time())
            c = step["marker"]
            store[(PHONE, "cancel_pending")]["asked_at"] = now + c["asked"]
            store[(PHONE, "cancel_pending")]["asked_turn_at"] = now + c.get("turn", c["asked"])
            if c.get("no_turn"):
                del store[(PHONE, "cancel_pending")]["asked_turn_at"]
            booking = {**booking, "reminder_at": now + c["reminder"]}
            store[(PHONE, "booking")] = dict(booking)
            which = c.get("booking", "this")
            key = None if which is None else t._booking_key(booking if which == "this" else {**booking, "time": "4:00 PM"})
            at = None if c["delivered"] is None else now + c["delivered"]
            if "raw_at" in c:
                at = c["raw_at"]
            store[(PHONE, "reminder_sent")] = {"at": at, "booking": key}
            continue
        if age_seconds and n == 1 and (PHONE, "cancel_pending") in store:
            store[(PHONE, "cancel_pending")]["asked_at"] -= age_seconds
        outs.append(turn(step, booking, "m-0" if same_message else f"m-{n}"))
    return {"outs": outs, "sent": sent[:], "deleted": deleted[:], "cancelled": cancelled[:],
            "pending_left": (PHONE, "cancel_pending") in store, "puts": puts[:],
            "pending": store.get((PHONE, "cancel_pending")),
            "marker": store.get((PHONE, "reminder_sent")), "now": int(time.time()),
            "booking_left": (PHONE, "booking") in store, "reminder_calls": reminder_calls[:]}

ES = {**BOOKING, "language": "es"}
# bookings whose reminder is still due, or already fired before the question was asked
DUE = {**BOOKING, "reminder_at": int(time.time()) + 120}
FIRED = {**BOOKING, "reminder_at": int(time.time()) - 600}
ASK, ASK_ES = "Cancel my visit", "Cancela mi visita"
cases = {
    "ask_en": sequence([ASK]),
    # the record carries when the asking turn began, from invocation_state as
    # agent.py passes it; without a usable value the tool's own clock stands in
    "ask_with_turn_start": sequence([{"prompt": ASK, "turn_offset": -40}]),
    "ask_turn_start_string": sequence([{"prompt": ASK, "turn_started_at": "1700000000"}]),
    "ask_turn_start_bool": sequence([{"prompt": ASK, "turn_started_at": True}]),
    "ask_turn_start_none": sequence([{"prompt": ASK, "turn_started_at": None}]),
    "ask_es": sequence([ASK_ES]),
    "ask_es_from_booking_lang": sequence(["[Button reply: Garden House]"], ES),
    "ask_ok_cancel": sequence(["ok cancel"]),
    "ask_please_cancel_my_visit": sequence(["please cancel my visit"]),
    "yes_without_question": sequence(["yes"]),
    "tap_yes_cancel_without_question": sequence([tap("Yes, cancel")]),
    "request_opening_with_no": sequence(["No puedo ir, cancela mi visita"], ES),
    # ask then confirm: the card's own Yes, or a typed reply on the closed list
    "tap_yes_cancel": sequence([ASK, tap("Yes, cancel")]),
    "tap_si_cancelar": sequence([ASK_ES, tap("Sí, cancelar")], ES),
    "tap_si_cancelar_decomposed": sequence([ASK_ES, tap("Si\u0301, cancelar")], ES),
    "typed_yes": sequence([ASK, "yes please"]),
    "typed_si": sequence([ASK_ES, "sí, cancélala"], ES),
    "yes_go_ahead": sequence([ASK, "yes go ahead"]),
    "yes_cancel_it": sequence([ASK, "Yes, cancel it."]),
    "yes_exclaimed": sequence([ASK, "Yes!!"]),
    "yes_im_sure_curly": sequence([ASK, "yes I\u2019m sure"]),
    "cancel_it_please": sequence([ASK, "cancel it please"]),
    "si_de_acuerdo": sequence([ASK_ES, "Sí, de acuerdo"], ES),
    "si_unaccented_por_favor": sequence([ASK_ES, "si, cancelala por favor"], ES),
    "si_fullstop": sequence([ASK_ES, "Sí. Cancélala, por favor."], ES),
    # commas and a full stop with words between them are punctuation, not a trailing off
    "si_commas_between_words": sequence([ASK_ES, "Sí, cancélala, por favor."], ES),
    # NFKC: a no-break space and full-width letters read as their plain forms
    "yes_nbsp_please": sequence([ASK, "yes\u00a0please"]),
    "yes_fullwidth": sequence([ASK, "\uff59\uff45\uff53"]),
    # ask then decline
    "typed_no": sequence([ASK, "no"]),
    "tap_no_keep_it": sequence([ASK, tap("No, keep it")]),
    "tap_no_mantener": sequence([ASK_ES, tap("No, mantener")], ES),
    "no_cancel_it": sequence([ASK, "no, cancel it"]),
    "nope": sequence([ASK, "nope"]),
    "changed_my_mind": sequence([ASK, "No, I changed my mind, please keep my booking"]),
    "cambie_de_opinion": sequence([ASK_ES, "Cambié de opinión"], ES),
    "mejor_no": sequence([ASK_ES, "mejor no"], ES),
    "dont_curly": sequence([ASK, "Don\u2019t"]),
    "dont_acute": sequence([ASK, "don\u00b4t cancel"]),
    "dont_backtick": sequence([ASK, "don`t"]),
    "ok_dont_cancel_curly": sequence([ASK, "ok don\u2019t cancel it"]),
    "ok_keep_it": sequence([ASK, "ok, keep it"]),
    "yes_keep_it": sequence([ASK, "yes I want to keep it"]),
    "keep_my_booking": sequence([ASK, "Keep my booking"]),
    "please_dont_cancel": sequence([ASK, "Please don't cancel"]),
    "nel": sequence([ASK_ES, "Nel"], ES),
    "mantenla": sequence([ASK_ES, "Mantenla"], ES),
    "si_dejala": sequence([ASK_ES, "sí, déjala"], ES),
    "thumbs_down": sequence([ASK, "\U0001f44e"]),
    "please_dont_cancel_no_apostrophe": sequence([ASK, "please dont cancel"]),
    "ok_dont_cancel_it_no_apostrophe": sequence([ASK, "ok dont cancel it"]),
    # ask then something that is neither: re-ask, keep the booking
    "ok_wait": sequence([ASK, "ok wait"]),
    "ok_nevermind": sequence([ASK, "ok nevermind"]),
    "sure_minute": sequence([ASK, "sure, give me a minute"]),
    "si_pero_manana": sequence([ASK_ES, "Sí, pero mañana te confirmo"], ES),
    "question_yes": sequence([ASK, "yes?"]),
    "long_text_with_yes": sequence([ASK, "yes I think the loft is nice but show me the garden house instead"]),
    "conditional_si": sequence([ASK_ES, "si puedo mañana te confirmo"], ES),
    "yes_no_problem": sequence([ASK, "yes, no problem"]),
    # a bare agreement word, or one that also confirms attendance, is not a yes
    "ok": sequence([ASK, "ok"]),
    "dale": sequence([ASK_ES, "dale"], ES),
    "confirmo": sequence([ASK_ES, "confirmo"], ES),
    "de_acuerdo": sequence([ASK_ES, "de acuerdo"], ES),
    "yeah_yeah": sequence([ASK, "yeah yeah"]),
    "thumbs_up": sequence([ASK, "\U0001f44d"]),
    # an emoji reaction reaches the agent as reaction_prompt's text, not as the emoji
    "reaction_thumbs_down": sequence(
        [ASK, {"prompt": "[User reacted \U0001f44e to message wamid.r1]", "message_type": "reaction"}]
    ),
    "reaction_thumbs_up": sequence(
        [ASK, {"prompt": "[User reacted \U0001f44d to message wamid.r2]", "message_type": "reaction"}]
    ),
    "check_mark": sequence([ASK, "\u2705"]),
    # a yes next to the booking is a request about it, not an answer
    "pending_please_cancel_my_visit": sequence([ASK, "please cancel my visit"]),
    "pending_cancela_por_favor": sequence([ASK_ES, "cancela mi visita por favor"], ES),
    "si_cancela_la_visita": sequence([ASK_ES, "sí, cancela la visita"], ES),
    "confirmo_mi_cita": sequence([ASK_ES, "Confirmo mi cita"], ES),
    "confirm_my_booking": sequence([ASK, "Confirm my booking"]),
    "si_va_la_cita": sequence([ASK_ES, "Sí va la cita"], ES),
    "va_mi_cita": sequence([ASK_ES, "Va mi cita"], ES),
    "yes_confirm_my_appointment": sequence([ASK, "Yes, confirm my appointment"]),
    # a yes with anything else attached is not a yes
    "yes_thumbs": sequence([ASK, "yes \U0001f44d"]),
    "ok_hand": sequence([ASK, "ok \u270b"]),
    "ok_thinking": sequence([ASK, "ok \U0001f914"]),
    "si_cross": sequence([ASK_ES, "Sí \u274c"], ES),
    "ok_question_emoji": sequence([ASK, "ok \u2753"]),
    "ok_fullwidth_question": sequence([ASK, "ok\uff1f"]),
    "si_trailing_off": sequence([ASK_ES, "Sí..."], ES),
    "si_ellipsis": sequence([ASK_ES, "Sí\u2026"], ES),
    "si_spaced_ellipsis": sequence([ASK_ES, "Sí. . ."], ES),
    "si_commas": sequence([ASK_ES, "Sí,,,"], ES),
    "yes_struck_through": sequence([ASK, "y\u0336e\u0336s\u0336"]),
    "si_tilde_overlay": sequence([ASK_ES, "s\u0334i\u0334"], ES),
    "yes_symbol_strike": sequence([ASK, "y\u20e5e\u20e5s\u20e5"]),
    "si_newline_cancelala": sequence([ASK_ES, "Sí\ncancélala"], ES),
    # a tap on another card, or a typed look-alike of a tap, is not the card's yes
    "tap_booking_yes": sequence([ASK, tap("Yes")]),
    "tap_booking_si": sequence([ASK_ES, tap("Sí")], ES),
    "typed_tap_lookalike": sequence([ASK, "[Button reply: Yes, cancel]"]),
    # the recorded question must be the live one
    "aged_31_min": sequence([ASK, "yes"], age_seconds=31 * 60),
    "aged_29_min": sequence([ASK, "yes"], age_seconds=29 * 60),
    # the re-ask is a fresh question, so the next yes answers it
    "aged_31_min_then_yes_again": sequence([ASK, "yes", "yes"], age_seconds=31 * 60),
    "old_shape_record": sequence(["<old-shape>", "yes"]),
    "asked_by_cap": sequence([ASK] + ["hmm"] * 24),
    "same_message_replay": sequence(["yes cancel it", "yes cancel it"], same_message=True),
    "replay_after_reask": sequence([{"prompt": "yes cancel it", "message_id": "m-0"}, {"prompt": "ok wait", "message_id": "m-1"}, {"prompt": "yes cancel it", "message_id": "m-0"}]),
    "rebook_between": sequence([ASK, "<rebook>", "ok cancel it"]),
    "other_booking_pending": sequence([ASK, "<swap>", "yes cancel it"]),
    "other_slot_pending": sequence([ASK, "<retime>", "yes cancel it"]),
    # the visit reminder fires while the question is open: the replies it invites
    # are on the yes list, so a typed yes no longer answers and, naming no cancel
    # verb, is not a request either; the card's Yes still answers
    "reminder_then_si_gracias": sequence([ASK_ES, "<reminder-fired>", "Sí, gracias"], ES),
    "reminder_same_second_then_yes": sequence([ASK, "<reminder-same-second>", "yes"]),
    "reminder_then_yes_thanks": sequence([ASK, "<reminder-fired>", "Yes, thanks!"]),
    "reminder_then_yes": sequence([ASK, "<reminder-fired>", "Yes"]),
    "reminder_then_yes_cancel_it": sequence([ASK, "<reminder-fired>", "yes, cancel it"]),
    "reminder_then_tap_yes_cancel": sequence([ASK, "<reminder-fired>", tap("Yes, cancel")]),
    "reminder_then_tap_no": sequence([ASK, "<reminder-fired>", tap("No, keep it")]),
    "reminder_then_no": sequence([ASK, "<reminder-fired>", "no"]),
    # the re-ask after the reminder is a fresh question. For two minutes after
    # reminder_at only a tap answers it (the grace window); later a typed yes does too
    "reminder_then_request_then_tap": sequence([ASK, "<reminder-fired>", "yes cancel it", tap("Yes, cancel")]),
    "reminder_late_then_request_then_yes": sequence([ASK, {"clock": {"asked": -153, "reminder": -180}}, "yes cancel it", "yes"]),
    # the reminder came late, inside the turn that asked: the record was written
    # 27 s after reminder_at, after the handler's delete. Three minutes on, a typed
    # "Sí, gracias" to the reminder is not a cancel answer (the grace window), a tap
    # still is, and "yes cancel it" is a fresh request; one second past the window
    # the question is live again
    "reminder_late_then_si_gracias": sequence([ASK_ES, {"clock": {"asked": -153, "reminder": -180}}, "Sí, gracias"], ES),
    "reminder_late_then_tap_yes_cancel": sequence([ASK, {"clock": {"asked": -153, "reminder": -180}}, tap("Yes, cancel")]),
    "reminder_late_then_yes_cancel_it": sequence([ASK, {"clock": {"asked": -153, "reminder": -180}}, "yes cancel it"]),
    "reminder_past_grace_then_yes": sequence([ASK, {"clock": {"asked": -59, "reminder": -180}}, "yes"]),
    "reminder_grace_edge_then_yes": sequence([ASK, {"clock": {"asked": -60, "reminder": -180}}, "yes"]),
    # the reminder is due in a second and the question was asked now: live
    "reminder_still_due_then_yes": sequence([ASK, {"clock": {"asked": 0, "reminder": 1}}, "yes"]),
    # with the state table the real reminder handler has run too, and deleted the
    # record before it sent: even a tap re-asks
    "reminder_deleted_then_tap_yes_cancel": sequence([ASK, "<reminder-handler>", tap("Yes, cancel")]),
    "reminder_deleted_then_yes": sequence([ASK, "<reminder-handler>", "Yes"]),
    # the whole path: book, ask, the real reminder fires, the user confirms attendance
    "booked_reminder_then_si_gracias": sequence(["<rebook>", ASK_ES, "<reminder-handler>", "Sí, gracias"]),
    "booked_reminder_then_yes_thanks": sequence(["<rebook>", ASK, "<reminder-handler>", "Yes, thanks!"]),
    "booked_reminder_then_yes": sequence(["<rebook>", ASK, "<reminder-handler>", "Yes"]),
    # the reminder arrives well after the moment it was scheduled for: booked at T
    # (reminder_at T+120), asked at T+250, the real handler delivers at T+290 and
    # the user answers at T+320. The fixed window after reminder_at has passed, so
    # only the handler's delivery marker tells "Sí, gracias" from a cancel answer;
    # a request then asks a fresh question, which a typed yes or a tap answers
    "late_reminder_then_si_gracias": sequence(["<rebook>", ASK_ES, {"late-handler": {"asked": -70, "reminder": -200, "delivered": -30}}, "Sí, gracias"]),
    "late_reminder_then_yes_thanks": sequence(["<rebook>", ASK, {"late-handler": {"asked": -70, "reminder": -200, "delivered": -30}}, "Yes, thanks!"]),
    "late_reminder_then_request_then_yes": sequence(["<rebook>", ASK, {"late-handler": {"asked": -70, "reminder": -200, "delivered": -30}}, "yes cancel it", "yes"]),
    "late_reminder_then_request_then_tap": sequence(["<rebook>", ASK, {"late-handler": {"asked": -70, "reminder": -200, "delivered": -30}}, "yes cancel it", tap("Yes, cancel")]),
    # the live race with a late delivery, the record outliving the handler's delete.
    # Two orders inside the asking turn, both through the real handler. Tool first:
    # the tool read state, the handler deleted and delivered at D, the tool wrote
    # the record 3 s after D (asked_at later than the marker) in a turn begun 3 s
    # before D. Handler first: the handler deleted and delivered at D before the tool
    # read state, so the tool found no record and wrote a fresh one, in a turn begun
    # 3 s before D. Either way the marker is at or after the turn's start, so a
    # typed "Sí, gracias" to the reminder is no cancel answer and no request; the
    # card's Yes still answers and "yes cancel it" asks afresh
    "late_reminder_inside_turn_then_si_gracias": sequence(["<rebook>", ASK_ES, {"late-handler": {"asked": -27, "turn": -33, "reminder": -64, "delivered": -30, "inside_turn": True}}, "Sí, gracias"]),
    "late_reminder_inside_turn_then_yes": sequence(["<rebook>", ASK, {"late-handler": {"asked": -20, "turn": -33, "reminder": -64, "delivered": -30, "inside_turn": True}}, "yes"]),
    "late_reminder_inside_turn_then_yes_cancel_it": sequence(["<rebook>", ASK, {"late-handler": {"asked": -27, "turn": -33, "reminder": -64, "delivered": -30, "inside_turn": True}}, "yes cancel it"]),
    "late_reminder_inside_turn_then_tap_yes_cancel": sequence(["<rebook>", ASK, {"late-handler": {"asked": -27, "turn": -33, "reminder": -64, "delivered": -30, "inside_turn": True}}, tap("Yes, cancel")]),
    "handler_first_then_si_gracias": sequence(["<rebook>", {"late-handler": {"turn": -33, "reminder": -64, "delivered": -30, "handler_first": True}}, ASK_ES, "Sí, gracias"]),
    "handler_first_then_yes_thanks": sequence(["<rebook>", {"late-handler": {"turn": -33, "reminder": -64, "delivered": -30, "handler_first": True}}, ASK, "Yes, thanks!"]),
    "handler_first_then_yes_cancel_it": sequence(["<rebook>", {"late-handler": {"turn": -33, "reminder": -64, "delivered": -30, "handler_first": True}}, ASK, "yes cancel it"]),
    "handler_first_then_tap_yes_cancel": sequence(["<rebook>", {"late-handler": {"turn": -33, "reminder": -64, "delivered": -30, "handler_first": True}}, ASK, tap("Yes, cancel")]),
    # the same two orders with the reminder five minutes late: the fixed window after
    # reminder_at has long passed, so only the marker closes the question
    "late_reminder_inside_turn_past_window_then_si_gracias": sequence(["<rebook>", ASK_ES, {"late-handler": {"asked": -27, "turn": -33, "reminder": -330, "delivered": -30, "inside_turn": True}}, "Sí, gracias"]),
    "late_reminder_inside_turn_past_window_then_tap": sequence(["<rebook>", ASK, {"late-handler": {"asked": -27, "turn": -33, "reminder": -330, "delivered": -30, "inside_turn": True}}, tap("Yes, cancel")]),
    "handler_first_past_window_then_si_gracias": sequence(["<rebook>", {"late-handler": {"turn": -33, "reminder": -330, "delivered": -30, "handler_first": True}}, ASK_ES, "Sí, gracias"]),
    "handler_first_past_window_then_yes_cancel_it": sequence(["<rebook>", {"late-handler": {"turn": -33, "reminder": -330, "delivered": -30, "handler_first": True}}, ASK, "yes cancel it"]),
    # the reply runs while the handler is still in the pause before the location
    # pin, the reminder more than two minutes late: the marker is already there
    "handler_first_mid_sleep_past_window_then_si_gracias": sequence(["<rebook>", {"late-handler": {"turn": -8, "reminder": -300, "delivered": -7, "handler_first": True, "mid_sleep": True}}, ASK_ES, "Sí, gracias"]),
    "late_reminder_inside_turn_mid_sleep_past_window_then_yes": sequence(["<rebook>", ASK, {"late-handler": {"asked": -4, "turn": -8, "reminder": -300, "delivered": -7, "inside_turn": True, "mid_sleep": True}}, "yes"]),
    # the turn ended a minute before the handler ran: the record survived (written
    # by that turn) and the delivery is after its start, so the question is closed
    "late_reminder_after_turn_then_yes": sequence(["<rebook>", ASK, {"late-handler": {"asked": -58, "turn": -60, "reminder": -200, "delivered": -30, "inside_turn": True}}, "yes"]),
    # the record survived the handler's delete and a marker is written by hand: the
    # marker alone closes the question for typed text, the tool drops the record,
    # a request asks afresh, the card's Yes and a no still answer
    "marker_then_si_gracias": sequence([ASK_ES, {"marker": {"asked": -70, "reminder": -200, "delivered": -30}}, "Sí, gracias"], ES),
    "marker_then_yes": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": -30}}, "yes"]),
    "marker_then_yes_cancel_it": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": -30}}, "yes cancel it"]),
    "marker_then_tap_yes_cancel": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": -30}}, tap("Yes, cancel")]),
    "marker_then_no": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": -30}}, "no"]),
    # the turn began in the same second the reminder was delivered: the reminder wins
    "marker_same_second_then_yes": sequence([ASK, {"marker": {"asked": -30, "reminder": -200, "delivered": -30}}, "yes"]),
    # the question was asked after the delivery: live, whatever the fixed window would
    # say, unless its turn began within the clock allowance of the delivery (5 s,
    # inclusive), which reads as inside the turn
    "marker_asked_after_then_yes": sequence([ASK, {"marker": {"asked": -28, "turn": -30, "reminder": -200, "delivered": -60}}, "yes"]),
    "marker_asked_in_window_after_then_yes": sequence([ASK, {"marker": {"asked": -100, "turn": -100, "reminder": -180, "delivered": -120}}, "yes"]),
    "marker_asked_inside_skew_then_yes": sequence([ASK, {"marker": {"asked": -50, "turn": -58, "reminder": -200, "delivered": -60}}, "yes"]),
    "marker_asked_at_skew_edge_then_yes": sequence([ASK, {"marker": {"asked": -50, "turn": -55, "reminder": -200, "delivered": -60}}, "yes"]),
    "marker_asked_past_skew_then_yes": sequence([ASK, {"marker": {"asked": -50, "turn": -54, "reminder": -200, "delivered": -60}}, "yes"]),
    # a record written before asked_turn_at existed is judged on asked_at
    "marker_old_record_asked_before_then_yes": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": -30, "no_turn": True}}, "yes"]),
    "marker_old_record_asked_after_then_yes": sequence([ASK, {"marker": {"asked": -20, "reminder": -200, "delivered": -30, "no_turn": True}}, "yes"]),
    # a marker about another booking from before the turn began, or without a
    # readable moment, is no marker: the fixed window decides, in either direction
    "marker_other_booking_then_yes": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": -100, "booking": "other"}}, "yes"]),
    "marker_other_booking_in_window_then_yes": sequence([ASK, {"marker": {"asked": -153, "reminder": -180, "delivered": -300, "booking": "other"}}, "yes"]),
    # the previous booking's reminder landing inside, at the start of, or after the
    # turn asking about the new one closes the question whichever booking it names:
    # the reply it invites reads the same, and the new booking's reminder_at is
    # still ahead, so the fixed window has nothing to say
    "marker_other_booking_inside_turn_then_si_gracias": sequence([ASK_ES, {"marker": {"asked": -27, "turn": -33, "reminder": 110, "delivered": -30, "booking": "other"}}, "Sí, gracias"], ES),
    "marker_other_booking_at_turn_start_then_yes": sequence([ASK, {"marker": {"asked": -27, "turn": -30, "reminder": 110, "delivered": -30, "booking": "other"}}, "yes"]),
    "marker_other_booking_after_turn_then_yes": sequence([ASK, {"marker": {"asked": -58, "turn": -60, "reminder": 110, "delivered": -30, "booking": "other"}}, "yes"]),
    "marker_other_booking_inside_turn_then_yes_cancel_it": sequence([ASK, {"marker": {"asked": -27, "turn": -33, "reminder": 110, "delivered": -30, "booking": "other"}}, "yes cancel it"]),
    "marker_unreadable_then_yes": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": None}}, "yes"]),
    "marker_string_at_then_yes": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": -30, "raw_at": "1700000000"}}, "yes"]),
    "marker_bool_at_then_yes": sequence([ASK, {"marker": {"asked": -70, "reminder": -200, "delivered": -30, "raw_at": True}}, "yes"]),
    # a reminder scheduled before the date travelled in its payload leaves a marker
    # with no booking: it is about the current one
    "marker_no_booking_then_si_gracias": sequence([ASK_ES, {"marker": {"asked": -70, "reminder": -200, "delivered": -30, "booking": None}}, "Sí, gracias"], ES),
    # a stale question is re-asked whatever the marker says
    "marker_stale_question_then_yes": sequence([ASK, {"marker": {"asked": -31 * 60, "reminder": -200, "delivered": -30}}, "yes"]),
    # a question asked while the reminder is still due, or after it fired, is live
    "reminder_due_then_yes": sequence([ASK, "yes"], DUE),
    "reminder_long_fired_then_yes": sequence([ASK, "yes"], FIRED),
    "reminder_due_then_tap": sequence([ASK, tap("Yes, cancel")], DUE),
    # a second cancel_visit call in the turn that cancelled finds no booking to ask about
    "second_call_after_cancel": sequence([ASK, "yes", {"prompt": "yes", "message_id": "m-1"}]),
    # a no with no question live is a no-op, never a new card
    "no_without_question": sequence(["no"]),
    "tap_no_without_question": sequence([tap("No, keep it")]),
    "no_without_question_ellipsis": sequence(['no…']),
    "no_without_question_dots": sequence(['no...']),
    "no_without_question_question": sequence(['no?']),
    "no_without_question_thumbs_text": sequence(['No 👎']),
    "no_without_question_thumbs_first": sequence(['👎 no']),
    "no_without_question_pray": sequence(['no 🙏']),
    "no_without_question_gracias": sequence(['no gracias']),
    "no_without_question_thanks": sequence(['no thanks']),
    "no_without_question_twice": sequence(['no no']),
    "no_without_question_typed_label_en": sequence(['No, keep it']),
    "no_without_question_typed_label_es": sequence(['No, mantener']),
    "no_without_question_no_keep_it": sequence(['no keep it']),
    "no_without_question_dont_no_apostrophe": sequence(['please dont cancel']),
    "stale_question_then_no": sequence([ASK, "No"], age_seconds=31 * 60),
    # with nothing live, a no without a cancel verb is a no, however long
    "no_without_question_keep_my_booking": sequence(["No thanks, keep my booking"]),
    "no_without_question_please_keep_it": sequence(["please keep it"]),
    "no_without_question_thrice": sequence(["no no no"]),
    "no_without_question_mantener_mi_cita": sequence(["No, mantener mi cita"], ES),
    "no_without_question_nel_dejala": sequence(["nel, déjala"], ES),
    # a no that carries a cancel verb is a request, and gets the card
    "request_no_cancel_it": sequence(["no, cancel it"]),
    # so is a no that says the user cannot come, with no cancel verb at all
    "request_no_puedo_ir": sequence(["No puedo ir"], ES),
    "request_no_voy_a_poder_ir": sequence(["No voy a poder ir mañana"], ES),
    "request_nope_cant_make_it": sequence(["Nope, can't make it"]),
    "request_no_puedo_asistir": sequence(["no puedo asistir"], ES),
    "no_without_question_nope": sequence(["nope"]),
    "no_without_question_thumbs_only": sequence(["👎"]),
    # a no that keeps the booking in its own words asks for nothing either
    "no_without_question_keep_my_appointment": sequence(["no thanks, keep my appointment"]),
    "no_without_question_keep_the_appointment": sequence(["No, keep the appointment"]),
    "no_without_question_manten_mi_cita": sequence(["No gracias, mantén mi cita"], ES),
    "no_without_question_leave_it": sequence(["no, leave it"]),
    # a stale question is re-asked whatever the reminder did: book_visit always writes
    # reminder_at, so this is the shipped shape of aged_31_min_then_yes_again
    "aged_31_min_reminder_fired_then_yes": sequence([ASK, {"clock": {"asked": -31 * 60, "reminder": -30 * 60}}, "yes"]),
    # the tap guards: the card's own Yes answers only a question under 30 minutes old,
    # about this booking, that this message did not ask
    "tap_aged_31_min": sequence([ASK, tap("Yes, cancel")], age_seconds=31 * 60),
    "tap_other_slot_pending": sequence([ASK, "<retime>", tap("Yes, cancel")]),
    "tap_same_message_twice": sequence([tap("Yes, cancel"), tap("Yes, cancel")], same_message=True),
    # a typed answer on a message without an id never answers; a tap does
    "typed_yes_no_id": sequence([ASK, {"prompt": "yes", "message_id": ""}]),
    "tap_yes_cancel_no_id": sequence([ASK, {**tap("Yes, cancel"), "message_id": ""}]),
    "no_id_cancel_it_twice": sequence([{"prompt": "cancel it", "message_id": ""}] * 2),
    # one card per message: a second call on the same message id sends nothing again
    "ask_twice_same_message": sequence([ASK, ASK], same_message=True),
    "ask_twice_same_message_after_swap": sequence([ASK, "<swap>", {"prompt": ASK, "message_id": "m-0"}]),
    "typed_no_no_id": sequence([ASK, {"prompt": "no", "message_id": ""}]),
    # a bare yes with nothing live is not a cancel request: no card
    "yes_thanks_without_question": sequence(["Yes, thanks!"]),
    "si_gracias_without_question": sequence(["Sí, gracias"], ES),
    "yes_cancel_it_without_question": sequence(["yes cancel it"]),
    "reminder_handler_then_yes_thanks_no_card": sequence([ASK, "<reminder-handler>", "Yes, thanks!"]),
    "reminder_deleted_then_yes_cancel_it": sequence(["<rebook>", ASK, "<reminder-handler>", "yes cancel it"]),
    "ask_twice_then_other_message": sequence(
        [ASK, {"prompt": ASK, "message_id": "m-0"}, {"prompt": ASK, "message_id": "m-1"}]
    ),
}
cases["helpers"] = {
    "lang_es": t.guess_language("Quiero cancelar mi visita"),
    "lang_en": t.guess_language("I want to cancel"),
    "lang_tap_si": t.guess_language("[Button reply: Sí, cancelar]"),
    "lang_tap_yes": t.guess_language("[Button reply: Yes, cancel]"),
    "lang_dale": t.guess_language("dale"),
    "lang_de_acuerdo": t.guess_language("de acuerdo"),
    "lang_no_hay_problema": t.guess_language("no hay problema"),
    "lang_ok_default_es": t.guess_language("ok", default="es"),
    "lang_loanword": t.guess_language("Yes, gracias!"),
    "lang_hola_english": t.guess_language("Hola, I need to cancel my visit", default="es"),
    "lang_homograph": t.guess_language("put it on my agenda please, do me a favor"),
    "labels_es": list(t.cancel_labels("es")),
    "labels_en": list(t.cancel_labels("en")),
    "label_lengths_ok": all(len(l) <= 20 for ls in t.CANCEL_LABELS.values() for l in ls),
    "no_is_not_a_yes": t.is_affirmative("no"),
    "yes_list_has_no_no_word": sorted(t._TYPED_YES & t._NO),
    "yes_list_names_no_booking": [y for y in t._TYPED_YES if any(n in y.split() for n in ("cita", "visita", "reserva", "booking", "visit", "viewing", "appointment", "va"))],
    "date_es": t.spanish_date("Thursday, October 29"),
    "date_passthrough": t.spanish_date("Jue 29"),
}
cases["typed_yes_list"] = sorted(t._TYPED_YES)
cases["reminder_delay_minutes"] = t.REMINDER_DELAY_MINUTES
cases["reminder_grace_seconds"] = t.REMINDER_GRACE_SECONDS
cases["clock_skew_seconds"] = t.CLOCK_SKEW_SECONDS
# what book_visit writes: the reminder moment, REMINDER_DELAY_MINUTES ahead of the clock
# it schedules with, and written afresh by a rebook (an old value is not kept)
_t0 = int(time.time())
sequence(["<rebook>"])
_first = store[(PHONE, "booking")]["reminder_at"]
store[(PHONE, "booking")]["reminder_at"] = 1
_ctx = Ctx({"phone": PHONE, "booking": dict(store[(PHONE, "booking")]), "prompt": "[Button reply: Yes]", "message_type": "button_reply", "message_id": "m-rebook-2", "language": "en"})
asyncio.run(t.execute_action(action_name="book_visit", params={"property_name": "Garden House"}, tool_context=_ctx))
cases["reminder_at"] = {
    "offset_ok": 0 <= _first - _t0 - t.REMINDER_DELAY_MINUTES * 60 <= 2,
    "scheduled_delay": scheduled[-1]["delay_minutes"],
    "scheduled_date": scheduled[-1]["data"]["date"],
    "rebook_refreshes": store[(PHONE, "booking")]["reminder_at"] >= _first,
    "rebook_drops": [list(d) for d in deleted if d[1] in ("cancel_pending", "reminder_sent")],
}
fixture = json.load(open(sys.argv[1], encoding="utf-8"))
cases["fixture"] = [
    {**r, "status": sequence([ASK, r["text"]])["outs"][1]["status"]}
    for r in fixture["replies"]
]
print(json.dumps(cases))
"""

# agent.py is imported with the runtime app and the model stubbed out: app.run() is a
# no-op and the Agent runs the real cancel_visit tool on the invocation_state it is
# given, so invoke() is exercised as the webhook drives it, down to the state writes.
_INVOKE_DRIVER = r"""
import asyncio, concurrent.futures, json, sys, time
import strands, strands.models
import bedrock_agentcore.runtime as rt
import socialagent_runtime
import socialagent_runtime.notify as notify
import socialagent_runtime.state as state
import socialagent_runtime.scheduler as scheduler

sent, deleted, store, seen = [], [], {}, []

async def fake_send(phone, msg, **kw):
    sent.append(phone)
async def fake_get(phone, key):
    return store.get((phone, key))
async def fake_put(phone, key, data, ttl_minutes=None):
    store[(phone, key)] = data
async def fake_delete(phone, key):
    deleted.append(key); store.pop((phone, key), None)
async def fake_cancel(schedule_id, phone=None):
    pass

notify.send_message = fake_send
state.get = fake_get
state.put = fake_put
state.delete = fake_delete
scheduler.cancel = fake_cancel
socialagent_runtime.create_session_manager = lambda payload: None

class FakeApp:
    def entrypoint(self, fn):
        return fn
    def run(self):
        pass

RAISE = {"on": False}

class Ctx:
    def __init__(self, s): self.invocation_state = s

class FakeAgent:
    # Strands' Agent: records what invoke() hands it, then runs the real tool on it
    # (the way the model would, calling cancel_visit on the user's message) or fails
    # like a model call that raised. agent() is synchronous inside the async
    # entrypoint, so the tool's coroutine runs on its own loop in a worker thread.
    def __init__(self, **kw):
        pass
    def __call__(self, prompt, invocation_state=None):
        seen.append({k: invocation_state.get(k) for k in ("phone", "prompt", "message_type", "message_id", "language", "booking", "turn_started_at")})
        if RAISE["on"]:
            raise RuntimeError("model call failed")
        from src import tools as t
        coro = t.execute_action(action_name="cancel_visit", params={}, tool_context=Ctx(invocation_state))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return json.dumps(ex.submit(asyncio.run, coro).result())

rt.BedrockAgentCoreApp = FakeApp
strands.Agent = FakeAgent
strands.models.BedrockModel = lambda **kw: None

import agent  # app.run() is the stub's

PHONE = "+15551234567"
BOOKING = {"property": "Downtown Loft", "date": "Thursday, October 29", "time": "9:00 AM",
           "location": "12 River St, Your City", "language": "en"}

def invoke(prompt, message_type, message_id, meta=True):
    payload = {"phone": PHONE, "prompt": prompt}
    if meta:
        payload["socialagent"] = {"message_type": message_type, "message_id": message_id}
    try:
        res = asyncio.run(agent.invoke(payload))
    except RuntimeError as e:
        return {"raised": str(e)}
    return {"result": json.loads(res["result"]), "outbound": res["outbound_messages"]}

def fresh(pending=None):
    sent.clear(); deleted.clear(); store.clear(); seen.clear()
    store[(PHONE, "booking")] = dict(BOOKING)
    if pending:
        store[(PHONE, "cancel_pending")] = {"asked_at": int(time.time()), "booking": ["Downtown Loft", "Thursday, October 29", "9:00 AM"], "asked_by": list(pending)}

out = {}
# (a) what invoke() hands the tools, from the webhook's payload["socialagent"]
fresh()
before = time.time()
out["ask"] = {**invoke("Cancel my visit", "text", "m-1"), "seen": seen[-1], "pending": store.get((PHONE, "cancel_pending")),
              "clock": [before, time.time()]}
fresh()
out["no_meta"] = {**invoke("Cancel my visit", "", "", meta=False), "seen": seen[-1]}
# (a2) the turn start is agent.py's own reading, taken before it reads state: with
# agent.py's clock replaced by a counter and the tool's left real, the record carries
# the counter's value, and the value invoke() took is lower than the one read at its
# first state.get
import types
reads = []
ticks = iter(range(1000, 2000))
async def recording_get(phone, key):
    reads.append((key, agent.time.time()))
    return store.get((phone, key))
state.get = recording_get
agent.time = types.SimpleNamespace(time=lambda: float(next(ticks)))
fresh()
out["pinned_clock"] = {**invoke("Cancel my visit", "text", "m-1"), "seen": seen[-1], "pending": store.get((PHONE, "cancel_pending")),
                       "reads": reads[:]}
agent.time = time
state.get = fake_get
# (b) the same text as a typed message and as a tap: only the tap cancels
fresh(["m-1"])
out["typed_lookalike"] = {**invoke("[Button reply: Yes, cancel]", "text", "m-2"), "deleted": deleted[:], "sent": len(sent)}
fresh(["m-1"])
out["tap"] = {**invoke("[Button reply: Yes, cancel]", "button_reply", "m-2"), "deleted": deleted[:], "sent": len(sent)}
fresh(["m-1"])
out["typed_yes"] = {**invoke("yes", "text", "m-2"), "deleted": deleted[:]}
# (b2) no metadata at all: the model calls the tool on "cancel it" twice, two turns
# running, before the user has seen a card. Typed text without an id never answers.
fresh()
first = invoke("cancel it", "", "", meta=False)
second = invoke("cancel it", "", "", meta=False)
out["no_id_cancel_it_twice"] = {"statuses": [first["result"]["status"], second["result"]["status"]],
                                "sent": len(sent), "deleted": deleted[:], "booking_left": (PHONE, "booking") in store,
                                "pending": store.get((PHONE, "cancel_pending"))}
# (c) the turn raises: the question this message asked is dropped, any other stays
RAISE["on"] = True
fresh(["m-1", "m-5"])
out["raise_own_question"] = {**invoke("yes", "text", "m-5"), "deleted": deleted[:], "pending_left": (PHONE, "cancel_pending") in store}
fresh(["m-1"])
out["raise_other_question"] = {**invoke("yes", "text", "m-6"), "deleted": deleted[:], "pending_left": (PHONE, "cancel_pending") in store}
fresh(["m-1"])
out["raise_no_id"] = {**invoke("yes", "text", "", meta=False), "deleted": deleted[:], "pending_left": (PHONE, "cancel_pending") in store}
print(json.dumps(out))
"""


def _run_script(tmp_path: Path, script: str, *args: str) -> dict:
    proj = tmp_path / "proj"
    shutil.copytree(BLUEPRINT, proj)
    shutil.copytree(
        REPO_ROOT / "packages" / "socialagent-runtime", proj / "socialagent_runtime"
    )
    (proj / "driver.py").write_text(script)
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(proj),
        "PYTHONIOENCODING": "utf-8",
        "NOTIFICATION_LAMBDA_ARN": "__socialagent_dev__",
        "HOME": str(tmp_path),
    }
    res = subprocess.run(
        [sys.executable, "driver.py", *args],
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert res.returncode == 0, res.stderr[-2000:]
    return json.loads(res.stdout.strip().splitlines()[-1])


def _run_driver(tmp_path: Path) -> dict:
    return _run_script(tmp_path, _DRIVER, str(FIXTURE))


@pytest.fixture(scope="module")
def out(tmp_path_factory) -> dict:
    """One subprocess for the module: every case and every fixture reply."""
    return _run_driver(tmp_path_factory.mktemp("cancel"))


def _buttons(sent_msg: dict) -> list[str]:
    btns = sent_msg["msg"]["payload"]["interactive"]["action"]["buttons"]
    return [b["reply"]["title"] for b in btns]


def _body_text(sent_msg: dict) -> str:
    return sent_msg["msg"]["payload"]["interactive"]["body"]["text"]


def _statuses(c: dict) -> list[str]:
    return [o["status"] for o in c["outs"]]


def _never_cancelled(c: dict, name: str) -> None:
    assert c["cancelled"] == [], name
    assert [PHONE, "booking"] not in c["deleted"], name


def test_the_first_call_asks_with_the_cards_own_buttons_and_cancels_nothing(out):
    for name in (
        "ask_en",
        "ask_es",
        "ask_es_from_booking_lang",
        "ask_ok_cancel",
        "ask_please_cancel_my_visit",
        "tap_yes_cancel_without_question",
        "request_opening_with_no",
        "request_no_cancel_it",
        "request_no_puedo_ir",
        "request_no_voy_a_poder_ir",
        "request_nope_cant_make_it",
        "request_no_puedo_asistir",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested"], (name, c["outs"])
        _never_cancelled(c, name)
        assert len(c["sent"]) == 1 and c["sent"][0]["phone"] == PHONE, name
        assert c["pending_left"], name
        assert c["puts"] == [["cancel_pending", 30]], name
        assert c["pending"]["booking"] == [
            "Downtown Loft",
            "Thursday, October 29",
            "9:00 AM",
        ], name
        assert c["pending"]["asked_by"] == ["m-0"] and c["pending"]["asked_at"] > 0, (
            name
        )
        assert "message_id" not in c["pending"], name
        assert set(c["pending"]) == {
            "asked_at",
            "asked_turn_at",
            "booking",
            "asked_by",
        }, name
        # no turn start was handed in: the tool's own clock stands in
        assert isinstance(c["pending"]["asked_turn_at"], int), name
        assert 0 <= c["pending"]["asked_at"] - c["pending"]["asked_turn_at"] <= 2, name
    # the turn start agent.py hands in is what the record carries, as an int
    c = out["ask_with_turn_start"]
    assert _statuses(c) == ["confirmation_requested"]
    assert isinstance(c["pending"]["asked_turn_at"], int)
    assert 39 <= c["pending"]["asked_at"] - c["pending"]["asked_turn_at"] <= 42
    # a value that is not a number (a string, a bool, None) is no turn start
    for name in ("ask_turn_start_string", "ask_turn_start_bool", "ask_turn_start_none"):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested"], name
        assert 0 <= c["pending"]["asked_at"] - c["pending"]["asked_turn_at"] <= 2, name
    assert _buttons(out["ask_en"]["sent"][0]) == ["Yes, cancel", "No, keep it"]
    assert _buttons(out["ask_es"]["sent"][0]) == ["Sí, cancelar", "No, mantener"]
    assert _buttons(out["ask_es_from_booking_lang"]["sent"][0]) == [
        "Sí, cancelar",
        "No, mantener",
    ]
    assert _buttons(out["request_opening_with_no"]["sent"][0]) == [
        "Sí, cancelar",
        "No, mantener",
    ]
    assert _body_text(out["ask_en"]["sent"][0]) == (
        "Your current viewing:\nDowntown Loft\nThursday, October 29 9:00 AM\n12 River St, Your City\n\nCancel this viewing?"
    )
    assert _body_text(out["ask_es"]["sent"][0]) == (
        "Tienes esta visita agendada:\nDowntown Loft\njueves 29 de octubre 9:00 AM\n12 River St, Your City\n\n¿Cancelo esta visita?"
    )
    assert "Toca 'Sí, cancelar' para confirmar." in out["ask_es"]["outs"][0]["message"]
    assert "Tap 'Yes, cancel' to confirm." in out["ask_en"]["outs"][0]["message"]


def test_a_tap_on_the_cards_yes_or_a_listed_typed_yes_cancels(out):
    for name in (
        "tap_yes_cancel",
        "tap_si_cancelar",
        "typed_yes",
        "typed_si",
        "yes_go_ahead",
        "yes_cancel_it",
        "yes_exclaimed",
        "yes_im_sure_curly",
        "cancel_it_please",
        "si_de_acuerdo",
        "si_unaccented_por_favor",
        "si_fullstop",
        "si_commas_between_words",
        "yes_nbsp_please",
        "yes_fullwidth",
        "tap_si_cancelar_decomposed",
        "aged_29_min",
        "reminder_due_then_yes",
        "reminder_long_fired_then_yes",
        "reminder_due_then_tap",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "cancelled"], (
            name,
            c["outs"],
        )
        assert [PHONE, "booking"] in c["deleted"] and c["cancelled"] == [
            "reminder-15551234567"
        ], name
        assert len(c["sent"]) == 1 and not c["pending_left"], name


def test_a_no_after_the_question_keeps_the_booking(out):
    for name in (
        "typed_no",
        "tap_no_keep_it",
        "tap_no_mantener",
        "no_cancel_it",
        "nope",
        "changed_my_mind",
        "cambie_de_opinion",
        "mejor_no",
        "dont_curly",
        "dont_acute",
        "dont_backtick",
        "ok_dont_cancel_curly",
        "ok_keep_it",
        "yes_keep_it",
        "keep_my_booking",
        "please_dont_cancel",
        "please_dont_cancel_no_apostrophe",
        "ok_dont_cancel_it_no_apostrophe",
        "nel",
        "mantenla",
        "si_dejala",
        "thumbs_down",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "declined"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 1 and not c["pending_left"], name


def test_anything_that_is_neither_re_asks_with_the_buttons(out):
    """A bare "ok", a yes next to the booking, a yes with an emoji or a question
    mark, a tap on the booking card, a typed look-alike of a tap: all re-ask."""
    for name in (
        "ok_wait",
        "ok_nevermind",
        "sure_minute",
        "si_pero_manana",
        "question_yes",
        "long_text_with_yes",
        "conditional_si",
        "yes_no_problem",
        "ok",
        "dale",
        "confirmo",
        "de_acuerdo",
        "yeah_yeah",
        "thumbs_up",
        "reaction_thumbs_down",
        "reaction_thumbs_up",
        "check_mark",
        "pending_please_cancel_my_visit",
        "pending_cancela_por_favor",
        "si_cancela_la_visita",
        "confirmo_mi_cita",
        "confirm_my_booking",
        "si_va_la_cita",
        "va_mi_cita",
        "yes_confirm_my_appointment",
        "yes_thumbs",
        "ok_hand",
        "ok_thinking",
        "si_cross",
        "ok_question_emoji",
        "ok_fullwidth_question",
        "si_trailing_off",
        "si_ellipsis",
        "si_spaced_ellipsis",
        "si_commas",
        "yes_struck_through",
        "yes_symbol_strike",
        "si_tilde_overlay",
        "si_newline_cancelala",
        "tap_booking_yes",
        "tap_booking_si",
        "typed_tap_lookalike",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 2 and c["pending_left"], name
        # the re-ask keeps the first question's id, so a replay of it cannot answer
        assert c["pending"]["asked_by"] == ["m-0", "m-1"], name


def test_the_question_must_be_recent_about_this_booking_and_not_the_asking_message(
    out,
):
    for name in ("aged_31_min", "same_message_replay", "other_booking_pending"):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
    # another booking is a fresh question: the earlier asking ids are not carried over
    assert out["other_booking_pending"]["pending"]["asked_by"] == ["m-2"]
    assert out["other_slot_pending"]["pending"]["asked_by"] == ["m-2"]
    # a re-ask refreshes the question, so the yes after it cancels
    c = out["aged_31_min_then_yes_again"]
    assert _statuses(c) == ["confirmation_requested"] * 2 + ["cancelled"], c["outs"]
    # a record from before asked_by existed is not a live question; it is replaced
    c = out["old_shape_record"]
    assert _statuses(c) == ["confirmation_requested"], c["outs"]
    _never_cancelled(c, "old shape")
    assert c["pending"]["asked_by"] == ["m-1"] and "message_id" not in c["pending"]
    # the ids are capped, keeping the most recent
    c = out["asked_by_cap"]
    assert _statuses(c) == ["confirmation_requested"] * 25, c["outs"]
    assert c["pending"]["asked_by"] == [f"m-{i}" for i in range(5, 25)]
    # the same property at another time is another booking
    c = out["other_slot_pending"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "other slot")
    assert c["pending"]["booking"] == [
        "Downtown Loft",
        "Thursday, October 29",
        "11:00 AM",
    ]
    # asked, re-asked by "ok wait", then the first message redelivered: it asked
    c = out["replay_after_reask"]
    assert _statuses(c) == ["confirmation_requested"] * 3, c["outs"]
    _never_cancelled(c, "replay after re-ask")
    assert c["pending"]["asked_by"] == ["m-0", "m-1"]
    c = out["rebook_between"]
    assert _statuses(c) == [
        "confirmation_requested",
        "booked",
        "confirmation_requested",
    ], c["outs"]
    assert c["cancelled"] == [] and (PHONE, "cancel_pending") in [
        tuple(d) for d in c["deleted"]
    ]
    assert c["pending"]["booking"] == [
        "Garden House",
        "Thursday, October 29",
        "1:00 PM",
    ]
    assert c["pending"]["asked_by"] == ["m-2"]  # another booking: a fresh question


def test_a_no_with_no_live_question_changes_nothing_and_sends_no_card(out):
    for name in [n for n in out if n.startswith("no_without_question")] + [
        "tap_no_without_question"
    ]:
        c = out[name]
        assert _statuses(c) == ["declined"], (name, c["outs"])
        _never_cancelled(c, name)
        assert c["sent"] == [] and c["puts"] == [] and not c["pending_left"], name
        assert "nothing was pending" in c["outs"][0]["message"], name
    c = out["stale_question_then_no"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    _never_cancelled(c, "stale then no")
    assert len(c["sent"]) == 1 and c["puts"] == [["cancel_pending", 30]]


def test_a_typed_yes_after_the_visit_reminder_fired_does_not_answer_a_question_asked_before_it(
    out,
):
    """The reminder invites "Sí, gracias" or "Yes, thanks", both on the yes list. The
    booking records when its reminder fires, so a question asked before that moment
    and answered after it is over for typed text: a bare yes is then not a request
    either, so the tool changes nothing, drops the closed question and sends no card;
    a yes that names the cancel verb, or a tap after the handler's delete, re-asks."""
    for name in (
        "reminder_then_si_gracias",
        "reminder_same_second_then_yes",
        "reminder_then_yes_thanks",
        "reminder_then_yes",
        "reminder_deleted_then_yes",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "declined"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 1 and c["booking_left"], name
        assert not c["pending_left"], name
        assert "does not ask to cancel" in c["outs"][1]["message"], name
    # the record survived the reminder: the bare yes deleted it itself
    assert [PHONE, "cancel_pending"] in out["reminder_then_yes"]["deleted"]
    for name in ("reminder_then_yes_cancel_it", "reminder_deleted_then_tap_yes_cancel"):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 2 and c["pending_left"] and c["booking_left"], name
    # the record survived the reminder: the re-ask carries the first asking id over
    assert out["reminder_then_yes_cancel_it"]["pending"]["asked_by"] == ["m-0", "m-2"]
    # the handler deleted it: the re-ask is a new question
    assert out["reminder_deleted_then_tap_yes_cancel"]["pending"]["asked_by"] == ["m-2"]
    assert out["reminder_delay_minutes"] == 2


def test_a_reminder_that_fired_late_closes_a_question_asked_within_the_grace_window(
    out,
):
    """The live false cancel: the EventBridge one-shot fired 34 s late, inside the
    turn that asked, so the handler's delete ran before the tool wrote the record,
    which then carried an asked_at 27 s after reminder_at. Three minutes on, the
    user's "Sí, gracias" to the reminder was a typed yes to a live question. With no
    delivery marker (the state table off, or a handler that wrote none) a typed
    answer to a question asked within REMINDER_GRACE_SECONDS of reminder_at is not
    an answer: a bare yes changes nothing and drops the record, a request asks a
    fresh question, a tap on the card's Yes still cancels. One second past the
    window, and before the reminder is due, the question is live."""
    c = out["reminder_late_then_si_gracias"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    _never_cancelled(c, "late reminder then sí gracias")
    assert len(c["sent"]) == 1 and c["booking_left"] and not c["pending_left"]
    assert c["puts"] == [["cancel_pending", 30]]
    c = out["reminder_grace_edge_then_yes"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    _never_cancelled(c, "yes at the edge of the window")
    c = out["reminder_late_then_tap_yes_cancel"]
    assert _statuses(c) == ["confirmation_requested", "cancelled"], c["outs"]
    assert c["cancelled"] == ["reminder-15551234567"]
    c = out["reminder_late_then_yes_cancel_it"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "late reminder then yes cancel it")
    assert len(c["sent"]) == 2 and c["pending"]["asked_by"] == ["m-0", "m-2"]
    for name in ("reminder_past_grace_then_yes", "reminder_still_due_then_yes"):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "cancelled"], (
            name,
            c["outs"],
        )
    assert out["reminder_grace_seconds"] == 120


def test_the_real_reminder_handler_closes_the_question_before_it_sends(out):
    """Book, ask, the blueprint's visit_reminder handler fires, the user confirms
    attendance with one of the replies the reminder invites: the handler deleted the
    question before the template went out, and recorded its delivery once the
    template was out, so the reply is not a cancel answer and, naming no cancel verb,
    not a request either: nothing cancels, no card goes out."""
    for name, template in (
        ("booked_reminder_then_si_gracias", "visit_reminder"),
        ("booked_reminder_then_yes_thanks", "visit_reminder"),
        ("booked_reminder_then_yes", "visit_reminder"),
        ("reminder_deleted_then_yes", "visit_reminder"),
    ):
        c = out[name]
        assert c["reminder_calls"] == [
            ["delete", "cancel_pending"],
            ["template", template],
            ["put", "reminder_sent", 180],
            ["message"],
            ["memory"],
        ], (name, c["reminder_calls"])
        assert _statuses(c)[-2:] == ["confirmation_requested", "declined"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert c["booking_left"], name
        assert not c["pending_left"], name
        assert c["marker"]["at"] > 0 and c["marker"]["booking"] is not None, name
    # the booking book_visit wrote carries when its reminder fires, and the payload
    # it scheduled carries what the handler's marker needs: the key the tool computes
    c = out["booked_reminder_then_yes"]
    assert _statuses(c) == ["booked", "confirmation_requested", "declined"]
    assert c["puts"][0] == ["booking", None]
    assert c["marker"]["booking"] == ["Garden House", "Thursday, October 29", "1:00 PM"]
    # the marker the handler wrote from the fixed booking is keyed the same way
    assert out["reminder_deleted_then_yes"]["marker"]["booking"] == [
        "Downtown Loft",
        "Thursday, October 29",
        "9:00 AM",
    ]
    # book_visit writes reminder_at REMINDER_DELAY_MINUTES ahead, the delay it
    # schedules with, and a rebook writes it afresh; the payload carries the date
    # for the handler's marker, and each booking drops the question and the marker
    assert out["reminder_at"] == {
        "offset_ok": True,
        "scheduled_delay": 2,
        "scheduled_date": "Thursday, October 29",
        "rebook_refreshes": True,
        "rebook_drops": [[PHONE, "cancel_pending"], [PHONE, "reminder_sent"]] * 2,
    }
    # after the handler's delete a request ("yes cancel it") asks a fresh question
    c = out["reminder_deleted_then_yes_cancel_it"]
    assert _statuses(c)[-2:] == ["confirmation_requested", "confirmation_requested"]
    assert c["pending"]["asked_by"] == ["m-3"]


def test_a_tap_or_a_no_after_the_reminder_still_answers(out):
    # a tap on the card's own Yes is an answer to the card whenever it comes
    c = out["reminder_then_tap_yes_cancel"]
    assert _statuses(c) == ["confirmation_requested", "cancelled"], c["outs"]
    # so is a no, tapped or typed; the typed one finds nothing pending and asks nothing
    c = out["reminder_then_tap_no"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    assert not c["pending_left"]
    c = out["reminder_then_no"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    assert len(c["sent"]) == 1 and c["puts"] == [["cancel_pending", 30]]
    # the re-ask after the reminder is a fresh question: within the grace window a
    # tap answers it, and once the window has passed a typed yes does too
    c = out["reminder_then_request_then_tap"]
    assert _statuses(c) == ["confirmation_requested"] * 2 + ["cancelled"], c["outs"]
    c = out["reminder_late_then_request_then_yes"]
    assert _statuses(c) == ["confirmation_requested"] * 2 + ["cancelled"], c["outs"]


def test_a_reminder_delivered_late_closes_the_question_it_found_open_whatever_the_clock(
    out,
):
    """EventBridge Scheduler has no delivery SLA, so the fixed window after
    reminder_at is a guess. Booked at T (reminder due T+120), asked at T+250, the
    real handler delivers at T+290 and records it, the user writes at T+320: the
    window has passed, and the marker alone says the question existed when the
    reminder arrived. "Sí, gracias" is then no cancel answer and, naming no cancel
    verb, no request: nothing cancels, no card, the record is gone, the booking
    stays. A request asks a fresh question, which the next typed yes or a tap on
    the card answers at once, without waiting for any window."""
    for name in ("late_reminder_then_si_gracias", "late_reminder_then_yes_thanks"):
        c = out[name]
        assert _statuses(c) == ["booked", "confirmation_requested", "declined"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 1 and c["booking_left"] and not c["pending_left"], name
        assert "does not ask to cancel" in c["outs"][2]["message"], name
        assert c["reminder_calls"][0] == ["delete", "cancel_pending"], name
        assert c["reminder_calls"][2] == ["put", "reminder_sent", 180], name
        # the marker the handler wrote from book_visit's payload is keyed as the
        # tool keys the booking, and stamped with the handler's clock
        assert c["marker"]["booking"] == [
            "Garden House",
            "Thursday, October 29",
            "1:00 PM",
        ], name
        assert c["marker"]["at"] > 0, name
    for name in (
        "late_reminder_then_request_then_yes",
        "late_reminder_then_request_then_tap",
    ):
        c = out[name]
        assert _statuses(c) == [
            "booked",
            "confirmation_requested",
            "confirmation_requested",
            "cancelled",
        ], (name, c["outs"])
        assert len(c["sent"]) == 2 and c["cancelled"] == ["reminder-15551234567"], name


def test_a_reminder_delivered_inside_the_asking_turn_closes_the_question_in_either_order(
    out,
):
    """The live incident, through the real handler. The reminder fires late, inside
    the turn that is asking. Tool first: the tool read state, then the handler
    deleted the record and delivered, then the tool wrote the record, so it outlived
    the delete with an asked_at later than the marker. Handler first: the handler
    deleted and delivered before the tool read state, so the tool found nothing and
    wrote a fresh record after the marker. In both the turn began before the
    delivery, and the record says so (asked_turn_at), so a typed "Sí, gracias" or
    "Yes, thanks!" to the reminder is no cancel answer and no request: nothing
    cancels, no card, the record is dropped, the booking stays. "yes cancel it" asks
    afresh and a tap on the card's Yes still answers. The same holds when the
    reminder is five minutes late, where the fixed window would call the question
    live, and when the turn ended a minute before the handler ran."""
    for name in (
        "late_reminder_inside_turn_then_si_gracias",
        "late_reminder_inside_turn_then_yes",
        "late_reminder_inside_turn_past_window_then_si_gracias",
        "handler_first_then_si_gracias",
        "handler_first_then_yes_thanks",
        "handler_first_past_window_then_si_gracias",
        "late_reminder_after_turn_then_yes",
    ):
        c = out[name]
        assert _statuses(c) == ["booked", "confirmation_requested", "declined"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 1 and c["booking_left"] and not c["pending_left"], name
        assert [PHONE, "cancel_pending"] in c["deleted"], name
        assert "does not ask to cancel" in c["outs"][2]["message"], name
        assert c["reminder_calls"][0] == ["delete", "cancel_pending"], name
        assert c["reminder_calls"][2] == ["put", "reminder_sent", 180], name
    # the handler stopped in the pause before the pin: the marker is the third
    # thing it did, after the delete and the template, so the reply meets it
    for name in (
        "handler_first_mid_sleep_past_window_then_si_gracias",
        "late_reminder_inside_turn_mid_sleep_past_window_then_yes",
    ):
        c = out[name]
        assert c["reminder_calls"] == [
            ["delete", "cancel_pending"],
            ["template", "visit_reminder"],
            ["put", "reminder_sent", 180],
            ["sleeping"],
        ], (name, c["reminder_calls"])
        assert _statuses(c) == ["booked", "confirmation_requested", "declined"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 1 and c["booking_left"] and not c["pending_left"], name
    # handler first: the handler found no record to delete, and the record the tool
    # then wrote began its turn 3 s before the delivery, after the marker was written
    for name in (
        "handler_first_then_si_gracias",
        "handler_first_past_window_then_si_gracias",
    ):
        c = out[name]
        assert c["puts"] == [["booking", None], ["cancel_pending", 30]], name
        assert c["marker"]["at"] > 0, name
    # a request asks afresh; the surviving record's asking id is carried over
    for name, ids in (
        ("late_reminder_inside_turn_then_yes_cancel_it", ["m-1", "m-3"]),
        ("handler_first_then_yes_cancel_it", ["m-2", "m-3"]),
        ("handler_first_past_window_then_yes_cancel_it", ["m-2", "m-3"]),
    ):
        c = out[name]
        assert _statuses(c) == ["booked"] + ["confirmation_requested"] * 2, (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 2 and c["pending"]["asked_by"] == ids, name
    for name in (
        "late_reminder_inside_turn_then_tap_yes_cancel",
        "late_reminder_inside_turn_past_window_then_tap",
        "handler_first_then_tap_yes_cancel",
    ):
        c = out[name]
        assert _statuses(c) == ["booked", "confirmation_requested", "cancelled"], (
            name,
            c["outs"],
        )
        assert c["cancelled"] == ["reminder-15551234567"], name


def test_the_delivery_marker_decides_for_typed_text_and_the_window_is_the_fallback(
    out,
):
    """The record survived the handler's delete and a marker about this booking is
    read: a typed yes answers only a question whose turn began after the delivery,
    with CLOCK_SKEW_SECONDS of allowance for the two clocks (a turn begun up to 5 s
    after the delivery, inclusive, still reads as inside it); the tool drops a
    closed record and sends no card, a request asks afresh, a tap and a no still
    answer. A record without asked_turn_at is judged on asked_at. A marker about
    another booking, or without a readable moment, is no marker, and the fixed
    window after reminder_at decides as it does without the state table. A marker
    with no booking (a reminder scheduled before the date travelled with it) is
    about the current one. A stale question is re-asked whatever the marker says."""
    for name in (
        "marker_then_si_gracias",
        "marker_then_yes",
        "marker_same_second_then_yes",
        "marker_asked_inside_skew_then_yes",
        "marker_asked_at_skew_edge_then_yes",
        "marker_old_record_asked_before_then_yes",
        "marker_no_booking_then_si_gracias",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "declined"], (
            name,
            c["outs"],
        )
        _never_cancelled(c, name)
        assert len(c["sent"]) == 1 and c["booking_left"] and not c["pending_left"], name
        assert [PHONE, "cancel_pending"] in c["deleted"], name
        assert "does not ask to cancel" in c["outs"][1]["message"], name
    c = out["marker_then_yes_cancel_it"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "marker then yes cancel it")
    assert len(c["sent"]) == 2 and c["pending"]["asked_by"] == ["m-0", "m-2"]
    c = out["marker_then_tap_yes_cancel"]
    assert _statuses(c) == ["confirmation_requested", "cancelled"], c["outs"]
    assert c["cancelled"] == ["reminder-15551234567"]
    c = out["marker_then_no"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    assert len(c["sent"]) == 1 and not c["pending_left"]
    # the turn began after the delivery, past the allowance: live, inside or outside
    # the fixed window; so is a question no marker speaks for
    for name in (
        "marker_asked_after_then_yes",
        "marker_asked_in_window_after_then_yes",
        "marker_asked_past_skew_then_yes",
        "marker_old_record_asked_after_then_yes",
        "marker_other_booking_then_yes",
        "marker_unreadable_then_yes",
        "marker_string_at_then_yes",
        "marker_bool_at_then_yes",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "cancelled"], (
            name,
            c["outs"],
        )
        assert c["cancelled"] == ["reminder-15551234567"], name
    # another booking's marker ignored, the window closes the question
    c = out["marker_other_booking_in_window_then_yes"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    _never_cancelled(c, "another booking's marker, inside the window")
    # another booking's reminder delivered at or after the turn began closes the
    # question itself: its reminder_at is ahead, so nothing else would
    for name in (
        "marker_other_booking_inside_turn_then_si_gracias",
        "marker_other_booking_at_turn_start_then_yes",
        "marker_other_booking_after_turn_then_yes",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested", "declined"], (name, c["outs"])
        _never_cancelled(c, name)
        assert len(c["sent"]) == 1 and c["booking_left"] and not c["pending_left"], name
    c = out["marker_other_booking_inside_turn_then_yes_cancel_it"]
    assert _statuses(c) == ["confirmation_requested"] * 2, c["outs"]
    _never_cancelled(c, "another booking's reminder inside the turn, then a request")
    assert len(c["sent"]) == 2 and c["pending"]["asked_by"] == ["m-0", "m-2"]
    c = out["marker_stale_question_then_yes"]
    assert _statuses(c) == ["confirmation_requested"] * 2, c["outs"]
    _never_cancelled(c, "stale question with a marker")
    assert len(c["sent"]) == 2 and c["pending_left"]
    assert out["clock_skew_seconds"] == 5


def test_the_cards_yes_answers_only_a_live_question_about_this_booking(out):
    """The tap guards: a tap on `Yes, cancel` cancels nothing when the question is over
    30 minutes old, is about another booking, or was asked by this same message; the
    card goes out again instead (once per message)."""
    c = out["tap_aged_31_min"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "tap on a 31-minute-old question")
    assert len(c["sent"]) == 2 and c["pending_left"] and c["booking_left"]
    c = out["tap_other_slot_pending"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "tap after the slot changed")
    assert len(c["sent"]) == 2 and c["booking_left"]
    assert c["pending"]["booking"] == [
        "Downtown Loft",
        "Thursday, October 29",
        "11:00 AM",
    ]
    assert c["pending"]["asked_by"] == ["m-2"]
    c = out["tap_same_message_twice"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "the same tap twice with no question")
    assert len(c["sent"]) == 1 and c["puts"] == [["cancel_pending", 30]]
    assert c["pending"]["asked_by"] == ["m-0"] and c["booking_left"]


def test_a_no_that_says_the_user_cannot_come_is_a_request_and_a_bare_no_is_not(out):
    """With nothing live, a no is bare only when nothing is left after the no words,
    the no phrases and their filler are taken out; "I can't go" phrasings say more,
    and get the card."""
    for name in (
        "request_no_puedo_ir",
        "request_no_voy_a_poder_ir",
        "request_nope_cant_make_it",
        "request_no_puedo_asistir",
        "request_opening_with_no",
    ):
        c = out[name]
        assert _statuses(c) == ["confirmation_requested"], (name, c["outs"])
        assert len(c["sent"]) == 1 and c["pending_left"], name
    for name in (
        "no_without_question",
        "no_without_question_nope",
        "no_without_question_typed_label_en",
        "no_without_question_gracias",
        "no_without_question_thanks",
        "no_without_question_please_keep_it",
        "no_without_question_keep_my_booking",
        "no_without_question_thrice",
        "no_without_question_mantener_mi_cita",
        "no_without_question_nel_dejala",
        "no_without_question_thumbs_only",
    ):
        c = out[name]
        assert _statuses(c) == ["declined"], (name, c["outs"])
        assert c["sent"] == [] and c["puts"] == [], name


def test_a_second_cancel_visit_call_in_the_turn_that_cancelled_asks_nothing(out):
    """The model may call the tool again on the same message after a `cancelled`. The
    booking read at the start of the turn is gone from state: no card, no question."""
    c = out["second_call_after_cancel"]
    assert _statuses(c) == ["confirmation_requested", "cancelled", "error"], c["outs"]
    assert c["outs"][2]["message"] == "No active booking found"
    assert len(c["sent"]) == 1 and c["puts"] == [["cancel_pending", 30]]
    assert not c["pending_left"] and not c["booking_left"]
    assert c["cancelled"] == ["reminder-15551234567"]


def test_a_typed_answer_without_a_message_id_never_answers_but_a_tap_does(out):
    """A message with no id cannot be told from the one that asked, so the model
    calling the tool twice on "cancel it" in one turn, or a caller that sends no
    metadata, gets a card, never a cancellation. The empty id is not recorded."""
    c = out["typed_yes_no_id"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "typed yes, no id")
    assert len(c["sent"]) == 2 and c["pending"]["asked_by"] == ["m-0"]
    c = out["tap_yes_cancel_no_id"]
    assert _statuses(c) == ["confirmation_requested", "cancelled"], c["outs"]
    assert c["cancelled"] == ["reminder-15551234567"]
    c = out["no_id_cancel_it_twice"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "cancel it twice, no id")
    assert len(c["sent"]) == 2 and c["booking_left"]
    assert c["pending"]["asked_by"] == [] and "" not in c["pending"]["asked_by"]


def test_one_message_gets_the_card_once_however_often_the_tool_is_called(out):
    """A second call on the same message id (the model re-checking in the turn, or
    the webhook redelivering) returns `confirmation_requested` and sends nothing,
    writes nothing; a later message asks again, once."""
    c = out["ask_twice_same_message"]
    assert _statuses(c) == ["confirmation_requested", "confirmation_requested"], c[
        "outs"
    ]
    _never_cancelled(c, "ask twice, same message")
    assert len(c["sent"]) == 1 and c["puts"] == [["cancel_pending", 30]]
    assert c["pending"]["asked_by"] == ["m-0"]
    assert "Tap 'Yes, cancel' to confirm." in c["outs"][1]["message"]
    c = out["ask_twice_then_other_message"]
    assert _statuses(c) == ["confirmation_requested"] * 3, c["outs"]
    assert len(c["sent"]) == 2 and c["puts"] == [["cancel_pending", 30]] * 2
    assert c["pending"]["asked_by"] == ["m-0", "m-1"]
    # the same id on a different booking is a new question: the card goes out again
    c = out["ask_twice_same_message_after_swap"]
    _never_cancelled(c, "ask twice, same message, booking swapped")
    assert len(c["sent"]) == 2 and len(c["puts"]) == 2


def test_a_no_that_keeps_the_booking_in_its_own_words_asks_for_nothing(out):
    """Added in review (fails on the branch as submitted): "keep" and "mantén" are not
    in the no phrases or the filler, so these keep-it replies get the cancel card."""
    for name in (
        "no_without_question_keep_my_appointment",
        "no_without_question_keep_the_appointment",
        "no_without_question_manten_mi_cita",
        "no_without_question_leave_it",
    ):
        c = out[name]
        assert _statuses(c) == ["declined"], (name, c["outs"])
        assert c["sent"] == [] and c["puts"] == [], name


def test_a_stale_question_is_re_asked_whatever_the_reminder_did(out):
    """Added in review (fails on the branch as submitted): with reminder_at written by
    book_visit, a question asked a minute before the reminder and answered with a bare
    yes 31 minutes later is a no-op that drops the record, while the same reply to a
    question the reminder did not close (aged_31_min_then_yes_again) gets the card."""
    c = out["aged_31_min_reminder_fired_then_yes"]
    assert _statuses(c) == ["confirmation_requested"] * 2, c["outs"]
    assert len(c["sent"]) == 2 and c["pending_left"]


def test_a_bare_yes_with_nothing_live_opens_no_card(out):
    """ "Yes, thanks!" to the visit reminder, or a yes to something else the model
    said, is not a request to cancel when no question about this booking is live: the
    tool changes nothing, writes nothing and sends no card. A yes that names the
    cancel verb is a request and asks. After the reminder handler's delete the bare
    yes finds no record at all, so there is nothing to drop either."""
    for name in (
        "yes_without_question",
        "yes_thanks_without_question",
        "si_gracias_without_question",
    ):
        c = out[name]
        assert _statuses(c) == ["declined"], (name, c["outs"])
        assert c["sent"] == [] and c["puts"] == [] and c["deleted"] == [], name
        assert "does not ask to cancel" in c["outs"][0]["message"], name
    c = out["yes_cancel_it_without_question"]
    assert _statuses(c) == ["confirmation_requested"], c["outs"]
    assert len(c["sent"]) == 1
    c = out["reminder_handler_then_yes_thanks_no_card"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    _never_cancelled(c, "reminder then yes thanks")
    assert len(c["sent"]) == 1 and c["booking_left"] and not c["pending_left"]
    assert [PHONE, "cancel_pending"] not in c[
        "deleted"
    ]  # the handler's, not the tool's


def test_a_typed_no_without_a_message_id_still_closes_the_question(out):
    c = out["typed_no_no_id"]
    assert _statuses(c) == ["confirmation_requested", "declined"], c["outs"]
    _never_cancelled(c, "typed no, no id")
    assert len(c["sent"]) == 1 and not c["pending_left"]


def test_the_closed_list_of_typed_yeses(out):
    """The list is the contract: a change to it changes what cancels, so it is pinned
    here in full, folded as _typed_answer spells it (no accents, no punctuation)."""
    assert out["typed_yes_list"] == [
        "cancel it",
        "cancel it please",
        "cancelala",
        "cancelala por favor",
        "cancelalo",
        "ok cancel it",
        "ok go ahead",
        "please cancel it",
        "si",
        "si adelante",
        "si cancela",
        "si cancelala",
        "si cancelala por favor",
        "si cancelalo",
        "si confirmo",
        "si dale",
        "si de acuerdo",
        "si gracias",
        "si por favor",
        "si porfa",
        "yeah",
        "yep",
        "yes",
        "yes cancel it",
        "yes cancel it please",
        "yes confirm",
        "yes go ahead",
        "yes im sure",
        "yes please",
        "yes please cancel it",
        "yes thanks",
        "yup",
    ]


def test_language_and_label_helpers(out):
    assert out["helpers"] == {
        "lang_es": "es",
        "lang_en": "en",
        "lang_tap_si": "es",
        "lang_tap_yes": "en",
        "lang_dale": "es",
        "lang_de_acuerdo": "es",
        "lang_no_hay_problema": "es",
        "lang_ok_default_es": "es",
        "lang_loanword": "en",
        "lang_hola_english": "en",
        "lang_homograph": "en",
        "labels_es": ["Sí, cancelar", "No, mantener"],
        "labels_en": ["Yes, cancel", "No, keep it"],
        "label_lengths_ok": True,
        "no_is_not_a_yes": False,
        "yes_list_has_no_no_word": [],
        "yes_list_names_no_booking": [],
        "date_es": "jueves 29 de octubre",
        "date_passthrough": "Jue 29",
    }


def _fixture_replies() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["replies"]


def _effective_label(reply: dict) -> str:
    """A Y that names the booking without being a listed yes is a request: A."""
    return (
        "A"
        if reply["label"] == "Y" and _BOOKING_NOUN.search(reply["text"])
        else reply["label"]
    )


@pytest.mark.parametrize(
    "reply", _fixture_replies(), ids=lambda r: f"{r['label']}:{r['text']!r}"
)
def test_every_labelled_reply_to_the_open_question(out, reply):
    """Typed text, sent while the question is open. N and A never cancel; Y cancels or
    re-asks, except the few that open with a no word, which decline."""
    result = next(r for r in out["fixture"] if r["text"] == reply["text"])
    label = _effective_label(reply)
    if label in ("N", "A"):
        assert result["status"] != "cancelled", reply
    elif reply["text"] in _Y_DECLINED:
        assert result["status"] == "declined", reply
    else:
        assert result["status"] in ("cancelled", "confirmation_requested"), reply


def test_fixture_counts(out):
    """How the Y replies land, so a change to the closed list is visible here."""
    counts = json.loads(FIXTURE.read_text(encoding="utf-8"))["counts"]
    results = out["fixture"]
    assert {k: sum(r["label"] == k for r in results) for k in counts} == counts
    ys = [r for r in results if _effective_label(r) == "Y"]
    by_status = {
        s: sum(r["status"] == s for r in ys)
        for s in ("cancelled", "confirmation_requested", "declined")
    }
    assert sum(r["status"] == "cancelled" for r in results if r["label"] != "Y") == 0
    assert by_status["declined"] == len(_Y_DECLINED)
    assert by_status == {"cancelled": 24, "confirmation_requested": 100, "declined": 4}
    assert sum(by_status.values()) == len(ys)


def test_the_cancellation_skill_delegates_the_confirmation_to_the_tool():
    skill = (BLUEPRINT / "src" / "skills" / "cancellation.md").read_text()
    assert 'execute_action("cancel_visit", {})' in skill
    assert "confirmation_requested" in skill
    assert (
        "attach_interactive"
        not in skill.split("Never send the confirmation yourself")[0]
    )
    assert "declined" in skill and "exactly one short line" in skill
    assert "Tap 'Yes, cancel' to confirm." in skill
    assert "Toca 'Sí, cancelar' para confirmar." in skill
    # while the question is open nothing else is asked, and only the reply to the
    # card reaches the tool: a yes to some other question must not cancel
    assert "ask the user nothing else" in skill
    assert "only on the user's reply to the card" in skill
    assert "A reply to any other question is not a cancel answer" in skill
    assert "whatever they" not in skill
    # a reaction never carries the emoji, so the no is a typed one
    assert "a typed 👎" in skill


def test_the_prompt_keeps_a_bare_ok_from_booking_and_forwards_only_card_replies():
    """agent.py starts the server on import, so the prompt is read with ast; the
    source's `\\` line breaks fall inside phrases."""
    src = (BLUEPRINT / "agent.py").read_text()
    tree = ast.parse(src)
    prompt = next(
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_SYSTEM_PROMPT_TEMPLATE"
            for t in node.targets
        )
    )
    rule = prompt[
        prompt.index("2. book_visit only after") : prompt.index(
            "3. NEVER mention internal IDs"
        )
    ]
    assert "tap on that card's `Yes`/`Sí`" in rule
    assert (
        'A bare "ok", "dale" or "confirmo"' in rule and "send the buttons again" in rule
    )
    assert '"ok", "confirmo")' not in rule  # neither is an example of a yes to book
    assert "ask the user nothing else" in rule
    assert "only on the user's reply to that card" in rule
    assert "A reply to any other question is not a cancel answer" in rule
    assert "whatever they" not in src
    # the slot_selection skill says the same: it used to let a bare "ok" or
    # "confirmo" book, against the prompt
    skill = (BLUEPRINT / "src" / "skills" / "slot_selection.md").read_text()
    assert "counts as the confirmation exactly like the tap" not in skill
    assert '"ok", "confirmo")' not in skill
    assert "tap on that card's `Yes`/`Sí`" in skill
    assert "plainly says yes to booking that slot" in skill
    assert (
        'A bare "ok", "dale" or "confirmo"' in skill
        and "send the buttons again" in skill
    )
    assert "a typed yes)" not in skill  # rule 5 defers to rule 6's reading of a yes


def test_neither_the_prompt_nor_the_docstring_asks_the_model_to_confirm_a_cancellation():
    src = (BLUEPRINT / "agent.py").read_text()
    rule = src[
        src.index("2. book_visit only after") : src.index(
            "3. NEVER mention internal IDs"
        )
    ]
    assert (
        "cancel_visit is the" in rule
        and "never send your own Yes/No for a cancellation" in rule
        and "`Yes, cancel`" in rule
    )
    # A blanket rule would make the model ask before cancel_visit, which asks itself.
    assert "NEVER call execute_action without prior user confirmation" not in src
    assert "without prior" not in src and "prior confirmation" not in src
    tools_src = (BLUEPRINT / "src" / "tools" / "__init__.py").read_text()
    assert "Only call after explicit user confirmation" not in tools_src
    readme = (BLUEPRINT / "README.md").read_text()
    assert "`cancel_visit` sends the booking details with the buttons" in readme
    assert "`Yes, cancel` / `No, keep it`" in readme


def test_agent_passes_the_message_and_its_type_to_tools_and_drops_an_undelivered_question():
    src = (BLUEPRINT / "agent.py").read_text()
    start = src.index("invocation_state = {")
    block = src[start : src.index("\n    }\n", start)]
    assert '"prompt": payload["prompt"]' in block
    assert '"message_type": meta.get("message_type", "")' in block
    assert '"message_id": meta.get("message_id", "")' in block
    assert '"turn_started_at": turn_started_at' in block
    assert 'meta = payload.get("socialagent") or {}' in src
    assert '"language": (booking or {}).get("language", "en")' in block
    # the turn start is the entrypoint's first statement, before any state read
    invoke = src[src.index("async def invoke") :]
    body = invoke[invoke.index('"""Handle an inbound WhatsApp message."""') :]
    statements = [
        line.strip()
        for line in body.split("\n")[1:]
        if line.strip() and not line.strip().startswith("#")
    ]
    assert statements[0] == "turn_started_at = time.time()"
    assert invoke.index("turn_started_at = time.time()") < invoke.index(
        "await state.get"
    )
    # the turn's exception path forgets a question this message asked
    assert "except Exception:" in invoke
    assert (
        'await _drop_unasked_question(phone, invocation_state["message_id"])' in invoke
    )
    assert invoke.index("except Exception:") < invoke.index(
        "_drop_unasked_question(phone"
    )
    # the error itself is still raised, so the webhook sends its error reply
    after_drop = invoke[invoke.index("_drop_unasked_question(phone") :]
    assert after_drop.split("\n")[1].strip() == "raise"


def test_drop_unasked_question_forgets_only_a_question_this_message_asked():
    """agent.py calls app.run() at import, so only the helper is loaded."""
    src = (BLUEPRINT / "agent.py").read_text()
    start = src.index("async def _drop_unasked_question")
    end = src.index("\n@app.entrypoint", start)

    class FakeState:
        def __init__(self, pending):
            self.pending, self.deleted = pending, []

        async def get(self, phone, key):
            return self.pending

        async def delete(self, phone, key):
            self.deleted.append(key)

    def run(pending, message_id):
        ns = {
            "state": FakeState(pending),
            "logger": __import__("logging").getLogger("t"),
        }
        exec(src[start:end], ns)  # noqa: S102
        asyncio.run(ns["_drop_unasked_question"](PHONE, message_id))
        return ns["state"].deleted

    live = {"asked_at": 1, "booking": ["x", "d", "t"], "asked_by": ["m-0", "m-1"]}
    assert run(live, "m-1") == ["cancel_pending"]
    assert run(live, "m-0") == ["cancel_pending"]
    assert run(live, "m-9") == []  # an earlier message's question stays
    assert run(live, "") == []
    assert run(None, "m-1") == []
    assert run({"asked_by": "m-1"}, "m-1") == []  # malformed shape: left alone


@pytest.fixture(scope="module")
def invoked(tmp_path_factory) -> dict:
    """invoke() run end to end, with the runtime app and the model stubbed."""
    return _run_script(tmp_path_factory.mktemp("invoke"), _INVOKE_DRIVER)


def test_invoke_hands_the_tools_the_message_its_type_and_its_id(invoked):
    c = invoked["ask"]
    turn_started_at = c["seen"].pop("turn_started_at")
    assert c["seen"] == {
        "phone": PHONE,
        "prompt": "Cancel my visit",
        "message_type": "text",
        "message_id": "m-1",
        "language": "en",
        "booking": {
            "property": "Downtown Loft",
            "date": "Thursday, October 29",
            "time": "9:00 AM",
            "location": "12 River St, Your City",
            "language": "en",
        },
    }
    # when the turn began, a number from invoke()'s own clock, carried into the record
    assert isinstance(turn_started_at, float) and not isinstance(turn_started_at, bool)
    assert c["clock"][0] <= turn_started_at <= c["clock"][1]
    assert c["pending"]["asked_turn_at"] == int(turn_started_at)
    # the real tool asked and recorded the question under this message's id
    assert c["result"]["status"] == "confirmation_requested"
    assert c["pending"]["asked_by"] == ["m-1"]
    # a payload without the webhook's metadata still runs: the fields are empty
    c = invoked["no_meta"]
    assert (c["seen"]["message_type"], c["seen"]["message_id"]) == ("", "")
    assert c["result"]["status"] == "confirmation_requested"


def test_invoke_takes_the_turn_start_from_its_own_clock_before_it_reads_state(invoked):
    """With agent.py's clock replaced by a counter (the tool's is the real one), the
    record carries the counter's reading, so the value comes from invoke() and not
    from the tool's fallback, and it is lower than the reading taken at invoke()'s
    first state.get: the turn start is the first thing the entrypoint does."""
    c = invoked["pinned_clock"]
    assert c["seen"]["turn_started_at"] == 1000.0
    assert c["pending"]["asked_turn_at"] == 1000
    assert c["reads"][0][0] == "booking"
    assert c["seen"]["turn_started_at"] < c["reads"][0][1]
    assert c["result"]["status"] == "confirmation_requested"


def test_invoke_lets_the_tool_tell_a_tap_from_the_same_text_typed(invoked):
    """The consent is the webhook's `button_reply`, which only a real tap carries.
    The same characters typed are text, and re-ask."""
    c = invoked["typed_lookalike"]
    assert c["result"]["status"] == "confirmation_requested", c
    assert "booking" not in c["deleted"] and c["sent"] == 1
    c = invoked["tap"]
    assert c["result"]["status"] == "cancelled", c
    assert c["deleted"] == ["cancel_pending", "booking"] and c["sent"] == 0
    c = invoked["typed_yes"]
    assert c["result"]["status"] == "cancelled", c


def test_invoke_without_message_metadata_asks_and_never_cancels_on_typed_text(invoked):
    """A payload without the webhook's metadata has no message id: the real tool, run
    on "cancel it" twice, sends a card each time and cancels nothing."""
    c = invoked["no_id_cancel_it_twice"]
    assert c["statuses"] == ["confirmation_requested", "confirmation_requested"], c
    assert c["sent"] == 2 and "booking" not in c["deleted"] and c["booking_left"]
    assert c["pending"]["asked_by"] == []


def test_invoke_drops_the_question_this_message_asked_when_the_turn_raises(invoked):
    """The queued card is lost with the turn, so a question this message asked is
    forgotten; one asked by an earlier message stays. The error still propagates."""
    c = invoked["raise_own_question"]
    assert c["raised"] == "model call failed"
    assert c["deleted"] == ["cancel_pending"] and not c["pending_left"]
    c = invoked["raise_other_question"]
    assert c["raised"] == "model call failed"
    assert c["deleted"] == [] and c["pending_left"]
    c = invoked["raise_no_id"]
    assert c["raised"] == "model call failed"
    assert c["deleted"] == [] and c["pending_left"]


def _event_handler(monkeypatch):
    """The blueprint's visit_cancelled handler, loaded as the Lambda would."""
    for k, v in {
        "NOTIFICATION_LAMBDA_ARN": "__socialagent_dev__",
        "AGENT_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        "MEMORY_ID": "m",
        "PHONE_NUMBER_ID": "p",
    }.items():
        monkeypatch.setenv(k, v)
    handlers_dir = BLUEPRINT / "handlers"
    spec = importlib.util.spec_from_file_location(
        "_event_probe", handlers_dir / "event.py"
    )
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(handlers_dir))
    spec.loader.exec_module(mod)
    return mod.app._event_handlers["visit_cancelled"]


def test_a_business_side_cancellation_clears_the_pending_question(monkeypatch):
    """handlers/event.py deletes the booking and the question asked about it."""
    handler = _event_handler(monkeypatch)
    calls: list = []

    class FakeState:
        async def get(self, phone, key):
            calls.append(("get", key))
            return {"property": "Garden House"} if key == "booking" else None

        async def delete(self, phone, key):
            calls.append(("delete", key))

    class FakeCtx:
        state = FakeState()

        async def cancel_schedule(self, schedule_id):
            calls.append(("cancel_schedule", schedule_id))

        async def write_to_memory(self, phone, text):
            calls.append(("memory", "Garden House" in text))

        async def send_template(self, phone, name, params, language="en"):
            calls.append(("template", name, list(params)))

    asyncio.run(
        handler(
            {"phone": "+15555550100", "property": "Garden House", "reason": "flooding"},
            FakeCtx(),
        )
    )
    assert calls == [
        ("get", "booking"),
        ("delete", "booking"),
        ("delete", "cancel_pending"),
        ("delete", "reminder_sent"),
        ("cancel_schedule", "reminder-15555550100"),
        ("memory", True),
        ("template", "visit_cancelled_by_business", ["Garden House", "flooding"]),
    ]
