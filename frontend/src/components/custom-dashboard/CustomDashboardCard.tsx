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
 * The chat surface for a `[[DASHBOARD:uuid]]` marker: the dashboard the agent
 * just built, drawn in place, with a link to its own page.
 *
 * The card shows the dashboard's CURRENT revision (the marker names the row,
 * not a copy), height-capped and scrollable so a long dashboard does not push
 * the conversation off screen. Its grid follows the card's width.
 */
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Spinner } from "@filigran/design-system";
import { LayoutGrid, Maximize2 } from "lucide-react";

import { UnavailableCard } from "@/components/chat/UnavailableCard";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useCustomDashboard } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { DashboardAskContext } from "./ask";
import { useAskAssistant } from "@/lib/ask-assistant";
import { CustomDashboardView } from "./CustomDashboardView";
import { normalizeDashboard } from "./spec";

function CardFrame({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("my-2 overflow-hidden rounded-sm border border-accent/25 layer-1 bg-elevation-default", className)}>
      {children}
    </div>
  );
}

export function CustomDashboardCard({ dashboardId }: { dashboardId: string }) {
  const { t_i18n } = useFormatter();
  const ask = useAskAssistant();
  const { data, isLoading, isError } = useCustomDashboard(dashboardId);
  const dashboard = useMemo(() => (data ? normalizeDashboard(data.spec) : null), [data]);

  if (isLoading) {
    return (
      <CardFrame className="flex items-center gap-2 px-3 py-2.5 text-[13px] text-default-secondary">
        <Spinner size="sm" />
        {t_i18n("Loading dashboard…")}
      </CardFrame>
    );
  }

  if (isError || !data || !dashboard) {
    return <UnavailableCard message={t_i18n("This dashboard is no longer available.")} />;
  }

  return (
    <CardFrame>
      <div className="flex items-center justify-between gap-3 border-b border-elevation-subtle-soft px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <LayoutGrid className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
          <span className="truncate text-[13px] font-medium text-default-primary">{data.title}</span>
          <span className="shrink-0 text-[11px] text-default-secondary">
            {t_i18n("{count} widgets", { count: data.widget_count })}
          </span>
        </div>
        <Tip content={t_i18n("Open the dashboard")}>
          <Button variant="ghost" size="sm" asChild aria-label={t_i18n("Open the dashboard")}>
            <Link to={`/dashboards/${data.id}`}>
              <Maximize2 className="h-3.5 w-3.5" />
            </Link>
          </Button>
        </Tip>
      </div>
      <div className="max-h-[560px] overflow-y-auto p-3">
        {dashboard.subtitle && <p className="mb-3 px-1 content-compact text-default-secondary">{dashboard.subtitle}</p>}
        <DashboardAskContext.Provider value={ask}>
          <CustomDashboardView dashboard={dashboard} dashboardId={data.id} revision={data.revision} />
        </DashboardAskContext.Provider>
      </div>
    </CardFrame>
  );
}
