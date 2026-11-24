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
 * The builder toolbar.
 *
 * Name, status, and every action that applies to the flow as a whole. Each
 * button states its shortcut in the tooltip, since the shortcuts are the point
 * of a canvas builder and a menu nobody reads is where they go to die.
 */

import { useEffect, useState } from "react";
import {
  Compass,
  Download,
  Grid3x3,
  Magnet,
  History,
  LayoutGrid,
  ListChecks,
  Play,
  Power,
  PowerOff,
  Redo2,
  Save,
  Undo2,
  Upload,
  UserCog,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tip } from "@/components/ui/tooltip";
import { BackButton } from "@/components/ui/back-button";
import { OwnerDeactivatedBadge } from "@/components/ui/owner-deactivated-notice";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { FlowStatus, FlowValidationIssue } from "@/types";
import { Spinner } from "@filigran/design-system";

/** ⌘ on Mac, Ctrl elsewhere — the tooltip must match the key that works. */
const MOD =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform)
    ? "⌘"
    : "Ctrl";

interface BuilderToolbarProps {
  name: string;
  status: FlowStatus;
  versionNumber: number;
  isDirty: boolean;
  isSaving: boolean;
  canUndo: boolean;
  canRedo: boolean;
  issues: FlowValidationIssue[];
  readOnly?: boolean;
  isRunning?: boolean;
  isActivating?: boolean;
  /** The graph contains an AI step and AI is off, so it cannot run. */
  heldBackByAi?: boolean;
  /** Not company-managed and its owner's account is deactivated. */
  pausedByOwner?: boolean;
  onRun?: () => void;
  onToggleActive?: () => void;
  onToggleRuns?: () => void;
  /** The Platform Health panel (#8261); omitted when the flag is off or the viewer may not manage the flow. */
  onToggleHealth?: () => void;
  onNameChange: (name: string) => void;
  onSave: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onAutoLayout: () => void;
  showGrid: boolean;
  onToggleGrid: () => void;
  snapGuides: boolean;
  onToggleSnapGuides: () => void;
  onExport: () => void;
  onImport: () => void;
  onToggleVersions: () => void;
  /** Opens the Owner and access drawer. Omitted (no button) for non-admins. */
  onToggleOwnership?: () => void;
}

