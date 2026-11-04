// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useCallback } from "react";

export interface Route {
  page: string;
  param?: string;
}

/** Parse hash like "#/stacks/socialagent-dev" → { page: "stacks", param: "socialagent-dev" } */
function parseHash(hash: string): Route {
  const path = hash.replace(/^#\/?/, "") || "/";
  if (path === "/") return { page: "/" };
  const parts = path.split("/").filter(Boolean);
  return { page: parts[0], param: parts.slice(1).join("/") || undefined };
}

export function useHashRouter() {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = useCallback((href: string) => {
    window.location.hash = href;
  }, []);

  return { route, navigate };
}
