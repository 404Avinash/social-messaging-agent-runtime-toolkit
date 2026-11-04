// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Box from "@cloudscape-design/components/box";

/**
 * Generic vertical flow diagram.
 *
 * Renders a linear sequence of steps with colored states (done/active/
 * failed/pending) and connecting lines. Click a step to trigger onStepClick
 * with its id — the parent can render additional detail.
 */

export interface FlowStep {
  id: string;
  title: string;
  icon: string;
  sub?: string;
}

export type StepState = "done" | "active" | "failed" | "pending";

interface FlowDiagramProps {
  steps: FlowStep[];
  state: Record<string, StepState>;
  selectedStepId?: string;
  onStepClick?: (stepId: string) => void;
}

const STATE_STYLES: Record<StepState, { border: string; bg: string; color: string; glow: string }> = {
  done:    { border: "#2DCCCD", bg: "#0a1a1a", color: "#2DCCCD", glow: "0 0 6px rgba(45,204,205,.2)" },
  active:  { border: "#f59e0b", bg: "#1f1a0a", color: "#f59e0b", glow: "0 0 8px rgba(245,158,11,.25)" },
  failed:  { border: "#ef4444", bg: "#1a0a0a", color: "#ef4444", glow: "0 0 6px rgba(239,68,68,.2)" },
  pending: { border: "#333", bg: "transparent", color: "#999", glow: "none" },
};

const NODE: React.CSSProperties = {
  width: 320,
  padding: "14px 18px",
  borderRadius: 8,
  borderWidth: 2,
  borderStyle: "solid",
  textAlign: "center",
  cursor: "pointer",
  transition: "all .2s",
  userSelect: "none",
};

const CONN: React.CSSProperties = {
  width: 2,
  height: 28,
  margin: "2px 0",
};

export function FlowDiagram({ steps, state, selectedStepId, onStepClick }: FlowDiagramProps) {
  if (!steps?.length) {
    return <Box color="text-status-inactive" textAlign="center" padding={{ vertical: "l" }}>No flow steps defined.</Box>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", padding: "24px 0" }}>
      {steps.map((step, i) => {
        const s = state[step.id] ?? "pending";
        const styles = STATE_STYLES[s];
        const isSelected = selectedStepId === step.id;
        return (
          <div key={step.id} style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
            <div
              role="button"
              tabIndex={0}
              onClick={() => onStepClick?.(step.id)}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") onStepClick?.(step.id); }}
              style={{
                ...NODE,
                borderColor: styles.border,
                background: styles.bg,
                color: styles.color,
                boxShadow: styles.glow,
                outline: isSelected ? "2px solid #6366f1" : "none",
                outlineOffset: 3,
              }}
            >
              <div style={{ fontSize: 22, marginBottom: 6 }}>{step.icon}</div>
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 3 }}>{step.title}</div>
              {step.sub && <div style={{ fontSize: 11, opacity: 0.7, whiteSpace: "pre-line" }}>{step.sub}</div>}
            </div>
            {i < steps.length - 1 && (
              <div
                style={{
                  ...CONN,
                  background: s === "done" ? "linear-gradient(180deg,#2DCCCD,rgba(45,204,205,.3))"
                    : s === "active" ? "linear-gradient(180deg,#f59e0b,rgba(245,158,11,.4))"
                      : s === "failed" ? "#ef4444"
                        : "#333",
                }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
