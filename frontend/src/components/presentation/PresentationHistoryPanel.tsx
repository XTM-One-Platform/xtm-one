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
 * Revision history of a deck — the side panel behind the toolbar's
 * `History` button, for decks and pages alike.
 *
 * Every save snapshots the spec into a `PresentationVersion`: the agent's
 * `generate_presentation` (with its own `change_summary`), a manual edit from
 * the editor panel, a restore, the copy a duplicate started from. The newest
 * `PRESENTATION_VERSION_RETENTION` are kept. Selecting a revision PREVIEWS it:
 * `onPreview` hands the host the stored spec and the stage renders it in place
 * of the current deck — the same mechanism the editor panel's live draft uses —
 * so a collaborator's change is inspected on the real canvas, not in a diff.
 * Restoring (managers only) brings the revision back as a NEW revision; nothing
 * is ever rewritten, so a restore is itself undoable from this list.
 */
import { useEffect } from "react";
import {
  Bot,
  Copy,
  Eye,
  History,
  Pencil,
  RotateCcw,
  Undo2,
  Upload,
  X,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  usePresentationVersion,
  usePresentationVersions,
  useRestorePresentationVersion,
} from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import { cn, pluralize, timeAgo } from "@/lib/utils";
import type { Presentation, PresentationSpec, PresentationVersion, PresentationVersionSource } from "@/types";

import { DECK_KEYS_OFF_ATTR } from "./deck-keys";
import { UserInitials } from "./PresenceAvatars";
import { Spinner } from "@filigran/design-system";

/** `PRESENTATION_VERSION_RETENTION` in `models/presentation.py`. */
const VERSION_RETENTION = 30;

/** Where a revision came from — icon + label per `VERSION_SOURCES`. */
const SOURCE_CONFIG: Record<PresentationVersionSource, { icon: LucideIcon; label: string }> = {
  agent: { icon: Bot, label: "Assistant" },
  manual: { icon: Pencil, label: "Manual edit" },
  restore: { icon: Undo2, label: "Restore" },
  duplicate: { icon: Copy, label: "Duplicate" },
  import: { icon: Upload, label: "Import" },
};

