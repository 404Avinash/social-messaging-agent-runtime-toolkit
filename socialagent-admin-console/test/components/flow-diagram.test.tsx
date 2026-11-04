// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { FlowDiagram, type FlowStep, type StepState } from "@/components/flow-diagram";

const steps: FlowStep[] = [
  { id: "trigger", title: "Request received", icon: "📋" },
  { id: "partyA_req", title: "Party A request", icon: "📱" },
  { id: "partyB_req", title: "Party B request", icon: "📱" },
  { id: "completed", title: "Meeting completed", icon: "🏁" },
];

const state: Record<string, StepState> = {
  trigger: "done",
  partyA_req: "done",
  partyB_req: "active",
  completed: "pending",
};

describe("FlowDiagram", () => {
  it("renders all step titles", () => {
    render(<FlowDiagram steps={steps} state={state} />);
    expect(screen.getByText("Request received")).toBeInTheDocument();
    expect(screen.getByText("Party A request")).toBeInTheDocument();
    expect(screen.getByText("Party B request")).toBeInTheDocument();
    expect(screen.getByText("Meeting completed")).toBeInTheDocument();
  });

  it("renders all step icons", () => {
    render(<FlowDiagram steps={steps} state={state} />);
    expect(screen.getAllByText("📱")).toHaveLength(2);
    expect(screen.getByText("📋")).toBeInTheDocument();
    expect(screen.getByText("🏁")).toBeInTheDocument();
  });

  it("calls onStepClick with step id when clicked", () => {
    const onStepClick = vi.fn();
    render(<FlowDiagram steps={steps} state={state} onStepClick={onStepClick} />);
    fireEvent.click(screen.getByText("Party A request"));
    expect(onStepClick).toHaveBeenCalledWith("partyA_req");
  });

  it("renders nothing when no steps", () => {
    const { container } = render(<FlowDiagram steps={[]} state={{}} />);
    expect(container.textContent).toContain("No flow steps");
  });
});
