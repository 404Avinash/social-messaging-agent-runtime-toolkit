# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Conversations routes: get/delete, list messages."""
from __future__ import annotations

import json

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.exceptions import BotoCoreError, ClientError

from shared.auth import require_group
from shared.aws import REGION
from shared.http import error_response as _error, failure_response, json_response as _json, request_id
from shared.registry import get_registry as _get_registry
from shared.resolver import app, from_legacy, query_params

tracer = Tracer(service="admin-console-backend")
logger = Logger(service="admin-console-backend", child=True)


def _log_memory_failure(event: str, exc: Exception) -> str:
    """Log *exc*, a failed AgentCore Memory call, by its error type and code,
    and return the code, or the error's type when it has none.

    Call from an ``except`` block. The actor and session ids carry the user's
    phone number, and an AgentCore or botocore error message can quote them,
    so a botocore error is a warning without its message or traceback, and
    the reason returned is never the message. Anything else, a fault in this
    code, keeps its traceback.
    """
    # stacklevel=3, one more than Powertools' default: the record's location
    # then names the function that handled the failure, not this helper.
    if not isinstance(exc, (ClientError, BotoCoreError)):
        logger.exception(event, stacklevel=3)
        return type(exc).__name__
    extra = {"error_type": type(exc).__name__}
    code = None
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        if code:
            extra["error_code"] = code
    logger.warning(event, extra=extra, stacklevel=3)
    return code or type(exc).__name__


def handle_delete_all_conversations(params, _body):
    """Delete ALL memory events for ALL actors in a stack."""
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    memory_id = config.get("outputs", {}).get("memory_id", "")
    if not memory_id:
        return _json(200, {"deleted": 0})
    region = config.get("region", REGION)
    try:
        agentcore = boto3.client("bedrock-agentcore", region_name=region)
        actors = agentcore.list_actors(memoryId=memory_id, maxResults=50).get("actorSummaries", [])
        total_deleted = 0
        for actor in actors:
            aid = actor["actorId"]
            sessions = agentcore.list_sessions(memoryId=memory_id, actorId=aid, maxResults=50)
            for s in sessions.get("sessionSummaries", []):
                sid = s["sessionId"]
                while True:
                    events = agentcore.list_events(memoryId=memory_id, actorId=aid, sessionId=sid, maxResults=100)
                    batch = events.get("events", [])
                    if not batch:
                        break
                    for ev in batch:
                        try:
                            agentcore.delete_event(memoryId=memory_id, actorId=aid, sessionId=sid, eventId=ev["eventId"])
                            total_deleted += 1
                        except Exception:
                            logger.exception("handler_error")
        return _json(200, {"deleted": total_deleted, "actors": len(actors)})
    except Exception:
        return _error(500, "Delete failed")


