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
 * The one bar a page DOCUMENT is operated from — the document
 * counterpart of `DeckToolbar`.
 *
 * A product brief or a one-pager is read, not presented, so the bar has no
 * fullscreen, no overview, no black screen and no speaker notes. Three
 * regions, laid out like the deck bar so the two feel like one product:
 *
 *   [ leading: where am I ]   [ ‹ page 1/2 › | − fit + ]   [ actions … more ]
 *
 * Page navigation scrolls the document to the page; zoom is relative to the
 * fit-width default (`PageController`). Every control shows its key in the
 * tooltip.
 */
import type { CSSProperties, ReactNode } from "react";
import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  History,
  Keyboard,
  Maximize,
  MessageSquare,
  SquarePen,
  ZoomIn,
  ZoomOut,
  type LucideIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { LabelWithKeys } from "@/components/ui/kbd";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";
import type { PresentationPresenceEntry, PresentationSlide } from "@/types";

import type { PageController } from "./page-controller";
import { DECK_TOOLBAR_ACTIONS } from "./DeckToolbar";
import { PresenceAvatars } from "./PresenceAvatars";
import { SlideJumpMenu } from "./SlideJumpMenu";

export interface PageToolbarProps {
  controller: PageController;
  pages: PresentationSlide[];
  jumpOpen: boolean;
  onJumpOpenChange: (open: boolean) => void;
  onOpenShortcuts: () => void;
  /** Left region: title, "all presentations". */
  leading?: ReactNode;
  /** Right region, before the `More` menu: the consumer's own buttons. */
  actions?: ReactNode;
  /** Appended to the `More` menu. */
  menuItems?: OverflowMenuItem[];
  /** Tighter bar for the chat card. */
  compact?: boolean;
  /** The standalone viewer, offered as "open in a new tab" from the chat card. */
  viewerUrl?: string;
  /** Review comments panel toggle; `openCommentCount` badges the button. */
  onToggleComments?: () => void;
  commentsOpen?: boolean;
  openCommentCount?: number;
  /** Revision history panel toggle. */
  onToggleHistory?: () => void;
  historyOpen?: boolean;
  /** Who else has the document open (the caller excluded). */
  presence?: PresentationPresenceEntry[];
  /** "Open in studio" (the chat card): land on the standalone viewer in this
   *  tab with the studio rail out. When set, "Open in a new tab" moves into
   *  the `More` menu. */
  onOpenEditor?: () => void;
  className?: string;
}

function ToolbarButton({
  icon: Icon,
  label,
  keys,
  onClick,
  pressed,
  inactive,
  compact,
  badge,
  className,
  testId,
}: {
  icon: LucideIcon;
  label: string;
  keys?: string[];
  onClick: () => void;
  pressed?: boolean;
  /** Nothing to do (no previous page): dimmed but still focusable, so the
   *  tooltip keeps working and the tab order never shifts. */
  inactive?: boolean;
  compact?: boolean;
  /** A small count pinned to the icon's corner (open comments). */
  badge?: number;
  className?: string;
  testId?: string;
}) {
  return (
    <Tip content={keys ? <LabelWithKeys label={label} keys={keys} /> : label}>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={onClick}
        aria-label={badge ? `${label} (${badge})` : label}
        aria-pressed={pressed}
        aria-disabled={inactive || undefined}
        data-testid={testId}
        className={cn(
          "relative px-0",
          compact ? "h-7 w-7" : "h-8 w-8",
          pressed && "bg-accent/15 text-accent hover:bg-accent/20",
          inactive && "opacity-35 hover:bg-transparent hover:text-default-secondary",
          className,
        )}
      >
        <Icon className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />
        {badge ? (
          <span
            aria-hidden="true"
            className="absolute -right-0.5 -top-0.5 inline-flex h-[14px] min-w-[14px] items-center justify-center rounded-full bg-accent px-1 text-[9px] font-semibold leading-none text-default-primary"
          >
            {badge > 99 ? "99+" : badge}
          </span>
        ) : null}
      </Button>
    </Tip>
  );
}

