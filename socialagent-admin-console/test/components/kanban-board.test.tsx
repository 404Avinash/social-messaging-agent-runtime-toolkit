// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { KanbanBoard, type KanbanColumn, type KanbanItem } from "@/components/kanban-board";

const columns: KanbanColumn[] = [
  { id: "pending", label: "Pending", color: "orange" },
  { id: "active", label: "Active", color: "blue" },
  { id: "done", label: "Done", color: "green" },
];

const items: KanbanItem[] = [
  { column: "pending", title: "Task 1", subtitle: "User A", body: "Detail A" },
  { column: "active", title: "Task 2", subtitle: "User B" },
  { column: "done", title: "Task 3", body: "Detail C" },
];

describe("KanbanBoard", () => {
  it("renders all column labels with counts", () => {
    render(<KanbanBoard columns={columns} items={items} />);
    // Each label appears alongside its item count like "Pending (1)"
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("places items under their declared column", () => {
    render(<KanbanBoard columns={columns} items={items} />);
    expect(screen.getByText("Task 1")).toBeInTheDocument();
    expect(screen.getByText("Task 2")).toBeInTheDocument();
    expect(screen.getByText("Task 3")).toBeInTheDocument();
  });

  it("renders optional subtitle and body when present", () => {
    render(<KanbanBoard columns={columns} items={items} />);
    expect(screen.getByText("User A")).toBeInTheDocument();
    expect(screen.getByText("Detail A")).toBeInTheDocument();
  });

  it("calls onItemClick with the item when clicked", () => {
    const onItemClick = vi.fn();
    render(<KanbanBoard columns={columns} items={items} onItemClick={onItemClick} />);
    fireEvent.click(screen.getByText("Task 1"));
    expect(onItemClick).toHaveBeenCalledWith(items[0]);
  });

  it("shows empty placeholder when no columns defined", () => {
    render(<KanbanBoard columns={[]} items={[]} />);
    expect(screen.getByText("No kanban columns defined.")).toBeInTheDocument();
  });
});
