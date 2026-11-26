// SPDX-License-Identifier: Apache-2.0

/**
 * Restore the primary assets of a past profile version (#1558, frontend of #1473).
 *
 * Every save of the organization profile is a version. This dialog lists them
 * with their primary-asset count, previews the rows of the one picked, and
 * restores its primary assets alone as a new version: the other fields stay as
 * they are, and a version saved meanwhile refuses the restore (409).
 */
import { useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import {
  useOrganizationContextVersion,
  useOrganizationContextVersions,
  useRestoreOrganizationContextVersion,
} from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { CRITICALITY_LABELS } from "@/lib/organization-profile";
import { cn } from "@/lib/utils";

function VersionRows({ version }: { version: number }) {
  const { t_i18n } = useFormatter();
  const past = useOrganizationContextVersion(version);
  if (!past.data) return <Skeleton className="h-32 w-full" />;
  const primaryAssets = past.data.profile.primary_assets ?? [];
  if (primaryAssets.length === 0) return <p className="text-default-secondary">{t_i18n("This version holds no primary asset.")}</p>;
  return (
    <ul className="space-y-1" aria-label={t_i18n("Primary assets of this version")} data-testid="settings.organization.restore-preview">
      {primaryAssets.map((pa, index) => (
        <li key={`${pa.name}-${index}`} className="flex items-center justify-between gap-2 content-compact">
          <span className="min-w-0 truncate text-default-primary">{pa.name}</span>
          <Badge variant="outline">{t_i18n(CRITICALITY_LABELS[pa.criticality as keyof typeof CRITICALITY_LABELS] ?? CRITICALITY_LABELS.high)}</Badge>
        </li>
      ))}
    </ul>
  );
}

export function OrganizationPrimaryAssetsRestoreDialog({
  open,
  onOpenChange,
  currentVersion,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The version on screen: the restore starts from it, so one saved meanwhile is refused. */
  currentVersion: number | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const versions = useOrganizationContextVersions(open);
  const restore = useRestoreOrganizationContextVersion();
  // The current version restores nothing: it is listed, never pickable.
  const rows = (versions.data ?? []).filter((row) => row.version !== currentVersion);
  const [picked, setPicked] = useState<number | null>(null);
  const selected = picked ?? rows.find((row) => (row.primary_assets ?? 0) > 0)?.version ?? null;

  // A refused restore shows inline above the footer; a 409 (a version saved since) offers a reload of the list.
  const refusedConflict = (restore.error as { status?: number } | null)?.status === 409;

  const close = (next: boolean) => {
    if (!next) {
      setPicked(null);
      restore.reset();
    }
    onOpenChange(next);
  };
  const reloadVersions = () => {
    restore.reset();
    setPicked(null);
    void versions.refetch();
  };
  const restoreSelected = (version: number) =>
    restore.mutate(
      { version, fields: ["primary_assets"], based_on_version: currentVersion },
      {
        onSuccess: (saved) => {
          toast({
            title: t_i18n("{count, plural, =0 {No primary asset restored from version {version}} one {# primary asset restored from version {version}} other {# primary assets restored from version {version}}}", {
              count: saved.profile.primary_assets?.length ?? 0,
              version,
            }),
            variant: "success",
          });
          close(false);
        },
      },
    );

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-3xl" data-testid="settings.organization.restore-dialog">
        <DialogHeader>
          <DialogTitle>{t_i18n("Restore primary assets from a past version")}</DialogTitle>
          <DialogDescription>
            {t_i18n("The primary assets of the version you pick replace the current ones, saved as a new version. The other fields stay as they are.")}
          </DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 gap-4 md:grid-cols-[15rem_1fr]">
          <ol className="flex max-h-[55vh] flex-col gap-1 overflow-y-auto" aria-label={t_i18n("Versions")}>
            {versions.isPending && <Skeleton className="h-40 w-full" />}
            {!versions.isPending && rows.length === 0 && (
              <li className="content-compact text-default-secondary">{t_i18n("No earlier version is kept yet.")}</li>
            )}
            {rows.map((row) => (
              <li key={row.version}>
                <button
                  type="button"
                  onClick={() => {
                    restore.reset();
                    setPicked(row.version);
                  }}
                  aria-current={selected === row.version}
                  data-testid="settings.organization.restore-version"
                  className={cn(
                    "flex w-full flex-col gap-0.5 rounded-sm px-3 py-2 text-left transition-colors hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                    selected === row.version && "bg-accent/10",
                  )}
                >
                  <span className="content-compact font-medium text-default-primary">{t_i18n("Version {version}", { version: row.version })}</span>
                  <span className="content-compact text-default-secondary">
                    {t_i18n("{count, plural, =0 {No primary asset} one {# primary asset} other {# primary assets}}", { count: row.primary_assets ?? 0 })}
                    {row.created_at ? ` · ${intl.formatDate(new Date(row.created_at), { dateStyle: "medium", timeStyle: "short" })}` : ""}
                  </span>
                </button>
              </li>
            ))}
          </ol>
          <div className="min-w-0 max-h-[55vh] overflow-y-auto rounded-sm border border-elevation-subtle p-3">
            {selected !== null ? <VersionRows version={selected} /> : <p className="text-default-secondary">{t_i18n("Pick a version to see its primary assets.")}</p>}
          </div>
        </div>
        {restore.isError && (
          <div role="alert" className="flex flex-wrap items-center gap-2 content-compact text-feedback-error-tertiary" data-testid="settings.organization.restore-error">
            <span>{t_i18n("The primary assets could not be restored: {reason}", { reason: restore.error.message })}</span>
            {refusedConflict && (
              <Button variant="secondary" size="sm" onClick={reloadVersions}>
                {t_i18n("Reload the versions")}
              </Button>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="secondary" onClick={() => close(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            className="gap-1.5"
            disabled={selected === null || restore.isPending}
            aria-busy={restore.isPending || undefined}
            onClick={() => selected !== null && restoreSelected(selected)}
          >
            {restore.isPending && <Spinner size="sm" tone="inherit" />}
            {!restore.isPending && <Icon name="rotate-ccw" size={16} aria-hidden />}
            {selected !== null ? t_i18n("Restore version {version}", { version: selected }) : t_i18n("Restore")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
