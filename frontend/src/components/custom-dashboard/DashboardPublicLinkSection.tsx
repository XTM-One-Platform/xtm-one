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
 * The published link of a dashboard, in its Share dialog: anyone with the
 * address sees the dashboard without an account (`PublicLinkCard`: on / off,
 * copy, replace), and, once it is on, what they see - a snapshot of the data
 * read with the publisher's access, when it was read and how often it is
 * read again, and "Read again now"; and, when it reads OpenCTI, the highest
 * markings it shows (as an OpenCTI public dashboard's): data marked above them
 * is left out, and an agent's written answer is not kept. Managers only; an
 * administrator turns published links on for the platform (off by default).
 */
import { Spinner } from "@filigran/design-system";
import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { MultiSelect } from "@/components/ui/multi-select";
import { PublicLinkCard } from "@/components/ui/public-link-card";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useCustomDashboard, useDashboardPublicLink, useDashboardPublicLinkActions, usePlatformSchema } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { timeAgo } from "@/lib/utils";

/** How often a snapshot may be read again (`dashboard_public_links.REFRESH_CHOICES`). @i18n-keys */
const REFRESH_LABELS = {
  "15": "Every 15 minutes",
  "60": "Every hour",
  "240": "Every 4 hours",
  "1440": "Every day",
} as const;

export function DashboardPublicLinkSection({ dashboardId, open }: { dashboardId: string; open: boolean }) {
  const { t_i18n, locale } = useFormatter();
  const { toast } = useToast();
  const link = useDashboardPublicLink(dashboardId, open);
  const actions = useDashboardPublicLinkActions(dashboardId);
  const data = link.data;
  const busy =
    actions.publish.isPending || actions.unpublish.isPending || actions.setRefresh.isPending || actions.setCeiling.isPending || actions.readAgain.isPending;
  const failed = () => toast({ title: t_i18n("The published link could not be changed"), variant: "error" });
  return (
    <div className="flex flex-col gap-3" data-testid="dashboard.share.publicLink.section">
      <PublicLinkCard
        state={
          data
            ? {
                available: data.enabled_on_platform,
                enabled: data.path !== null,
                path: data.path,
                created_at: data.created_at,
                created_by_name: data.publisher_name,
              }
            : undefined
        }
        isLoading={link.isLoading}
        isError={link.isError}
        busy={busy}
        replacing={actions.publish.isPending}
        enable={() => actions.publish.mutateAsync({})}
        disable={() => actions.unpublish.mutateAsync()}
        regenerate={() => actions.publish.mutateAsync({ regenerate: true })}
        description={t_i18n(
          "Anyone with this link can view the dashboard without an account: a snapshot of its data, read with your access and read again from time to time. No source, input or click action is shown.",
        )}
        offConfirm={t_i18n(
          "Whoever has the link will be told the dashboard is not available. The people and groups it is shared with keep their access.",
        )}
        adminOffKept={t_i18n(
          "An administrator has turned published dashboard links off on this platform: this dashboard's link opens nothing until they are turned back on.",
        )}
        testIdPrefix="dashboard.share.publicLink"
      />
      {data?.path && data.enabled_on_platform && (
        <div className="flex flex-wrap items-center gap-2 rounded-sm border border-elevation-subtle-soft p-3 content-caption text-default-secondary">
          <span className="min-w-0 flex-1">
            {data.snapshot_at
              ? t_i18n("Data read {time} with {name}'s access.", { time: timeAgo(data.snapshot_at, locale), name: data.publisher_name ?? "?" })
              : t_i18n("The data has not been read yet.")}
          </span>
          <div className="w-44">
            <SearchableSelect
              aria-label={t_i18n("Read the data again")}
              value={String(data.refresh_minutes ?? 60)}
              onValueChange={(value) => actions.setRefresh.mutate(Number(value), { onError: failed })}
              options={(Object.keys(REFRESH_LABELS) as (keyof typeof REFRESH_LABELS)[]).map((key) => ({ value: key, label: t_i18n(REFRESH_LABELS[key]) }))}
              sort={false}
              searchable={false}
              disabled={busy}
            />
          </div>
          <Button size="sm" variant="outline" className="gap-1.5" disabled={busy} aria-busy={actions.readAgain.isPending || undefined} onClick={() => actions.readAgain.mutate(undefined, { onError: failed })}>
            {actions.readAgain.isPending && <Spinner size="sm" tone="inherit" />}
            {!actions.readAgain.isPending && <RefreshCw className="h-3.5 w-3.5" />}
            {t_i18n("Read again now")}
          </Button>
          <MarkingCeiling
            dashboardId={dashboardId}
            value={data.marking_ceiling ?? []}
            disabled={busy}
            onChange={(next) => actions.setCeiling.mutate(next, { onError: failed })}
          />
        </div>
      )}
    </div>
  );
}

/** The OpenCTI integration a dashboard reads: the first of its platform sources. */
function openctiIntegration(spec: unknown): string | null {
  const sources = (spec as { dataSources?: Record<string, { type?: string; options?: { source?: string; integration_id?: string } }> } | null)?.dataSources;
  const found = Object.values(sources ?? {}).find((source) => source?.type === "platform" && source.options?.source === "opencti");
  return found?.options?.integration_id ?? null;
}

/** The highest markings the link shows, picked among the OpenCTI markings of the integration the dashboard reads. */
function MarkingCeiling({ dashboardId, value, disabled, onChange }: { dashboardId: string; value: string[]; disabled: boolean; onChange: (next: string[]) => void }) {
  const { t_i18n } = useFormatter();
  const dashboard = useCustomDashboard(dashboardId);
  const integrationId = openctiIntegration(dashboard.data?.spec);
  const markings = usePlatformSchema(
    integrationId ? { source: "opencti", integration_id: integrationId, kind: "values", key: "objectMarking", perspective: "entities", search: "" } : null,
  );
  if (!integrationId) return null;
  // Picked by name (TLP:AMBER), what holds from one OpenCTI to another.
  const names = [...new Set([...(markings.data?.items ?? []).map((item) => item.label), ...value])];
  return (
    <div className="flex w-full flex-col gap-1" data-testid="dashboard.share.publicLink.ceiling">
      <span>{t_i18n("Highest markings shown")}</span>
      <MultiSelect
        options={names.map((name) => ({ value: name, label: name }))}
        selected={value}
        onToggle={(name) => {
          if (!disabled) onChange(value.includes(name) ? value.filter((v) => v !== name) : [...value, name]);
        }}
        placeholder={t_i18n("No ceiling: everything the publisher sees")}
        searchPlaceholder={t_i18n("Filter")}
        emptyOptionsLabel={t_i18n("No value found")}
        noMatchLabel={t_i18n("No value found")}
        sort={false}
      />
      <span className="text-default-disabled">
        {t_i18n("OpenCTI data marked above them, or with a marking of a type not picked, is left out; an agent's written answer is not kept.")}
      </span>
    </div>
  );
}
