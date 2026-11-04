// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "@/App";

// The api module is globally mocked via test/setup.ts (vi.mock("@/api/client")),
// resolving to src/api/__mocks__/client.ts. Auth needs an Amplify provider, so
// stub the hook instead.
vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({ email: "operator@example.com", signOut: () => {} }),
}));

describe("App breadcrumbs", () => {
  afterEach(() => {
    window.location.hash = "";
  });

  it.each(["__proto__", "constructor"])("labels an unknown page %s with its own name", async (page) => {
    // The page comes from the URL hash, so a crafted link can name any key. A
    // plain lookup read Object.prototype members as labels, and an object as a
    // breadcrumb's text failed the render.
    window.location.hash = `#/${page}`;
    render(<App />);
    // The breadcrumb group renders its current item more than once.
    expect((await screen.findAllByText(page)).length).toBeGreaterThan(0);
    // An unknown page falls back to Overview.
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });
});
