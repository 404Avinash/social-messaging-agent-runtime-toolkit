// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import LogsPage from "@/pages/logs";
import OverviewPage from "@/pages/overview";
import StackDetailPage from "@/pages/stack-detail";
import EventsPage from "@/pages/events";
import { api } from "@/api/client";
import { ApiError } from "@/api/errors";
import type { LogRead, StackEvent } from "@/types";

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts.

// The backend lists a log group it could not read in full beside the rows it
// did read. The pages showed an empty list as "No errors in the last hour",
// "No errors" or "No matching log events", whether or not anything was read.

const EMPTY: LogRead<never> = { results: [], unreadable: [] };
const PARTIAL: LogRead<never> = {
  results: [],
  unreadable: [
    { lambda: "dev-bot-notification", reason: "ThrottlingException" },
    { lambda: "dev-bot-event", reason: "budget" },
  ],
};
const NAMED = /dev-bot-notification \(ThrottlingException\), dev-bot-event \(ran out of time or calls\)/;

// A route that could read no log group answers 502, which the client turns
// into an ApiError naming the groups in its message.
function nothingRead() {
  return new ApiError(
    "Could not read any log group (request id r1): dev-bot-webhook (AccessDeniedException), dev-bot-event (AccessDeniedException)",
    502,
    [{ lambda: "dev-bot-webhook", reason: "AccessDeniedException" }, { lambda: "dev-bot-event", reason: "AccessDeniedException" }],
  );
}

afterEach(() => {
  vi.mocked(api.searchLogs).mockResolvedValue(EMPTY);
  vi.mocked(api.getErrors).mockResolvedValue(EMPTY);
  vi.mocked(api.getEvents).mockResolvedValue(EMPTY);
});

describe("Logs page", () => {
  async function search() {
    render(<LogsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText(/^Results$/);
  }

  it("names the log groups it could not read instead of saying nothing matched", async () => {
    vi.mocked(api.searchLogs).mockResolvedValue(PARTIAL);
    await search();

    expect(await screen.findByText("Results may be incomplete")).toBeInTheDocument();
    expect(screen.getByText(NAMED)).toBeInTheDocument();
    expect(screen.queryByText("No matching log events")).not.toBeInTheDocument();
  });

  it("says nothing matched when every log group was read", async () => {
    await search();

    expect(await screen.findByText("No matching log events")).toBeInTheDocument();
    expect(screen.queryByText("Results may be incomplete")).not.toBeInTheDocument();
  });

  it("shows a search that could read no log group as a failure, not as nothing matched", async () => {
    vi.mocked(api.searchLogs).mockRejectedValue(nothingRead());
    await search();

    expect(await screen.findByText(/dev-bot-webhook \(AccessDeniedException\)/)).toBeInTheDocument();
    expect(screen.getByText("Search failed")).toBeInTheDocument();
    expect(screen.queryByText("No matching log events")).not.toBeInTheDocument();
    expect(screen.queryByText(/Try a wider time window/)).not.toBeInTheDocument();
  });

  it("drops the last search's warning when the next one fails", async () => {
    vi.mocked(api.searchLogs).mockResolvedValueOnce(PARTIAL).mockRejectedValueOnce(nothingRead());
    await search();
    expect(await screen.findByText("Results may be incomplete")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText(/dev-bot-webhook \(AccessDeniedException\)/)).toBeInTheDocument();
    expect(screen.queryByText("Results may be incomplete")).not.toBeInTheDocument();
    expect(screen.queryByText(NAMED)).not.toBeInTheDocument();
  });
});

describe("Overview Recent errors", () => {
  it("warns that the errors may be incomplete instead of an all-clear", async () => {
    vi.mocked(api.searchLogs).mockResolvedValue(PARTIAL);
    render(<OverviewPage navigate={() => {}} />);

    expect(await screen.findByText("Recent errors may be incomplete")).toBeInTheDocument();
    expect(screen.getByText(NAMED)).toBeInTheDocument();
    expect(screen.queryByText("No errors in the last hour")).not.toBeInTheDocument();
  });

  it("names the log groups of a search that could read none", async () => {
    // The client puts a 502's unreadable groups in the error message.
    vi.mocked(api.searchLogs).mockRejectedValue(new Error("Could not read any log group: dev-bot-webhook (AccessDeniedException)"));
    render(<OverviewPage navigate={() => {}} />);

    expect(await screen.findByText("Could not check for recent errors")).toBeInTheDocument();
    expect(screen.getByText(/dev-bot-webhook \(AccessDeniedException\)/)).toBeInTheDocument();
    expect(screen.queryByText("No errors in the last hour")).not.toBeInTheDocument();
  });
});

describe("Stack Detail", () => {
  it("the Status tab warns, though its Recent errors section has no rows to show", async () => {
    vi.mocked(api.getErrors).mockResolvedValue(PARTIAL);
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);

    expect(await screen.findByText("Recent errors may be incomplete")).toBeInTheDocument();
    expect(screen.getByText(NAMED)).toBeInTheDocument();
  });

  it("the Errors tab warns instead of saying there are no errors", async () => {
    vi.mocked(api.getErrors).mockResolvedValue(PARTIAL);
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Errors" }));

    expect(await screen.findByText("Results may be incomplete")).toBeInTheDocument();
    expect(screen.getByText("Nothing found in the logs that were read")).toBeInTheDocument();
    expect(screen.queryByText("No errors")).not.toBeInTheDocument();
  });

  it("the Errors tab says there are no errors when every log group was read", async () => {
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Errors" }));

    expect(await screen.findByText("No errors")).toBeInTheDocument();
    expect(screen.queryByText(/may be incomplete/)).not.toBeInTheDocument();
  });
});

