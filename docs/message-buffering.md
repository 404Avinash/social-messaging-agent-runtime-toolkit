# Message Buffering (Inbound Debounce)

WhatsApp users often split one thought across several rapid messages:

> "hola"
> "tengo una pregunta"
> "sobre mi pedido 1234"

Without buffering, each fragment triggers a separate agent invocation and a
separate reply. **Message buffering** coalesces a burst into a single agent
invocation once the user stops sending for a short window, so the agent reads
the whole thought and replies once.

It is **opt-in**, **deterministic** (timer-only — no LLM), and **text-only**
(see [Limitations](#limitations)).

---

## How it works

The buffer never sits *in front of* the webhook. Inbound messages still arrive
synchronously; an **SQS-delayed flush** acts as the debounce timer off to the
side.

```
INBOUND (per message, synchronous):
  EUM → SNS → webhook Lambda
                 ├─ parse; send typing
                 ├─ buffering ON & message is text?
                 │     ├─ ingest → DynamoDB  (atomic seq, append)
                 │     ├─ enqueue delayed flush → SQS (DelaySeconds = window, body = {phone, seq})
                 │     └─ return  ← NO agent call yet
                 └─ otherwise → invoke agent immediately (default path)

FLUSH (fires ~window seconds later):
  SQS (delayed) → webhook Lambda (second event source)
                 ├─ should_flush(phone, seq)?
                 │     ├─ superseded (a newer message bumped the token) → drop
                 │     ├─ buffer empty (already flushed)               → drop
                 │     └─ window elapsed / max-wait → combine buffered messages →
                 │                                     invoke agent once
                 │                                     → send reply → clear the flushed snapshot
```

### The generation token

Each inbound message atomically increments a per-phone counter (`latest_seq`)
in DynamoDB and schedules a flush carrying *its* `seq`. When a flush fires it
compares its `seq` against the current `latest_seq`:

- **`seq == latest_seq`** → this was the last message of the burst → **flush**.
- **`seq < latest_seq`** → a newer message arrived (and scheduled its own,
  later flush) → **abort** (`superseded`).

So a burst of 4 messages schedules 4 flushes, but only the 4th runs. The window
always resets from the *last* message (timer-reset semantics).

### Max-wait cap

If a user keeps typing forever, the window keeps resetting and they'd never get
a response. `buffer_max_wait_seconds` (default 45) forces a flush once the
*oldest* buffered message exceeds the cap, even if newer messages exist.

### Storage

The buffer reuses the **DynamoDB state table** under a dedicated key namespace
(`pk = "buf#{phone}"`), so it never collides with agent state. Items carry a TTL
so an abandoned buffer self-cleans. Buffering therefore **requires
`enable_state_table = true`**.

---

## Enabling it

Buffering is off by default. Enable it per stack in its `socialagent.<env>.yaml`:

```yaml
features:
  state_table: true                # required — the buffer reuses the state table
terraform:
  message_buffering_enabled: true
  buffer_window_seconds: 5         # debounce window (SQS DelaySeconds, max 900)
  buffer_max_wait_seconds: 45      # hard cap from the first buffered message
```

Terraform then provisions an SQS queue (`<prefix>-message-buffer`, SSE-SQS
encrypted, with a DLQ), wires it as a second event source on the webhook Lambda,
and grants the webhook role DynamoDB + SQS permissions. When
`message_buffering_enabled = false`, none of that infrastructure is created.

It is always **disabled in dev mode** (`socialagent dev`) — the local REPL is
single-user, where per-message behavior is what you want.

---

## Configuration

| tfvar / env var | Default | Description |
|-----------------|---------|-------------|
| `message_buffering_enabled` / `MESSAGE_BUFFERING_ENABLED` | `false` | Master switch. Requires the state table. |
| `buffer_window_seconds` / `BUFFER_WINDOW_SECONDS` | `5` | Debounce window. SQS `DelaySeconds`, capped at 900. |
| `buffer_max_wait_seconds` / `BUFFER_MAX_WAIT_SECONDS` | `45` | Force a flush this long after the first buffered message. |
| `buffer_ttl_seconds` / `BUFFER_TTL_SECONDS` | `900` | TTL backstop on buffer items so an abandoned buffer self-cleans. |
| `BUFFER_QUEUE_URL` | — | SQS flush queue URL (set automatically by Terraform). |

All values flow through `SocialAgentConfig.from_env()` — the buffer reads no
environment variables directly.

---

## Limitations

- **Text only.** Only `text` messages are buffered. Media, interactive
  replies, and location messages bypass the buffer and are processed
  immediately. If a user interleaves media mid-burst, the pending text still
  flushes on its own timer, so they may receive two replies.
- **Disabled in dev mode** — `socialagent dev` never buffers.
- **A text that cannot be buffered gets the generic error.** If storing it in DynamoDB or scheduling its flush on SQS fails (after botocore's own retries), the webhook sends the configured `generic` error message so the user can send it again. Lambda does not retry the SNS event, so without that message the text would go unanswered. A text stored before its flush failed to schedule goes out with the user's next message.
- **One response per burst** — the agent sees the combined prompt and replies
  once.
- **A burst that continues while the agent is replying gets a second reply to its start.** A flush removes the messages it read from the buffer only after the agent's turn and the reply, so when a follow-up's window ends while that turn is still running (a turn can take longer than the default 5-second window), the follow-up's flush reads the earlier messages again, and the agent answers them a second time together with the follow-up. Any side effect of the first turn can then run twice. A follow-up tracks the fix.

### Media messages

Only text messages are buffered; the webhook passes media, location and interactive replies straight to the agent. The buffer engine itself is media-aware: `MessageBuffer.ingest()` accepts a `msg_type` and `metadata`, and `combine_prompt()` renders placeholders for image, audio, location, button and document messages. Buffering media would take two webhook changes and none in the engine: route media types into `buffer.ingest(...)`, and move media prompt resolution (S3 download, transcription) into the flush path.

---

## Cost

Per buffered message: one DynamoDB `UpdateItem` + `PutItem`, one SQS
`SendMessage`; per flush: a DynamoDB `Query` + `BatchWriteItem` and one runtime
invocation. This is far cheaper than one runtime invocation *per fragment* —
the whole point of the feature.

---

## Implementation

| File | Role |
|------|------|
| `src/socialagent/buffer/core.py` | `MessageBuffer` (decision logic) + `combine_prompt` |
| `src/socialagent/buffer/store.py` | `BufferStore` — DynamoDB persistence (dev-file fallback) |
| `src/socialagent/buffer/queue.py` | `send_flush` — SQS enqueue helper |
| `src/socialagent/handlers/webhook.py` | SNS-vs-SQS routing, buffering decision, flush path |
| `infra-terraform/modules/lambdas/main.tf` | SQS queue + DLQ + event-source mapping + env |
| `infra-terraform/modules/iam/main.tf` | webhook role DynamoDB + SQS grants |

## See Also

- [Architecture](architecture.md) — the four Lambda roles and message flow
- [Configuration Reference](configuration.md) — all config options
- [State](state.md) — the DynamoDB state table the buffer reuses
