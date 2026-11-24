/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The Dashboard Studio's rail: the shared studio rail
 * (`components/studio/StudioRail`) bound to the dashboard on the stage.
 *
 * Its conversations carry `custom_dashboard_id`; every message says which
 * widget is selected (`studio_widget`), so "this widget" means the one on
 * screen; the picker offers the assistants carrying the dashboard tools
 * (`useToolAgents("custom_dashboards")`); a save the dashboard tools report
 * from inside their execution (`dashboard_saved`) refreshes the page at once
 * (`useChatApi`'s `handleToolProgress`) and the host learns which revision landed
 * and which widget it added or changed (`focus_widget_id`), so the stage can
 * follow the agent. The "Create a dashboard" dialog's brief arrives as
 * `autoSendMessage`, sent once with the agent it picked (`initialAgentId`).
 */
import { useCallback, useMemo } from "react";

import { StudioHandle, StudioRail, type StudioSubject } from "@/components/studio/StudioRail";
import { DASHBOARD_SAVED_EVENT, useBindConversationDashboard, type ToolProgressFrame } from "@/hooks/api/useChatApi";
import { useToolAgents } from "@/hooks/useToolAgents";
import { useFormatter } from "@/i18n";
import type { CustomDashboard } from "@/types";

/** The built-in tool id of the dashboard tools. */
const DASHBOARD_TOOL_ID = "custom_dashboards";

export function DashboardStudioHandle({ onOpen }: { onOpen: () => void }) {
  return <StudioHandle onOpen={onOpen} testId="dashboard.studio" />;
}

export interface DashboardStudioRailProps {
  dashboard: CustomDashboard;
  /** The selected widget: its id and what to call it ("Exposure score"). */
  focusedWidget: { id: string; label: string } | null;
  /** A dashboard tool saved a revision of THIS dashboard; `focusWidgetId` is the widget it added or changed. */
  onSaved?: (revision: number, focusWidgetId: string | null) => void;
  initialConversationId: string | null;
  onConversationChange: (id: string | null) => void;
  onCollapse: () => void;
  primedMessage?: string | null;
  onPrimedMessageApplied?: () => void;
  /** The first message, sent once when the panel can start the conversation (the create dialog's brief). */
  autoSendMessage?: string | null;
  onAutoSendApplied?: () => void;
  /** Who answers it: an agent id, `null` for the General Assistant, absent for the rail's own default. */
  initialAgentId?: string | null;
  className?: string;
}

export function DashboardStudioRail({ dashboard, focusedWidget, onSaved, ...rail }: DashboardStudioRailProps) {
  const { t_i18n } = useFormatter();
  const builders = useToolAgents(DASHBOARD_TOOL_ID);
  const { mutateAsync: bindToDashboard, isPending: binding } = useBindConversationDashboard();

  const onToolProgress = useCallback(
    (frame: ToolProgressFrame) => {
      if (frame.event !== DASHBOARD_SAVED_EVENT || frame.dashboard_id !== dashboard.id) return;
      if (typeof frame.revision !== "number") return;
      onSaved?.(frame.revision, typeof frame.focus_widget_id === "string" ? frame.focus_widget_id : null);
    },
    [onSaved, dashboard.id],
  );
  const sendContext = useMemo(() => (focusedWidget ? { studio_widget: focusedWidget.id } : {}), [focusedWidget]);
  const origin = useMemo(
    () => ({
      conversationId: dashboard.conversation_id ?? null,
      label: t_i18n("Continue the conversation that built this dashboard"),
      bind: (conversationId: string) => bindToDashboard({ conversationId, dashboardId: dashboard.id }),
      pending: binding,
    }),
    [bindToDashboard, binding, t_i18n, dashboard.conversation_id, dashboard.id],
  );
  const suggestions = useMemo(
    () => [
      t_i18n("Add the number of reports created this month from OpenCTI"),
      t_i18n("Show OpenAEV simulation results over time"),
      t_i18n("Make this widget half width and add a trend"),
      t_i18n("Add my OpenCRQ annual loss expectancy by scope"),
    ],
    [t_i18n],
  );

  const subject: StudioSubject = {
    kind: "dashboard",
    field: "custom_dashboard_id",
    id: dashboard.id,
    title: dashboard.title,
    toolAgents: builders,
    sendContext,
    onToolProgress,
    focus: {
      label: focusedWidget ? focusedWidget.label : t_i18n("No widget selected"),
      tip: t_i18n("The assistant is told which widget you selected"),
    },
    suggestions,
    origin,
    testId: "dashboard.studio",
    labels: {
      rail: t_i18n("Dashboard Studio assistant"),
      history: t_i18n("Conversations about this dashboard"),
      noAgentShort: t_i18n("No agent with the dashboard tools"),
      noAgentTitle: t_i18n("No agent carries the dashboard tools yet"),
      blockedHint: t_i18n(
        "The studio works with agents that have the Custom dashboards tool. Turn it on in one of your agents' tools and come back here.",
      ),
      unavailableHint: t_i18n("The studio opened this dashboard's own conversation instead."),
    },
  };

  return <StudioRail subject={subject} {...rail} />;
}
