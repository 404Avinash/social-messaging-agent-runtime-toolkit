// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Answers of the routes that read CloudWatch Logs: log search, a stack's
 * errors and its events.
 *
 * They answered a bare list, and a failed read gave the same empty list as a
 * quiet one, so the console said "No errors" for logs it never read. They now
 * answer `{results, unreadable}`. This lives apart from the client so that
 * pages and tests that mock `@/api/client` use the real helpers.
 */
import type { LogRead, UnreadableLog } from "@/types";

/** The well-formed entries of an `unreadable` field, or none. */
export function unreadableList(value: unknown): UnreadableLog[] {
  if (!Array.isArray(value)) return [];
  return value.filter((u): u is UnreadableLog =>
    !!u && typeof u === "object" && typeof (u as UnreadableLog).lambda === "string" && typeof (u as UnreadableLog).reason === "string");
}

/**
 * A log route's answer as `{results, unreadable}`. A backend from before the
 * envelope, which a console mid-redeploy can still reach, answers the bare
 * list.
 */
export function toLogRead<T>(raw: unknown): LogRead<T> {
  if (Array.isArray(raw)) return { results: raw as T[], unreadable: [] };
  const body = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
  return {
    results: Array.isArray(body.results) ? body.results as T[] : [],
    unreadable: unreadableList(body.unreadable),
  };
}

/** "dev-bot-webhook (ThrottlingException), dev-bot-event (ran out of time or calls)" */
export function describeUnreadable(unreadable: UnreadableLog[]): string {
  return unreadable
    .map((u) => `${u.lambda} (${u.reason === "budget" ? "ran out of time or calls" : u.reason})`)
    .join(", ");
}

/**
 * What a list's empty state says while a log group is unread: nothing found
 * there is not "no errors" or "no events".
 */
export const PARTIAL_EMPTY_TEXT = "Nothing found in the logs that were read";