export function PresentationHistoryPanel({
  presentation,
  selected,
  onSelect,
  onPreview,
  onClose,
  className,
}: {
  presentation: Presentation;
  /** The revision being previewed — owned by the host, which also draws the
   *  "previewing" banner over the stage and can clear it from there. */
  selected: number | null;
  onSelect: (revision: number | null) => void;
  /**
   * The spec of the revision being previewed, `null` when the current deck
   * should show again (selection cleared, panel closed, restore done). The
   * host renders it in place of the stored spec.
   */
  onPreview: (spec: PresentationSpec | null) => void;
  onClose: () => void;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const isPage = presentation.kind === "page";
  const unit = isPage ? "page" : "slide";

  const { data: versions, isLoading } = usePresentationVersions(presentation.id, presentation.revision);
  const { data: preview, isLoading: previewLoading } = usePresentationVersion(presentation.id, selected);
  const restore = useRestorePresentationVersion();

  // The current revision is never "previewed": selecting it is clearing.
  const previewing = selected !== null && selected !== presentation.revision;

  // Hand the host the revision to render as soon as its spec arrives; back to
  // the current deck when the selection is cleared or the panel closes.
  useEffect(() => {
    onPreview(previewing && preview?.spec ? preview.spec : null);
  }, [previewing, preview, onPreview]);
  useEffect(
    () => () => {
      onPreview(null);
      onSelect(null);
    },
    [onPreview, onSelect],
  );

  // A new revision landed (a save, a collaborator's edit, our own restore):
  // what was being previewed is now history of a different present — show the
  // deck as it is rather than an older revision over a newer one.
  useEffect(() => {
    onSelect(null);
  }, [presentation.revision, onSelect]);

  const describe = (version: PresentationVersion): string => {
    if (version.summary) return version.summary;
    switch (version.source) {
      case "agent":
        return t_i18n("Generated by the assistant");
      case "manual":
        return t_i18n("Edited in the editor");
      case "restore":
        return version.restored_from_revision !== null
          ? t_i18n("Restored revision {revision}", { revision: version.restored_from_revision })
          : t_i18n("Restored an earlier revision");
      case "duplicate":
        return t_i18n("Copied from another presentation");
      default:
        return "";
    }
  };

  const doRestore = async (version: PresentationVersion) => {
    const ok = await confirm({
      title: t_i18n("Restore revision {revision}?", { revision: version.revision }),
      description: t_i18n(
        "The presentation goes back to this revision as a new revision {next}. Nothing is lost - every revision stays in this list.",
        { next: presentation.revision + 1 },
      ),
      confirmLabel: t_i18n("Restore"),
      variant: "warning",
    });
    if (!ok) return;
    try {
      await restore.mutateAsync({ id: presentation.id, revision: version.revision });
      onSelect(null);
      toast({
        title: t_i18n("Revision {revision} restored", { revision: version.revision }),
        variant: "success",
      });
    } catch (err) {
      toast({
        title: t_i18n("Could not restore this revision"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  const selectedVersion = versions?.find((v) => v.revision === selected) ?? null;

  return (
    <aside
      className={cn(
        "flex h-full w-full min-w-0 flex-col border-l border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm",
        className,
      )}
      aria-label={t_i18n("History")}
      {...DECK_KEYS_OFF_ATTR}
    >
      <div className="flex items-center justify-between border-b border-elevation-subtle-soft px-4 py-3">
        <div>
          <h2 className="content-base font-semibold">{t_i18n("History")}</h2>
          <p className="content-compact text-default-secondary">
            {t_i18n("Every change is kept, one entry per editing session (the last {count}). Click a revision to preview it.", {
              count: VERSION_RETENTION,
            })}
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label={t_i18n("Close")}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {isLoading ? (
          <div className="flex justify-center py-8">
            <Spinner size="md" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
          </div>
        ) : !versions || versions.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
            <History className="h-6 w-6 text-default-secondary" aria-hidden="true" />
            <p className="text-[12.5px] text-default-secondary">
              {t_i18n("No revisions recorded yet. The next save will start the history.")}
            </p>
          </div>
        ) : (
          <ol className="relative space-y-1.5">
            {versions.map((version) => {
              const isCurrent = version.revision === presentation.revision;
              const isSelected = selected === version.revision;
              const source = SOURCE_CONFIG[version.source] ?? SOURCE_CONFIG.manual;
              const SourceIcon = source.icon;
              return (
                <li key={version.id}>
                  <button
                    type="button"
                    onClick={() => onSelect(isCurrent || isSelected ? null : version.revision)}
                    aria-pressed={isSelected}
                    className={cn(
                      "w-full rounded-sm border px-3 py-2.5 text-left transition-colors",
                      isSelected
                        ? "border-accent/50 bg-accent/[0.06] shadow-[0_0_0_1px_color-mix(in_srgb,_var(--color-accent)_calc(0.25_*_100%),_transparent)]"
                        : "border-elevation-subtle-soft bg-elevation-default hover:bg-elevation-hover",
                      isCurrent && !isSelected && "border-accent/25",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span className="shrink-0 rounded-sm bg-elevation-hover px-1.5 py-0.5 font-mono-plex content-compact tabular-nums text-default-primary">
                        v{version.revision}
                      </span>
                      <Badge variant="outline" className="shrink-0 gap-1 px-1.5 py-0">
                        <SourceIcon className="h-2.5 w-2.5" />
                        {t_i18n(source.label)}
                      </Badge>
                      <span className="ml-auto flex shrink-0 items-center gap-1">
                        {isCurrent && (
                          <Badge variant="cyan" className="px-1.5 py-0">
                            {t_i18n("Current")}
                          </Badge>
                        )}
                        {isSelected && (
                          <Badge variant="cyan" className="gap-1 px-1.5 py-0">
                            {previewLoading && <Spinner size="sm" tone="inherit" />}{!previewLoading && <Eye className="h-2.5 w-2.5" />}
                            {t_i18n("Previewing")}
                          </Badge>
                        )}
                      </span>
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-[12.5px] leading-snug text-default-secondary">
                      {describe(version)}
                    </p>
                    <div className="mt-1.5 flex items-center gap-1.5 content-compact text-default-secondary">
                      <UserInitials userId={version.author_id} name={version.author_name} size="sm" />
                      <span className="truncate">
                        {version.author_name?.trim() || t_i18n("Assistant")}
                      </span>
                      <span aria-hidden="true">·</span>
                      {/* An edit session reads as its latest save. */}
                      {(version.updated_at ?? version.created_at) && (
                        <span className="shrink-0">{timeAgo((version.updated_at ?? version.created_at) as string)}</span>
                      )}
                      <span className="ml-auto shrink-0 tabular-nums">
                        {version.slide_count} {t_i18n(pluralize(version.slide_count, unit))}
                      </span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ol>
        )}
      </div>

      <div className="flex items-center justify-between gap-2 border-t border-elevation-subtle-soft px-4 py-3">
        {previewing && selectedVersion ? (
          <>
            <span className="min-w-0 truncate content-compact text-default-secondary">
              {t_i18n("Previewing revision {revision}", { revision: selectedVersion.revision })}
            </span>
            <div className="flex shrink-0 items-center gap-2">
              <Button variant="ghost" size="sm" onClick={() => onSelect(null)}>
                {t_i18n("Back to current")}
              </Button>
              {presentation.can_manage && (
                <Tip content={t_i18n("Bring this revision back as the current one")}>
                  <Button
                    size="sm"
                    className="gap-1.5"
                    onClick={() => void doRestore(selectedVersion)}
                    disabled={restore.isPending || previewLoading} aria-busy={restore.isPending || undefined}>
                    {restore.isPending && <Spinner size="sm" tone="inherit" />}{!restore.isPending && <RotateCcw className="h-3.5 w-3.5" />}
                    {t_i18n("Restore")}
                  </Button>
                </Tip>
              )}
            </div>
          </>
        ) : (
          <span className="content-compact tabular-nums text-default-secondary">
            {t_i18n("Revision {revision}", { revision: presentation.revision })}
            {versions && versions.length > 0 && (
              <>
                {" · "}
                {t_i18n("{count} kept", { count: versions.length })}
              </>
            )}
          </span>
        )}
      </div>
      {ConfirmEl}
    </aside>
  );
}
