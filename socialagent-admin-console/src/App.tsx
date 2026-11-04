// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import AppLayoutToolbar from "@cloudscape-design/components/app-layout-toolbar";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import Button from "@cloudscape-design/components/button";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useHashRouter } from "@/hooks/use-hash-router";
import { useAuth } from "@/hooks/use-auth";
import OverviewPage from "@/pages/overview";
import StacksPage from "@/pages/stacks";
import StackDetailPage from "@/pages/stack-detail";
import TemplatesPage from "@/pages/templates";
import EventsPage from "@/pages/events";
import LogsPage from "@/pages/logs";
import SettingsPage from "@/pages/settings";
import ConversationsPage from "@/pages/conversations";
import SchedulesPage from "@/pages/schedules";
import StatePage from "@/pages/state";
import PhoneNumbersPage from "@/pages/phone-numbers";
import { SendMessageModal } from "@/components/send-message-modal";
import { useState } from "react";

const NAV_ITEMS = [
  { type: "link" as const, text: "Overview", href: "#/" },
  { type: "link" as const, text: "Stacks", href: "#/stacks" },
  { type: "divider" as const },
  // Cross-stack views. Phone-number binding/unbinding lives only here (the
  // per-stack tab is read-only).
  { type: "link" as const, text: "Conversations", href: "#/conversations" },
  { type: "link" as const, text: "Schedules", href: "#/schedules" },
  { type: "link" as const, text: "State", href: "#/state" },
  { type: "link" as const, text: "Phone numbers", href: "#/phone-numbers" },
  { type: "divider" as const },
  { type: "link" as const, text: "Events", href: "#/events" },
  { type: "link" as const, text: "Templates", href: "#/templates" },
  { type: "link" as const, text: "Logs", href: "#/logs" },
  { type: "divider" as const },
  { type: "link" as const, text: "Settings", href: "#/settings" },
];

function breadcrumbs(page: string, param?: string) {
  const items = [{ text: "SMART", href: "#/" }];
  const labels: Record<string, string> = {
    stacks: "Stacks",
    templates: "Templates",
    events: "Events",
    logs: "Logs",
    settings: "Settings",
    conversations: "Conversations",
    schedules: "Schedules",
    state: "State",
    "phone-numbers": "Phone numbers",
  };
  if (page !== "/") {
    // page comes from the URL hash. An own-property lookup keeps "__proto__" or
    // "constructor" from reading an Object.prototype member as the label.
    const label = Object.prototype.hasOwnProperty.call(labels, page) ? labels[page] : page;
    items.push({ text: label, href: `#/${page}` });
  }
  if (param) {
    items.push({ text: param, href: `#/${page}/${param}` });
  }
  return items;
}

function contentType(page: string, param?: string): string {
  if (page === "/" || (page === "stacks" && !param)) return page === "/" ? "cards" : "table";
  if (["templates", "events"].includes(page)) return "table";
  if (page === "settings") return "form";
  return "default";
}

function activeHref(page: string) {
  if (page === "/") return "#/";
  return `#/${page}`;
}

export default function App() {
  const { route, navigate } = useHashRouter();
  const { page, param } = route;
  const { email, signOut } = useAuth();

  const renderPage = () => {
    switch (page) {
      case "/": return <OverviewPage navigate={navigate} />;
      case "stacks": return param
        ? <StackDetailPage stackId={param} navigate={navigate} />
        : <StacksPage navigate={navigate} />;
      case "templates": return <TemplatesPage navigate={navigate} />;
      case "events": return <EventsPage navigate={navigate} />;
      case "conversations": return <ConversationsPage navigate={navigate} />;
      case "schedules": return <SchedulesPage navigate={navigate} />;
      case "state": return <StatePage navigate={navigate} />;
      case "phone-numbers": return <PhoneNumbersPage navigate={navigate} />;
      case "logs": return <LogsPage />;
      case "settings": return <SettingsPage />;
      default: return <OverviewPage navigate={navigate} />;
    }
  };

  const [sendMsgOpen, setSendMsgOpen] = useState(false);

  return (
    <>
    <SendMessageModal visible={sendMsgOpen} onDismiss={() => setSendMsgOpen(false)} />
    <AppLayoutToolbar
      contentType={contentType(page, param) as "table" | "cards" | "form" | "default"}
      navigation={
        <SpaceBetween size="m">
          <SideNavigation
            activeHref={activeHref(page)}
            header={{ text: "SMART", href: "#/" }}
            items={NAV_ITEMS}
            onFollow={(e) => { e.preventDefault(); navigate(e.detail.href); }}
          />
          <Box padding={{ horizontal: "l" }}>
            <SpaceBetween size="xs">
              <Button variant="primary" onClick={() => setSendMsgOpen(true)}>Send message</Button>
              <Box variant="small" color="text-body-secondary">{email}</Box>
              <Button variant="link" onClick={signOut}>Sign out</Button>
            </SpaceBetween>
          </Box>
        </SpaceBetween>
      }
      breadcrumbs={
        <BreadcrumbGroup
          items={breadcrumbs(page, param)}
          onFollow={(e) => { e.preventDefault(); navigate(e.detail.href); }}
        />
      }
      content={renderPage()}
    />
    </>
  );
}
