// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { UnreadableLog } from "@/types";

/**
 * A failed API call, with the HTTP status so callers can tell a 403 or a 404
 * apart. `name` stays "Error": pages show `${e}`, so a new name would change
 * every message they display.
 *
 * `unreadable` carries the log groups a log route's 502 lists, so a
 * cross-stack list can name them rather than the stack alone.
 *
 * It lives apart from the client so that code which tells statuses apart
 * still sees the real class where the tests mock `@/api/client`.
 */
export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly unreadable: UnreadableLog[] = []) {
    super(message);
  }
}
