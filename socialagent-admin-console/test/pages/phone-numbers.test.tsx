// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import PhoneNumbersPage from "@/pages/phone-numbers";
import { api } from "@/api/client";
import { GroupsContext } from "@/hooks/use-groups";
import type { GlobalPhoneNumber } from "@/types";

const OLD_TOPIC = "arn:aws:sns:us-east-1:111122223333:dev-old-whatsapp";

const unbound: GlobalPhoneNumber = {
  phoneNumberId: "arn:aws:social-messaging:us-east-1:111122223333:phone-number-id/b2",
  displayPhone: "+1 555-0101", displayName: "Bot b2", qualityRating: "GREEN", metaPhoneNumberId: "meta-b2",
  wabaName: "Shop", wabaId: "waba-1", snsTopic: OLD_TOPIC, boundStack: "", wabaSharing: [],
};

afterEach(() => {
  vi.mocked(api.listAllPhoneNumbers).mockResolvedValue([]);
  vi.mocked(api.bindPhoneNumber).mockResolvedValue({ bound: true });
});

/** Open the bind dialog for the only row and pick the mock's one stack. */
async function openBindAndPickStack() {
  await screen.findByText(unbound.displayPhone);
  const table = createWrapper().findTable()!.getElement();
  fireEvent.click(within(table).getByRole("button", { name: "Bind to stack" }));
  const select = createWrapper().findModal()!.findContent().findSelect()!;
  select.openDropdown();
  select.selectOption(1);
}

describe("Phone numbers page", () => {
  it("shows a refused list as an error, not as no numbers linked", async () => {
    vi.mocked(api.listAllPhoneNumbers).mockRejectedValue(new Error("Forbidden: Requires 'operator' group membership"));
    render(<PhoneNumbersPage navigate={() => {}} />);

    expect(await screen.findByText(/Requires 'operator' group membership/)).toBeInTheDocument();
    expect(screen.queryByText("No WhatsApp phone numbers are linked in EUM Social.")).not.toBeInTheDocument();
  });

  it("names the event destination a bind takes over", async () => {
    vi.mocked(api.listAllPhoneNumbers).mockResolvedValue([unbound]);
    render(<PhoneNumbersPage navigate={() => {}} />);

    await openBindAndPickStack();

    const modal = createWrapper().findModal()!.getElement();
    expect(modal.textContent).toContain(OLD_TOPIC);
    expect(modal.textContent).toMatch(/stops receiving/);
  });

  it("warns when the bind could not set the WABA's event destination", async () => {
    vi.mocked(api.listAllPhoneNumbers).mockResolvedValue([unbound]);
    vi.mocked(api.bindPhoneNumber).mockResolvedValue({
      bound: true, mode: "exclusive_waba", eventDestinationUpdated: false,
      nextStep: "The WABA's event destination was not changed: setting it failed.",
      warning: "Pointing WABA waba-1's event destination at arn:sns failed.",
    });
    const { container } = render(<PhoneNumbersPage navigate={() => {}} />);

    await openBindAndPickStack();
    const modal = createWrapper().findModal()!.getElement();
    fireEvent.click(within(modal).getByRole("button", { name: "Bind to stack" }));

    await waitFor(() => {
      expect(createWrapper(container).findFlashbar()!.findItemsByType("warning")).toHaveLength(1);
    });
    expect(createWrapper(container).findFlashbar()!.findItemsByType("success")).toHaveLength(0);
  });

  describe("a WABA whose events go to a topic no stack uses", () => {
    // The mock's one stack publishes arn:sns, so OLD_TOPIC belongs to no stack:
    // the backend refuses the bind for anyone below admin.
    function bindButton() {
      const modal = createWrapper().findModal()!;
      return modal.findFooter()!.findAllButtons().find((b) => b.findTextRegion()?.getElement().textContent === "Bind to stack")!;
    }

    /** Cloudscape announces a disabled reason as the button's accessible description. */
    function disabledReason(b: ReturnType<typeof bindButton>) {
      const id = b.getElement().getAttribute("aria-describedby");
      return id ? document.getElementById(id)?.textContent ?? "" : "";
    }

    it("is an admin's call: an operator's bind is disabled, with the reason", async () => {
      vi.mocked(api.listAllPhoneNumbers).mockResolvedValue([unbound]);
      render(<GroupsContext.Provider value={["operator"]}><PhoneNumbersPage navigate={() => {}} /></GroupsContext.Provider>);

      await openBindAndPickStack();

      expect(createWrapper().findModal()!.getElement().textContent).toMatch(/No registered stack uses that topic/);
      expect(bindButton().isDisabled()).toBe(true);
      expect(disabledReason(bindButton())).toMatch(/Requires the admin group/);
    });

    it("leaves an admin's bind enabled", async () => {
      vi.mocked(api.listAllPhoneNumbers).mockResolvedValue([unbound]);
      render(<GroupsContext.Provider value={["admin"]}><PhoneNumbersPage navigate={() => {}} /></GroupsContext.Provider>);

      await openBindAndPickStack();

      expect(bindButton().isDisabled()).toBe(false);
    });

    it("does not apply to a topic a registered stack uses", async () => {
      vi.mocked(api.listAllPhoneNumbers).mockResolvedValue([{ ...unbound, snsTopic: "arn:sns" }]);
      render(<GroupsContext.Provider value={["operator"]}><PhoneNumbersPage navigate={() => {}} /></GroupsContext.Provider>);

      await openBindAndPickStack();

      expect(createWrapper().findModal()!.getElement().textContent).not.toMatch(/No registered stack uses that topic/);
      expect(bindButton().isDisabled()).toBe(false);
    });
  });
});
