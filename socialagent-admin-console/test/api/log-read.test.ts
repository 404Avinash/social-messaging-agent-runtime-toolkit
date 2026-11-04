// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { describeUnreadable, toLogRead, unreadableList } from "@/api/log-read";

describe("toLogRead", () => {
  it("passes the {results, unreadable} envelope through", () => {
    const unreadable = [{ lambda: "dev-bot-webhook", reason: "ThrottlingException" }];
    expect(toLogRead({ results: [{ message: "a" }], unreadable })).toEqual({ results: [{ message: "a" }], unreadable });
  });

  it("reads a bare list, which a backend from before the envelope answers, as complete", () => {
    expect(toLogRead([{ message: "a" }])).toEqual({ results: [{ message: "a" }], unreadable: [] });
  });

  it("never yields a non-array results or unreadable list", () => {
    for (const raw of [undefined, null, {}, 42, "oops", { results: null, unreadable: "x" }]) {
      expect(toLogRead(raw)).toEqual({ results: [], unreadable: [] });
    }
  });
});

describe("unreadableList", () => {
  it("keeps only entries with a lambda and a reason", () => {
    expect(unreadableList([
      { lambda: "dev-bot-webhook", reason: "AccessDeniedException" },
      { lambda: "dev-bot-event" },
      null,
      "dev-bot-notification",
      { lambda: 1, reason: "x" },
    ])).toEqual([{ lambda: "dev-bot-webhook", reason: "AccessDeniedException" }]);
  });
});

describe("describeUnreadable", () => {
  it("names each Lambda with its reason, and says what the budget reason means", () => {
    expect(describeUnreadable([
      { lambda: "dev-bot-webhook", reason: "ThrottlingException" },
      { lambda: "dev-bot-event", reason: "budget" },
    ])).toBe("dev-bot-webhook (ThrottlingException), dev-bot-event (ran out of time or calls)");
  });
});
