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
 * The one bar a deck is operated from.
 *
 * Everything sits at the top, in a single row, and nothing is drawn over the
 * slide: reveal's corner arrows and progress bar are off, the slide counter is
 * a button here rather than a badge on the canvas, and the help sheet and jump
 * menu are app dialogs rather than reveal overlays. Three regions:
 *
 *   [ leading: where am I ]   [ first ‹ 3/12 › last | overview ]   [ who else is here | ▶ Present | comments history | actions … more ]
 *
 * The middle cluster is the same everywhere (chat card, standalone viewer,
 * fullscreen); `leading`, `actions` and `menuItems` are the consumer's. Every
 * control shows its key in the tooltip, and the `More` menu carries the rarer
 * presenter actions (black screen, the speaker view, the shortcut sheet).
 *
 * `Present` is the one labelled verb on the bar and is never folded into the
 * menu: going fullscreen is what a deck is for, and an icon alone was missed.
 * Where the browser refuses its fullscreen the stage takes the app's viewport
 * instead; where the API is unavailable outright (the embedded chat inside an
 * iframe without `allow="fullscreen"`) it opens the standalone viewer — so the
 * button always presents something. The collaboration entry points — the review
 * comments with their open count, the revision history, the readers' initials
 * (`PresenceAvatars`) — are optional slots the viewer fills; the chat card
 * offers `Open in studio` instead, which lands on that viewer with the chat
 * that made the deck beside it.
 *
 * Progress is a hairline along the bar's bottom edge — a presenter's glance,
 * not a bar across the slide.
 */
import type { CSSProperties, ReactNode } from "react";
import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  ExternalLink,
  History,
  Keyboard,
  LayoutGrid,
  MessageSquare,
  Minimize2,
  MonitorOff,
  NotebookPen,
  Play,
  Printer,
  ScreenShare,
  SquarePen,
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

import type { DeckController } from "./deck-controller";
import type { ElementFullscreen } from "./fullscreen";
import { PresenceAvatars } from "./PresenceAvatars";
import { SlideJumpMenu } from "./SlideJumpMenu";

/**
 * The right region of a deck or page bar: it grows to push the actions to the
 * end but never shrinks under its content, and no action in it shrinks.
 */
export const DECK_TOOLBAR_ACTIONS = "flex min-w-fit flex-1 items-center justify-end gap-0.5 [&>*]:shrink-0";

