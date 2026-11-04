// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import OverviewPage from "@/pages/overview";
import { api } from "@/api/client";

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts.

describe("Overview", () => {
  afterEach(() => {
    vi.mocked(api.searchLogs).mockResolvedValue({ results: [], unreadable: [] });
  });

  it("says Recent errors could not load instead of showing an all-clear", async () => {
    // PUT /logs is operator-level, so a viewer always lands here.
    vi.mocked(api.searchLogs).mockRejectedValue(new Error("Forbidden: Requires 'operator' group membership"));
    render(<OverviewPage navigate={() => {}} />);

    expect(await screen.findByText(/Requires 'operator' group membership/)).toBeInTheDocument();
    expect(screen.queryByText("No errors in the last hour")).not.toBeInTheDocument();
  });

  it("shows the all-clear when the search worked and found nothing", async () => {
    render(<OverviewPage navigate={() => {}} />);
    expect(await screen.findByText("No errors in the last hour")).toBeInTheDocument();
  });

  it("renders stack card", async () => {
    render(<OverviewPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("test-dev")).toBeInTheDocument(); });
  });

  it("shows health indicator", async () => {
    render(<OverviewPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("healthy")).toBeInTheDocument(); });
  });

  it("shows counter", async () => {
    render(<OverviewPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("(1)")).toBeInTheDocument(); });
  });
});

describe("Overview template health", () => {
  // The reclassification banner is the only place the Overview reports Meta's
  // category moves, so a missing banner must mean that none were found: a
  // failed or partial check has to say so instead of showing nothing.
  afterEach(() => {
    vi.mocked(api.getTemplateHealth).mockReset().mockResolvedValue({ reclassified: [], nonApproved: [], total: 0, unchecked: [], failedStacks: [] });
  });

  it("warns when the health check fails", async () => {
    // A request over many templates can run past the console API's 30-second limit.
    vi.mocked(api.getTemplateHealth).mockRejectedValue(new Error("API error: 504 Gateway Timeout"));
    const navigate = vi.fn();
    render(<OverviewPage navigate={navigate} />);

    expect(await screen.findByText("Template health check failed")).toBeInTheDocument();
    expect(screen.getByText(/API error: 504 Gateway Timeout/)).toBeInTheDocument();
    expect(screen.getByText(/reclassified\s+templates may not be shown/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review templates" }));
    expect(navigate).toHaveBeenCalledWith("#/templates");
  });

  it("says how many templates were not checked and which stacks failed", async () => {
    vi.mocked(api.getTemplateHealth).mockResolvedValue({
      reclassified: [], nonApproved: [], total: 3,
      unchecked: [{ name: "dev_bot_a", stack: "bot-dev" }, { name: "dev_bot_b", stack: "bot-dev" }],
      failedStacks: ["big-prod", "shop-prod"],
    });
    const navigate = vi.fn();
    render(<OverviewPage navigate={navigate} />);

    expect(await screen.findByText("Template health check incomplete")).toBeInTheDocument();
    expect(screen.getByText(/2 template\(s\) not checked for reclassification/)).toBeInTheDocument();
    expect(screen.getByText("big-prod, shop-prod")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review templates" }));
    expect(navigate).toHaveBeenCalledWith("#/templates");
  });

  it("reports unchecked templates when no stack failed", async () => {
    vi.mocked(api.getTemplateHealth).mockResolvedValue({
      reclassified: [], nonApproved: [], total: 1, unchecked: [{ name: "dev_bot_a", stack: "bot-dev" }], failedStacks: [],
    });
    render(<OverviewPage navigate={() => {}} />);

    expect(await screen.findByText(/1 template\(s\) not checked for reclassification/)).toBeInTheDocument();
    expect(screen.queryByText(/Templates not listed/)).not.toBeInTheDocument();
  });

  it("stays quiet for a complete check, including from a backend without the new fields", async () => {
    vi.mocked(api.getTemplateHealth).mockResolvedValue({ reclassified: [], nonApproved: [], total: 4 });
    render(<OverviewPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("test-dev")).toBeInTheDocument(); });
    await waitFor(() => { expect(api.getTemplateHealth).toHaveBeenCalled(); });

    expect(screen.queryByText(/Template health check/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review templates" })).not.toBeInTheDocument();
  });
});
