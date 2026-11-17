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
 * browser for this dashboard; reads stay under each viewer's rate limit.
 */
import { useEffect, useState } from "react";
import { useIsFetching } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
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

export function DashboardRefresh({ dashboardId, className }: { dashboardId: string; className?: string }) {
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
