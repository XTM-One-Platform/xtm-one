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
 * A dashboard's history: every kept version (who, when, what changed), a
 * preview of the one picked, and - for those who may edit it - "Restore",
 * which writes that version back as a NEW revision (an agent's refresh, a
 * colleague's edit or a restore are all undoable this way).
 *
 * The preview draws the version's widgets without live data: a platform
 * widget says what it reads, and draws it once restored. "Compare" sets the
 * picked version against another (the current one by default): the two
 * definitions side by side or unified, with which widgets changed.
 */
import { useMemo, useState } from "react";
import { Spinner } from "@filigran/design-system";
import { GitCompare, History, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { VersionDiffDialog } from "@/components/ui/version-diff-dialog";
import {
  useDashboardVersion,
  useDashboardVersionDiff,
  useDashboardVersions,
  useRestoreDashboardVersion,
} from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { cn, timeAgo } from "@/lib/utils";
import type { CustomDashboard, CustomDashboardVersion } from "@/types";

import { CustomDashboardView } from "./CustomDashboardView";
import { normalizeDashboard } from "./spec";

/** Where a version came from. @i18n-keys */
const SOURCE_LABELS = {
  agent: "By the assistant",
  manual: "Edited by hand",
  restore: "Restored",
  import: "Imported",
} as const satisfies Record<CustomDashboardVersion["source"], string>;

function VersionPreview({ dashboardId, revision }: { dashboardId: string; revision: number }) {
  const version = useDashboardVersion(dashboardId, revision);
  const spec = useMemo(() => (version.data ? normalizeDashboard(version.data.spec) : null), [version.data]);
  if (!spec) return <Skeleton className="h-64 w-full" />;
  return <CustomDashboardView dashboard={spec} />;
}

export function DashboardHistoryDialog({
  dashboard,
  open,
  onOpenChange,
}: {
  dashboard: CustomDashboard;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const { toast } = useToast();
  const versions = useDashboardVersions(dashboard.id, open);
  const restore = useRestoreDashboardVersion(dashboard.id);
  const [picked, setPicked] = useState<number | null>(null);
  const rows = versions.data ?? [];
  const selected = picked ?? rows[0]?.revision ?? null;
  // The version set against the picked one: chosen, else the current one, else the one before it.
  const [against, setAgainst] = useState<number | null>(null);
  const [comparing, setComparing] = useState(false);
  const others = rows.filter((version) => version.revision !== selected);
  const fallback = others.find((version) => version.revision === dashboard.revision) ?? others[0];
  const other = against !== null && against !== selected ? against : (fallback?.revision ?? null);
  // Older first, whichever was picked first.
  const pair = selected !== null && other !== null ? [Math.min(selected, other), Math.max(selected, other)] : null;
  const diff = useDashboardVersionDiff(dashboard.id, comparing && pair ? pair[0] : null, comparing && pair ? pair[1] : null);

  const restoreSelected = (revision: number) =>
    restore.mutate(
      { revision, current: dashboard.revision },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Revision {revision} restored", { revision }), variant: "success" });
          setPicked(null);
          onOpenChange(false);
        },
        onError: (error) =>
          toast({ title: t_i18n("The version could not be restored"), description: error.message, variant: "error" }),
      },
    );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-5xl" data-testid="dashboard.history.dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <History className="h-4 w-4 text-accent" />
            {t_i18n("Dashboard history")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n("Every change is kept: restoring a version saves it again as the newest one.")}
          </DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 gap-4 md:grid-cols-[16rem_1fr]">
          <ol className="flex max-h-[60vh] flex-col gap-1 overflow-y-auto" aria-label={t_i18n("Versions")}>
            {versions.isPending && <Skeleton className="h-40 w-full" />}
            {rows.length === 0 && !versions.isPending && (
              <li className="content-compact text-default-secondary">{t_i18n("No earlier version is kept yet.")}</li>
            )}
            {rows.map((version) => (
              <li key={version.revision}>
                <button
                  type="button"
                  onClick={() => setPicked(version.revision)}
                  aria-current={selected === version.revision}
                  className={cn(
                    "flex w-full flex-col gap-0.5 rounded-sm px-3 py-2 text-left transition-colors hover:bg-elevation-hover",
                    selected === version.revision && "bg-accent/10",
                  )}
                >
                  <span className="flex items-center justify-between gap-2 content-compact font-medium text-default-primary">
                    {t_i18n("Revision {revision}", { revision: version.revision })}
                    {version.revision === dashboard.revision && (
                      <span className="content-caption text-accent">{t_i18n("Current")}</span>
                    )}
                  </span>
                  <span className="content-caption text-default-secondary">
                    {t_i18n(SOURCE_LABELS[version.source])}
                    {version.author_name ? ` · ${version.author_name}` : ""}
                    {version.created_at ? ` · ${timeAgo(version.created_at, locale)}` : ""}
                  </span>
                  {version.summary && (
                    <span className="truncate content-caption text-default-secondary">{version.summary}</span>
                  )}
                </button>
              </li>
            ))}
          </ol>
          <div className="flex min-w-0 flex-col gap-3">
            {selected !== null && (
              <>
                <div className="max-h-[52vh] overflow-y-auto rounded-sm border border-elevation-subtle p-3">
                  <VersionPreview dashboardId={dashboard.id} revision={selected} />
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  {others.length > 0 && (
                    <>
                      <div className="w-48">
                        <SearchableSelect
                          aria-label={t_i18n("Compare with")}
                          value={other !== null ? String(other) : null}
                          onValueChange={(value) => setAgainst(Number(value))}
                          options={others.map((version) => ({
                            value: String(version.revision),
                            label:
                              version.revision === dashboard.revision
                                ? t_i18n("Revision {revision} (current)", { revision: version.revision })
                                : t_i18n("Revision {revision}", { revision: version.revision }),
                          }))}
                          sort={false}
                          searchable="auto"
                        />
                      </div>
                      <Button variant="outline" className="gap-1.5" disabled={other === null} aria-busy={(comparing && diff.isFetching) || undefined} onClick={() => setComparing(true)}>
                        {comparing && diff.isFetching && <Spinner size="sm" tone="inherit" />}
                        {!(comparing && diff.isFetching) && <GitCompare className="h-4 w-4" />}
                        {t_i18n("Compare")}
                      </Button>
                    </>
                  )}
                  {dashboard.can_manage && selected !== dashboard.revision && (
                    <Button className="gap-1.5" disabled={restore.isPending} aria-busy={restore.isPending || undefined} onClick={() => restoreSelected(selected)}>
                      {restore.isPending && <Spinner size="sm" tone="inherit" />}
                      {!restore.isPending && <RotateCcw className="h-4 w-4" />}
                      {t_i18n("Restore this version")}
                    </Button>
                  )}
                </div>
                {comparing && diff.isError && (
                  <p role="alert" className="content-caption text-feedback-error-primary">
                    {t_i18n("The versions could not be compared.")}
                  </p>
                )}
              </>
            )}
          </div>
        </div>
      </DialogContent>
      {pair && (
        <VersionDiffDialog
          open={comparing && !!diff.data}
          onOpenChange={(open) => !open && setComparing(false)}
          diff={diff.data ?? null}
          title={t_i18n("Revision {from} and revision {to}", { from: pair[0], to: pair[1] })}
          description={diff.data?.changes ?? t_i18n("No widget changed.")}
        />
      )}
    </Dialog>
  );
}
