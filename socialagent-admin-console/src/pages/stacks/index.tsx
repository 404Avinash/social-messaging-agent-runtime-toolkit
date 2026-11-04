// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import TextFilter from "@cloudscape-design/components/text-filter";
import Pagination from "@cloudscape-design/components/pagination";
import CollectionPreferences from "@cloudscape-design/components/collection-preferences";
import Link from "@cloudscape-design/components/link";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Alert from "@cloudscape-design/components/alert";
import Popover from "@cloudscape-design/components/popover";
import { api } from "@/api/client";
import { useApi } from "@/hooks/use-api";
import { stackId } from "@/types";
import type { StackStatus } from "@/types";

const healthType = (h: string) =>
  h === "healthy" ? "success" : h === "recovering" ? "info" : h === "degraded" ? "warning" : "error";

export default function StacksPage({ navigate }: { navigate: (href: string) => void }) {
  const { data: items, loading, error, refresh } = useApi<StackStatus[]>(() => api.listStacks());
  const [filterText, setFilterText] = useState("");
  const [selectedItems, setSelectedItems] = useState<StackStatus[]>([]);

  const filtered = (items ?? []).filter((s) =>
    stackId(s.stack).toLowerCase().includes(filterText.toLowerCase()) ||
    s.stack.region.toLowerCase().includes(filterText.toLowerCase())
  );

  return (
    <>
      {error && <Alert type="error" header="Failed to load stacks">{error}</Alert>}
      <Table
        variant="full-page"
        stickyHeader
        resizableColumns
        selectionType="single"
        loading={loading}
        loadingText="Loading stacks"
        items={filtered}
        trackBy={(s) => stackId(s.stack)}
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        columnDefinitions={[
          { id: "name", header: "Stack", minWidth: 180, cell: (s) => <Link variant="primary" onFollow={(e) => { e.preventDefault(); navigate(`#/stacks/${stackId(s.stack)}`); }}>{stackId(s.stack)}</Link> },
          { id: "health", header: "Health", minWidth: 130, cell: (s) => <Popover content={<div style={{ maxWidth: 340 }}>
            <div style={{ marginBottom: 6 }}>{s.healthDetail || s.health}</div>
            <div style={{ fontSize: 11, opacity: 0.6, borderTop: "1px solid #333", paddingTop: 6 }}>
              <div>✅ healthy — probe OK, clean metrics</div>
              <div>ℹ️ recovering — probe OK, past errors clearing</div>
              <div>⚠️ degraded — probe OK, elevated error rate</div>
              <div>❌ error / down — probe failed</div>
            </div>
          </div>} dismissButton={false} size="medium"><StatusIndicator type={healthType(s.health)}>{s.health}</StatusIndicator></Popover> },
          { id: "type", header: "Type", minWidth: 120, cell: (s) => <Badge color="blue">{s.stack.deployment_type}</Badge> },
          { id: "env", header: "Environment", minWidth: 120, cell: (s) => s.stack.environment },
          { id: "region", header: "Region", minWidth: 150, cell: (s) => s.stack.region },
          { id: "messages", header: "Webhook events (24h)", minWidth: 160, cell: (s) => s.messages24h.toLocaleString() },
          { id: "deploy", header: "Last deploy", minWidth: 180, cell: (s) => s.lastDeploy || "-" },
        ]}
        header={
          <Header
            variant="awsui-h1-sticky"
            counter={selectedItems.length ? `(${selectedItems.length}/${filtered.length})` : `(${filtered.length})`}
            description="All deployed SMART WhatsApp chatbot stacks."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button disabled={selectedItems.length === 0} onClick={() => selectedItems[0] && navigate(`#/stacks/${stackId(selectedItems[0].stack)}`)}>View details</Button>
                <Button iconName="refresh" variant="icon" onClick={refresh} />
              </SpaceBetween>
            }
          >
            Stacks
          </Header>
        }
        filter={<TextFilter filteringText={filterText} filteringPlaceholder="Find stacks" onChange={({ detail }) => setFilterText(detail.filteringText)} />}
        pagination={<Pagination currentPageIndex={1} pagesCount={1} />}
        preferences={
          <CollectionPreferences
            title="Preferences" confirmLabel="Confirm" cancelLabel="Cancel"
            preferences={{ pageSize: 10, wrapLines: false, stripedRows: false, contentDensity: "comfortable" }}
            pageSizePreference={{ title: "Page size", options: [{ value: 10, label: "10 stacks" }, { value: 25, label: "25 stacks" }] }}
          />
        }
        empty={
          <Box textAlign="center" color="inherit">
            <Box fontWeight="bold" color="inherit">No stacks</Box>
            <Box variant="p" color="inherit">No SMART stacks have been registered yet.</Box>
          </Box>
        }
      />
    </>
  );
}
