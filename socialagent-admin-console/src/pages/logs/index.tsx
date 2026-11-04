// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import Select from "@cloudscape-design/components/select";
import Input from "@cloudscape-design/components/input";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Container from "@cloudscape-design/components/container";
import { api } from "@/api/client";
import { PARTIAL_EMPTY_TEXT } from "@/api/log-read";
import { useApi } from "@/hooks/use-api";
import { UnreadableLogsAlert } from "@/components/unreadable-logs-alert";
import type { LogEntry, UnreadableLog } from "@/types";

const WINDOW_OPTIONS = [
  { label: "Last 15 minutes", value: "15" },
  { label: "Last hour", value: "60" },
  { label: "Last 6 hours", value: "360" },
  { label: "Last 24 hours", value: "1440" },
];

export default function LogsPage() {
  const { data: stackOptions } = useApi(() => api.getStackOptions());
  const [stack, setStack] = useState<{ label: string; value: string }>({ label: "All stacks", value: "all" });
  const [window, setWindow] = useState(WINDOW_OPTIONS[1]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<LogEntry[]>([]);
  const [unreadable, setUnreadable] = useState<UnreadableLog[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  const search = async () => {
    setLoading(true);
    setError(null);
    try {
      // The backend treats an empty stack id as "all stacks"; the option value
      // is "all", so map it back to "" before the request.
      const stackId = stack.value === "all" ? "" : stack.value;
      const read = await api.searchLogs(stackId, query.trim(), Number(window.value));
      setResults(read.results);
      setUnreadable(read.unreadable);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Log search failed");
      setResults([]);
      setUnreadable([]);
    } finally {
      setLoading(false);
      setSearched(true);
    }
  };

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error">{error}</Alert>}
      <UnreadableLogsAlert unreadable={unreadable} />
      <Container header={<Header variant="h2" description="Search CloudWatch logs across a stack's Lambdas (webhook, notification, scheduler, event).">Log search</Header>}>
        <SpaceBetween direction="horizontal" size="xs">
          <Select
            inlineLabelText="Stack"
            selectedOption={stack}
            options={stackOptions ?? [{ label: "All stacks", value: "all" }]}
            onChange={({ detail }) => setStack(detail.selectedOption as { label: string; value: string })}
          />
          <Select
            inlineLabelText="Time window"
            selectedOption={window}
            options={WINDOW_OPTIONS}
            onChange={({ detail }) => setWindow(detail.selectedOption as { label: string; value: string })}
          />
          <Input
            value={query}
            placeholder="Filter pattern (optional)"
            onChange={({ detail }) => setQuery(detail.value)}
            onKeyDown={({ detail }) => { if (detail.key === "Enter") search(); }}
          />
          <Button variant="primary" loading={loading} onClick={search}>Search</Button>
        </SpaceBetween>
      </Container>
      <Table
        variant="full-page" stickyHeader resizableColumns
        loading={loading} loadingText="Searching logs..."
        items={results} trackBy={(e) => `${e.timestamp}-${e.logGroup}-${e.message}`}
        header={<Header variant="awsui-h1-sticky" counter={`(${results.length})`}>Results</Header>}
        columnDefinitions={[
          { id: "timestamp", header: "Time", minWidth: 190, cell: (e) => new Date(e.timestamp).toLocaleString() },
          { id: "lambda", header: "Lambda", minWidth: 160, cell: (e) => <Badge color="blue">{e.lambda}</Badge> },
          { id: "message", header: "Message", minWidth: 400, cell: (e) => <Box variant="code">{e.message}</Box> },
        ]}
        empty={error
          // A search that read no log group (a 502) is not one that found nothing.
          ? <Box textAlign="center" color="inherit"><Box fontWeight="bold">Search failed</Box><Box>See the error above.</Box></Box>
          : unreadable.length > 0
          ? <Box textAlign="center" color="inherit"><Box fontWeight="bold">{PARTIAL_EMPTY_TEXT}</Box><Box>Some log groups were not read in full; see the warning above.</Box></Box>
          : <Box textAlign="center" color="inherit"><Box fontWeight="bold">{searched ? "No matching log events" : "No results yet"}</Box><Box>{searched ? "Try a wider time window or a different filter." : "Choose a stack and time window, then Search."}</Box></Box>}
      />
    </SpaceBetween>
  );
}
