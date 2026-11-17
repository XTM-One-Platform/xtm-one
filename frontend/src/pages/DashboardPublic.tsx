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
 * A dashboard opened by its published link - `/public/dashboards/:token`.
 *
 * The one dashboard page with no session: the token in the address is the
 * credential, and the page holds nothing else. It draws the dashboard as its
 * publisher sees it, from a snapshot of the data read on the server with the
 * publisher's access (`dashboard_public_links.py`; `DashboardAccessContext`,
 * kind `snapshot`): no read is live, no question is asked, and the page has
 * no inputs, click actions, menus, links into a platform, sharing or editing.
 * It says when the data was read. Files a canvas draws come from the
 * token-gated asset route, which serves only those. A link turned off,
 * replaced or never issued is one answer (404), and the page says so without
 * guessing which.
 */
import { useMemo } from "react";
import { useParams } from "react-router-dom";
import { AlertTriangle, Globe, LayoutGrid, RefreshCw } from "lucide-react";
import { Spinner } from "@filigran/design-system";

import { CustomDashboardView } from "@/components/custom-dashboard/CustomDashboardView";
import { normalizeDashboard } from "@/components/custom-dashboard/spec";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { DashboardAccessContext, type DashboardAccess } from "@/hooks/api/dashboard-access";
import { usePublicDashboard } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";

export default function DashboardPublic() {
  const { token = "" } = useParams<{ token: string }>();
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading, isError, error, refetch } = usePublicDashboard(token);
  const dashboard = useMemo(() => (data ? normalizeDashboard(data.definition) : null), [data]);
  const access = useMemo<DashboardAccess | null>(
    () => (data ? { kind: "snapshot", publicToken: token, snapshot: data.snapshot } : null),
    [data, token],
  );

  if (isLoading) {
    return (
      <div className="flex h-dvh items-center justify-center bg-elevation-default">
        <Spinner size="md" label={t_i18n("Loading...")} />
      </div>
    );
  }

  if (isError || !data || !dashboard || !access) {
    const gone = !error || error.status === 404;
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-3 bg-elevation-default px-6 text-center" data-testid="dashboard.public.unavailable">
        <AlertTriangle className="h-6 w-6 text-feedback-warning-primary" />
        <p className="content-base font-medium">
          {gone ? t_i18n("This link no longer opens a dashboard.") : t_i18n("The dashboard could not be loaded.")}
        </p>
        <p className="max-w-md content-base text-default-secondary">
          {gone
            ? t_i18n("Whoever shared it may have turned the link off or replaced it with a new one, or the dashboard was deleted. Ask them for a new link.")
            : t_i18n("Check your connection and try again.")}
        </p>
        {!gone && (
          <Button variant="outline" size="sm" onClick={() => void refetch()} className="gap-1.5">
            <RefreshCw className="h-3.5 w-3.5" /> {t_i18n("Try again")}
          </Button>
        )}
      </div>
    );
  }

  return (
    <div className="min-h-dvh bg-elevation-default text-default-primary" data-testid="dashboard.public">
      <header className="flex min-h-12 items-center gap-2 border-b border-elevation-subtle-soft px-4 py-2">
        <LayoutGrid className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
        <h1 className="min-w-0 truncate content-base font-medium">{data.title}</h1>
        <Tip content={t_i18n("Anyone with this link can view this dashboard")}>
          <span
            className="hidden shrink-0 items-center gap-1 rounded-sm border border-elevation-subtle-soft px-1.5 py-0.5 content-caption font-medium uppercase tracking-wide text-default-secondary sm:inline-flex"
            data-testid="dashboard.public.badge"
          >
            <Globe className="h-3 w-3" aria-hidden="true" />
            {t_i18n("Public")}
          </span>
        </Tip>
        {data.snapshot_at && (
          <span className="ml-auto shrink-0 content-caption text-default-secondary" data-testid="dashboard.public.read-at">
            {t_i18n("Data read {time}", { time: formatTimeAgo(intl, data.snapshot_at) })}
          </span>
        )}
      </header>
      <main className="p-4 sm:p-6">
        <DashboardAccessContext.Provider value={access}>
          <CustomDashboardView dashboard={dashboard} dashboardId={`public:${token}`} />
        </DashboardAccessContext.Provider>
      </main>
    </div>
  );
}
