// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Box from "@cloudscape-design/components/box";

/** Generic kanban board component. Columns are declarative, items get placed by column id. */

export interface KanbanColumn {
  id: string;
  label: string;
  color?: "blue" | "orange" | "green" | "red" | "purple" | "grey";
}

export interface KanbanItem {
  column: string;
  title: string;
  subtitle?: string;
  body?: string;
}

interface KanbanBoardProps {
  columns: KanbanColumn[];
  items: KanbanItem[];
  onItemClick?: (item: KanbanItem) => void;
}

const COLOR_MAP: Record<string, { header: string; border: string; accent: string }> = {
  blue:   { header: "#0a1a2f", border: "#152a45", accent: "#0C5FB2" },
  orange: { header: "#1f1a0a", border: "#3a2a1a", accent: "#f59e0b" },
  green:  { header: "#0a1f1f", border: "#153535", accent: "#2DCCCD" },
  red:    { header: "#1f0a0a", border: "#3a1a1a", accent: "#ef4444" },
  purple: { header: "#0f0a1f", border: "#1f1a3f", accent: "#a78bfa" },
  grey:   { header: "#1a1a1a", border: "#2a2a2a", accent: "#888" },
};

export function KanbanBoard({ columns, items, onItemClick }: KanbanBoardProps) {
  if (!columns?.length) {
    return <Box color="text-status-inactive" textAlign="center" padding={{ vertical: "l" }}>No kanban columns defined.</Box>;
  }

  return (
    <div style={{ display: "flex", gap: 12, overflowX: "auto", paddingBottom: 8, minHeight: 300 }}>
      {columns.map((col) => {
        const colItems = items.filter((i) => i.column === col.id);
        const c = COLOR_MAP[col.color ?? "grey"] ?? COLOR_MAP.grey;
        return (
          <div key={col.id} style={{ flex: "1 1 220px", minWidth: 220 }}>
            <div style={{
              padding: "10px 12px",
              borderRadius: "8px 8px 0 0",
              background: c.header,
              border: `1px solid ${c.border}`,
              borderBottom: `2px solid ${c.accent}`,
              color: c.accent,
              fontSize: 12,
              fontWeight: 600,
              display: "flex",
              justifyContent: "space-between",
            }}>
              <span>{col.label}</span>
              <span style={{ opacity: 0.7, fontWeight: 400 }}>{colItems.length}</span>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "8px 0" }}>
              {colItems.map((it, idx) => (
                <div
                  key={`${col.id}-${idx}`}
                  role={onItemClick ? "button" : undefined}
                  tabIndex={onItemClick ? 0 : undefined}
                  onClick={() => onItemClick?.(it)}
                  onKeyDown={(e) => { if (onItemClick && (e.key === "Enter" || e.key === " ")) onItemClick(it); }}
                  style={{
                    background: "#111827",
                    border: "1px solid #1e2d3d",
                    borderRadius: 8,
                    padding: 12,
                    fontSize: 12,
                    cursor: onItemClick ? "pointer" : "default",
                    transition: "border-color .2s",
                  }}
                  onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.borderColor = "#333"; }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.borderColor = "#1e2d3d"; }}
                >
                  <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>{it.title}</div>
                  {it.subtitle && <div style={{ color: "#6b7f95", fontSize: 11, marginBottom: 2 }}>{it.subtitle}</div>}
                  {it.body && (
                    <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid #1e2d3d", color: "#6b7f95", fontSize: 11 }}>
                      {it.body}
                    </div>
                  )}
                </div>
              ))}
              {colItems.length === 0 && (
                <div style={{ padding: 16, textAlign: "center", color: "#475569", fontSize: 11 }}>
                  Empty
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