describe("Events page", () => {
  it("names the event logs it could not read instead of saying there are no events", async () => {
    vi.mocked(api.getEvents).mockResolvedValue(PARTIAL);
    render(<EventsPage navigate={() => {}} />);

    expect(await screen.findByText("Results may be incomplete")).toBeInTheDocument();
    expect(screen.getByText(NAMED)).toBeInTheDocument();
    expect(screen.queryByText("No events")).not.toBeInTheDocument();
  });

  it("says there are no events when every event log was read", async () => {
    render(<EventsPage navigate={() => {}} />);

    expect(await screen.findByText("No events")).toBeInTheDocument();
    expect(screen.queryByText("Results may be incomplete")).not.toBeInTheDocument();
  });

  it("shows events that could not be read at all as a failure, not as none recorded", async () => {
    vi.mocked(api.getEvents).mockRejectedValue(nothingRead());
    render(<EventsPage navigate={() => {}} />);

    expect(await screen.findByText("Failed to load events")).toBeInTheDocument();
    expect(screen.getByText(/dev-bot-webhook \(AccessDeniedException\)/)).toBeInTheDocument();
    expect(screen.getByText("Events not loaded")).toBeInTheDocument();
    expect(screen.queryByText("No events")).not.toBeInTheDocument();
    expect(screen.queryByText(/No events have been recorded yet/)).not.toBeInTheDocument();
  });

  it("drops the last load's rows and warning when a refresh fails", async () => {
    // useApi keeps the last answer when a load fails, so the page showed the
    // previous stack's events under the error.
    const event: StackEvent = {
      timestamp: "2026-10-30T10:00:00Z", stack: "bot-dev", source: "API Gateway", eventType: "order_update",
      lambdaInvoked: "event", payloadSummary: "{}", status: "success",
    };
    vi.mocked(api.getEvents).mockResolvedValueOnce({ results: [event], unreadable: PARTIAL.unreadable }).mockRejectedValueOnce(nothingRead());
    render(<EventsPage navigate={() => {}} />);
    expect(await screen.findByText("order_update")).toBeInTheDocument();
    expect(screen.getByText("Results may be incomplete")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh events" }));

    expect(await screen.findByText("Failed to load events")).toBeInTheDocument();
    expect(screen.queryByText("order_update")).not.toBeInTheDocument();
    expect(screen.queryByText("Results may be incomplete")).not.toBeInTheDocument();
  });
});