export interface DeckToolbarProps {
  controller: DeckController;
  slides: PresentationSlide[];
  fullscreen: ElementFullscreen;
  jumpOpen: boolean;
  onJumpOpenChange: (open: boolean) => void;
  onOpenShortcuts: () => void;
  /** Left region: title, breadcrumb, "all presentations". */
  leading?: ReactNode;
  /** Right region, before the `More` menu: the consumer's own buttons. */
  actions?: ReactNode;
  /** Appended to the `More` menu after the presenter actions. */
  menuItems?: OverflowMenuItem[];
  /** Tighter bar for the chat card: first/last and overview move to the menu. */
  compact?: boolean;
  /** Portal target for tooltips — the fullscreen stage while it is fullscreen. */
  portalContainer?: HTMLElement | null;
  /** Where to present when fullscreen is unavailable here (an iframe without
   *  `allow="fullscreen"`, as in the embedded chat). */
  viewerUrl?: string;
  /** Go fullscreen — the `Present` button. Defaults to the stage's own
   *  fullscreen; a consumer that presents elsewhere overrides it. */
  onPresent?: () => void;
  /** Open the speaker window — `More > Speaker view`, key `S`. Absent
   *  for a page (portrait document), which is read, not presented. */
  onOpenSpeakerView?: () => void;
  /** Present to a meeting - `More > Present to a meeting`, key `A`: the
   *  audience window (slides only, to share) and the speaker view. */
  onPresentToMeeting?: () => void;
  /** Open the printable slides + notes handout — `More > Notes handout`. */
  onOpenNotesHandout?: () => void;
  /** Review comments panel toggle; `openCommentCount` badges the button. */
  onToggleComments?: () => void;
  commentsOpen?: boolean;
  openCommentCount?: number;
  /** Revision history panel toggle. */
  onToggleHistory?: () => void;
  historyOpen?: boolean;
  /** Who else has the deck open (the caller excluded) — drawn as initials. */
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
  portalContainer,
  badge,
  className,
  testId,
}: {
  icon: LucideIcon;
  label: string;
  keys?: string[];
  onClick: () => void;
  pressed?: boolean;
  /** Nothing to do (no previous slide): dimmed but still focusable, so the
   *  tooltip keeps working and the tab order never shifts. */
  inactive?: boolean;
  compact?: boolean;
  portalContainer?: HTMLElement | null;
  /** A small count pinned to the icon's corner (open comments). */
  badge?: number;
  className?: string;
  testId?: string;
}) {
  return (
    <Tip content={keys ? <LabelWithKeys label={label} keys={keys} /> : label} container={portalContainer}>
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

/**
 * A labelled verb (`Present`, `Open in studio`, the viewer's `Studio` toggle):
 * icon + text, the text dropping away on a narrow bar
 * (`.deck-toolbar-verb-label`, a container query in `presentation.css`) so the
 * chat card at a tight column keeps the icon and its tooltip. `pressed` draws
 * the toggled state (the studio rail is out); `tooltip` says what the click
 * does when that differs from the label (`Studio` reads "Hide the studio"
 * while it is open). `outline` is a second verb beside the primary one (the
 * video bar's `Render` beside `Play`). `labelClassName` lets a bar that is
 * not the deck's own container drop the text on its own width (the video
 * editor's bar, a Tailwind container query).
 */
export function ToolbarVerb({
  icon: Icon,
  label,
  tooltip,
  keys,
  onClick,
  primary,
  outline,
  pressed,
  disabled,
  compact,
  portalContainer,
  labelClassName,
  testId,
}: {
  icon: LucideIcon;
  label: string;
  tooltip?: string;
  keys?: string[];
  onClick: () => void;
  primary?: boolean;
  outline?: boolean;
  pressed?: boolean;
  disabled?: boolean;
  compact?: boolean;
  portalContainer?: HTMLElement | null;
  labelClassName?: string;
  testId?: string;
}) {
  const tip = tooltip ?? label;
  return (
    <Tip content={keys ? <LabelWithKeys label={tip} keys={keys} /> : tip} container={portalContainer} wrap={disabled}>
      <Button
        type="button"
        variant={primary ? "default" : outline ? "outline" : "ghost"}
        size="sm"
        onClick={onClick}
        disabled={disabled}
        aria-label={tip}
        aria-pressed={pressed}
        data-testid={testId}
        className={cn(
          "shrink-0 gap-1.5 whitespace-nowrap font-medium",
          compact ? "h-7 px-2 text-[11px]" : "h-8 px-2.5 content-compact",
          primary && !disabled && "shadow-[0_0_16px_color-mix(in_srgb,_var(--color-accent)_calc(0.25_*_100%),_transparent)]",
          pressed && "bg-accent/15 text-accent",
        )}
      >
        <Icon className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />
        <span className={cn("deck-toolbar-verb-label", labelClassName)}>{label}</span>
      </Button>
    </Tip>
  );
}

export function DeckToolbar({
  controller,
  slides,
  fullscreen,
  jumpOpen,
  onJumpOpenChange,
  onOpenShortcuts,
  leading,
  actions,
  menuItems = [],
  compact = false,
  portalContainer,
  viewerUrl,
  onPresent,
  onOpenSpeakerView,
  onPresentToMeeting,
  onOpenNotesHandout,
  onToggleComments,
  commentsOpen = false,
  openCommentCount = 0,
  onToggleHistory,
  historyOpen = false,
  presence,
  onOpenEditor,
  className,
}: DeckToolbarProps) {
  const { t_i18n } = useFormatter();
  const { index, total, hasPrev, hasNext, overview, blackout } = controller;

  // Presenting: the stage's fullscreen — the browser's, or the in-app viewport
  // where the browser refuses it (`fullscreen.ts`). Where the API is not even
  // available (the embedded chat inside an iframe without `allow="fullscreen"`)
  // the standalone viewer is preferred, a tab that CAN go fullscreen, over
  // presenting inside a small embedded frame; with no viewer to offer, the
  // viewport mode it is. The button always presents something.
  const presentHere = !!onPresent || fullscreen.supported || !viewerUrl;
  const presentLabel = presentHere ? t_i18n("Present") : t_i18n("Open the viewer to present fullscreen");
  const onPresentClick = presentHere
    ? (onPresent ?? fullscreen.toggle)
    : () => window.open(withBasePath(viewerUrl), "_blank", "noopener");

  const presenterItems: OverflowMenuItem[] = [
    ...(compact
      ? [
          {
            id: "overview",
            label: overview ? t_i18n("Close slide overview") : t_i18n("Slide overview"),
            icon: LayoutGrid,
            shortcut: "O",
            onSelect: controller.toggleOverview,
          },
        ]
      : []),
    {
      id: "blackout",
      label: blackout ? t_i18n("Resume from black screen") : t_i18n("Black screen"),
      icon: MonitorOff,
      shortcut: "B",
      onSelect: controller.toggleBlackout,
    },
    ...(onOpenSpeakerView
      ? [
          {
            id: "speaker",
            label: t_i18n("Speaker view"),
            icon: NotebookPen,
            shortcut: "S",
            onSelect: onOpenSpeakerView,
          },
        ]
      : []),
    ...(onPresentToMeeting
      ? [
          {
            id: "meeting",
            label: t_i18n("Present to a meeting"),
            icon: ScreenShare,
            shortcut: "A",
            onSelect: onPresentToMeeting,
          },
        ]
      : []),
    ...(onOpenNotesHandout
      ? [
          {
            id: "handout",
            label: t_i18n("Notes handout"),
            icon: Printer,
            onSelect: onOpenNotesHandout,
          },
        ]
      : []),
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
  ];
  const allItems: OverflowMenuItem[] = [
    ...presenterItems,
    ...menuItems.map((item, i) => (i === 0 ? { ...item, separatorBefore: true } : item)),
  ];

  const progress = total > 0 ? (index + 1) / total : 0;

  return (
    <div
      role="toolbar"
      aria-label={t_i18n("Presentation controls")}
      className={cn(
        "deck-toolbar relative flex shrink-0 items-center gap-2 border-b border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm print:hidden",
        compact ? "min-h-10 px-2 py-1" : "min-h-12 px-3 py-1.5",
        className,
      )}
      style={{ "--deck-progress": progress } as CSSProperties}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2">{leading}</div>

      <div className="flex shrink-0 items-center gap-0.5">
        {!compact && (
          <ToolbarButton
            icon={ChevronsLeft}
            label={t_i18n("First slide")}
            keys={["Home"]}
            onClick={controller.first}
            inactive={!hasPrev}
            portalContainer={portalContainer}
            className="deck-toolbar-edge"
          />
        )}
        <ToolbarButton
          icon={ChevronLeft}
          label={t_i18n("Previous slide")}
          keys={["←"]}
          onClick={controller.prev}
          inactive={!hasPrev}
          compact={compact}
          portalContainer={portalContainer}
        />
        <SlideJumpMenu
          open={jumpOpen}
          onOpenChange={onJumpOpenChange}
          slides={slides}
          index={index}
          total={total}
          onJump={controller.goTo}
          compact={compact}
        />
        <ToolbarButton
          icon={ChevronRight}
          label={t_i18n("Next slide")}
          keys={["→"]}
          onClick={controller.next}
          inactive={!hasNext}
          compact={compact}
          portalContainer={portalContainer}
        />
        {!compact && (
          <ToolbarButton
            icon={ChevronsRight}
            label={t_i18n("Last slide")}
            keys={["End"]}
            onClick={controller.last}
            inactive={!hasNext}
            portalContainer={portalContainer}
            className="deck-toolbar-edge"
          />
        )}

        {!compact && <div className="mx-1 h-5 w-px bg-[var(--border-elevation-subtle)]" aria-hidden="true" />}

        {!compact && (
          <ToolbarButton
            icon={LayoutGrid}
            label={overview ? t_i18n("Close slide overview") : t_i18n("Slide overview")}
            keys={["O"]}
            onClick={controller.toggleOverview}
            pressed={overview}
            portalContainer={portalContainer}
          />
        )}
        {fullscreen.isFullscreen && (
          <ToolbarButton
            icon={Minimize2}
            label={t_i18n("Exit fullscreen")}
            keys={["Esc"]}
            onClick={fullscreen.exit}
            pressed
            compact={compact}
            portalContainer={portalContainer}
          />
        )}
      </div>

      {/* `min-w-fit` + `[&>*]:shrink-0`: the actions never shrink under their
          own width (at 1024 px with the studio open they were squeezed to 16 px
          and painted over the slide counter); the title on the left truncates
          instead (#9375, A1 of #10302 r8). */}
      <div className={DECK_TOOLBAR_ACTIONS}>
        {presence && presence.length > 0 && (
          <PresenceAvatars
            viewers={presence}
            compact={compact}
            portalContainer={portalContainer}
            className="mr-1.5"
          />
        )}
        {!fullscreen.isFullscreen && (
          <ToolbarVerb
            icon={Play}
            label={presentLabel}
            keys={presentHere ? ["F"] : undefined}
            onClick={onPresentClick}
            primary
            compact={compact}
            portalContainer={portalContainer}
            testId="presentation.present"
          />
        )}
        {onToggleComments && (
          <ToolbarButton
            icon={MessageSquare}
            label={commentsOpen ? t_i18n("Hide comments") : t_i18n("Comments")}
            onClick={onToggleComments}
            pressed={commentsOpen}
            badge={openCommentCount}
            compact={compact}
            portalContainer={portalContainer}
            className={cn(presentHere && !fullscreen.isFullscreen && "ml-1")}
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
            portalContainer={portalContainer}
            testId="presentation.history"
          />
        )}
        {actions}
        {onOpenEditor ? (
          <ToolbarVerb
            icon={SquarePen}
            label={t_i18n("Open in studio")}
            onClick={onOpenEditor}
            compact={compact}
            portalContainer={portalContainer}
            testId="presentation.openInEditor"
          />
        ) : (
          viewerUrl &&
          !fullscreen.isFullscreen && (
            <Tip content={t_i18n("Open in a new tab")} container={portalContainer}>
              <Button
                variant="ghost"
                size="sm"
                asChild
                aria-label={t_i18n("Open in a new tab")}
                className={cn("px-0", compact ? "h-7 w-7" : "h-8 w-8")}
              >
                <a href={withBasePath(viewerUrl)} target="_blank" rel="noreferrer">
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
          // In the bar: painted inside a fullscreen stage, and part of the bar
          // that the idle timer keeps up while it holds an open menu.
          inline
        />
      </div>

      {/* Progress hairline: scaled from the left by `--deck-progress`. */}
      <div className="deck-toolbar-progress" aria-hidden="true" />
    </div>
  );
}
