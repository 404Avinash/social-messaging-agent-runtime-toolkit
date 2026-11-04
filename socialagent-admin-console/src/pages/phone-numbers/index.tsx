// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Table from "@cloudscape-design/components/table";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import Modal from "@cloudscape-design/components/modal";
import Alert from "@cloudscape-design/components/alert";
import Select from "@cloudscape-design/components/select";
import Flashbar from "@cloudscape-design/components/flashbar";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";
import Link from "@cloudscape-design/components/link";
import Popover from "@cloudscape-design/components/popover";
import { useState, useEffect } from "react";
import { api } from "@/api/client";
import { errorText } from "@/api/bulk-delete";
import { usePermission } from "@/hooks/use-groups";
import type { GlobalPhoneNumber, StackStatus } from "@/types";

export default function PhoneNumbersPage({ navigate }: { navigate: (h: string) => void }) {
  const [numbers, setNumbers] = useState<GlobalPhoneNumber[]>([]);
  const [stacks, setStacks] = useState<StackStatus[]>([]);
  const [loading, setLoading] = useState(true);
  // A list that failed to load is not "no numbers linked": its reason is shown instead.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [flash, setFlash] = useState<{ type: "success" | "warning" | "error"; content: string }[]>([]);

  // Bind modal
  const [bindTarget, setBindTarget] = useState<GlobalPhoneNumber | null>(null);
  const [selectedStack, setSelectedStack] = useState<{ label: string; value: string } | null>(null);
  const [binding, setBinding] = useState(false);

  // Unbind modal
  const [unbindTarget, setUnbindTarget] = useState<GlobalPhoneNumber | null>(null);
  const [unbinding, setUnbinding] = useState(false);

  const load = () => {
    setLoading(true);
    setLoadError(null);
    Promise.all([api.listAllPhoneNumbers(), api.listStacks()])
      .then(([nums, ss]) => { setNumbers(nums); setStacks(ss); })
      .catch((e) => setLoadError(errorText(e)))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const handleBind = async () => {
    if (!bindTarget || !selectedStack) return;
    setBinding(true);
    try {
      const result = await api.bindPhoneNumber(bindTarget.phoneNumberId, selectedStack.value, bindTarget.wabaId);
      const mode = (result as { mode?: string })?.mode;
      const siblings = (result as { siblingStacks?: string[] })?.siblingStacks || [];
      // nextStep says what the bind changed and which project config keys make the bot use the number.
      const { nextStep, warning, eventDestinationUpdated } = result as { nextStep?: string; warning?: string; eventDestinationUpdated?: boolean };
      const msg = mode === "shared_waba"
        ? `Bound ${bindTarget.displayPhone} to ${selectedStack.value} in the console registry, with shared SNS (siblings: ${siblings.join(", ")}).`
        : `Bound ${bindTarget.displayPhone} to ${selectedStack.value} in the console registry.`;
      // A bind whose event destination was not set leaves inbound messages going elsewhere.
      const type = warning || eventDestinationUpdated === false ? "warning" : "success";
      setFlash([{ type, content: [msg, nextStep, warning].filter(Boolean).join(" ") }]);
      setBindTarget(null);
      setSelectedStack(null);
      load();
    } catch (e) {
      setFlash([{ type: "error", content: `Bind failed: ${e}` }]);
    } finally {
      setBinding(false);
    }
  };

  const handleUnbind = async () => {
    if (!unbindTarget) return;
    setUnbinding(true);
    try {
      const { nextStep } = (await api.unbindPhoneNumber(unbindTarget.boundStack)) as { nextStep?: string };
      const msg = `Unbound ${unbindTarget.displayPhone} from ${unbindTarget.boundStack} in the console registry.`;
      setFlash([{ type: "success", content: [msg, nextStep].filter(Boolean).join(" ") }]);
      setUnbindTarget(null);
      load();
    } catch (e) {
      setFlash([{ type: "error", content: `Unbind failed: ${e}` }]);
    } finally {
      setUnbinding(false);
    }
  };

  const stackId = (s: StackStatus) => `${s.stack.project}-${s.stack.environment}`;
  // The topic an exclusive bind would replace as the WABA's event destination,
  // when it is not the selected stack's own: whatever subscribes to it stops
  // receiving the WABA's inbound messages.
  const selectedTopic = stacks.find((s) => stackId(s) === selectedStack?.value)?.stack.outputs?.sns_topic_arn;
  const replacedTopic = bindTarget?.snsTopic && selectedStack && bindTarget.snsTopic !== selectedTopic ? bindTarget.snsTopic : "";
  // A topic no registered stack reads may belong to another workload, so
  // taking the WABA from it is an admin's call, as the backend checks.
  const unconsumed = !!replacedTopic && !stacks.some((s) => [s.stack.outputs?.sns_topic_arn, s.stack.shared_sns_topic_arn].includes(replacedTopic));
  const admin = usePermission("admin");
  const bindBlocked = unconsumed && !admin.allowed;

  return (
    <SpaceBetween size="l">
      {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}

      {/* Bind modal */}
      <Modal visible={!!bindTarget} onDismiss={() => { setBindTarget(null); setSelectedStack(null); }}
        header={`Bind ${bindTarget?.displayPhone || "phone"} to stack`}
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => { setBindTarget(null); setSelectedStack(null); }}>Cancel</Button>
          <Button variant="primary" loading={binding} disabled={!selectedStack || bindBlocked} disabledReason={bindBlocked ? admin.reason : undefined} onClick={handleBind}>Bind to stack</Button>
        </SpaceBetween>}
      >
        <SpaceBetween size="m">
          {bindTarget?.wabaSharing && bindTarget.wabaSharing.length > 0 ? (
            <Alert type="info" header="Shared WABA detected">
              This phone belongs to WABA <strong>{bindTarget.wabaName}</strong> which is already
              used by stack(s): <strong>{bindTarget.wabaSharing.join(", ")}</strong>. Since EUM Social
              allows only one SNS event destination per WABA, binding records in the console registry
              that this stack reuses the existing SNS topic with a Meta phone-number filter, and adds
              the filter to the sibling stacks' registry entries. The WABA's event destination is not
              changed. The bot sends from <code>whatsapp.phone_number_id</code> in the project config, so
              none of this reaches the deployed stacks until you set the keys the result lists in each
              stack's <code>socialagent.&lt;env&gt;.yaml</code> and run <code>socialagent deploy</code>.
            </Alert>
          ) : (
            <Alert type="warning">
              This records the number in the selected stack's console registry entry and points the
              WABA's SNS event destination at the stack's topic, so inbound messages on the WABA reach
              that stack. The bot sends from <code>whatsapp.phone_number_id</code> in the project's{" "}
              <code>socialagent.&lt;env&gt;.yaml</code>: set it there and run <code>socialagent deploy</code>,
              or the bot keeps its current number and a later deploy can rewrite the registry entry.
              {replacedTopic && (
                <> The WABA's events go to <code>{replacedTopic}</code> now. After the bind they go to
                the selected stack's topic instead, so a stack still subscribed to that one, such as
                one this number was unbound from, stops receiving inbound messages.</>
              )}
              {unconsumed && (
                <> No registered stack uses that topic, so it may belong to another workload, and
                only an admin can move the WABA off it.</>
              )}
            </Alert>
          )}
          <Select
            selectedOption={selectedStack}
            onChange={({ detail }) => setSelectedStack(detail.selectedOption as { label: string; value: string })}
            options={stacks.map((s) => ({ label: stackId(s), value: stackId(s), description: s.stack.description || s.stack.deployment_type }))}
            placeholder="Select a stack"
            filteringType="auto"
          />
        </SpaceBetween>
      </Modal>

      {/* Unbind modal */}
      <Modal visible={!!unbindTarget} onDismiss={() => setUnbindTarget(null)}
        header={`Unbind ${unbindTarget?.displayPhone || "phone"} from ${unbindTarget?.boundStack || "stack"}`}
        footer={<SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => setUnbindTarget(null)}>Cancel</Button>
          <Button variant="primary" loading={unbinding} onClick={handleUnbind}>Unbind</Button>
        </SpaceBetween>}
      >
        <Alert type="warning">This removes the phone number from the stack's console registry entry only, so the console stops showing it for the stack. The SNS event destination is NOT changed, and the bot keeps sending from <code>whatsapp.phone_number_id</code> in the project's <code>socialagent.&lt;env&gt;.yaml</code>. To stop the bot using the number, clear that key and run <code>socialagent deploy</code>. The number stays on this page, unbound, and can be bound again.</Alert>
      </Modal>

      {loadError ? (
        <Alert type="error" header="Could not load phone numbers" action={<Button iconName="refresh" onClick={load}>Retry</Button>}>
          {loadError}
        </Alert>
      ) : <Table
        variant="full-page"
        loading={loading}
        loadingText="Loading phone numbers..."
        header={<Header variant="awsui-h1-sticky" counter={`(${numbers.length})`}
          description="Every WhatsApp phone number linked in EUM Social in the console's region, and the stack whose registry entry names it."
          actions={<Button iconName="refresh" onClick={load} />}
        >Phone numbers</Header>}
        items={numbers}
        columnDefinitions={[
          { id: "phone", header: "Phone number", minWidth: 160, cell: (p) => <Box fontWeight="bold">{p.displayPhone || "-"}</Box> },
          { id: "name", header: "Display name", minWidth: 150, cell: (p) => p.displayName || "-" },
          { id: "quality", header: "Quality", minWidth: 100, cell: (p) => <Popover content={<div style={{ maxWidth: 300, fontSize: 12 }}>
            <div style={{ marginBottom: 6 }}><strong>Meta Quality Rating</strong></div>
            <div>🟢 <strong>GREEN</strong> — Good quality, no issues</div>
            <div>🟡 <strong>YELLOW</strong> — Some users reported issues</div>
            <div>🔴 <strong>RED</strong> — High block/report rate, at risk of being disabled</div>
            <div>⏳ <strong>UNKNOWN</strong> — New number, not enough messages sent yet (~50+ needed)</div>
            <div style={{ marginTop: 8, opacity: 0.6 }}>Rated by Meta based on user feedback (blocks, reports). Updates automatically.</div>
          </div>} dismissButton={false} size="medium"><StatusIndicator type={p.qualityRating === "GREEN" ? "success" : p.qualityRating === "YELLOW" ? "warning" : p.qualityRating === "RED" ? "error" : "pending"}>{p.qualityRating}</StatusIndicator></Popover> },
          { id: "waba", header: "WABA", minWidth: 200, cell: (p) => p.wabaSharing && p.wabaSharing.length > 0
            ? <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                <span>{p.wabaName}</span>
                <Popover
                  header="Shared WABA"
                  content={<div style={{ maxWidth: 300, fontSize: 12 }}>
                    <div>This WABA is shared with: <strong>{p.wabaSharing.join(", ")}</strong></div>
                    <div style={{ marginTop: 6 }}>EUM Social allows only one SNS destination per WABA, so all phones under this WABA route to the same topic. Each stack filters events by Meta phone number ID.</div>
                  </div>}
                  dismissButton={false}
                  size="medium"
                ><Badge color="blue">shared</Badge></Popover>
              </SpaceBetween>
            : p.wabaName },
          { id: "stack", header: "Bound stack", minWidth: 150, cell: (p) => p.boundStack
            ? <Link variant="primary" onFollow={(e) => { e.preventDefault(); navigate(`#/stacks/${p.boundStack}`); }}>{p.boundStack}</Link>
            : <Badge color="grey">Unbound</Badge>
          },
          { id: "sns", header: "SNS topic", minWidth: 250, cell: (p) => p.snsTopic ? <CopyToClipboard variant="inline" textToCopy={p.snsTopic} copySuccessText="Copied" copyErrorText="Failed" /> : "-" },
          { id: "actions", header: "Actions", minWidth: 150, cell: (p) => p.boundStack
            ? <Button variant="inline-link" onClick={() => setUnbindTarget(p)}>Unbind</Button>
            : <Button variant="primary" onClick={() => setBindTarget(p)}>Bind to stack</Button>
          },
        ]}
        empty={<Box textAlign="center" color="inherit"><Box fontWeight="bold">No phone numbers found</Box><Box>No WhatsApp phone numbers are linked in EUM Social.</Box></Box>}
      />}
    </SpaceBetween>
  );
}
