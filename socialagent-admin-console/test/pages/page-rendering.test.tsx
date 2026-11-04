// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import OverviewPage from "@/pages/overview";
import StacksPage from "@/pages/stacks";
import TemplatesPage from "@/pages/templates";
import ConversationsPage from "@/pages/conversations";
import SchedulesPage from "@/pages/schedules";
import StatePage from "@/pages/state";
import PhoneNumbersPage from "@/pages/phone-numbers";
import EventsPage from "@/pages/events";
import SettingsPage from "@/pages/settings";

const noop = () => {};

describe("Page rendering", () => {
  it("Overview renders and shows stack", async () => {
    render(<OverviewPage navigate={noop} />);
    await waitFor(() => { expect(screen.getByText("Overview")).toBeInTheDocument(); });
    await waitFor(() => { expect(screen.getByText("test-dev")).toBeInTheDocument(); });
  });

  it("Stacks renders and shows stack", async () => {
    render(<StacksPage navigate={noop} />);
    await waitFor(() => { expect(screen.getByText("Stacks")).toBeInTheDocument(); });
    await waitFor(() => { expect(screen.getByText("test-dev")).toBeInTheDocument(); });
  });

  it("Templates renders", async () => {
    render(<TemplatesPage navigate={noop} />);
    await waitFor(() => { expect(screen.getByText("Templates")).toBeInTheDocument(); });
  });

  it("Conversations renders", async () => {
    render(<ConversationsPage navigate={noop} />);
    await waitFor(() => { expect(screen.getByText("Conversations")).toBeInTheDocument(); });
  });

  it("Events renders", async () => {
    render(<EventsPage navigate={noop} />);
    await waitFor(() => { expect(screen.getByText("Events")).toBeInTheDocument(); });
  });

  // Cross-stack pages: wired into App nav/routing so their features (notably
  // phone-number binding) are reachable. Guard that each renders.
  it("Schedules renders", async () => {
    render(<SchedulesPage navigate={noop} />);
    await waitFor(() => { expect(screen.getByText("Schedules")).toBeInTheDocument(); });
  });

  it("State renders", async () => {
    render(<StatePage navigate={noop} />);
    await waitFor(() => { expect(screen.getByText("User state")).toBeInTheDocument(); });
  });

  it("Phone numbers renders", async () => {
    render(<PhoneNumbersPage navigate={noop} />);
    await waitFor(() => { expect(screen.getByText("Phone numbers")).toBeInTheDocument(); });
  });

  it("Settings renders", async () => {
    render(<SettingsPage />);
    await waitFor(() => { expect(screen.getByText("AWS configuration")).toBeInTheDocument(); });
  });
});