export function PageToolbar({
  controller,
  pages,
  jumpOpen,
  onJumpOpenChange,
  onOpenShortcuts,
  leading,
  actions,
  menuItems = [],
  compact = false,
  viewerUrl,
  onToggleComments,
  commentsOpen = false,
  openCommentCount = 0,
  onToggleHistory,
  historyOpen = false,
  presence,
  onOpenEditor,
  className,
}: PageToolbarProps) {
  const { t_i18n } = useFormatter();
  const { index, total, hasPrev, hasNext, zoom, canZoomIn, canZoomOut } = controller;

  const allItems: OverflowMenuItem[] = [
    {
      id: "shortcuts",
      label: t_i18n("Keyboard shortcuts"),
      icon: Keyboard,
      shortcut: "?",
      onSelect: onOpenShortcuts,
    },
    // With "Open in studio" on the bar, the new-tab form is the rarer one.
    ...(viewerUrl && onOpenEditor
      ? [
          {
            id: "new-tab",
            label: t_i18n("Open in a new tab"),
            icon: ExternalLink,
            onSelect: () => window.open(withBasePath(viewerUrl), "_blank", "noopener"),
          },
        ]
      : []),
    ...menuItems.map((item, i) => (i === 0 ? { ...item, separatorBefore: true } : item)),
  ];

  const progress = total > 0 ? (index + 1) / total : 0;
  const isFit = Math.abs(zoom - 1) < 1e-6;
  const zoomLabel = isFit ? t_i18n("Fit width") : `${Math.round(zoom * 100)}%`;

  return (
    <div
      role="toolbar"
      aria-label={t_i18n("Document controls")}
      className={cn(
        "deck-toolbar relative flex shrink-0 items-center gap-2 border-b border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm print:hidden",
        compact ? "min-h-10 px-2 py-1" : "min-h-12 px-3 py-1.5",
        className,
      )}
      style={{ "--deck-progress": progress } as CSSProperties}
    >
      {/* On a narrow bar (the viewer at 1024 px with the studio rail out) the
          zoom in / out buttons leave like the deck's first / last slide
          buttons: + and - still zoom, the fit button stays and the shortcut
          sheet names the keys, so the leading region keeps room for its
          fixed parts (#9375, B1 of #10528 r1). */}
      <div className="flex min-w-0 flex-1 items-center gap-2">{leading}</div>

      <div className="flex shrink-0 items-center gap-0.5">
        <ToolbarButton
          icon={ChevronLeft}
          label={t_i18n("Previous page")}
          keys={["←"]}
          onClick={controller.prev}
          inactive={!hasPrev}
          compact={compact}
        />
        <SlideJumpMenu
          open={jumpOpen}
          onOpenChange={onJumpOpenChange}
          slides={pages}
          index={index}
          total={total}
          onJump={controller.goTo}
          compact={compact}
          kind="page"
        />
        <ToolbarButton
          icon={ChevronRight}
          label={t_i18n("Next page")}
          keys={["→"]}
          onClick={controller.next}
          inactive={!hasNext}
          compact={compact}
        />

        <div className={cn("mx-1 w-px bg-[var(--border-elevation-subtle)]", compact ? "h-4" : "h-5")} aria-hidden="true" />

        <ToolbarButton
          icon={ZoomOut}
          label={t_i18n("Zoom out")}
          keys={["-"]}
          onClick={controller.zoomOut}
          inactive={!canZoomOut}
          compact={compact}
          className="deck-toolbar-edge"
        />
        <Tip content={<LabelWithKeys label={t_i18n("Fit width")} keys={["0"]} />}>
          <button
            type="button"
            onClick={controller.fitWidth}
            aria-label={
              isFit ? `${t_i18n("Zoom")}: ${zoomLabel}` : `${t_i18n("Zoom")}: ${zoomLabel}. ${t_i18n("Fit width")}`
            }
            aria-pressed={isFit}
            className={cn(
              "inline-flex h-8 min-w-[3.25rem] items-center justify-center gap-1 rounded-sm px-1.5 font-mono-plex tabular-nums transition-colors",
              compact ? "text-[11px]" : "content-compact",
              isFit ? "text-default-secondary" : "text-default-primary",
              "hover:bg-elevation-hover hover:text-default-primary",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
            )}
          >
            {isFit ? <Maximize className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} /> : zoomLabel}
          </button>
        </Tip>
        <ToolbarButton
          icon={ZoomIn}
          label={t_i18n("Zoom in")}
          keys={["+"]}
          onClick={controller.zoomIn}
          inactive={!canZoomIn}
          compact={compact}
          className="deck-toolbar-edge"
        />
      </div>

      <div className={DECK_TOOLBAR_ACTIONS}>
        {presence && presence.length > 0 && (
          <PresenceAvatars viewers={presence} kind="page" compact={compact} className="mr-1.5" />
        )}
        {onToggleComments && (
          <ToolbarButton
            icon={MessageSquare}
            label={commentsOpen ? t_i18n("Hide comments") : t_i18n("Comments")}
            onClick={onToggleComments}
            pressed={commentsOpen}
            badge={openCommentCount}
            compact={compact}
            testId="presentation.comments"
          />
        )}
        {onToggleHistory && (
          <ToolbarButton
            icon={History}
            label={historyOpen ? t_i18n("Hide history") : t_i18n("History")}
            onClick={onToggleHistory}
            pressed={historyOpen}
            compact={compact}
            testId="presentation.history"
          />
        )}
        {actions}
        {onOpenEditor ? (
          <Tip content={t_i18n("Open in studio")}>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onOpenEditor}
              aria-label={t_i18n("Open in studio")}
              data-testid="presentation.openInEditor"
              className={cn("gap-1.5 font-medium", compact ? "h-7 px-2 text-[11px]" : "h-8 px-2.5 content-compact")}
            >
              <SquarePen className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />
              <span className="deck-toolbar-verb-label">{t_i18n("Open in studio")}</span>
            </Button>
          </Tip>
        ) : (
          viewerUrl && (
            <Tip content={t_i18n("Open in a new tab")}>
              <Button
                asChild
                variant="ghost"
                size="sm"
                className={cn("px-0", compact ? "h-7 w-7" : "h-8 w-8")}
              >
                <a
                  href={withBasePath(viewerUrl)}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={t_i18n("Open in a new tab")}
                >
                  <ExternalLink className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />
                </a>
              </Button>
            </Tip>
          )
        )}
        <OverflowMenu
          items={allItems}
          label={t_i18n("More actions")}
          className={compact ? "h-7 w-7" : undefined}
        />
      </div>

      {/* Progress hairline: how far into the document the reader is. */}
      <div className="deck-toolbar-progress" aria-hidden="true" />
    </div>
  );
}
