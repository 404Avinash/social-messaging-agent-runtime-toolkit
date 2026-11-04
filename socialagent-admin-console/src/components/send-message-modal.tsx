// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Button from "@cloudscape-design/components/button";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Textarea from "@cloudscape-design/components/textarea";
import Select from "@cloudscape-design/components/select";
import Alert from "@cloudscape-design/components/alert";
import Flashbar from "@cloudscape-design/components/flashbar";
import Tabs from "@cloudscape-design/components/tabs";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import Spinner from "@cloudscape-design/components/spinner";
import { useState, useEffect } from "react";
import { api } from "@/api/client";
import { useApi } from "@/hooks/use-api";
import type { WhatsAppTemplate, TemplateDetail } from "@/types";

interface Props {
  visible: boolean;
  onDismiss: () => void;
  defaultStackId?: string;
}

export function SendMessageModal({ visible, onDismiss, defaultStackId }: Props) {
  const { data: stacks } = useApi(() => api.listStacks());
  const [stack, setStack] = useState<{ label: string; value: string } | null>(defaultStackId ? { label: defaultStackId, value: defaultStackId } : null);
  const [phone, setPhone] = useState("");
  const [mode, setMode] = useState("text");
  const [text, setText] = useState("");
  // Template state
  const [templates, setTemplates] = useState<WhatsAppTemplate[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<{ label: string; value: string } | null>(null);
  const [templateDetail, setTemplateDetail] = useState<TemplateDetail | null>(null);
  const [templateLoading, setTemplateLoading] = useState(false);
  const [paramValues, setParamValues] = useState<string[]>([]);
  const [language, setLanguage] = useState("en");

  const [sending, setSending] = useState(false);
  const [flash, setFlash] = useState<{ type: "success" | "error"; content: string }[]>([]);

  const stackOptions = (stacks ?? []).map((s) => ({ label: `${s.stack.project}-${s.stack.environment}`, value: `${s.stack.project}-${s.stack.environment}` }));

  // Load templates when stack changes
  useEffect(() => {
    if (!stack || !visible) { setTemplates([]); return; }
    api.getTemplates(stack.value).then(setTemplates).catch(() => setTemplates([]));
  }, [stack?.value, visible]);

  // Load template detail when selected
  useEffect(() => {
    if (!stack || !selectedTemplate) { setTemplateDetail(null); return; }
    setTemplateLoading(true);
    api.getTemplateDetail(stack.value, selectedTemplate.value)
      .then((d) => {
        setTemplateDetail(d);
        setLanguage(d.language || "en");
        const body = d.components.find((c) => c.type === "BODY");
        const varCount = (body?.text?.match(/\{\{\d+\}\}/g) || []).length;
        setParamValues(new Array(varCount).fill(""));
      })
      .catch(() => setTemplateDetail(null))
      .finally(() => setTemplateLoading(false));
  }, [stack?.value, selectedTemplate?.value]);

  const handleSend = async () => {
    if (!stack || !phone) return;
    setSending(true);
    try {
      if (mode === "template" && selectedTemplate) {
        await api.sendTemplateMessage(stack.value, phone, selectedTemplate.value, language, paramValues);
      } else {
        await api.sendMessage(stack.value, phone, text);
      }
      setFlash([{ type: "success", content: `Message sent to ${phone}` }]);
      setText("");
    } catch (e) {
      setFlash([{ type: "error", content: `Failed: ${e}` }]);
    } finally {
      setSending(false);
    }
  };

  const canSend = stack && phone && (mode === "text" ? text : selectedTemplate);

  // Render template preview with filled params
  const renderPreview = () => {
    if (!templateDetail) return null;
    const header = templateDetail.components.find((c) => c.type === "HEADER");
    const body = templateDetail.components.find((c) => c.type === "BODY");
    const footer = templateDetail.components.find((c) => c.type === "FOOTER");
    const filledBody = body?.text?.replace(/\{\{(\d+)\}\}/g, (_m, n) => {
      const idx = parseInt(n) - 1;
      return paramValues[idx] || `{{${n}}}`;
    }) || "";
    return (
      <div style={{ background: "#1a1d21", borderRadius: "12px", padding: "16px", maxWidth: "360px" }}>
        <div style={{ background: "#2a2e33", borderRadius: "12px 12px 12px 4px", padding: "12px", color: "#fff", fontSize: "14px", lineHeight: "1.6" }}>
          {header?.text && <div style={{ fontWeight: 700, marginBottom: "8px" }}>{header.text}</div>}
          <div style={{ whiteSpace: "pre-wrap" }}>{filledBody}</div>
          {footer?.text && <div style={{ fontSize: "12px", opacity: 0.5, marginTop: "8px" }}>{footer.text}</div>}
          <div style={{ fontSize: "11px", opacity: 0.4, marginTop: "6px", textAlign: "right" }}>now</div>
        </div>
      </div>
    );
  };

  return (
    <Modal visible={visible} onDismiss={onDismiss} header="Send WhatsApp message" size="large"
      footer={<SpaceBetween direction="horizontal" size="xs">
        <Button variant="link" onClick={onDismiss}>Close</Button>
        <Button variant="primary" loading={sending} disabled={!canSend} onClick={handleSend}>Send message</Button>
      </SpaceBetween>}
    >
      <SpaceBetween size="m">
        {flash.length > 0 && <Flashbar items={flash.map((f, i) => ({ ...f, id: String(i), dismissible: true, onDismiss: () => setFlash([]) }))} />}

        <FormField label="Stack">
          <Select selectedOption={stack} options={stackOptions} placeholder="Select stack"
            onChange={({ detail }) => { setStack(detail.selectedOption as typeof stack); setSelectedTemplate(null); setTemplateDetail(null); }} />
        </FormField>

        <FormField label="Phone number" constraintText="E.164 format (e.g. +525555550100)">
          <Input value={phone} placeholder="+525555550100" onChange={({ detail }) => setPhone(detail.value)} />
        </FormField>

        <Tabs activeTabId={mode} onChange={({ detail }) => setMode(detail.activeTabId)} tabs={[
          { id: "text", label: "Free text", content: (
            <SpaceBetween size="s">
              <Alert type="info">Free text only works within the 24-hour conversation window (after the user messages you first).</Alert>
              <FormField label="Message">
                <Textarea value={text} placeholder="Hello! This is a test message." rows={4} onChange={({ detail }) => setText(detail.value)} />
              </FormField>
            </SpaceBetween>
          )},
          { id: "template", label: "Template", content: (
            <SpaceBetween size="m">
              <Alert type="info">Templates can be sent anytime — no 24-hour window restriction. They must be pre-approved by Meta.</Alert>
              <FormField label="Template">
                <Select selectedOption={selectedTemplate}
                  options={templates.map((t) => ({ label: t.name, value: t.name, description: `${t.category} · ${t.language} · ${t.approvalStatus}`, tags: [t.approvalStatus === "approved" ? "✅ Approved" : "⏳ Pending"] }))}
                  placeholder="Select template" filteringType="auto"
                  onChange={({ detail }) => setSelectedTemplate(detail.selectedOption as typeof selectedTemplate)} />
              </FormField>

              {templateLoading && <Box textAlign="center"><Spinner /></Box>}

              {templateDetail && !templateLoading && (
                <>
                  {templateDetail.status !== "approved" && (
                    <Alert type="warning">This template is <strong>{templateDetail.status}</strong> — Meta has not approved it yet. The message will be silently dropped by WhatsApp.</Alert>
                  )}
                  <SpaceBetween direction="horizontal" size="xs">
                    <Badge color="blue">{templateDetail.category}</Badge>
                    <Badge color={templateDetail.status === "approved" ? "green" : "grey"}>{templateDetail.status}</Badge>
                    <Badge color="grey">{templateDetail.language}</Badge>
                  </SpaceBetween>

                  {paramValues.length > 0 && (
                    <FormField label="Parameters" description="Fill in the template variables.">
                      <SpaceBetween size="xs">
                        {paramValues.map((v, i) => {
                          const allVars = templateDetail.components.flatMap((c) => c.variables ?? []);
                          const varInfo = allVars.find((vr) => vr.position === i + 1);
                          return (
                            <Input key={i} value={v}
                              placeholder={varInfo?.name || `Parameter {{${i + 1}}}`}
                              onChange={({ detail }) => { const next = [...paramValues]; next[i] = detail.value; setParamValues(next); }}
                            />
                          );
                        })}
                      </SpaceBetween>
                    </FormField>
                  )}

                  <FormField label="Preview">
                    {renderPreview()}
                  </FormField>
                </>
              )}
            </SpaceBetween>
          )},
        ]} />
      </SpaceBetween>
    </Modal>
  );
}
