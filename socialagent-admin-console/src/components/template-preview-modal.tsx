// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Modal from "@cloudscape-design/components/modal";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { TemplateDetail } from "@/types";

interface Props {
  visible: boolean;
  onDismiss: () => void;
  stackId: string;
  templateName: string;
}

export function TemplatePreviewModal({ visible, onDismiss, stackId, templateName }: Props) {
  const [detail, setDetail] = useState<TemplateDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!visible || !templateName) return;
    setLoading(true);
    setError(null);
    api.getTemplateDetail(stackId, templateName)
      .then(setDetail)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [visible, stackId, templateName]);

  const header = detail?.components.find((c) => c.type === "HEADER");
  const body = detail?.components.find((c) => c.type === "BODY");
  const footer = detail?.components.find((c) => c.type === "FOOTER");
  const buttons = detail?.components.find((c) => c.type === "BUTTONS");
  const allVars = detail?.components.flatMap((c) => c.variables ?? []) ?? [];

  return (
    <Modal visible={visible} onDismiss={onDismiss} header={`Template — ${templateName}`} size="medium">
      {loading && <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>}
      {error && <Alert type="error">{error}</Alert>}
      {!loading && detail && (
        <SpaceBetween size="l">
          {/* Metadata */}
          <ColumnLayout columns={3}>
            <div><Box variant="awsui-key-label">Category</Box><Badge color="blue">{detail.category}</Badge></div>
            <div><Box variant="awsui-key-label">Language</Box><Box>{detail.language}</Box></div>
            <div><Box variant="awsui-key-label">Status</Box><Badge color={detail.status === "approved" ? "green" : "grey"}>{detail.status}</Badge></div>
          </ColumnLayout>

          {/* WhatsApp-style preview */}
          <Box>
            <Box variant="awsui-key-label" margin={{ bottom: "xs" }}>Preview</Box>
            <div style={{
              background: "#1a1d21",
              borderRadius: "12px",
              padding: "16px",
              maxWidth: "400px",
            }}>
              {/* Chat bubble */}
              <div style={{
                background: "#2a2e33",
                borderRadius: "12px 12px 12px 4px",
                padding: "12px",
                color: "#fff",
                fontSize: "14px",
                lineHeight: "1.6",
              }}>
                {/* Header */}
                {header?.text && (
                  <div style={{ fontWeight: 700, fontSize: "15px", marginBottom: "8px" }}>
                    {header.text}
                  </div>
                )}

                {/* Body */}
                {body?.text && (
                  <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                    {body.text.replace(/\{\{(\d+)\}\}/g, (_m, n) => {
                      const v = allVars.find((v) => String(v.position) === n);
                      return v?.name ? `[${v.name}]` : `{{${n}}}`;
                    })}
                  </div>
                )}

                {/* Footer */}
                {footer?.text && (
                  <div style={{ fontSize: "12px", opacity: 0.5, marginTop: "8px" }}>
                    {footer.text}
                  </div>
                )}

                {/* Timestamp */}
                <div style={{ fontSize: "11px", opacity: 0.4, marginTop: "6px", textAlign: "right" }}>
                  12:00
                </div>
              </div>

              {/* Buttons */}
              {buttons?.text && (
                <div style={{ marginTop: "4px", display: "flex", flexDirection: "column", gap: "2px" }}>
                  {buttons.text.split(",").map((btn, i) => (
                    <div key={i} style={{
                      background: "#2a2e33",
                      borderRadius: i === 0 ? "0" : "0",
                      padding: "10px",
                      textAlign: "center",
                      color: "#539fe5",
                      fontSize: "14px",
                      fontWeight: 500,
                      borderTop: "1px solid #3a3e43",
                    }}>
                      {btn.trim()}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </Box>

          {/* Variables */}
          {allVars.length > 0 && (
            <Box>
              <Box variant="awsui-key-label" margin={{ bottom: "xs" }}>Variables</Box>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                {allVars.map((v, i) => (
                  <Badge key={i} color="blue">{`{{${v.position}}} = ${v.name}`}</Badge>
                ))}
              </div>
            </Box>
          )}

          {/* Raw components for debugging */}
          {(!body?.text && !header?.text) && (
            <Alert type="info">No HEADER or BODY components found in this template.</Alert>
          )}
        </SpaceBetween>
      )}
    </Modal>
  );
}