def handle_delete_conversation(params, _body):
    """Delete all events for a specific conversation (tries all session/actor formats).

    Answers 502 with the counts when an event could not be deleted or listed,
    since the conversation is then still there, at least in part. Nothing left
    to delete is a 200 with ``deleted: 0``.
    """
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    memory_id = config.get("outputs", {}).get("memory_id", "")
    phone = params["phone_hash"]
    if not memory_id:
        return _json(200, {"deleted": 0, "phone": phone})
    region = config.get("region", REGION)
    digits = "".join(c for c in phone if c.isdigit())
    try:
        agentcore = boto3.client("bedrock-agentcore", region_name=region)
        deleted = 0
        failed = 0
        listed_any = False
        unlisted = False
        # Try all known actor/session format combinations: the digits SMART
        # writes first, then the number as given when it differs. Not a set:
        # a set of strings iterates in hash order, which changes with every
        # process, so each cold start logged the failures in another order.
        for actor_id in dict.fromkeys((digits, phone)):
            try:
                # Paginate sessions
                sessions_kwargs: dict = {"memoryId": memory_id, "actorId": actor_id, "maxResults": 100}
                session_ids = []
                while True:
                    sessions_resp = agentcore.list_sessions(**sessions_kwargs)
                    for s in sessions_resp.get("sessionSummaries", []):
                        session_ids.append(s["sessionId"])
                    next_tok = sessions_resp.get("nextToken")
                    if not next_tok:
                        break
                    sessions_kwargs["nextToken"] = next_tok
            except Exception as e:
                # Only one form has to list: "+52..." is not even a valid actor id.
                _log_memory_failure("list_sessions_failed", e)
                continue
            listed_any = True

            for sid in session_ids:
                events_kwargs: dict = {"memoryId": memory_id, "actorId": actor_id, "sessionId": sid, "maxResults": 100}
                try:
                    while True:
                        events = agentcore.list_events(**events_kwargs)
                        batch = events.get("events", [])
                        if not batch:
                            break
                        for ev in batch:
                            try:
                                agentcore.delete_event(memoryId=memory_id, actorId=actor_id, sessionId=sid, eventId=ev["eventId"])
                                deleted += 1
                            except Exception as e:
                                # Keep deleting the rest; the count reports it.
                                failed += 1
                                _log_memory_failure("delete_event_failed", e)
                        next_tok = events.get("nextToken")
                        if not next_tok:
                            break
                        events_kwargs["nextToken"] = next_tok
                except Exception as e:
                    unlisted = True
                    _log_memory_failure("list_events_failed", e)
        if failed or unlisted or not listed_any:
            summary = f"Deleted {deleted} events"
            if failed:
                summary += f"; {failed} could not be deleted"
            if unlisted or not listed_any:
                summary += "; some could not be listed"
            rid = request_id()
            body = {"error": f"{summary} (request id {rid})" if rid else summary,
                    "deleted": deleted, "failed": failed, "phone": phone}
            if rid:
                body["requestId"] = rid
            return _json(502, body)
        return _json(200, {"deleted": deleted, "phone": phone})
    except Exception:
        return _error(500, "Delete failed")


