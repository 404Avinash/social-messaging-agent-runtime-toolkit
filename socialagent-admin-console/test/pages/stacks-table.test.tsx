// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import StacksPage from "@/pages/stacks";

describe("Stacks table", () => {
  it("shows stack from API", async () => {
    render(<StacksPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("test-dev")).toBeInTheDocument(); });
  });

  it("shows counter", async () => {
    render(<StacksPage navigate={() => {}} />);
    await waitFor(() => { expect(screen.getByText("(1)")).toBeInTheDocument(); });
  });
});
