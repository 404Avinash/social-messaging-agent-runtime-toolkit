// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest";
import { NOT_FOUND, deleteEach, deleteFlash, deleteSchedule, deleteStateKey } from "@/api/bulk-delete";
import { api } from "@/api/client";
import { ApiError } from "@/api/errors";

describe("deleteEach", () => {
  it("counts deleted, not found and failed items apart, keeping the first reason", async () => {
    const outcome = await deleteEach(["a", "b", "c", "d"], async (item) => {
      if (item === "b") return NOT_FOUND;
      if (item === "c") throw new Error("Forbidden");
      if (item === "d") throw new Error("Throttled");
      return { deleted: true };
    });

    expect(outcome).toEqual({ total: 4, deleted: 1, notFound: 1, failed: 2, reason: "Forbidden" });
  });
});

describe("deleteFlash", () => {
  it("is a success only when every item was deleted", () => {
    expect(deleteFlash({ total: 2, deleted: 2, notFound: 0, failed: 0, reason: null }, "key(s)"))
      .toEqual({ type: "success", content: "Deleted 2 key(s)" });
  });

  it("is a warning when an item was not found", () => {
    expect(deleteFlash({ total: 2, deleted: 1, notFound: 1, failed: 0, reason: null }, "key(s)"))
      .toEqual({ type: "warning", content: "Deleted 1 key(s), 1 not found" });
  });

  it("is an error with the counts over the reason when an item failed", () => {
    expect(deleteFlash({ total: 3, deleted: 1, notFound: 1, failed: 1, reason: "Forbidden" }, "key(s)"))
      .toEqual({ type: "error", header: "Deleted 1 key(s), 1 not found, 1 failed", content: "Forbidden" });
  });
});

describe("deleteStateKey", () => {
  afterEach(() => {
    vi.mocked(api.deleteStateKey).mockResolvedValue({ deleted: true });
  });

  it("resolves to NOT_FOUND when the backend found no row", async () => {
    vi.mocked(api.deleteStateKey).mockResolvedValue({ deleted: false });
    await expect(deleteStateKey("bot-dev", "+15555550100", "name")).resolves.toBe(NOT_FOUND);
  });

  it("resolves to nothing when it removed the row", async () => {
    await expect(deleteStateKey("bot-dev", "+15555550100", "name")).resolves.toBeUndefined();
  });
});

describe("deleteSchedule", () => {
  afterEach(() => {
    vi.mocked(api.deleteSchedule).mockResolvedValue({ deleted: "test" });
  });

  it("resolves to NOT_FOUND when the schedule was already gone", async () => {
    // A one-shot schedule deletes itself once it fires.
    vi.mocked(api.deleteSchedule).mockRejectedValue(new ApiError("Schedule not found", 404));
    await expect(deleteSchedule("socialagent-bot-dev-reminder-0100")).resolves.toBe(NOT_FOUND);
  });

  it("resolves to nothing when it deleted the schedule", async () => {
    await expect(deleteSchedule("socialagent-bot-dev-reminder-0100")).resolves.toBeUndefined();
  });

  it("rejects with any other failure", async () => {
    const refused = new ApiError("Forbidden: Requires 'admin' group membership", 403);
    vi.mocked(api.deleteSchedule).mockRejectedValue(refused);
    await expect(deleteSchedule("socialagent-bot-dev-reminder-0100")).rejects.toBe(refused);
  });
});
