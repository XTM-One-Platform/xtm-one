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
 * The version history of a fleet's documentation (#9768 S2, UX round 1 B1):
 * the version shown now, then the kept ones (`GET /fleets/{id}/documentation/history`,
 * newest first), each with its author and its relative and absolute date. A
 * reader who manages the fleet can "Restore this version": it is saved as the
 * next version (`POST /fleets/{id}/documentation/restore`), so the version it
 * replaces stays in the history and nothing is lost. No model call.
 *
 * For the people who manage the fleet, each version also lists what the
 * generator left out or derived (`notes`, #10246: generator defects, never
 * shown on the About page itself), and a version that lost a visual block or
 * misses a section is flagged "Needs a regeneration"; on the version shown
 * now that flag carries the Regenerate button.
 */
import { AlertTriangle, RotateCcw, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { RefusalAlert } from "@/components/ui/refusal-alert";
import {
  useFleetDocumentationHistory,
  useRestoreFleetDocumentationVersion,
  type FleetDocumentationVersion,
} from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { timeAgo } from "@/lib/utils";

import { authorKind, restoredLabel, versionAuthorLabel, versionNoteText } from "./fleet-documentation";
import { Spinner } from "@filigran/design-system";

export function FleetDocumentationHistoryDialog({
  fleetId,
  open,
  onOpenChange,
  canRestore,
  currentVersion,
  onRegenerate,
}: {
  fleetId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The reader manages the fleet: "Restore this version" is offered. */
  canRestore: boolean;
  /** The version the page shows, sent as the base so a newer save is refused (409). */
  currentVersion: number;
  /** Regenerates the documentation (one model call): offered on the version shown now when it needs it. */
  onRegenerate?: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" data-testid="about-history-dialog">
        <DialogHeader>
          <DialogTitle>{t_i18n("Version history")}</DialogTitle>
          <DialogDescription>
            {t_i18n("The versions of this documentation, newest first. A restored version is saved as the next version, so nothing is lost.")}
          </DialogDescription>
        </DialogHeader>
        {/* Mounted per opening: the list is read fresh each time. */}
        {open && (
          <HistoryList
            fleetId={fleetId}
            canRestore={canRestore}
            currentVersion={currentVersion}
            onRestored={() => onOpenChange(false)}
            onRegenerate={
              onRegenerate
                ? () => {
                    onOpenChange(false);
                    onRegenerate();
                  }
                : undefined
            }
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function HistoryList({
  fleetId,
  canRestore,
  currentVersion,
  onRestored,
  onRegenerate,
}: {
  fleetId: string;
  canRestore: boolean;
  currentVersion: number;
  onRestored: () => void;
  onRegenerate?: () => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const history = useFleetDocumentationHistory(fleetId);
  const restore = useRestoreFleetDocumentationVersion(fleetId);

  if (history.isLoading) {
    return (
      <p className="flex items-center gap-2 content-compact text-default-secondary">
        <Spinner size="sm" tone="inherit" />
        {t_i18n("Reading the version history")}
      </p>
    );
  }
  if (history.isError || !history.data) {
    return <RefusalAlert message={t_i18n("The version history could not be read. Try again in a moment.")} detail={history.error?.message} />;
  }
  const absolute = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" });
  const restoring = restore.isPending ? restore.variables?.version : undefined;

  return (
    <div className="space-y-3">
      {restore.isError && (
        <RefusalAlert message={t_i18n("This version could not be restored. Reload the page, then try again.")} detail={restore.error?.message} />
      )}
      <ul className="divide-y divide-[var(--border-elevation-subtle-soft)] rounded-sm border border-elevation-subtle" data-testid="about-history-list">
        {history.data.versions.map((entry: FleetDocumentationVersion) => (
          <li key={entry.version} className="flex items-start justify-between gap-3 px-3 py-2" data-testid="about-history-version">
            <div className="min-w-0 space-y-1">
              <p className="content-compact font-medium">
                {t_i18n("Version {version}", { version: entry.version })}
                {entry.current && <span className="ml-2 text-default-secondary">{t_i18n("Shown now")}</span>}
              </p>
              <p className="content-compact text-default-secondary">
                {entry.restored_from
                  ? restoredLabel(entry.restored_from, t_i18n)
                  : t_i18n(versionAuthorLabel(authorKind(entry.updated_by)))}
                {entry.updated_at
                  ? `, ${timeAgo(entry.updated_at, locale)} (${absolute.format(new Date(entry.updated_at))})`
                  : ""}
              </p>
              {entry.needs_regenerate && (
                <Badge variant="warning" className="gap-1" data-testid="about-history-needs-regenerate">
                  <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
                  {t_i18n("Needs a regeneration")}
                </Badge>
              )}
              {(entry.notes?.length ?? 0) > 0 && (
                <ul className="list-disc space-y-0.5 pl-5 content-compact text-default-secondary" data-testid="about-history-notes">
                  {(entry.notes ?? []).map((note, i) => {
                    const text = versionNoteText(note, t_i18n);
                    return text ? <li key={`${note.code}-${i}`}>{text}</li> : null;
                  })}
                </ul>
              )}
            </div>
            {entry.current && entry.needs_regenerate && onRegenerate && (
              <Button type="button" size="sm" className="shrink-0 gap-1.5" onClick={onRegenerate} data-testid="about-history-regenerate">
                <Sparkles className="h-4 w-4" aria-hidden />
                {t_i18n("Regenerate")}
              </Button>
            )}
            {canRestore && !entry.current && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="shrink-0 gap-1.5"
                aria-disabled={restore.isPending || undefined}
                onClick={() => {
                  if (restore.isPending) return;
                  restore.mutate({ version: entry.version, base_version: currentVersion }, { onSuccess: onRestored });
                }}
                data-testid="about-history-restore" aria-busy={(restoring === entry.version) || undefined}>
                {restoring === entry.version && <Spinner size="sm" tone="inherit" />}{!(restoring === entry.version) && <RotateCcw className="h-4 w-4" aria-hidden />}
                {t_i18n("Restore this version")}
              </Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
