// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Modal from "@cloudscape-design/components/modal";
import Box from "@cloudscape-design/components/box";

interface Props {
  visible: boolean;
  onDismiss: () => void;
  title: string;
  payload: string;
}

export function PayloadModal({ visible, onDismiss, title, payload }: Props) {
  let formatted = payload;
  try {
    formatted = JSON.stringify(JSON.parse(payload), null, 2);
  } catch { /* not valid JSON, show raw */ }

  return (
    <Modal visible={visible} onDismiss={onDismiss} header={title} size="large">
      <Box padding="s">
        <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0, fontFamily: "monospace", fontSize: "13px" }}>{formatted}</pre>
      </Box>
    </Modal>
  );
}
