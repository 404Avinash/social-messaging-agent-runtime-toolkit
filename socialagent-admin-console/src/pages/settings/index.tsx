// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Form from "@cloudscape-design/components/form";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Select from "@cloudscape-design/components/select";
import Toggle from "@cloudscape-design/components/toggle";
import Button from "@cloudscape-design/components/button";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Alert from "@cloudscape-design/components/alert";
import Flashbar from "@cloudscape-design/components/flashbar";
import { useState } from "react";
import { api } from "@/api/client";
import { useApi } from "@/hooks/use-api";
import { usePermission } from "@/hooks/use-groups";
import type { AdminSettings } from "@/types";

const REGION_OPTIONS = [
  { label: "US East (N. Virginia) — us-east-1", value: "us-east-1" },
  { label: "US East (Ohio) — us-east-2", value: "us-east-2" },
  { label: "US West (Oregon) — us-west-2", value: "us-west-2" },
  { label: "EU (Ireland) — eu-west-1", value: "eu-west-1" },
  { label: "EU (Frankfurt) — eu-central-1", value: "eu-central-1" },
  { label: "Asia Pacific (Singapore) — ap-southeast-1", value: "ap-southeast-1" },
  { label: "Asia Pacific (Tokyo) — ap-northeast-1", value: "ap-northeast-1" },
];

export default function SettingsPage() {
  const { data: settings, error, refresh } = useApi<AdminSettings>(() => api.getSettings());
  const [local, setLocal] = useState<AdminSettings | null>(null);
  const [flash, setFlash] = useState<{ type: "success" | "error"; content: string }[]>([]);
  // Reading settings is operator-level; writing them is admin-only.
  const admin = usePermission("admin");

  const current = local ?? settings;
  if (error) return <Alert type="error" header="Failed to load settings">{error}</Alert>;
  if (!current) return null;

  const update = (patch: Partial<AdminSettings>) => setLocal({ ...current, ...patch });
  const regionOption = REGION_OPTIONS.find((r) => r.value === current.defaultRegion) ?? REGION_OPTIONS[0];

  const save = async () => {
    try {
      await api.updateSettings(current);
      setFlash([{ type: "success", content: "Settings saved." }]);
    } catch (e) {
      setFlash([{ type: "error", content: `Failed to save: ${e}` }]);
    }
  };

  return (
    <>
      {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}
      <Form
        header={<Header variant="h1" description="Manage global configuration for the SMART Admin Console.">Settings</Header>}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => { setLocal(null); refresh(); }}>Cancel</Button>
            <Button variant="primary" disabled={!admin.allowed} disabledReason={admin.reason} onClick={save}>Save settings</Button>
          </SpaceBetween>
        }
      >
        <SpaceBetween size="l">
          <Container header={<Header variant="h2" description="AWS credentials and default region.">AWS configuration</Header>}>
            <SpaceBetween size="l">
              <FormField label="AWS profile" description="Named profile from local credentials file." constraintText="Leave blank for default credential chain.">
                <Input value={current.awsProfile} placeholder="default" onChange={({ detail }) => update({ awsProfile: detail.value })} />
              </FormField>
              <FormField label="Default region">
                <Select selectedOption={regionOption} filteringType="auto" filteringPlaceholder="Find a region" options={REGION_OPTIONS} onChange={({ detail }) => update({ defaultRegion: detail.selectedOption.value! })} />
              </FormField>
            </SpaceBetween>
          </Container>
          <Container header={<Header variant="h2" description="Alert configuration.">Notification preferences</Header>}>
            <SpaceBetween size="l">
              <FormField label="Slack webhook URL — optional" constraintText="Format: https://hooks.slack.com/services/...">
                <Input value={current.slackWebhookUrl} placeholder="https://hooks.slack.com/services/..." onChange={({ detail }) => update({ slackWebhookUrl: detail.value })} />
              </FormField>
              <FormField label="Notification email — optional">
                <Input type="email" value={current.notificationEmail} placeholder="ops-team@example.com" onChange={({ detail }) => update({ notificationEmail: detail.value })} />
              </FormField>
              <FormField label="Stack degradation alerts">
                <Toggle checked={current.degradationAlerts} onChange={({ detail }) => update({ degradationAlerts: detail.checked })}>Notify on stack degradation</Toggle>
              </FormField>
              <FormField label="Deployment failure alerts">
                <Toggle checked={current.deploymentFailureAlerts} onChange={({ detail }) => update({ deploymentFailureAlerts: detail.checked })}>Notify on deployment failure</Toggle>
              </FormField>
            </SpaceBetween>
          </Container>
          <Container header={<Header variant="h2" description="Direct links to AWS observability consoles.">AWS console links</Header>}>
            <KeyValuePairs columns={3} items={[
              { label: "CloudWatch logs", value: <Link href="https://console.aws.amazon.com/cloudwatch/home#logsV2:log-groups" external variant="primary">Open CloudWatch Logs</Link> },
              { label: "CloudWatch metrics", value: <Link href="https://console.aws.amazon.com/cloudwatch/home#metricsV2" external variant="primary">Open CloudWatch Metrics</Link> },
              { label: "CloudWatch alarms", value: <Link href="https://console.aws.amazon.com/cloudwatch/home#alarmsV2" external variant="primary">Open CloudWatch Alarms</Link> },
              { label: "AWS X-Ray traces", value: <Link href="https://console.aws.amazon.com/xray/home#traces" external variant="primary">Open X-Ray Traces</Link> },
              { label: "Lambda functions", value: <Link href="https://console.aws.amazon.com/lambda/home#/functions" external variant="primary">Open Lambda console</Link> },
              { label: "CodeBuild projects", value: <Link href="https://console.aws.amazon.com/codesuite/codebuild/projects" external variant="primary">Open CodeBuild console</Link> },
            ]} />
          </Container>
        </SpaceBetween>
      </Form>
    </>
  );
}
