// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import type { ReactElement } from "react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import StackDetailPage from "@/pages/stack-detail";
import ConversationsPage from "@/pages/conversations";
import SchedulesPage from "@/pages/schedules";
import StatePage from "@/pages/state";
import SettingsPage from "@/pages/settings";
import { ConversationDetailModal } from "@/components/conversation-detail-modal";
import { GroupsContext } from "@/hooks/use-groups";
import { api } from "@/api/client";
import type { Conversation, Schedule } from "@/types";

// The delete routes, nuclear reset, the SNS destination and settings writes are
// admin-only (backend/tests/unit/test_route_authorization.py). Offering them to
// an operator only to have the backend refuse them reads as a broken console.

const conversation: Conversation = {
  phoneHash: "hash-1", fullPhone: "+15550000001", stack: "test-dev", region: "us-east-1",
  messageCount: 3, lastActivity: "2024-07-01", sessionStatus: "active", memoryId: "mem-1",
};
const schedule: Schedule = {
  name: "socialagent-test-dev-reminder-555", stack: "test-dev", state: "ENABLED",
  expression: "cron(0 9 * * ? *)", type: "recurring",
  targetArn: "arn:aws:lambda:us-east-1:111122223333:function:test-dev-scheduler",
  operation: "reminder", phone: "+15550000000", payload: {}, createdAt: "2024-07-01", lastModified: "2024-07-01",
};
const stateEntry = { phone: "+15550000000", key: "name", value: "Ana", ttl: 0 };

beforeEach(() => {
  vi.mocked(api.getConversationsPage).mockResolvedValue({ conversations: [conversation] });
  vi.mocked(api.getConversations).mockResolvedValue([conversation]);
  vi.mocked(api.listSchedules).mockResolvedValue([schedule]);
  vi.mocked(api.getState).mockResolvedValue([stateEntry]);
  vi.mocked(api.listAllState).mockResolvedValue([{ ...stateEntry, stack: "test-dev" }]);
  vi.mocked(api.getPhoneNumbers).mockResolvedValue([{
    stack: "test-dev", phoneNumberId: "arn:aws:social-messaging:us-east-1:111122223333:phone-number-id/abc",
    displayPhone: "+1 555 000 0000", displayName: "Bot", qualityRating: "GREEN", wabaId: "waba-1", wabaName: "WABA",
    metaPhoneNumberId: "123", snsTopic: "arn:aws:sns:us-east-1:111122223333:t",
  } as never]);
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  vi.mocked(api.getConversationsPage).mockResolvedValue({ conversations: [] });
  vi.mocked(api.getConversations).mockResolvedValue([]);
  vi.mocked(api.listSchedules).mockResolvedValue([]);
  vi.mocked(api.getState).mockResolvedValue([]);
  vi.mocked(api.listAllState).mockResolvedValue([]);
  vi.mocked(api.getPhoneNumbers).mockResolvedValue([]);
});

function renderAs(groups: string[], ui: ReactElement) {
  return render(<GroupsContext.Provider value={groups}>{ui}</GroupsContext.Provider>);
}

/**
 * The Cloudscape button labelled `text`, in a modal or outside one. jsdom loads
 * no CSS, so a closed modal's buttons are in the DOM and count as visible.
 */
async function button(text: string, { inModal = false } = {}) {
  let found: ReturnType<ReturnType<typeof createWrapper>["findButton"]> = null;
  await waitFor(() => {
    found = createWrapper().findAllButtons().find((b) =>
      b.findTextRegion()?.getElement().textContent === text
      && !!b.getElement().closest("[role=dialog]") === inModal) ?? null;
    expect(found).not.toBeNull();
  });
  return found!;
}

/** Cloudscape announces a disabled reason as the button's accessible description. */
function disabledReason(b: NonNullable<Awaited<ReturnType<typeof button>>>) {
  const id = b.getElement().getAttribute("aria-describedby");
  return id ? document.getElementById(id)?.textContent ?? null : null;
}

async function openStackTab(groups: string[], label: string) {
  renderAs(groups, <StackDetailPage stackId="test-dev" navigate={() => {}} />);
  await waitFor(() => { expect(screen.getByText("Stack summary")).toBeInTheDocument(); });
  if (label !== "Status") fireEvent.click(screen.getByRole("tab", { name: label }));
}

