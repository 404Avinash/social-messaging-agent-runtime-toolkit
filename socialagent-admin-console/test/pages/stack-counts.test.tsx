// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// The stack counts say what they count. The 24-hour figure is the webhook
// Lambda's invocations less the console's probes, and WhatsApp's sent,
// delivered and read receipts invoke that Lambda as well as inbound messages,
// so it is not a message count. "Active conversations" was always 0, and
// "Last invocation" showed when the function was last modified.

import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import OverviewPage from "@/pages/overview";
import StacksPage from "@/pages/stacks";
import StackDetailPage from "@/pages/stack-detail";

describe("Stack counts", () => {
  it("Stacks table labels the webhook figure and has no conversation count", async () => {
    render(<StacksPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("test-dev")).toBeInTheDocument(); });
    expect(screen.getAllByText("Webhook events (24h)").length).toBeGreaterThan(0);
    expect(screen.queryByText("Messages (24h)")).not.toBeInTheDocument();
    expect(screen.queryByText("Active conversations")).not.toBeInTheDocument();
  });

  it("Overview cards label the webhook figure", async () => {
    render(<OverviewPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("test-dev")).toBeInTheDocument(); });
    expect(screen.getAllByText("Webhook events (24h)").length).toBeGreaterThan(0);
    expect(screen.queryByText("Messages (24h)")).not.toBeInTheDocument();
  });

  it("Stack Detail labels the webhook figure and the Lambdas' last change", async () => {
    render(<StackDetailPage stackId="test-dev" navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Lambda functions")).toBeInTheDocument(); });
    expect(screen.getAllByText("Webhook events (24h)").length).toBeGreaterThan(0);
    expect(screen.queryByText("Messages (24h)")).not.toBeInTheDocument();
    expect(screen.getAllByText("Last modified").length).toBeGreaterThan(0);
    expect(screen.queryByText("Last invocation")).not.toBeInTheDocument();
  });
});
