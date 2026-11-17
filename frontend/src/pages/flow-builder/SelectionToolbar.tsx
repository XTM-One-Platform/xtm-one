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
 * Floating toolbar for a multi-node selection.
 *
 * Rubber-band selection and ⌘/⇧-click already worked, but nothing told the user
 * what was caught or what could be done with it. This appears only while more
 * than one element is selected, states the count, and carries the actions that
 * operate on a selection as a whole.
 *
 * It sits above the canvas rather than in the top toolbar so it is next to the
 * thing it acts on, and it announces itself politely to screen readers — a
 * selection count that changes silently is invisible to anyone not looking.
 *
 * It stays inside the canvas at every width: the
 * canvas area is a size container (`@container` in index.tsx), the text
 * buttons show their label only when the canvas is at least 48rem wide and are
 * icon-only below (aria-label and Tip kept), the bar is capped at the canvas
 * width minus 1rem and the summary, on one line, truncates with its title as a
 * last resort. At 1024 px with the palette open the canvas is about 522 px and
 * the bar is one row; with the node editor open as well it is about 200 px, so
 * below 28rem of canvas the summary takes its own row and the buttons wrap
 * under it, every one of them still inside the canvas.
 */

import { Bookmark, Combine, Copy, Group, Scissors, Trash2, Ungroup, X } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

interface SelectionToolbarProps {
  nodeCount: number;
  edgeCount: number;
  /** True while the detach request is in flight. */
  isDetaching: boolean;
  readOnly?: boolean;
  onDetach: () => void;
  /** Collapse the selection into a sub-flow, leaving a wired flow.call. */
  onCollapse?: () => void;
  onSaveSnippet?: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onClear: () => void;
  /** ≥2 groupable nodes selected → offer grouping. */
  canGroup?: boolean;
  /** The selection contains group container(s) → offer ungrouping. */
  canUngroup?: boolean;
  onGroup?: () => void;
  onUngroup?: () => void;
}

/** A labelled action that turns icon-only when the canvas is narrow. */
function LabelledAction({
  label,
  tip,
  icon,
  disabled,
  onClick,
}: {
  label: string;
  tip?: string;
  icon: ReactNode;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    // One Tip per button: Radix's Trigger (asChild) takes exactly ONE child —
    // several buttons inside a single Tip crash the whole page with
    // React.Children.only the moment the toolbar renders.
    <Tip content={tip ?? label} wrap={!!disabled}>
      <Button
        variant="ghost"
        size="sm"
        className="h-7 shrink-0 gap-1.5 px-1.5 content-compact @3xl:px-2.5"
        disabled={disabled}
        onClick={onClick}
        aria-label={label}
      >
        {icon}
        <span className="hidden @3xl:inline">{label}</span>
      </Button>
    </Tip>
  );
}

export function SelectionToolbar({
  nodeCount,
  edgeCount,
  isDetaching,
  readOnly,
  onDetach,
  onCollapse,
  onSaveSnippet,
  onDuplicate,
  onDelete,
  onClear,
  canGroup,
  canUngroup,
  onGroup,
  onUngroup,
}: SelectionToolbarProps) {
  const { t_i18n } = useFormatter();
  // A single selected group still gets the toolbar (for Ungroup).
  if (nodeCount + edgeCount < 2 && !canUngroup) return null;

  // `pluralize` returns only the word and only in English — wrong tool for a
  // count the user reads in four locales. The catalogue carries the number.
  const summary = [
    nodeCount > 0 ? t_i18n("{count} nodes", { count: nodeCount }) : null,
    edgeCount > 0 ? t_i18n("{count} connections", { count: edgeCount }) : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const summaryText = t_i18n("{selection} selected", { selection: summary });

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="flow-selection-toolbar"
      className="pointer-events-auto absolute left-1/2 top-4 z-10 flex w-max max-w-[calc(100%-1rem)] -translate-x-1/2 flex-wrap items-center justify-center gap-1 rounded-sm border border-elevation-subtle layer-1 bg-elevation-default px-2 py-1.5 shadow-overlay"
    >
      <span
        className="min-w-0 basis-full truncate whitespace-nowrap px-1.5 text-center content-compact text-default-secondary @md:basis-auto @md:text-left"
        title={summaryText}
      >
        {summaryText}
      </span>

      <span className="mx-1 hidden h-4 w-px shrink-0 bg-[var(--border-elevation-subtle)] @md:block" aria-hidden="true" />

      {canGroup && onGroup && (
        <LabelledAction
          label={t_i18n("Group")}
          icon={<Group className="h-3.5 w-3.5" />}
          disabled={readOnly}
          onClick={onGroup}
        />
      )}
      {canUngroup && onUngroup && (
        <LabelledAction
          label={t_i18n("Ungroup")}
          icon={<Ungroup className="h-3.5 w-3.5" />}
          disabled={readOnly}
          onClick={onUngroup}
        />
      )}
      {onSaveSnippet && (
        <LabelledAction
          label={t_i18n("Snippet")}
          tip={t_i18n("Save the selection as a reusable snippet.")}
          icon={<Bookmark className="h-3.5 w-3.5" />}
          disabled={readOnly}
          onClick={onSaveSnippet}
        />
      )}
      {onCollapse && (
        <LabelledAction
          label={t_i18n("Collapse")}
          tip={t_i18n(
            "Collapse the selection into a sub-flow, replaced by a wired Call flow node.",
          )}
          icon={<Combine className="h-3.5 w-3.5" />}
          disabled={readOnly || isDetaching}
          onClick={onCollapse}
        />
      )}
      <LabelledAction
        label={isDetaching ? t_i18n("Detaching…") : t_i18n("Detach")}
        tip={t_i18n("Move the selection into its own flow")}
        icon={<Scissors className="h-3.5 w-3.5" />}
        disabled={readOnly || isDetaching || nodeCount === 0}
        onClick={onDetach}
      />

      <Tip content={t_i18n("Duplicate the selection")} wrap={!!readOnly}>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          disabled={readOnly}
          onClick={onDuplicate}
          aria-label={t_i18n("Duplicate the selection")}
        >
          <Copy className="h-3.5 w-3.5" />
        </Button>
      </Tip>

      <Tip content={t_i18n("Delete the selection")} wrap={!!readOnly}>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0 text-feedback-error-primary hover:bg-feedback-error-primary/10 hover:text-feedback-error-primary"
          disabled={readOnly}
          onClick={onDelete}
          aria-label={t_i18n("Delete the selection")}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </Tip>

      <Tip content={t_i18n("Clear the selection")}>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          onClick={onClear}
          aria-label={t_i18n("Clear the selection")}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </Tip>
    </div>
  );
}
