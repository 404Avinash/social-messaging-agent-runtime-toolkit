// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Cards from "@cloudscape-design/components/cards";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import Link from "@cloudscape-design/components/link";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Popover from "@cloudscape-design/components/popover";
import Container from "@cloudscape-design/components/container";
import Table from "@cloudscape-design/components/table";
import { useState } from "react";
import { api } from "@/api/client";
import { PARTIAL_EMPTY_TEXT } from "@/api/log-read";
import { useApi } from "@/hooks/use-api";
import { UnreadableLogsAlert } from "@/components/unreadable-logs-alert";
import { stackId } from "@/types";
import type { StackStatus, LogEntry, LogRead, TemplateHealth } from "@/types";

const healthType = (h: string) =>
  h === "healthy" ? "success" : h === "recovering" ? "info" : h === "degraded" ? "warning" : "error";

export default function OverviewPage({ navigate }: { navigate: (href: string) => void }) {
  const { data: items, loading, error, refresh } = useApi<StackStatus[]>(() => api.listStacks());
  const { data: templateHealth, error: templateHealthError } = useApi<TemplateHealth>(() => api.getTemplateHealth());
  const [filterText, setFilterText] = useState("");
  // Absent from a backend that predates them, which read every template it listed.
  const uncheckedTemplates = templateHealth?.unchecked ?? [];
  const failedTemplateStacks = templateHealth?.failedStacks ?? [];

  const filtered = (items ?? []).filter((s) => {
    const id = stackId(s.stack);
    return id.toLowerCase().includes(filterText.toLowerCase()) ||
      s.stack.region.toLowerCase().includes(filterText.toLowerCase());
  });

  return (
    <SpaceBetween size="m">
      <Header
        variant="h1"
        description="Monitor and manage all your SMART WhatsApp agent stacks deployed across AWS."
        actions={<Button iconName="refresh" variant="icon" onClick={refresh} />}
      >
        Overview
      </Header>
      {error && <Alert type="error" header="Failed to load stacks">{error}</Alert>}
      {templateHealth && templateHealth.reclassified.length > 0 && (
        <Alert
          type="warning"
          header={`${templateHealth.reclassified.length} WhatsApp template(s) reclassified by Meta`}
          action={<Button onClick={() => navigate("#/templates")}>Review templates</Button>}
          dismissible
        >
          One or more templates have been moved out of their original category (usually UTILITY → MARKETING).
          Meta silently suppresses delivery for reclassified templates when the recipient has marketing opt-out.
          Affected stacks: <strong>{Array.from(new Set(templateHealth.reclassified.map((r) => r.stack))).join(", ")}</strong>.
        </Alert>
      )}
      {templateHealthError && (
        <Alert
          type="warning"
          header="Template health check failed"
          action={<Button onClick={() => navigate("#/templates")}>Review templates</Button>}
        >
          The console could not check whether Meta reclassified any WhatsApp template ({templateHealthError}), so reclassified
          templates may not be shown here. The Templates page checks each stack in a request of its own.
        </Alert>
      )}
      {(uncheckedTemplates.length > 0 || failedTemplateStacks.length > 0) && (
        <Alert
          type={failedTemplateStacks.length > 0 ? "warning" : "info"}
          header="Template health check incomplete"
          action={<Button onClick={() => navigate("#/templates")}>Review templates</Button>}
        >
          {uncheckedTemplates.length > 0 && (
            <>{uncheckedTemplates.length} template(s) not checked for reclassification: the request ran out of time against the limit of 10 template reads a second, or a read failed. </>
          )}
          {failedTemplateStacks.length > 0 && (
            <>Templates not listed, or not all of them, for <strong>{failedTemplateStacks.join(", ")}</strong>. </>
          )}
          A reclassified template among them would not be reported here. The Templates page checks each stack in a request of its own.
        </Alert>
      )}
      <Cards
        variant="full-page"
        stickyHeader
        loading={loading}
        loadingText="Loading stacks"
        items={filtered}
        trackBy={(s) => stackId(s.stack)}
        cardsPerRow={[{ cards: 1 }, { minWidth: 600, cards: 2 }, { minWidth: 992, cards: 3 }, { minWidth: 1400, cards: 4 }]}
        cardDefinition={{
          header: (s) => (
            <Link fontSize="heading-m" onFollow={(e) => { e.preventDefault(); navigate(`#/stacks/${stackId(s.stack)}`); }}>
              {stackId(s.stack)}
            </Link>
          ),
          sections: [
            { id: "desc", header: "Description", width: 100, content: (s) => s.stack.description || "-" },
            { id: "health", header: "Health status", width: 50, content: (s) => <Popover content={<div style={{ maxWidth: 340 }}>
            <div style={{ marginBottom: 6 }}>{s.healthDetail || s.health}</div>
            <div style={{ fontSize: 11, opacity: 0.6, borderTop: "1px solid #333", paddingTop: 6 }}>
              <div>✅ healthy — probe OK, clean metrics</div>
              <div>ℹ️ recovering — probe OK, past errors clearing</div>
              <div>⚠️ degraded — probe OK, elevated error rate</div>
              <div>❌ error / down — probe failed</div>
            </div>
          </div>} dismissButton={false} size="medium"><StatusIndicator type={healthType(s.health)}>{s.health}</StatusIndicator></Popover> },
            { id: "region", header: "Region", width: 50, content: (s) => s.stack.region },
            { id: "type", header: "Type", width: 25, content: (s) => <Badge color="blue">{s.stack.deployment_type}</Badge> },
            { id: "messages", header: "Webhook events (24h)", width: 50, content: (s) => s.messages24h.toLocaleString() },
            { id: "deploy", header: "Last deploy", width: 100, content: (s) => s.lastDeploy || "-" },
          ],
        }}
        header={
          <Header variant="awsui-h1-sticky" counter={`(${filtered.length})`} description="One card per deployed SMART stack.">
            Stacks
          </Header>
        }
        filter={<TextFilter filteringText={filterText} filteringPlaceholder="Find stacks by name or region" onChange={({ detail }) => setFilterText(detail.filteringText)} />}
        pagination={<Pagination currentPageIndex={1} pagesCount={1} />}
        empty={
          <Box textAlign="center" color="inherit">
            <Box fontWeight="bold" color="inherit" padding={{ bottom: "s" }}>No stacks found</Box>
            <Box variant="p" color="inherit">No SMART stacks have been registered yet. Deploy a stack and register its socialagent.yaml in the registry bucket.</Box>
          </Box>
        }
      />
      <RecentErrors />
    </SpaceBetween>
  );
}

