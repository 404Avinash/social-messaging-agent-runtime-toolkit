// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { PropsWithChildren } from "react";

// Stand in for Amplify: a signed-in user whose cached ID token carries `groups`.
// A forced refresh would call Cognito and mint a token with the user's current
// groups, `current` when set.
let groups: unknown = ["operator"];
let current: unknown = undefined;
vi.mock("aws-amplify", () => ({ Amplify: { configure: vi.fn() } }));
vi.mock("aws-amplify/auth", () => ({
  fetchAuthSession: vi.fn(async (options?: { forceRefresh?: boolean }) => {
    if (options?.forceRefresh && current !== undefined) groups = current;
    return { tokens: { idToken: { payload: { "cognito:groups": groups }, toString: () => "id-token" } } };
  }),
}));
const { fetchAuthSession } = await import("aws-amplify/auth");
vi.mock("@aws-amplify/ui-react", () => {
  const Authenticator = ({ children }: PropsWithChildren) => <>{children}</>;
  Authenticator.Provider = ({ children }: PropsWithChildren) => <>{children}</>;
  return { Authenticator, useAuthenticator: () => ({ authStatus: "authenticated" }) };
});

const { AuthProvider } = await import("@/components/auth/AuthProvider");
const { usePermission } = await import("@/hooks/use-groups");

function AdminGate() {
  const admin = usePermission("admin");
  return <span>{admin.allowed ? "admin allowed" : admin.reason}</span>;
}

describe("AuthProvider", () => {
  beforeEach(() => {
    current = undefined;
    vi.mocked(fetchAuthSession).mockClear();
  });

  it("gives the page the groups from the ID token", async () => {
    groups = ["operator"];
    render(<AuthProvider><AdminGate /></AuthProvider>);
    expect(await screen.findByText(/Requires the admin group/)).toBeInTheDocument();
  });

  it("allows admin actions for a member of the admin group", async () => {
    groups = ["admin"];
    render(<AuthProvider><AdminGate /></AuthProvider>);
    expect(await screen.findByText("admin allowed")).toBeInTheDocument();
  });

  it("treats a token without the claim as a user in no group", async () => {
    groups = undefined;
    render(<AuthProvider><AdminGate /></AuthProvider>);
    expect(await screen.findByText(/Requires the admin group/)).toBeInTheDocument();
  });

  it("reads the groups from the cached session, without asking Cognito", async () => {
    // A forced refresh on page load called Cognito on every load, and any
    // refresh error but a network one made the Authenticator sign the user
    // out. A group granted since the token was minted shows once Amplify
    // refreshes it, within the hour, or at the next sign-in.
    groups = ["operator"];
    current = ["admin"];
    render(<AuthProvider><AdminGate /></AuthProvider>);
    expect(await screen.findByText(/Requires the admin group/)).toBeInTheDocument();
    expect(vi.mocked(fetchAuthSession).mock.calls.some(([options]) => options?.forceRefresh)).toBe(false);
  });
});
