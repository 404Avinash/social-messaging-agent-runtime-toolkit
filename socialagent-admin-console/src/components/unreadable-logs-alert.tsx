// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Alert from "@cloudscape-design/components/alert";
import type { UnreadableLog } from "@/types";
import { describeUnreadable } from "@/api/log-read";

/**
 * Names the log groups a read did not finish, for any page that lists log
 * lines. Without it, logs CloudWatch refused or throttled read as a list with
 * nothing in it, and the page said there were no errors. Renders nothing when
 * every group was read.
 */
export function UnreadableLogsAlert({ unreadable, header = "Results may be incomplete" }: { unreadable: UnreadableLog[]; header?: string }) {
  if (unreadable.length === 0) return null;
  return (
    <Alert type="warning" header={header}>
      Not read in full, so anything they hold is missing here: {describeUnreadable(unreadable)}.
    </Alert>
  );
}