def _structured_message(text: str, role: str) -> dict | None:
    """The message an event's JSON text holds, or None when the text is plain.

    The Strands session manager and the SDK's memory writer store
    ``{"message": {"role": ..., "content": ...}}`` as the text. An agent on
    ``create_memory_client`` stores what was said, which can parse as JSON
    too: a menu choice of "5" is a number, not a message.

    The text counts as a message only when it names the event's own ``role``
    (lower case), as both writers' text does. A user can type JSON that names
    the assistant, and a tool's output can be such JSON too; the event's role
    is set by whoever wrote the event, so those read as plain text.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    msg = parsed.get("message", parsed) if isinstance(parsed, dict) else None
    if not isinstance(msg, dict) or not isinstance(msg.get("role"), str):
        return None
    return msg if msg["role"].lower() == role else None


def _empty_token_summary() -> dict:
    """The token totals of a conversation with no model invocation found."""
    return {
        "billed_input_tokens": 0,
        "context_input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "invocations": 0,
        # Back-compat aliases (older clients read input_tokens/output_tokens).
        "input_tokens": 0,
    }


# The pages of 100 events the messages route reads at most, newest first.
_MAX_EVENT_PAGES = 5
# Why a transcript was read short when no call failed: it has more events
# than _MAX_EVENT_PAGES pages hold.
UNREAD_LIMIT = "limit"


def handle_get_conversation_messages(params, _body):
    """Get messages for a specific conversation from AgentCore Memory.

    Answers ``{messages, token_summary}``, newest first. A read that stopped
    short adds ``incomplete``: the error code of the ListEvents call that
    failed after at least one page was read, or ``UNREAD_LIMIT`` when the
    conversation has more events than the route reads. The messages read
    before it are worth showing, so that is a 200, as a log read that
    stopped short is. A failure on the first page answers 502, since nothing
    was read: it used to answer 200 with no messages, so an access denial or
    a throttled call showed as an empty conversation. A stack without memory,
    and a conversation with no events, answer 200 with no messages and no
    ``incomplete``.
    """
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    memory_id = config.get("outputs", {}).get("memory_id", "")
    if not memory_id:
        # The same shape as a conversation with no events, not a bare list,
        # so a caller never has to tell the two answers apart.
        return _json(200, {"messages": [], "token_summary": _empty_token_summary()})
    phone_hash = params["phone_hash"]
    phone = phone_hash  # Frontend sends fullPhone, not the masked hash
    try:
        agentcore = boto3.client("bedrock-agentcore", region_name=config.get("region", REGION))
        session_id = f"whatsapp-session-conversation-{phone}"
        messages = []
        next_token = None
        pages = 0
        incomplete = None
        while True:
            kwargs = dict(memoryId=memory_id, sessionId=session_id, actorId=phone, maxResults=100)
            if next_token:
                kwargs["nextToken"] = next_token
            try:
                resp = agentcore.list_events(**kwargs)
            except Exception as e:
                incomplete = _log_memory_failure("list_events_failed", e)
                break
            for event in resp.get("events", []):
                timestamp = str(event.get("eventTimestamp", ""))
                # ListEvents returns the newest event first, but an event holds
                # its messages (a log_turn turn, a reply and its reasoning) oldest
                # first. Reversed, they keep the list newest first throughout, the
                # order the modal reverses into a transcript.
                entries: list[dict] = []
                for payload_item in event.get("payload", []):
                    conv = payload_item.get("conversational", {})
                    content_obj = conv.get("content", {})
                    text = content_obj.get("text", "")
                    if text:
                        # The event names its speaker. A TOOL event is a call or
                        # result the user never saw.
                        role = str(conv.get("role") or "USER").lower()
                        msg = _structured_message(text, role)
                        if msg is None:
                            if role in ("user", "assistant"):
                                entries.append({"role": role, "content": text, "timestamp": timestamp})
                        else:
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                # Handle content blocks (text, reasoningContent, toolUse, toolResult)
                                parts = []
                                reasoning_parts = []
                                tool_calls = []
                                for c in content:
                                    if not isinstance(c, dict):
                                        continue
                                    if c.get("reasoningContent") or c.get("reasoning_content"):
                                        # Real extended-thinking block emitted by the model
                                        # (Bedrock Converse reasoningContent.reasoningText.text).
                                        # Redacted thinking carries no readable text.
                                        rc = c.get("reasoningContent") or c.get("reasoning_content")
                                        rtext = ""
                                        if isinstance(rc, dict):
                                            rt = rc.get("reasoningText") or rc.get("reasoning_text") or {}
                                            if isinstance(rt, dict):
                                                rtext = rt.get("text", "")
                                            elif isinstance(rt, str):
                                                rtext = rt
                                            if not rtext and (rc.get("redactedContent") or rc.get("redacted_content")):
                                                rtext = "[razonamiento redactado]"
                                        if rtext:
                                            reasoning_parts.append(rtext)
                                    elif c.get("text"):
                                        parts.append(c["text"])
                                    elif c.get("toolUse") or c.get("tool_use"):
                                        tu = c.get("toolUse") or c.get("tool_use")
                                        tool_calls.append({
                                            "type": "tool_call",
                                            "tool": tu.get("name", ""),
                                            "input": tu.get("input", {}),
                                            "toolUseId": tu.get("toolUseId", tu.get("tool_use_id", "")),
                                            "timestamp": timestamp,
                                        })
                                    elif c.get("toolResult") or c.get("tool_result"):
                                        tr = c.get("toolResult") or c.get("tool_result")
                                        tr_content = tr.get("content", [])
                                        tr_text = ""
                                        if isinstance(tr_content, list):
                                            tr_text = " ".join(x.get("text", "") for x in tr_content if isinstance(x, dict))
                                        elif isinstance(tr_content, str):
                                            tr_text = tr_content
                                        # Truncate long tool results for display
                                        # Strip verbose fields (like skill instructions)
                                        try:
                                            tr_parsed = json.loads(tr_text)
                                            if isinstance(tr_parsed, dict) and "instructions" in tr_parsed:
                                                tr_parsed.pop("instructions")
                                                tr_text = json.dumps(tr_parsed, ensure_ascii=False)
                                        except (json.JSONDecodeError, TypeError):
                                            pass
                                        tool_calls.append({
                                            "type": "tool_result",
                                            "toolUseId": tr.get("toolUseId", tr.get("tool_use_id", "")),
                                            "status": tr.get("status", "success"),
                                            "content": tr_text,
                                            "timestamp": timestamp,
                                        })
                                # Emit the model's real reasoning (extended thinking) as its
                                # own entry, before the tool calls and answer of this turn.
                                if reasoning_parts and role == "assistant":
                                    entries.append({
                                        "role": "assistant",
                                        "content": "\n".join(reasoning_parts),
                                        "timestamp": timestamp,
                                        "thinking": True,
                                    })
                                # Add tool calls as separate entries
                                for tc in tool_calls:
                                    entries.append(tc)
                                # Add text content as message. This is the user-facing answer —
                                # NOT thinking. Genuine reasoning is captured above from
                                # reasoningContent blocks, not inferred from tool-call presence.
                                text_content = " ".join(parts)
                                if text_content and role in ("user", "assistant"):
                                    entries.append({"role": role, "content": text_content, "timestamp": timestamp, "thinking": False})
                            elif role in ("user", "assistant") and content:
                                entries.append({"role": role, "content": content, "timestamp": timestamp})
                messages.extend(reversed(entries))
            next_token = resp.get("nextToken")
            pages += 1
            if not next_token:
                break
            if pages >= _MAX_EVENT_PAGES:
                incomplete = UNREAD_LIMIT
                break
        if not pages:
            # The error text names the code only: an AgentCore message can
            # quote the actor or session id, which carry the user's number.
            return failure_response(
                502, f"Could not read this conversation from AgentCore Memory ({incomplete})", reason=incomplete
            )
        # Get token usage from Bedrock invocation logs and match to messages.
        #
        # Two distinct metrics, because they answer different questions:
        #   - billed_input_tokens: SUM of every invocation's inputTokenCount. This is
        #     what Bedrock actually charges — each agentic-loop call re-sends the full
        #     growing context, so the prefix is billed once per turn.
        #   - context_input_tokens: the SINGLE largest invocation's input (the real
        #     size of the conversation context). Summing per-turn inputs would count
        #     the shared prefix N times and read as wildly "inflated" vs the transcript.
        # cache_read/cache_write tokens are tracked separately, never folded into input.
        token_summary = _empty_token_summary()
        if messages:
            def _to_epoch(ts_val):
                if not ts_val:
                    return 0
                try:
                    return float(ts_val)
                except (ValueError, TypeError):
                    from datetime import datetime as _dt
                    try:
                        return _dt.fromisoformat(str(ts_val)).timestamp()
                    except Exception:
                        return 0
            timestamps = [_to_epoch(m.get("timestamp")) for m in messages if m.get("timestamp")]
            timestamps = [t for t in timestamps if t > 0]
            if timestamps:
                try:
                    logs_client = boto3.client("logs", region_name=config.get("region", REGION))
                    min_ts = int(min(timestamps) * 1000) if min(timestamps) > 1e9 else int(min(timestamps))
                    max_ts = int(max(timestamps) * 1000) if max(timestamps) > 1e9 else int(max(timestamps))
                    log_group = f"/aws/bedrock/{params['id']}/invocations"
                    log_resp = logs_client.filter_log_events(
                        logGroupName=log_group,
                        startTime=min_ts - 60000,
                        endTime=max_ts + 60000,
                        limit=500,
                    )
                    # Collect invocation entries with timestamps.
                    # NOTE: the Bedrock model-invocation log group is account/region
                    # scoped, not per-conversation, so without a correlation id we
                    # can only bound by this conversation's time window (above). Other
                    # conversations active in the same window on the same stack may
                    # still leak in — a known limitation, documented for the reader.
                    invocations = []
                    for ev in log_resp.get("events", []):
                        msg = ev.get("message", "")
                        if not msg or not msg.startswith("{"):
                            continue
                        try:
                            entry = json.loads(msg)
                            inv_ts = ev.get("timestamp", 0) / 1000.0  # ms to seconds
                            inp = entry.get("input", {}) or {}
                            out = entry.get("output", {}) or {}
                            it = inp.get("inputTokenCount", 0) or 0
                            ot = out.get("outputTokenCount", 0) or 0
                            cr = inp.get("cacheReadInputTokenCount", 0) or 0
                            cw = inp.get("cacheWriteInputTokenCount", 0) or 0
                            token_summary["billed_input_tokens"] += it
                            token_summary["output_tokens"] += ot
                            token_summary["cache_read_tokens"] += cr
                            token_summary["cache_write_tokens"] += cw
                            # context_input_tokens = largest single invocation, NOT the
                            # sum — that single call carries the full conversation context.
                            if it > token_summary["context_input_tokens"]:
                                token_summary["context_input_tokens"] = it
                            token_summary["invocations"] += 1
                            invocations.append({"ts": inv_ts, "input": it, "output": ot, "used": False})
                        except (json.JSONDecodeError, TypeError):
                            pass
                    # Back-compat: input_tokens mirrors the billed sum (prior behaviour).
                    token_summary["input_tokens"] = token_summary["billed_input_tokens"]
                    # Attribute invocations to the answer turns. Match in timestamp
                    # order and CONSUME each invocation once, so a single call is never
                    # counted toward multiple messages (the old overlapping-window
                    # logic inflated the per-message badge).
                    if invocations:
                        invocations.sort(key=lambda x: x["ts"])
                        answer_msgs = sorted(
                            (m for m in messages if m.get("role") == "assistant" and not m.get("thinking")),
                            key=lambda m: _to_epoch(m.get("timestamp", 0)),
                        )
                        for am in answer_msgs:
                            am_ts = _to_epoch(am.get("timestamp", 0))
                            matched_in = 0
                            matched_out = 0
                            for inv in invocations:
                                if inv["used"]:
                                    continue
                                if am_ts - 30 <= inv["ts"] <= am_ts + 5:
                                    matched_in += inv["input"]
                                    matched_out += inv["output"]
                                    inv["used"] = True
                            if matched_in or matched_out:
                                am["tokens"] = {"input": matched_in, "output": matched_out}
                except Exception:
                    pass
        body = {"messages": messages, "token_summary": token_summary}
        if incomplete:
            body["incomplete"] = incomplete
        return _json(200, body)
    except Exception:
        return _error(500, "Could not read this conversation")


def _parse_page_size(raw, default: int = 25) -> int:
    """Coerce a query-string page_size into a positive int, falling back to default."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    # AgentCore's list_actors caps maxResults at 100 and rejects more with a
    # ValidationException — which the caller's `except Exception: actors = []`
    # then swallows into a 200 {"conversations": []}, indistinguishable from an
    # empty stack. Clamp so a large page_size returns data instead of silence.
    return min(value, 100)


