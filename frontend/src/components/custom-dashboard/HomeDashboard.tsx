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
 * The custom dashboard a person chose as their dashboard (`User.home_dashboard_id`),
 * shown on `/dashboard` (and on the home page when it is the dashboard) in place
 * of the platform's.
 *
 * The page stays the platform's when the dashboard cannot be read (deleted
 * meanwhile, a failed request): `fallback` is the platform's dashboard, so the
 * route never ends on an error. Rearranging, refining and deleting stay on the
 * dashboard's own page (`/dashboards/:id`); here, the way back to the standard
 * dashboard.
 */
import { useMemo, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { LayoutGrid, Maximize2, Undo2 } from "lucide-react";

import { DashboardAskContext } from "@/components/custom-dashboard/ask";
import { useAskAssistant } from "@/lib/ask-assistant";
import { CustomDashboardView } from "@/components/custom-dashboard/CustomDashboardView";
import { normalizeDashboard } from "@/components/custom-dashboard/spec";
import { Button } from "@/components/ui/button";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useCustomDashboard } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { timeAgo } from "@/lib/utils";

function HomeDashboard({ dashboardId, fallback }: { dashboardId: string; fallback: ReactNode }) {
  const { t_i18n, locale } = useFormatter();
  const ask = useAskAssistant();
  const navigate = useNavigate();
  const updateProfile = useUpdateProfile();
  const { data, isLoading, isError } = useCustomDashboard(dashboardId);
  const dashboard = useMemo(() => (data ? normalizeDashboard(data.spec) : null), [data]);

  if (isLoading) return <PageSkeleton cards={4} />;
  if (isError || !data || !dashboard) return <>{fallback}</>;

  return (
    <div className="flex w-full flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <h1 className="flex items-center gap-2 text-3xl font-bold">
            <LayoutGrid className="h-6 w-6 shrink-0 text-accent" aria-hidden="true" />
            <span className="min-w-0 break-words">{data.title}</span>
          </h1>
          {dashboard.subtitle && <p className="content-base text-default-secondary">{dashboard.subtitle}</p>}
          {data.updated_at && (
            <p className="content-caption text-default-secondary">
              {t_i18n("Updated {when}", { when: timeAgo(data.updated_at, locale) })}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" className="gap-1.5" asChild>
            <Link to={`/dashboards/${data.id}`}>
              <Maximize2 className="h-4 w-4" /> {t_i18n("Open the dashboard")}
            </Link>
          </Button>
          <Button
            variant="ghost"
            className="gap-1.5"
            disabled={updateProfile.isPending}
            onClick={() => updateProfile.mutate({ home_dashboard_id: null })}
          >
            <Undo2 className="h-4 w-4" /> {t_i18n("Use the standard dashboard")}
          </Button>
        </div>
      </div>
      <DashboardAskContext.Provider value={data.can_manage ? ask : null}>
        <CustomDashboardView dashboard={dashboard} dashboardId={data.id} revision={data.revision} editing={null} onNavigate={navigate} />
      </DashboardAskContext.Provider>
    </div>
  );
}
export default HomeDashboard;