function RecentErrors() {
  const { data, loading, error } = useApi<LogRead<LogEntry>>(() => api.searchLogs("", "ERROR", 60));
  const items = (data?.results ?? []).slice(0, 10);
  const unreadable = data?.unreadable ?? [];
  return (
    <Container header={<Header variant="h2" counter={items.length > 0 ? `(${items.length})` : ""} description="Errors from the last hour across all stacks.">Recent errors</Header>}>
      {/* A failed search found nothing, which is not the same as no errors;
          nor is a search that left a log group unread. */}
      {error ? <Alert type="error" header="Could not check for recent errors">{error}</Alert> : <SpaceBetween size="s">
        <UnreadableLogsAlert unreadable={unreadable} header="Recent errors may be incomplete" />
        <Table loading={loading} loadingText="Checking for errors..." items={items}
          columnDefinitions={[
            { id: "time", header: "Time", minWidth: 180, cell: (e) => <Box variant="small">{e.timestamp}</Box> },
            { id: "lambda", header: "Lambda", minWidth: 150, cell: (e) => <Badge color="red">{e.lambda}</Badge> },
            { id: "message", header: "Message", minWidth: 400, cell: (e) => <Box variant="small"><pre style={{ margin: 0, whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: "12px" }}>{e.message.slice(0, 300)}{e.message.length > 300 ? "..." : ""}</pre></Box> },
          ]}
          empty={<Box textAlign="center" color="inherit">{unreadable.length > 0
            ? <StatusIndicator type="warning">{PARTIAL_EMPTY_TEXT}</StatusIndicator>
            : <StatusIndicator type="success">No errors in the last hour</StatusIndicator>}</Box>}
        />
      </SpaceBetween>}
    </Container>
  );
}