def _fetch_last_activity(agentcore, memory_id, phone):
    """Fetch only the most recent event timestamp for one actor — O(1).

    Reads a single event page (maxResults=1); does NOT count messages. The
    exact message count is O(events) per actor (it forced the N+1 that timed
    out the listing) and now lives only in the conversation detail view.
    Returns (last_activity, has_events); has_events is False for ghost actors
    (failed invocations / old formats) so the caller can skip them.
    """
    session_id = f"whatsapp-session-conversation-{phone}"
    try:
        resp = agentcore.list_events(
            memoryId=memory_id, sessionId=session_id, actorId=phone, maxResults=1
        )
        events = resp.get("events", [])
        if events:
            return str(events[0].get("eventTimestamp", "")), True
    except Exception:
        logger.exception("handler_error")
    return "", False


def _list_conversations(config, cursor=None, page_size=25):
    """List one page of conversations for a stack from AgentCore Memory.

    Returns (conversations, next_token); next_token is None on the last page.
    Paginates actors at the source via list_actors(nextToken=cursor), so the
    console scales past a single page instead of silently capping. Each actor's
    last activity is fetched in parallel with a single-page list_events call —
    collapsing the old N sequential multi-page walks (the 29s timeout) into N
    parallel O(1) calls. Ghost actors with no events are skipped.
    """
    memory_id = config.get("outputs", {}).get("memory_id", "")
    if not memory_id:
        return [], None
    stack_id = f"{config.get('project', 'unknown')}-{config.get('environment', 'unknown')}"
    region = config.get("region", REGION)
    agentcore = boto3.client("bedrock-agentcore", region_name=region)

    next_token = None
    try:
        kwargs = dict(memoryId=memory_id, maxResults=page_size)
        if cursor:
            kwargs["nextToken"] = cursor
        resp = agentcore.list_actors(**kwargs)
        actors = resp.get("actorSummaries", [])
        next_token = resp.get("nextToken")
    except Exception:
        actors = []

    candidates = []
    for actor in actors:
        phone = actor.get("actorId", "")
        if phone and phone.isdigit() and len(phone) >= 10:
            candidates.append(phone)

    # Enrich each actor's last activity in parallel. boto3 clients are
    # thread-safe (AWS docs), same bounded-pool pattern as
    # handle_list_user_operations.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as ex:
        enriched = list(ex.map(
            lambda p: (p, _fetch_last_activity(agentcore, memory_id, p)),
            candidates,
        ))

    conversations = []
    for phone, (last_activity, has_events) in enriched:
        if not has_events:
            continue  # ghost actor (failed invocation / old format)
        conversations.append({
            "phoneHash": phone[:4] + "..." + phone[-4:],
            "fullPhone": phone,
            "stack": stack_id,
            "region": region,
            "lastActivity": last_activity,
            "sessionStatus": "active",
            "memoryId": memory_id,
        })
    return conversations, next_token


