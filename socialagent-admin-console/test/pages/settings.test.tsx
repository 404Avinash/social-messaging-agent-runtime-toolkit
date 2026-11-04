// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import SettingsPage from "@/pages/settings";

describe("Settings", () => {
  it("renders AWS configuration", async () => {
    render(<SettingsPage />);
    await waitFor(() => { expect(screen.getByText("AWS configuration")).toBeInTheDocument(); });
  });

  it("renders notification preferences", async () => {
    render(<SettingsPage />);
    await waitFor(() => { expect(screen.getByText("Notification preferences")).toBeInTheDocument(); });
  });

  it("renders console links", async () => {
    render(<SettingsPage />);
    await waitFor(() => { expect(screen.getByText("Open CloudWatch Logs")).toBeInTheDocument(); });
  });
});
