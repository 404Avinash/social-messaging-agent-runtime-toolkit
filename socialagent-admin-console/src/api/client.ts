// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * API client — calls the real backend. No mock data, no fallbacks.
 */
import type { StackStatus, Conversation, ConversationsPageResult, WhatsAppTemplate, StackEvent, DeployRecord, AdminSettings, ConversationMessage, ConversationTokenSummary, ConversationTranscript, TemplateDetail, TemplateHealth, ConfigUpdate, LambdaError, PhoneNumber, GlobalPhoneNumber, Schedule, StateEntry, LogEntry, LogRead, UnreadableLog, AdminUser } from "@/types";
import { stackId } from "@/types";
import { ApiError } from "@/api/errors";
import { describeUnreadable, toLogRead, unreadableList } from "@/api/log-read";

export { ApiError };

const API_BASE = import.meta.env.VITE_API_URL ?? "";

type TokenProvider = () => Promise<string | null>;

let _tokenProvider: TokenProvider = async () => null;

/**
 * Register how to obtain the bearer token for each request.
 *
 * Resolved per request rather than cached: the Cognito ID token is valid for one
 * hour, while Amplify keeps reporting `authenticated` until its refresh token
 * expires (24h), so a token captured at sign-in would 401 later requests.
 * Amplify's fetchAuthSession refreshes on demand, so asking it each time keeps
 * the token current.
 */
export function setAuthTokenProvider(provider: TokenProvider) {
  _tokenProvider = provider;
}

/** Authorization header for the current request, empty when unauthenticated. */
async function authHeaders(): Promise<Record<string, string>> {
  let token: string | null = null;
  try {
    token = await _tokenProvider();
  } catch {
    // Leave the header off and let the request 401 — clearer than a hang.
  }
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Reject with the backend's own error text when it provides one.
 *
 * Routes return `{"error": "..."}` with the reason a call failed — a Meta
 * template rejection, an IAM denial. Throwing only the HTTP status discarded that,
 * leaving operators with "API error: 502 Bad Gateway" for something the backend
 * had already explained. A 403 from require_group says "Forbidden" in `error`
 * and which group the route needs in `message`, so keep both. A log route that
 * could read no log group answers 502 with them in `unreadable`; the message
 * names them, so every page's error alert does.
 */
async function throwApiError(resp: Response, path: string): Promise<never> {
  let detail = "";
  let unreadable: UnreadableLog[] = [];
  try {
    const body = await resp.json();
    if (body && typeof body === "object" && typeof body.error === "string") {
      detail = typeof body.message === "string" && body.message ? `${body.error}: ${body.message}` : body.error;
      unreadable = unreadableList(body.unreadable);
      if (unreadable.length > 0) detail = `${detail}: ${describeUnreadable(unreadable)}`;
    }
  } catch { /* non-JSON body — fall back to the status */ }
  throw new ApiError(detail || `API error: ${resp.status} ${resp.statusText || path}`, resp.status, unreadable);
}

/**
 * Cross-stack lists skip a stack whose own call fails, but never a 403: the
 * caller's group is the same for every stack, so a refusal is not "this stack
 * has none", and an empty list would tell them there is nothing to see.
 */
function rethrowForbidden(e: unknown): void {
  if (e instanceof ApiError && e.status === 403) throw e;
}

async function get<T>(path: string): Promise<T> {
  const headers: Record<string, string> = { ...(await authHeaders()) };
  const resp = await fetch(`${API_BASE}${path}`, { headers });
  if (!resp.ok) await throwApiError(resp, path);
  return await resp.json() as T;
}

async function put<T>(path: string, body: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(await authHeaders()) };
  const resp = await fetch(`${API_BASE}${path}`, { method: "PUT", headers, body: JSON.stringify(body) });
  if (!resp.ok) await throwApiError(resp, path);
  return await resp.json() as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(await authHeaders()) };
  const resp = await fetch(`${API_BASE}${path}`, { method: "POST", headers, body: JSON.stringify(body) });
  if (!resp.ok) await throwApiError(resp, path);
  return await resp.json() as T;
}

async function del<T>(path: string): Promise<T> {
  const headers: Record<string, string> = { ...(await authHeaders()) };
  const resp = await fetch(`${API_BASE}${path}`, { method: "DELETE", headers });
  if (!resp.ok) await throwApiError(resp, path);
  return await resp.json() as T;
}

