// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { groupsFromClaim, hasGroup } from "@/hooks/use-groups";

describe("groupsFromClaim", () => {
  it("reads the list the ID token carries", () => {
    expect(groupsFromClaim(["Admin", " operator "])).toEqual(["admin", "operator"]);
  });

  it("reads the bracketed string API Gateway forwards", () => {
    // backend/shared/auth.py parses the same claim this way.
    expect(groupsFromClaim("[admin operator]")).toEqual(["admin", "operator"]);
  });

  it("is empty for a user in no group", () => {
    expect(groupsFromClaim(undefined)).toEqual([]);
    expect(groupsFromClaim("")).toEqual([]);
    expect(groupsFromClaim(42)).toEqual([]);
  });
});

describe("hasGroup", () => {
  // Hierarchical like the backend: a higher group includes the lower ones.
  it.each([
    [["admin"], "admin", true],
    [["admin"], "operator", true],
    [["operator"], "admin", false],
    [["operator"], "viewer", true],
    [["viewer"], "operator", false],
    [["support"], "viewer", false],
    [[], "viewer", false],
  ] as const)("%j has %s: %s", (groups, required, expected) => {
    expect(hasGroup([...groups], required)).toBe(expected);
  });
});
