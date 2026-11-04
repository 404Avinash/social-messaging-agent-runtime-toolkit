// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import StatePage from "@/pages/state";
import StackDetailPage from "@/pages/stack-detail";
import { api } from "@/api/client";
import type { StateEntry } from "@/types";

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts.

const KEYS = ["booking", "counter#replies", "notes"];

function entries(): StateEntry[] {
  return KEYS.map((key) => ({ phone: "+15555550100", key, value: "v", ttl: 0 }));
}

/** One key deleted, one the backend found nothing under, one whose call failed. */
function answerEachDifferently() {
  vi.mocked(api.deleteStateKey)
    .mockResolvedValueOnce({ deleted: true })
    .mockResolvedValueOnce({ deleted: false })
    .mockRejectedValueOnce(new Error("Delete state key failed"));
}

/** Select every row with the header checkbox, then delete them. */
async function deleteEveryKey() {
  await screen.findByText(KEYS[0]);
  fireEvent.click(screen.getAllByRole("checkbox")[0]);
  fireEvent.click(screen.getByRole("button", { name: `Delete selected (${KEYS.length})` }));
}

describe("Deleting state keys", () => {
  afterEach(() => {
    vi.mocked(api.deleteStateKey).mockReset().mockResolvedValue({ deleted: true });
    vi.mocked(api.listAllState).mockResolvedValue([]);
    vi.mocked(api.getState).mockResolvedValue([]);
  });

  // DeleteItem succeeds on a key that is not there, so the backend says
  // deleted: false, and the count must not report that key as deleted.
  it("the State page reports a key that was already gone as not found", async () => {
    vi.mocked(api.listAllState).mockResolvedValue(entries().map((e) => ({ ...e, stack: "test-dev" })));
    answerEachDifferently();

    render(<StatePage navigate={() => {}} />);
    await deleteEveryKey();

    expect(await screen.findByText("Deleted 1 key(s), 1 not found, 1 failed")).toBeInTheDocument();
    expect(api.deleteStateKey).toHaveBeenCalledTimes(KEYS.length);
  });

  it("the stack's State tab reports a key that was already gone as not found", async () => {
    vi.mocked(api.getState).mockResolvedValue(entries());
    answerEachDifferently();

    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await screen.findByText("Stack summary");
    fireEvent.click(screen.getByRole("tab", { name: "State" }));
    await deleteEveryKey();

    expect(await screen.findByText("Deleted 1 key(s), 1 not found, 1 failed")).toBeInTheDocument();
    expect(api.deleteStateKey).toHaveBeenCalledTimes(KEYS.length);
  });

  it("nuclear reset reports a key that was already gone as not found, not cleared", async () => {
    vi.mocked(api.getState).mockResolvedValue(entries());
    answerEachDifferently();

    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await screen.findByText("Stack summary");
    fireEvent.click(screen.getByRole("button", { name: /Actions/i }));
    fireEvent.click(await screen.findByText("Nuclear reset"));
    fireEvent.change(screen.getByPlaceholderText("test-dev"), { target: { value: "test-dev" } });
    fireEvent.click(screen.getByRole("button", { name: "Reset everything" }));

    expect(await screen.findByText(/⚠️ 1 state keys cleared, 1 not found, 1 failed/)).toBeInTheDocument();
    expect(api.deleteStateKey).toHaveBeenCalledTimes(KEYS.length);
  });

  it("says nothing of not found or failed when every key was deleted", async () => {
    vi.mocked(api.listAllState).mockResolvedValue(entries().map((e) => ({ ...e, stack: "test-dev" })));

    render(<StatePage navigate={() => {}} />);
    await deleteEveryKey();

    expect(await screen.findByText(`Deleted ${KEYS.length} key(s)`)).toBeInTheDocument();
  });
});
