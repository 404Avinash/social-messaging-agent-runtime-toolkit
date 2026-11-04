// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import TemplatesPage from "@/pages/templates";
import ConversationsPage from "@/pages/conversations";
import EventsPage from "@/pages/events";

describe("Cross-stack tables", () => {
  it("Templates renders with empty data", async () => {
    render(<TemplatesPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Templates")).toBeInTheDocument(); });
  });

  it("Conversations renders with empty data", async () => {
    render(<ConversationsPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Conversations")).toBeInTheDocument(); });
  });

  it("Events renders with empty data", async () => {
    render(<EventsPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("Events")).toBeInTheDocument(); });
  });
});