/**
 * Encode one dynamic path segment. Every value interpolated into a request path
 * goes through this.
 *
 * Segment values come from API responses, the URL hash and, for template names,
 * conversation text an end user wrote. Interpolated raw, a `/`, `?` or `#` in one
 * moves the request to another admin route under the operator's token.
 * encodeURIComponent keeps the value inside one segment, but `.` and `..` pass
 * through it unchanged and the URL parser still resolves them, so those are
 * refused, as is an empty segment.
 */
function segment(value: string): string {
  if (!value || value === "." || value === "..") throw new Error("Invalid path segment");
  return encodeURIComponent(value);
}

export const api = {
  async listStacks(): Promise<StackStatus[]> {
    return get("/stacks");
  },

  async getStack(id: string): Promise<StackStatus> {
    return get(`/stacks/${segment(id)}`);
  },

  async getConversationsPage(id: string, cursor?: string, pageSize = 25): Promise<ConversationsPageResult> {
    const params = new URLSearchParams();
    params.set("page_size", String(pageSize));
    if (cursor) params.set("cursor", cursor);
    const raw = await get<ConversationsPageResult | Conversation[]>(`/stacks/${segment(id)}/conversations?${params.toString()}`);
    // Normalize: tolerate a bare array (older backend) or a missing/non-array
    // `conversations` field so callers never see `undefined` for the list.
    if (Array.isArray(raw)) return { conversations: raw };
    return { conversations: Array.isArray(raw?.conversations) ? raw.conversations : [], nextToken: raw?.nextToken };
  },

  async getConversations(stackFilter?: string): Promise<Conversation[]> {
    // Drain every page for a single stack into a flat list. Used by callers that
    // need the complete set (cross-stack aggregation, nuclear reset) rather than
    // a single paginated page. Paginated UI should use getConversationsPage.
    const fetchAll = async (sid: string): Promise<Conversation[]> => {
      const acc: Conversation[] = [];
      let cursor: string | undefined;
      do {
        const page = await this.getConversationsPage(sid, cursor);
        acc.push(...page.conversations);
        cursor = page.nextToken;
      } while (cursor);
      return acc;
    };

    if (stackFilter && stackFilter !== "all") {
      return fetchAll(stackFilter);
    }
    const stacks = await this.listStacks();
    const all: Conversation[] = [];
    for (const s of stacks) {
      try {
        all.push(...await fetchAll(stackId(s.stack)));
      } catch (e) { rethrowForbidden(e); /* stack may not have conversations */ }
    }
    return all;
  },

  /**
   * Templates of one stack, or of every stack with the ids of those that failed.
   *
   * One stack's request can fail on its own: an EUM Social error, or a listing
   * too long for the console API's 30-second limit. Skipping it silently left the
   * Templates page looking complete with that stack's reclassified templates
   * missing, so its id goes in `failedStacks` for the page to name. With one stack
   * selected the error throws, as for any other request.
   */
  async listTemplates(stackFilter?: string): Promise<{ templates: WhatsAppTemplate[]; failedStacks: string[] }> {
    if (stackFilter && stackFilter !== "all") {
      return { templates: await get<WhatsAppTemplate[]>(`/stacks/${segment(stackFilter)}/templates`), failedStacks: [] };
    }
    const stacks = await this.listStacks();
    const templates: WhatsAppTemplate[] = [];
    const failedStacks: string[] = [];
    for (const s of stacks) {
      const id = stackId(s.stack);
      try {
        templates.push(...await get<WhatsAppTemplate[]>(`/stacks/${segment(id)}/templates`));
      } catch (e) {
        rethrowForbidden(e);
        failedStacks.push(id);
      }
    }
    return { templates, failedStacks };
  },

  /** Templates to pick from in a composer, where a stack that failed has none to offer. */
  async getTemplates(stackFilter?: string): Promise<WhatsAppTemplate[]> {
    return (await this.listTemplates(stackFilter)).templates;
  },

  async getTemplateHealth(): Promise<TemplateHealth> {
    return get("/templates/health");
  },

  async blueprintQuery<T = Record<string, unknown>>(stackId: string, eventType: string, data?: Record<string, unknown>): Promise<{ ok: boolean; data: T }> {
    return post(`/stacks/${segment(stackId)}/blueprint-query`, { event_type: eventType, data: data ?? {} });
  },

  /**
   * Events of one stack, or of every stack, with the event logs that were not
   * read in full.
   *
   * A stack whose event Lambda never ran answers no events, so a stack whose
   * call fails is not one without events: skipping it showed the Events page
   * complete, or empty, with its events missing. Its Lambdas go in
   * `unreadable`, as the backend's 502 names them, or the stack itself.
   */
  async getEvents(stackFilter?: string): Promise<LogRead<StackEvent>> {
    if (stackFilter && stackFilter !== "all") {
      return toLogRead<StackEvent>(await get<unknown>(`/stacks/${segment(stackFilter)}/events`));
    }
    const stacks = await this.listStacks();
    const all: LogRead<StackEvent> = { results: [], unreadable: [] };
    for (const s of stacks) {
      const id = stackId(s.stack);
      try {
        const read = toLogRead<StackEvent>(await get<unknown>(`/stacks/${segment(id)}/events`));
        all.results.push(...read.results);
        all.unreadable.push(...read.unreadable);
      } catch (e) {
        rethrowForbidden(e);
        if (e instanceof ApiError && e.unreadable.length > 0) all.unreadable.push(...e.unreadable);
        else all.unreadable.push({ lambda: id, reason: e instanceof Error ? e.message : String(e) });
      }
    }
    return all;
  },

  async getDeploys(id: string): Promise<DeployRecord[]> {
    return get(`/stacks/${segment(id)}/deploys`);
  },

  async getSettings(): Promise<AdminSettings> {
    return get("/settings");
  },

  async updateSettings(settings: AdminSettings): Promise<void> {
    await put("/settings", settings);
  },

  async getStackOptions(): Promise<{ label: string; value: string }[]> {
    const stacks = await this.listStacks();
    return [
      { label: "All stacks", value: "all" },
      ...stacks.map((s) => ({ label: stackId(s.stack), value: stackId(s.stack) })),
    ];
  },

  /**
   * A conversation's chat messages. The backend answers 502 when it could read
   * none of the conversation, which rejects like any failed call, and adds
   * `incomplete` to a 200 when it stopped short, which is passed on: both used
   * to read as a conversation with no messages.
   */
  async getConversationMessages(stackIdStr: string, phoneHash: string): Promise<ConversationTranscript> {
    const raw = await get<unknown>(`/stacks/${segment(stackIdStr)}/conversations/${segment(phoneHash)}/messages`);
    // The backend answers {messages, token_summary}; an older one answered a
    // bare array for a stack without memory. Its entries also carry each
    // turn's tool calls, tool results and reasoning, which were never sent to
    // the user: keep only the chat messages, or a tool result renders as raw
    // JSON from the bot.
    const body = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
    const entries: unknown[] = Array.isArray(raw) ? raw : Array.isArray(body.messages) ? body.messages : [];
    const messages = entries.filter((m): m is ConversationMessage => {
      if (!m || typeof m !== "object") return false;
      const e = m as Record<string, unknown>;
      return (e.role === "user" || e.role === "assistant") && typeof e.content === "string" && !e.thinking && !e.type;
    });
    const summary = body.token_summary;
    return {
      messages,
      tokenSummary: summary && typeof summary === "object" ? summary as ConversationTokenSummary : null,
      incomplete: typeof body.incomplete === "string" && body.incomplete ? body.incomplete : null,
    };
  },

  async getTemplateDetail(stackIdStr: string, templateName: string): Promise<TemplateDetail> {
    return get(`/stacks/${segment(stackIdStr)}/templates/${segment(templateName)}`);
  },

  async updateStackConfig(stackIdStr: string, config: ConfigUpdate): Promise<{ updated: string[] }> {
    return put(`/stacks/${segment(stackIdStr)}/config`, config);
  },

  async getErrors(stackIdStr: string): Promise<LogRead<LambdaError>> {
    return toLogRead<LambdaError>(await get<unknown>(`/stacks/${segment(stackIdStr)}/errors`));
  },

  async getPhoneNumbers(stackIdStr: string): Promise<PhoneNumber[]> {
    return get(`/stacks/${segment(stackIdStr)}/phone-numbers`);
  },

  async deleteConversation(stackIdStr: string, fullPhone: string): Promise<{ deleted: number }> {
    return del(`/stacks/${segment(stackIdStr)}/conversations/${segment(fullPhone)}`);
  },

  async deleteAllConversations(stackIdStr: string): Promise<{ deleted: number }> {
    return del(`/stacks/${segment(stackIdStr)}/conversations`);
  },

  async restartLambda(stackIdStr: string, lambdaName: string): Promise<{ restarted: string }> {
    return put(`/stacks/${segment(stackIdStr)}/restart-lambda/${segment(lambdaName)}`, {});
  },

  async restartRuntime(stackIdStr: string): Promise<{ restarted: string }> {
    return put(`/stacks/${segment(stackIdStr)}/restart-runtime`, {});
  },

  async nuclearReset(stackIdStr: string): Promise<{ conversations_deleted: number; runtime_restarted: boolean; lambdas_restarted: string[] }> {
    return put(`/stacks/${segment(stackIdStr)}/nuclear-reset`, {});
  },

  /**
   * Point the WABA of the stack's bound phone at an SNS topic (admin only).
   *
   * The backend resolves the WABA itself and only accepts a topic of this stack
   * or of a registered stack on the same WABA; omit the ARN to use the stack's own.
   */
  async updateSnsDestination(stackIdStr: string, snsTopicArn?: string): Promise<{ updated: boolean; wabaId: string; snsTopicArn: string }> {
    return put(`/stacks/${segment(stackIdStr)}/update-sns-destination`, snsTopicArn ? { snsTopicArn } : {});
  },

  async listAllPhoneNumbers(): Promise<GlobalPhoneNumber[]> {
    return get("/phone-numbers");
  },

  async bindPhoneNumber(phoneNumberId: string, stackId: string, wabaId: string): Promise<{ bound: boolean }> {
    return put("/phone-numbers/bind", { phoneNumberId, stackId, wabaId });
  },

  async unbindPhoneNumber(stackId: string): Promise<{ unbound: boolean }> {
    return put("/phone-numbers/unbind", { stackId });
  },

  async triggerEvent(stackId: string, payload: Record<string, unknown>): Promise<{ status: number; response: string; url: string; credentialWarnings?: string[] }> {
    return put(`/stacks/${segment(stackId)}/trigger-event`, { payload });
  },

  async listSchedules(): Promise<Schedule[]> {
    return get("/schedules");
  },

  async deleteSchedule(name: string): Promise<{ deleted: string }> {
    return del(`/schedules/${segment(name)}`);
  },

  async getState(stackId: string): Promise<StateEntry[]> {
    return get(`/stacks/${segment(stackId)}/state`);
  },

  /**
   * Delete one key from a stack's state table (admin only).
   *
   * Phone and key travel in the body. The phone keeps its `+` and keys carry `#`
   * (`counter#`, `claim#`); in a path both reach the backend still
   * percent-encoded, so the delete would match nothing and still report success.
   */
  async deleteStateKey(stackId: string, phone: string, key: string): Promise<{ deleted: boolean }> {
    return post(`/stacks/${segment(stackId)}/state/delete`, { phone, key });
  },

  async listAllState(): Promise<(StateEntry & { stack: string })[]> {
    return get("/state");
  },

  async searchLogs(stack: string, q: string, minutes: number): Promise<LogRead<LogEntry>> {
    return toLogRead<LogEntry>(await put<unknown>("/logs", { stack, q, minutes }));
  },

  async sendMessage(stackId: string, phone: string, text: string): Promise<{ sent: boolean }> {
    return put(`/stacks/${segment(stackId)}/send-message`, { phone, text, type: "text" });
  },

  async sendTemplateMessage(stackId: string, phone: string, templateName: string, language: string, parameters: string[]): Promise<{ sent: boolean }> {
    return put(`/stacks/${segment(stackId)}/send-message`, { phone, type: "template", templateName, language, parameters });
  },

  async listUsers(): Promise<AdminUser[]> {
    return get("/users");
  },

  async createUser(email: string): Promise<{ created: boolean }> {
    return put("/users", { email });
  },

  async deleteUser(username: string): Promise<{ deleted: boolean }> {
    return del(`/users/${segment(username)}`);
  },
};
