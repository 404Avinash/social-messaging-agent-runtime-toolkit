// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConversationDetailModal } from "@/components/conversation-detail-modal";

// Use the REAL client with fetch stubbed, so the modal sees exactly what the
// backend sends. The manual mock had no getConversationMessages, which is how
// a response shape the modal could not read stayed green.
vi.unmock("@/api/client");

const backendBody = {
  messages: [
    { role: "assistant", content: "Hola Ana, en que te ayudo?", timestamp: "2026-10-01T10:00:05", thinking: false },
    { type: "tool_result", toolUseId: "t1", status: "success", content: "{\"profile\": \"loaded\"}", timestamp: "2026-10-01T10:00:04" },
    { type: "tool_call", tool: "recall", input: {}, toolUseId: "t1", timestamp: "2026-10-01T10:00:03" },
    { role: "assistant", content: "I should greet the user by name.", timestamp: "2026-10-01T10:00:02", thinking: true },
    { role: "user", content: "Hola, soy Ana", timestamp: "2026-10-01T10:00:00" },
  ],
  token_summary: {
    billed_input_tokens: 2400, context_input_tokens: 1300, output_tokens: 75,
    cache_read_tokens: 0, cache_write_tokens: 0, invocations: 2, input_tokens: 2400,
  },
};

describe("ConversationDetailModal", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, statusText: "OK", json: async () => backendBody,
    }));
    // jsdom has no layout, so it does not implement scrollIntoView.
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function renderModal() {
    render(
      <ConversationDetailModal visible onDismiss={() => {}} stackId="bot-dev"
        phoneHash="5255...0100" fullPhone="+525555550100" />,
    );
  }

  it("opens a conversation from the backend's {messages, token_summary} response", async () => {
    renderModal();
    expect(await screen.findByText("Hola, soy Ana")).toBeInTheDocument();
    expect(screen.getByText("Hola Ana, en que te ayudo?")).toBeInTheDocument();
    expect(screen.queryByText(/is not a function/)).not.toBeInTheDocument();
    expect(screen.getByText(/2 messages/)).toBeInTheDocument();
  });

  it("does not show tool results or reasoning as messages from the bot", async () => {
    renderModal();
    await screen.findByText("Hola, soy Ana");
    expect(screen.queryByText(/profile/)).not.toBeInTheDocument();
    expect(screen.queryByText(/greet the user by name/)).not.toBeInTheDocument();
  });

  it("shows the backend's 502 as a failed read, not as a conversation with no messages", async () => {
    // AgentCore Memory refused or throttled the first page. This answered 200
    // with no messages, so the modal said "No messages found." and "0 messages".
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 502, statusText: "Bad Gateway",
      json: async () => ({
        error: "Could not read this conversation from AgentCore Memory (ThrottlingException) (request id req-123)",
        requestId: "req-123", reason: "ThrottlingException",
      }),
    }));
    renderModal();
    expect(await screen.findByText("Could not read this conversation")).toBeInTheDocument();
    expect(screen.getByText(/\(ThrottlingException\) \(request id req-123\)/)).toBeInTheDocument();
    expect(screen.queryByText("No messages found.")).not.toBeInTheDocument();
    expect(screen.queryByText(/^0 messages/)).not.toBeInTheDocument();
  });

  it("shows a read the backend marks incomplete with a warning", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, statusText: "OK", json: async () => ({ ...backendBody, incomplete: "ThrottlingException" }),
    }));
    renderModal();
    expect(await screen.findByText("Hola, soy Ana")).toBeInTheDocument();
    expect(screen.getByText("This conversation may be incomplete")).toBeInTheDocument();
    expect(screen.getByText("2 messages read")).toBeInTheDocument();
  });

  it("does not present the token summary as this conversation's usage", async () => {
    // token_summary counts every model call on the stack in the conversation's
    // time window, from a log group the stack does not create.
    renderModal();
    await screen.findByText("Hola, soy Ana");
    expect(screen.queryByText(/model calls/)).not.toBeInTheDocument();
    expect(screen.queryByText(/2,400/)).not.toBeInTheDocument();
  });
});
