// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// A schedule can name the IANA zone its expression is read in, so a daily
// cron(0 9 * * ? *) may fire at 9am Mexico City time. Both schedule tables say
// which zone, or an operator reads every expression as UTC.

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SchedulesPage from "@/pages/schedules";
import StackDetailPage from "@/pages/stack-detail";
import { api } from "@/api/client";
import type { Schedule } from "@/types";

function makeSchedule(name: string, overrides: Partial<Schedule>): Schedule {
  return {
    name,
    stack: "test-dev",
    state: "ENABLED",
    expression: "cron(0 9 * * ? *)",
    type: "recurring",
    targetArn: "arn:aws:lambda:us-east-1:111122223333:function:test-dev-scheduler",
    operation: "reminder",
    phone: "+15550000000",
    payload: {},
    createdAt: "2024-07-01",
    lastModified: "2024-07-01",
    ...overrides,
  };
}

const SCHEDULES = [
  makeSchedule("zoned-cron", { timezone: "America/Mexico_City" }),
  makeSchedule("utc-cron", { expression: "cron(0 15 * * ? *)", timezone: "UTC" }),
  makeSchedule("zoned-at", {
    type: "one-shot",
    expression: "at(2026-11-02T09:00:00)",
    timezone: "America/New_York",
  }),
  // A backend from before the field existed: every expression was UTC.
  makeSchedule("older-backend", { type: "one-shot", expression: "at(2026-10-10T14:00:00)" }),
];

function expectZonesShown() {
  expect(screen.getByText("Cron: cron(0 9 * * ? *) (America/Mexico_City)")).toBeInTheDocument();
  expect(screen.getByText("Cron: cron(0 15 * * ? *) (UTC)")).toBeInTheDocument();
  expect(screen.getByText("Fires at 2026-11-02T09:00:00 America/New_York")).toBeInTheDocument();
  expect(screen.getByText("Fires at 2026-10-10T14:00:00 UTC")).toBeInTheDocument();
}

describe("Schedule time zones", () => {
  afterEach(() => {
    vi.mocked(api.listSchedules).mockResolvedValue([]);
  });

  it("the Schedules page names each schedule's zone", async () => {
    vi.mocked(api.listSchedules).mockResolvedValue(SCHEDULES);
    render(<SchedulesPage navigate={() => {}} />);
    await screen.findByText("zoned-cron");
    expectZonesShown();
  });

  it("the stack's Schedules tab names each schedule's zone", async () => {
    vi.mocked(api.listSchedules).mockResolvedValue(SCHEDULES);
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await screen.findByText("Stack summary");
    fireEvent.click(screen.getByRole("tab", { name: "Schedules" }));
    await screen.findByText("zoned-cron");
    expectZonesShown();
  });
});
