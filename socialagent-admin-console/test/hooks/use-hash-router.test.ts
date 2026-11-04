// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useHashRouter } from "@/hooks/use-hash-router";

describe("useHashRouter", () => {
  beforeEach(() => { window.location.hash = ""; });
  afterEach(() => { window.location.hash = ""; });

  it("defaults to root route when no hash", () => {
    const { result } = renderHook(() => useHashRouter());
    expect(result.current.route).toEqual({ page: "/" });
  });

  it("parses simple page route", () => {
    window.location.hash = "#/stacks";
    const { result } = renderHook(() => useHashRouter());
    expect(result.current.route).toEqual({ page: "stacks", param: undefined });
  });

  it("parses route with param", () => {
    window.location.hash = "#/stacks/socialagent-dev";
    const { result } = renderHook(() => useHashRouter());
    expect(result.current.route).toEqual({ page: "stacks", param: "socialagent-dev" });
  });

  it("updates route on hashchange", async () => {
    const { result } = renderHook(() => useHashRouter());
    expect(result.current.route.page).toBe("/");

    act(() => {
      window.location.hash = "#/templates";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });

    expect(result.current.route).toEqual({ page: "templates", param: undefined });
  });

  it("navigate() changes the hash", () => {
    const { result } = renderHook(() => useHashRouter());
    act(() => { result.current.navigate("#/settings"); });
    expect(window.location.hash).toBe("#/settings");
  });
});