async function selectFirstRow() {
  await waitFor(() => {
    expect(createWrapper().findTable()?.findRowSelectionArea(1)).toBeTruthy();
  });
  createWrapper().findTable()!.findRowSelectionArea(1)!.click();
}

describe("admin-only actions for an operator", () => {
  it("disables Nuclear reset and says why", async () => {
    await openStackTab(["operator"], "Status");
    const actions = createWrapper().findButtonDropdown()!;
    actions.openDropdown();
    expect(actions.findItemById("nuclear", { disabled: true })).not.toBeNull();
    expect(actions.findOpenDropdown()!.getElement().textContent).toMatch(/Requires the admin group/);
  });

  it("disables Delete all conversations", async () => {
    await openStackTab(["operator"], "Conversations");
    await screen.findByText("hash-1");
    const b = await button("Delete all");
    expect(b.isDisabled()).toBe(true);
    expect(disabledReason(b)).toMatch(/Requires the admin group/);
  });

  it("disables cancelling schedules on the Schedules tab", async () => {
    await openStackTab(["operator"], "Schedules");
    await selectFirstRow();
    const b = await button("Cancel selected (1)");
    expect(b.isDisabled()).toBe(true);
    expect(disabledReason(b)).toMatch(/Requires the admin group/);
  });

  it("disables deleting state keys on the State tab", async () => {
    await openStackTab(["operator"], "State");
    await selectFirstRow();
    expect((await button("Delete selected (1)")).isDisabled()).toBe(true);
  });

  it("disables changing the SNS destination", async () => {
    // An inline link cannot carry a disabled reason, so the dialog still opens
    // to show the current topic and its update button says why it is off.
    await openStackTab(["operator"], "Phone numbers");
    fireEvent.click((await button("Change")).getElement());
    const b = await button("Update destination", { inModal: true });
    expect(b.isDisabled()).toBe(true);
    expect(disabledReason(b)).toMatch(/Requires the admin group/);
  });

  it("disables deleting conversations on the Conversations page", async () => {
    renderAs(["operator"], <ConversationsPage navigate={() => {}} />);
    await selectFirstRow();
    expect((await button("Delete selected (1)")).isDisabled()).toBe(true);
  });

  it("disables cancelling schedules on the Schedules page", async () => {
    renderAs(["operator"], <SchedulesPage navigate={() => {}} />);
    await selectFirstRow();
    expect((await button("Cancel selected (1)")).isDisabled()).toBe(true);
  });

  it("disables deleting state keys on the State page", async () => {
    renderAs(["operator"], <StatePage navigate={() => {}} />);
    await selectFirstRow();
    expect((await button("Delete selected (1)")).isDisabled()).toBe(true);
  });

  it("disables saving settings", async () => {
    renderAs(["operator"], <SettingsPage />);
    const b = await button("Save settings");
    expect(b.isDisabled()).toBe(true);
    expect(disabledReason(b)).toMatch(/Requires the admin group/);
  });

  it("disables deleting a conversation from its transcript", async () => {
    renderAs(["operator"],
      <ConversationDetailModal visible onDismiss={() => {}} stackId="test-dev" phoneHash="hash-1" fullPhone="+15550000001" />);
    const b = await button("Delete conversation", { inModal: true });
    expect(b.isDisabled()).toBe(true);
    expect(disabledReason(b)).toMatch(/Requires the admin group/);
  });
});

describe("admin-only actions for an admin", () => {
  it("leaves Nuclear reset enabled", async () => {
    await openStackTab(["admin"], "Status");
    const actions = createWrapper().findButtonDropdown()!;
    actions.openDropdown();
    expect(actions.findItemById("nuclear")!.getElement().getAttribute("aria-disabled")).not.toBe("true");
  });

  it("leaves the deletes enabled", async () => {
    renderAs(["admin"], <StatePage navigate={() => {}} />);
    await selectFirstRow();
    expect((await button("Delete selected (1)")).isDisabled()).toBe(false);
  });

  it("leaves saving settings enabled", async () => {
    renderAs(["admin"], <SettingsPage />);
    expect((await button("Save settings")).isDisabled()).toBe(false);
  });
});