export function BuilderToolbar({
  name,
  status,
  versionNumber,
  isDirty,
  isSaving,
  canUndo,
  canRedo,
  issues,
  readOnly,
  isRunning,
  isActivating,
  heldBackByAi = false,
  pausedByOwner = false,
  onRun,
  onToggleActive,
  onToggleRuns,
  onToggleHealth,
  onNameChange,
  onSave,
  onUndo,
  onRedo,
  onAutoLayout,
  showGrid,
  onToggleGrid,
  snapGuides,
  onToggleSnapGuides,
  onExport,
  onImport,
  onToggleVersions,
  onToggleOwnership,
}: BuilderToolbarProps) {
  const { t_i18n } = useFormatter();
  // Local while typing so each keystroke does not rebuild the whole canvas;
  // the parent is told on blur.
  const [draftName, setDraftName] = useState(name);
  useEffect(() => setDraftName(name), [name]);

  const errors = issues.filter((i) => i.severity === "error").length;
  const warnings = issues.filter((i) => i.severity === "warning").length;
  const isActive = status === "active";

  return (
    // Wraps instead of clipping: at 1024 the actions moved to a
    // second row rather than hiding Run past the edge and squeezing the name.
    <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-elevation-subtle layer-1 bg-elevation-default px-3 py-2">
      <BackButton to="/flows/library" label="Back to Flows" />

      <Input
        value={draftName}
        disabled={readOnly}
        onChange={(e) => setDraftName(e.target.value)}
        onBlur={() => draftName !== name && onNameChange(draftName)}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
        className="h-8 min-w-40 flex-1 max-w-xs border-transparent bg-transparent content-base font-medium hover:border-elevation-subtle focus:border-elevation-subtle"
        aria-label={t_i18n("Flow name")}
      />

      <Badge
        variant={isActive ? "success" : "outline"}
        className="shrink-0 content-caption capitalize"
      >
        {t_i18n(status)}
      </Badge>
      {pausedByOwner && <OwnerDeactivatedBadge entity="flow" />}
      <span className="shrink-0 content-caption text-default-secondary">
        v{versionNumber}
      </span>

      {errors > 0 && (
        <span className="shrink-0 content-caption text-feedback-error-primary">
          {t_i18n("{count, plural, one {# error} other {# errors}}", { count: errors })}
        </span>
      )}
      {errors === 0 && warnings > 0 && (
        <span className="shrink-0 content-caption text-feedback-warning-primary">
          {t_i18n("{count} warnings", { count: warnings })}
        </span>
      )}

      {/* `Tip`'s `wrap` injects `aria-disabled="true"` — it exists so a tooltip
          still fires over a natively-disabled control, which swallows pointer
          events. Passing it unconditionally would announce an ENABLED button as
          disabled, so each tooltip wraps only while its control really is. */}
      <div className="ml-auto flex flex-wrap items-center justify-end gap-1">
        <Tip content={`${t_i18n("Undo")} (${MOD}Z)`} wrap={!canUndo || !!readOnly}>
          <Button
            variant="ghost"
            size="icon"
            disabled={!canUndo || readOnly}
            onClick={onUndo}
            aria-label={t_i18n("Undo")}
          >
            <Undo2 className="h-4 w-4" />
          </Button>
        </Tip>
        <Tip content={`${t_i18n("Redo")} (${MOD}⇧Z)`} wrap={!canRedo || !!readOnly}>
          <Button
            variant="ghost"
            size="icon"
            disabled={!canRedo || readOnly}
            onClick={onRedo}
            aria-label={t_i18n("Redo")}
          >
            <Redo2 className="h-4 w-4" />
          </Button>
        </Tip>
        <Tip
          content={
            showGrid ? t_i18n("Hide the guide grid") : t_i18n("Show the guide grid")
          }
        >
          <Button
            variant="ghost"
            size="icon"
            className={cn(showGrid && "bg-accent/10 text-accent")}
            onClick={onToggleGrid}
            aria-label={t_i18n("Toggle the guide grid")}
          >
            <Grid3x3 className="h-4 w-4" />
          </Button>
        </Tip>
        <Tip
          content={
            snapGuides
              ? t_i18n("Disable alignment guides")
              : t_i18n("Enable alignment guides (snap while dragging)")
          }
          wrap={!!readOnly}
        >
          <Button
            variant="ghost"
            size="icon"
            disabled={readOnly}
            className={cn(snapGuides && "bg-accent/10 text-accent")}
            onClick={onToggleSnapGuides}
            aria-label={t_i18n("Toggle alignment guides")}
          >
            <Magnet className="h-4 w-4" />
          </Button>
        </Tip>
        <Tip content={t_i18n("Tidy up the layout")} wrap={!!readOnly}>
          <Button
            variant="ghost"
            size="icon"
            disabled={readOnly}
            onClick={onAutoLayout}
            aria-label={t_i18n("Auto-layout")}
          >
            <LayoutGrid className="h-4 w-4" />
          </Button>
        </Tip>
        <Tip content={t_i18n("Version history")}>
          <Button
            variant="ghost"
            size="icon"
            onClick={onToggleVersions}
            aria-label={t_i18n("Version history")}
          >
            <History className="h-4 w-4" />
          </Button>
        </Tip>
        {onToggleOwnership && (
          <Tip content={t_i18n("Owner and access")}>
            <Button
              variant="ghost"
              size="icon"
              onClick={onToggleOwnership}
              aria-label={t_i18n("Owner and access")}
            >
              <UserCog className="h-4 w-4" />
            </Button>
          </Tip>
        )}
        <Tip content={t_i18n("Export as JSON")}>
          <Button
            variant="ghost"
            size="icon"
            onClick={onExport}
            aria-label={t_i18n("Export as JSON")}
          >
            <Download className="h-4 w-4" />
          </Button>
        </Tip>
        <Tip content={t_i18n("Import a flow into a new draft")}>
          <Button
            variant="ghost"
            size="icon"
            onClick={onImport}
            aria-label={t_i18n("Import")}
          >
            <Upload className="h-4 w-4" />
          </Button>
        </Tip>

        <span className="mx-1 h-5 w-px shrink-0 bg-[var(--border-elevation-subtle)]" aria-hidden="true" />
        {onToggleHealth && (
          <Tip content={t_i18n("Improvements")}>
            <Button variant="ghost" size="icon" onClick={onToggleHealth} aria-label={t_i18n("Improvements")}>
              <Compass className="h-4 w-4" />
            </Button>
          </Tip>
        )}
        <Tip content={t_i18n("Run history")}>
          <Button
            variant="ghost"
            size="icon"
            onClick={onToggleRuns}
            aria-label={t_i18n("Run history")}
          >
            <ListChecks className="h-4 w-4" />
          </Button>
        </Tip>
        <Tip
          content={
            isActive
              ? t_i18n("Deactivate: triggers stop firing")
              : t_i18n("Activate: triggers go live")
          }
          wrap={!!readOnly || isActivating}
        >
          <Button
            variant="ghost"
            size="icon"
            disabled={readOnly || isActivating}
            onClick={onToggleActive}
            aria-label={isActive ? t_i18n("Deactivate") : t_i18n("Activate")}
          >
            {isActive ? (
              <Power className="h-4 w-4 text-feedback-success-primary" />
            ) : (
              <PowerOff className="h-4 w-4 text-default-secondary" />
            )}
          </Button>
        </Tip>
        {/* Runs work on drafts too — this is also the test button.
            Unsaved edits would silently run the OLD graph, so a dirty
            canvas disables it and the tooltip says to save first. */}
        <Tip
          content={
            pausedByOwner
              ? t_i18n("Paused: the owner's account is deactivated")
              : heldBackByAi
                ? t_i18n(
                    "This flow contains a step that needs AI, and AI is disabled on this platform.",
                  )
                : isDirty
                  ? t_i18n("Save before running: a run uses the saved graph")
                  : t_i18n("Run now")
          }
          wrap={isRunning || errors > 0 || isDirty || heldBackByAi || pausedByOwner}
        >
          <Button
            variant="outline"
            className="ml-1 gap-1.5"
            disabled={isRunning || errors > 0 || isDirty || heldBackByAi || pausedByOwner}
            onClick={onRun}
          >
            {isRunning && <Spinner size="sm" tone="inherit" />}{!isRunning && <Play className="h-4 w-4" />}
            {isRunning ? t_i18n("Running…") : t_i18n("Run")}
          </Button>
        </Tip>

        <Tip content={`${t_i18n("Save")} (${MOD}S)`} wrap={!!readOnly || isSaving || !isDirty}>
          <Button
            className={cn("ml-2 gap-1.5", !isDirty && "opacity-70")}
            disabled={readOnly || isSaving || !isDirty}
            onClick={onSave}
          >
            <Save className="h-4 w-4" />
            {isSaving
              ? t_i18n("Saving…")
              : isDirty
                ? t_i18n("Save")
                : t_i18n("Saved")}
          </Button>
        </Tip>
      </div>
    </div>
  );
}
