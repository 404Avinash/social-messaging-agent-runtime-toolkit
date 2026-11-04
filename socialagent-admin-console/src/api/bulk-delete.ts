// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * One-at-a-time deletes that report what actually happened.
 *
 * Every delete route is admin-only, so an operator's bulk delete is refused
 * item by item. Counting before the await, or swallowing the rejection, turned
 * that into "Deleted 3 key(s)" while every key was still there.
 */
import { api } from "@/api/client";
import { ApiError } from "@/api/errors";

/**
 * What a `remove` resolves to when the backend found nothing to delete, so
 * nothing was deleted for that item.
 */
export const NOT_FOUND = Symbol("not found");

/** What a run of deletes achieved. */
export interface DeleteOutcome {
  total: number;
  deleted: number;
  /** Items the backend found nothing under, so nothing was deleted for them. */
  notFound: number;
  failed: number;
  /** The first failure's message; with a 403 it is every failure's. */
  reason: string | null;
}

/** A flash a bulk delete shows: the counts, and with a failure the reason under them. */
export interface DeleteFlash {
  type: "success" | "warning" | "error";
  header?: string;
  content: string;
}

/** Message of a caught value, for showing to the user. */
export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Delete `items` in order, counting an item only once its `remove` resolves. */
export async function deleteEach<T>(items: T[], remove: (item: T, index: number) => Promise<unknown>): Promise<DeleteOutcome> {
  let deleted = 0;
  let notFound = 0;
  let failed = 0;
  let reason: string | null = null;
  for (let i = 0; i < items.length; i++) {
    try {
      if (await remove(items[i], i) === NOT_FOUND) notFound++;
      else deleted++;
    } catch (e) {
      failed++;
      reason ??= errorText(e);
    }
  }
  return { total: items.length, deleted, notFound, failed, reason };
}

/** ", 1 not found, 2 failed", leaving out a count that is zero. */
export function problemCounts(outcome: DeleteOutcome): string {
  return `${outcome.notFound > 0 ? `, ${outcome.notFound} not found` : ""}${outcome.failed > 0 ? `, ${outcome.failed} failed` : ""}`;
}

/**
 * The flash for a bulk delete, such as "Deleted 1 key(s), 1 not found, 1 failed".
 * Any failure makes it an error with the reason under the counts, and an item
 * not found makes it a warning: success only when every item was deleted.
 */
export function deleteFlash(outcome: DeleteOutcome, noun: string): DeleteFlash {
  const counts = `Deleted ${outcome.deleted} ${noun}${problemCounts(outcome)}`;
  if (outcome.failed > 0) return { type: "error", header: counts, content: outcome.reason ?? "" };
  return { type: outcome.notFound > 0 ? "warning" : "success", content: counts };
}

/**
 * Delete one state key. The backend answers `deleted: false` when DynamoDB
 * had no item under the key; that resolves to NOT_FOUND.
 */
export async function deleteStateKey(stackId: string, phone: string, key: string): Promise<typeof NOT_FOUND | undefined> {
  const result = await api.deleteStateKey(stackId, phone, key);
  return result.deleted === false ? NOT_FOUND : undefined;
}

/**
 * Delete one schedule. A one-shot schedule deletes itself once it fires, so
 * one listed a moment ago can be gone; the backend answers 404 for that, and
 * it resolves to NOT_FOUND.
 */
export async function deleteSchedule(name: string): Promise<typeof NOT_FOUND | undefined> {
  try {
    await api.deleteSchedule(name);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return NOT_FOUND;
    throw e;
  }
  return undefined;
}
