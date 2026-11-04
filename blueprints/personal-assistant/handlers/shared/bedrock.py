# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""One-shot model calls from the handler Lambdas, through the Bedrock Converse API.

Converse takes the same request shape for every Bedrock model that supports it,
so MODEL_ID can name any model that accepts image and document input, not only
Claude. The agent itself already calls Bedrock through Converse (Strands
``BedrockModel``).
"""

import os

from shared.model import MODEL_ID

# Stop reasons whose text, if any, is not an answer to pass on to the agent.
_BLOCKED = {"guardrail_intervened", "content_filtered"}


class NoTextResponse(Exception):
    """The model answered without usable text (blocked, or cut off before any text)."""


def response_text(response: dict) -> str:
    """Join the text blocks of a Converse response.

    Models that reason can return ``reasoningContent`` blocks before the text,
    and later models may add block types, so blocks are picked by key rather
    than by position. A blocked reply, or one with no text, raises so the caller
    falls back instead of passing a partial or substituted answer to the agent.
    """
    stop_reason = response.get("stopReason")
    content = ((response.get("output") or {}).get("message") or {}).get("content", [])
    parts: list[str] = []
    for block in content:
        if "text" in block:
            parts.append(block["text"])
        elif "citationsContent" in block:
            # With document citations enabled, the generated text that a citation
            # supports arrives here, not in a top-level text block.
            for generated in block["citationsContent"].get("content") or []:
                if "text" in generated:
                    parts.append(generated["text"])
    text = "".join(parts).strip()
    if stop_reason in _BLOCKED or not text:
        raise NoTextResponse(
            f"no usable text in model response (stopReason={stop_reason})"
        )
    return text


def guardrail_config() -> dict | None:
    """The agent's guardrail as a Converse ``guardrailConfig``, or None without one.

    Terraform gives the webhook the guardrail it gives the agent, and sets both
    variables empty when there is none ("Guardrails" in docs/configuration.md
    in the SMART repo). `or`, not a get() default: present but empty means
    unset. A missing version falls back to the draft, as the agent's does
    (``AgentConfig.guardrail_config``).
    """
    guardrail_id = os.environ.get("BEDROCK_GUARDRAIL_ID") or ""
    if not guardrail_id:
        return None
    return {
        "guardrailIdentifier": guardrail_id,
        "guardrailVersion": os.environ.get("BEDROCK_GUARDRAIL_VERSION") or "DRAFT",
    }


def ask_model(bedrock_client, content: list[dict], max_tokens: int) -> str:
    """Send one user turn in Converse content blocks and return the model's text.

    ``max_tokens`` also has to cover any reasoning the model does before it answers.
    With a guardrail configured, the call passes it. The stack's guardrail
    evaluates the text of the request and of the answer only (it enables no image
    modality); a PDF summary, which comes back in ``citationsContent`` blocks, is
    treated as unguarded ("Guardrails" in docs/configuration.md in the SMART
    repo). An intervention, a mask included, stops with ``guardrail_intervened``,
    which raises NoTextResponse.
    """
    request = {
        "modelId": MODEL_ID,
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    guardrail = guardrail_config()
    if guardrail:
        request["guardrailConfig"] = guardrail
    response = bedrock_client.converse(**request)
    return response_text(response)
