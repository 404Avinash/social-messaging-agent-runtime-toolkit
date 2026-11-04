// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { vi } from "vitest";
import type { StackStatus, AdminSettings } from "@/types";

const mockStack: StackStatus = {
  stack: {
    schema_version: 1, project: "test", region: "us-east-1", environment: "dev",
    deployment_type: "docker", phone_number_id: "arn:aws:social-messaging:us-east-1:123:phone/abc",
    features: { scheduler: true, state_table: true, events_api: true },
    outputs: { runtime_arn: "arn:runtime", runtime_id: "rt-123", memory_id: "mem-123", state_table_name: "test-state", sns_topic_arn: "arn:sns", event_api_url: "https://api.example.com", webhook_lambda: "test-webhook", notification_lambda_arn: "arn:lambda:notif", scheduler_role_arn: "arn:iam:sched", ecr_repository_url: "123.dkr.ecr.us-east-1.amazonaws.com/test" },
  },
  health: "healthy", healthDetail: "All 2 Lambda functions healthy", runtimeStatus: "running", messages24h: 100, lastDeploy: "2024-07-01", lambdas: [
    { name: "webhook", invocations: 100, errors: 0, errorRate: "0.00%", lastInvocation: "2024-07-01", status: "healthy", probeStatus: "pass", userInvocations: 100, probeInvocations: 0 },
    { name: "notification", invocations: 50, errors: 0, errorRate: "0.00%", lastInvocation: "2024-07-01", status: "healthy", probeStatus: "pass", userInvocations: 100, probeInvocations: 0 },
  ],
};

const mockSettings: AdminSettings = {
  awsProfile: "default", defaultRegion: "us-east-1", slackWebhookUrl: "", notificationEmail: "", degradationAlerts: true, deploymentFailureAlerts: true,
};

export const api = {
  listStacks: vi.fn().mockResolvedValue([mockStack]),
  listAllPhoneNumbers: vi.fn().mockResolvedValue([]),
  listAllState: vi.fn().mockResolvedValue([]),
  getState: vi.fn().mockResolvedValue([]),
  deleteStateKey: vi.fn().mockResolvedValue({ deleted: true }),
  restartLambda: vi.fn().mockResolvedValue({ restarted: "test" }),
  restartRuntime: vi.fn().mockResolvedValue({ restarted: "test" }),
  bindPhoneNumber: vi.fn().mockResolvedValue({ bound: true }),
  unbindPhoneNumber: vi.fn().mockResolvedValue({ unbound: true }),
  listSchedules: vi.fn().mockResolvedValue([]),
  deleteSchedule: vi.fn().mockResolvedValue({ deleted: "test" }),
  searchLogs: vi.fn().mockResolvedValue({ results: [], unreadable: [] }),
  sendMessage: vi.fn().mockResolvedValue({ sent: true }),
  sendTemplateMessage: vi.fn().mockResolvedValue({ sent: true }),
  listUsers: vi.fn().mockResolvedValue([]),
  createUser: vi.fn().mockResolvedValue({ created: true }),
  deleteUser: vi.fn().mockResolvedValue({ deleted: true }),
  getStack: vi.fn().mockResolvedValue(mockStack),
  getConversations: vi.fn().mockResolvedValue([]),
  getConversationsPage: vi.fn().mockResolvedValue({ conversations: [] }),
  getConversationMessages: vi.fn().mockResolvedValue({ messages: [], tokenSummary: null, incomplete: null }),
  getErrors: vi.fn().mockResolvedValue({ results: [], unreadable: [] }),
  getPhoneNumbers: vi.fn().mockResolvedValue([]),
  deleteConversation: vi.fn().mockResolvedValue({ deleted: 0 }),
  deleteAllConversations: vi.fn().mockResolvedValue({ deleted: 0 }),
  listTemplates: vi.fn().mockResolvedValue({ templates: [], failedStacks: [] }),
  getTemplates: vi.fn().mockResolvedValue([]),
  getTemplateDetail: vi.fn().mockRejectedValue(new Error("Template not found")),
  getTemplateHealth: vi.fn().mockResolvedValue({ reclassified: [], nonApproved: [], total: 0, unchecked: [], failedStacks: [] }),
  blueprintQuery: vi.fn().mockResolvedValue({ ok: true, data: {} }),
  getEvents: vi.fn().mockResolvedValue({ results: [], unreadable: [] }),
  getDeploys: vi.fn().mockResolvedValue([]),
  getSettings: vi.fn().mockResolvedValue(mockSettings),
  updateSettings: vi.fn().mockResolvedValue(undefined),
  getStackOptions: vi.fn().mockResolvedValue([{ label: "All stacks", value: "all" }, { label: "test-dev", value: "test-dev" }]),
};

export function setAuthTokenProvider(_provider: () => Promise<string | null>) {}