def handle_get_conversations(params, _body):
    config = _get_registry(params["id"])
    if not config:
        return _json(404, {"error": "Stack not found"})
    page_size = _parse_page_size(params.get("page_size"))
    cursor = params.get("cursor")
    conversations, next_token = _list_conversations(config, cursor=cursor, page_size=page_size)
    body = {"conversations": conversations}
    if next_token:
        body["nextToken"] = next_token
    return _json(200, body)




@app.get("/api/stacks/<id>/conversations")
@require_group("operator")
def get_conversations(id: str):
    qs = query_params()
    return from_legacy(handle_get_conversations(
        {"id": id, "cursor": qs.get("cursor"), "page_size": qs.get("page_size")}, {}
    ))


@app.get("/api/stacks/<id>/conversations/<phone_hash>/messages")
@require_group("operator")
def get_conversation_messages(id: str, phone_hash: str):
    return from_legacy(handle_get_conversation_messages({"id": id, "phone_hash": phone_hash}, {}))


@app.delete("/api/stacks/<id>/conversations/<phone_hash>")
@require_group("admin")
def delete_conversation(id: str, phone_hash: str):
    return from_legacy(handle_delete_conversation({"id": id, "phone_hash": phone_hash}, {}))


@app.delete("/api/stacks/<id>/conversations")
@require_group("admin")
def delete_all_conversations(id: str):
    return from_legacy(handle_delete_all_conversations({"id": id}, {}))
