// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The signed-in user's Cognito groups, so the UI can disable what the backend
 * would refuse.
 *
 * Read from the ID token's `cognito:groups` claim, the one the backend falls
 * back to (backend/shared/auth.py). The backend normally asks Cognito live, so
 * the claim can lag a group change until the token refreshes (within the hour,
 * or at the next sign-in). That is fine for disabling buttons: the backend
 * still decides every request, and its 403 is shown when the two disagree.
 */
import { createContext, useContext } from "react";

export type Group = "admin" | "operator" | "viewer";

// Mirrors GROUP_LEVELS in backend/shared/auth.py: a higher group includes the lower ones.
const GROUP_LEVELS: Record<Group, number> = { admin: 3, operator: 2, viewer: 1 };

/** Group names from a `cognito:groups` claim: a list, or "[a b]" as API Gateway forwards it. */
export function groupsFromClaim(raw: unknown): string[] {
  const list = Array.isArray(raw) ? raw : typeof raw === "string" ? raw.replace(/[[\],]/g, " ").split(/\s+/) : [];
  return list.map((g) => String(g).trim().toLowerCase()).filter(Boolean);
}

/** Whether any of `groups` meets or exceeds `required`. */
export function hasGroup(groups: string[], required: Group): boolean {
  return groups.some((g) => (GROUP_LEVELS[g as Group] ?? 0) >= GROUP_LEVELS[required]);
}

/**
 * The signed-in user's groups. `null` means unknown (no provider above), which
 * allows everything and leaves the decision to the backend.
 */
export const GroupsContext = createContext<string[] | null>(null);

/** Whether the user may take an action that requires `required`, and if not, why. */
export function usePermission(required: Group): { allowed: boolean; reason?: string } {
  const groups = useContext(GroupsContext);
  if (groups === null || hasGroup(groups, required)) return { allowed: true };
  return { allowed: false, reason: `Requires the ${required} group. Sign in again if you were just added to it.` };
}
