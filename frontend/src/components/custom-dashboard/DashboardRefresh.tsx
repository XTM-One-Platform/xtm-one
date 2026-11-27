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
 * A dashboard's Refresh, as OpenCTI's: a button reading every widget again from its platform, and an interval
 * doing it on its own (Off, 1, 5, 15 or 30 minutes, an hour). The interval is the viewer's, kept in this
 * browser for this dashboard; reads stay under each viewer's rate limit. In a studio bar (`compact`) it is one
 * icon: Refresh, and the interval in its menu (`More actions`' vocabulary), the button carrying the interval's
 * dot while one is set.
 */
import { useEffect, useState } from "react";
import { useIsFetching } from "@tanstack/react-query";
import { ChevronDown, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Tip } from "@/components/ui/tooltip";
import { useRefreshDashboard } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/** Each interval's label, in minutes (0: off). @i18n-keys */
const INTERVALS = {
  "0": "No auto-refresh",
  "1": "Every minute",
  "5": "Every 5 minutes",
  "15": "Every 15 minutes",
  "30": "Every 30 minutes",
  "60": "Every hour",
} as const;
type Interval = keyof typeof INTERVALS;

const storageKey = (dashboardId: string) => `xtm-dashboard-refresh:${dashboardId}`;

function storedInterval(dashboardId: string): Interval {
  try {
    const stored = window.localStorage.getItem(storageKey(dashboardId));
    return stored && stored in INTERVALS ? (stored as Interval) : "0";
  } catch {
    return "0";
  }
}

export function DashboardRefresh({ dashboardId, className, compact = false }: { dashboardId: string; className?: string; compact?: boolean }) {
  const { t_i18n } = useFormatter();
  const refresh = useRefreshDashboard(dashboardId);
  const reading = useIsFetching({ queryKey: ["custom-dashboards", dashboardId, "widget-data"] }) > 0;
  const [interval, setInterval_] = useState<Interval>(() => storedInterval(dashboardId));

  useEffect(() => {
    const minutes = Number(interval);
    if (!minutes) return;
    const timer = window.setInterval(() => void refresh(), minutes * 60_000);
    return () => window.clearInterval(timer);
  }, [interval, refresh]);

  const choose = (next: string) => {
    const picked = next in INTERVALS ? (next as Interval) : "0";
    setInterval_(picked);
    try {
      window.localStorage.setItem(storageKey(dashboardId), picked);
    } catch {
      // A browser that keeps nothing still refreshes while the page is open.
    }
  };

  if (compact) {
    const label = interval === "0" ? t_i18n("Refresh") : `${t_i18n("Refresh")} - ${t_i18n(INTERVALS[interval])}`;
    return (
      <div className={cn("flex items-center", className)} data-testid="dashboard.refresh">
        <Tip content={label}>
          <Button variant="ghost" size="sm" className="relative h-8 w-8 px-0" aria-label={label} onClick={() => void refresh()} disabled={reading}>
            <RefreshCw className={cn("h-4 w-4", reading && "animate-spin")} aria-hidden="true" />
            {interval !== "0" && (
              <span aria-hidden="true" className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-accent" data-testid="dashboard.refresh.auto" />
            )}
          </Button>
        </Tip>
        <OverflowMenu
          label={t_i18n("Auto-refresh")}
          header={t_i18n("Auto-refresh")}
          trigger={<ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />}
          className="h-8 w-5 px-0"
          items={(Object.keys(INTERVALS) as Interval[]).map((key) => ({
            id: `interval-${key}`,
            label: t_i18n(INTERVALS[key]),
            checked: key === interval,
            onSelect: () => choose(key),
          }))}
        />
      </div>
    );
  }

  return (
    <div className={cn("flex items-center gap-1", className)} data-testid="dashboard.refresh">
      <Tip content={t_i18n("Refresh")}>
        <Button variant="ghost" size="sm" aria-label={t_i18n("Refresh")} onClick={() => void refresh()} disabled={reading}>
          <RefreshCw className={cn("h-4 w-4", reading && "animate-spin")} aria-hidden="true" />
        </Button>
      </Tip>
      <SearchableSelect
        aria-label={t_i18n("Auto-refresh")}
        value={interval}
        onValueChange={choose}
        options={(Object.keys(INTERVALS) as Interval[]).map((key) => ({ value: key, label: t_i18n(INTERVALS[key]) }))}
        sort={false}
        searchable={false}
        className="h-8 w-44 content-compact"
      />
    </div>
  );
}
