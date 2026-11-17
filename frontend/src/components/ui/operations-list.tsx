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
 * The Operations list contract: one row anatomy for every list of the
 * Operations page: Work Items, Shared State, Worksets, Executions, Background
 * Tasks and Workspaces, all on the same components (gate:
 * pages/operations/operations-row-actions.test.ts).
 *
 * ROW ANATOMY. A row is a grid of five columns, the same x on every row:
 *
 *   | lead | main (title line / one line below) | meta | updated | actions |
 *
 * - lead: the selection box and the status icon, top aligned.
 * - main: line 1 is the title (truncated, never wrapped) then the chips;
 *   line 2 is ONE line: what a blocked row waits for, else the summary.
 * - meta (`OPS_META_COLUMN`, from lg): two lines of compact tokens (icon and
 *   text, one line each), the essentials only (who owns it, then the key or
 *   the references), the rest folded into the "+N" details token.
 * - updated (`OPS_UPDATED_COLUMN`): the relative time, right aligned, the
 *   absolute time in the viewer's time zone in its tooltip.
 * - actions: one fixed width per tab (each tab passes the width its longest
 *   resting action set needs), so no label ever shifts the columns.
 *
 * HEIGHT. Every row of a tab has the same height (`OPS_ROW_HEIGHT`), whatever
 * it carries: nothing in a row wraps, a value that does not fit is cut with an
 * ellipsis and given whole in the hover card or a tooltip. The height is a
 * floor, never a cap: the row grows to its two lines and padding when the
 * type renders taller (a larger root size, a zoom, a font fallback), so the
 * card's `overflow-hidden` never cuts line 2.
 *
 * ON DEMAND. The hover card (`OpsHoverCard`) holds every fact of the row,
 * names uncut: it opens on hover after a delay and on keyboard focus of its
 * trigger, and closes on blur or Escape. A click on the row opens the tab's
 * detail: its dialog (Work Items), or, on a tab whose detail is the record
 * itself (Shared State, Worksets), a panel unfolded under the row
 * (`OpsExpandToggle`); on a tab whose detail is another page (Background
 * Tasks: the run the task belongs to; Workspaces: the workspace page), that
 * page. The row above a panel keeps its height.
 *
 * ORDERING. The facts read in this order on every tab: what it waits for,
 * who owns it, who holds it, who created it, where it comes from, who it is
 * for, then the state facts a chip can carry (priority, pickup, stale,
 * visibility), then keys and references, then timestamps.
 *
 * DENSITY. No text under `content-compact` (12 px, the design standards
 * floor); chips are one size (`content-compact`, h-5); tokens one line. Line 1 never cuts a chip: a chip
 * that does not fit whole is not shown (`OpsChipStrip`), and the hover card
 * names every fact such a chip carries, so a narrow row loses nothing.
 * The chips a row must never lose (its state, its priority) sit outside the
 * strip and take the title's room first.
 *
 * FOCUS. Every focusable piece draws the Button's ring (2 px offset plus 2 px
 * `ring-focus`): `BUTTON_ROLE_FOCUS_RING` on buttons, `TIP_WRAP_FOCUS_RING` on
 * a focusable tooltip trigger.
 *
 * ACTIONS. Every icon action of the actions column is an `OpsRowAction`: one
 * size, always visible, named, its tooltip the same word on every tab, a tab
 * stop only on the button itself. A tab never draws a raw `<button>` there
 * (gate: pages/operations/operations-row-actions.test.ts).
 */
import { useLayoutEffect, useRef } from "react";
import type { ElementType, ReactNode } from "react";

import { ChevronRight } from "lucide-react";
import { Spinner } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { Skeleton } from "@/components/ui/skeleton";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { formatTimeAgo } from "@/lib/relative-time";
import { formatInZone, viewerTimeZone } from "@/lib/scheduled-work-items";
import { cn } from "@/lib/utils";

/**
 * Every row of an Operations list is at least this tall: title line, one line
 * below, padding. A floor (`min-h-*`), never a fixed `h-*`: a fixed height cut
 * the summary line of every Executions card when line 1 and line 2 needed more
 * than the 44 px it left inside the padding (#8922). The row is a column
 * flex box that centres its grid: a row taller than its content (the floor,
 * a grid cell stretched to its neighbour) keeps every slot on the card's
 * vertical centre, never stuck to the top edge.
 */
export const OPS_ROW_HEIGHT = "flex min-h-16 flex-col justify-center";

/**
 * The vertical alignment of every slot of `OpsRowGrid`: each one (lead, the
 * title block, the meta stack, the updated cell, the actions) sits on the
 * row's vertical centre, whatever the number of lines of its neighbours
 * (Sam, 12 Nov 2026: the date and the kebab sat at the top of a card whose
 * owner and key formed a two-line stack). One class here, never a per-tab
 * override (gate: operations-list.test.tsx).
 */
export const OPS_ROW_ALIGN = "items-center";

/**
 * The edge of every Operations row card: the default hairline
 * (`border-elevation-subtle`), not the fainter `-soft` one `Paper` draws by
 * default (design-system arbitration #11). The rows are a dense stack of
 * identical page-flow surfaces where the edge is the only separator; at
 * about 1.09:1 on dark the soft hairline read as a missing bottom border on a
 * row with an empty summary line. Every row card passes this first; a state
 * class passed after it (selected, failed, stale, HITL) replaces the colour,
 * and a hover may only strengthen it (gate:
 * pages/operations/operations-row-actions.test.ts).
 */
export const OPS_ROW_CARD = "border-elevation-subtle";

/**
 * The meta column: shown from lg, one width on every row; narrower below xl
 * so the title keeps its room on a 1024 px screen.
 */
const OPS_META_COLUMN = "hidden w-44 shrink-0 lg:flex xl:w-52";

/** The updated column: one width on every row, right aligned. */
const OPS_UPDATED_COLUMN = "w-24 shrink-0 text-right";

/** Hover card delay: long enough not to flash while the pointer crosses a list. */
const OPS_HOVER_CARD_DELAY = 500;

/** One labelled fact of the hover card. */
export interface OpsFact {
  label: string;
  value: ReactNode;
  tone?: "warning";
  testId?: string;
}

/**
 * The facts of a row as a description list, names uncut (they wrap): the
 * hover card's body, and the body tests read.
 */
function OpsFactList({ facts, testId }: { facts: OpsFact[]; testId?: string }) {
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 content-compact" data-testid={testId}>
      {facts.map((fact) => (
        <div key={fact.label} className="contents" data-testid={fact.testId}>
          {/* The warning term on the Tip surface: warning-tertiary reads 4.5:1 in both
              themes, warning-primary does not on the light Tip. */}
          <dt
            className={cn("whitespace-nowrap", fact.tone === "warning" ? "text-feedback-warning-tertiary" : "text-default-secondary")}
            data-tone={fact.tone}
          >
            {fact.label}
          </dt>
          <dd className="min-w-0 break-words text-default-primary">{fact.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * The hover card of a row: the title whole, then every fact. The trigger is
 * any focusable element (the row's title button, its "+N" token); the card
 * opens after `OPS_HOVER_CARD_DELAY` on hover and at once on keyboard focus.
 */
export function OpsHoverCard({ title, facts, children }: { title: string; facts: OpsFact[]; children: ReactNode }) {
  return (
    <Tip
      delayDuration={OPS_HOVER_CARD_DELAY}
      side="bottom"
      align="start"
      content={
        <div className="max-w-[22rem] space-y-2 py-1" data-testid="ops-hover-card">
          <p className="break-words font-medium text-default-primary">{title}</p>
          {facts.length > 0 && <OpsFactList facts={facts} />}
        </div>
      }
    >
      {children}
    </Tip>
  );
}

/**
 * A compact metadata token: an icon and one line of text, cut with an
 * ellipsis. `copy` adds a copy button (keys, references, run ids); `full`
 * names the value whole in a tooltip when the token cuts it.
 */
export function OpsToken({
  icon: Icon,
  children,
  full,
  copy,
  copyLabel,
  tone,
  testId,
  className,
}: {
  icon?: ElementType;
  children: ReactNode;
  full?: string;
  copy?: string;
  copyLabel?: string;
  tone?: "warning" | "muted";
  testId?: string;
  className?: string;
}) {
  const body = (
    <span
      className={cn(
        "inline-flex min-w-0 items-center gap-1 overflow-hidden whitespace-nowrap content-compact",
        tone === "warning" ? "text-feedback-warning-primary" : tone === "muted" ? "text-default-secondary" : "text-default-primary",
        className,
      )}
      data-testid={testId}
    >
      {Icon && <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />}
      <span className="min-w-0 truncate">{children}</span>
    </span>
  );
  return (
    <span className="inline-flex min-w-0 max-w-full items-center gap-0.5">
      {full ? <Tip content={full}>{body}</Tip> : body}
      {copy && (
        <span onClick={(e) => e.stopPropagation()} className="inline-flex shrink-0">
          <CopyIconButton value={copy} label={copyLabel ?? copy} className="p-0.5" />
        </span>
      )}
    </span>
  );
}

/**
 * The "+N" token of the meta column: how many facts the row leaves to its
 * hover card. Focus or hover opens the card, a click opens the row's detail.
 */
export function OpsMoreToken({
  count,
  title,
  facts,
  onOpen,
  testId,
}: {
  count: number;
  title: string;
  facts: OpsFact[];
  onOpen: () => void;
  testId?: string;
}) {
  const { t_i18n } = useFormatter();
  if (count <= 0) return null;
  return (
    <OpsHoverCard title={title} facts={facts}>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onOpen();
        }}
        className={cn("shrink-0 rounded-sm px-1 content-compact text-default-secondary hover:text-accent", BUTTON_ROLE_FOCUS_RING)}
        aria-label={t_i18n("{count, plural, one {# more detail} other {# more details}}", { count })}
        data-testid={testId}
      >
        +{count}
      </button>
    </OpsHoverCard>
  );
}

/**
 * The expand control of a row whose detail unfolds under it (Shared State,
 * Worksets): one chevron in the lead column, named Expand or Collapse.
 */
export function OpsExpandToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <button
      type="button"
      className={cn("shrink-0 rounded-sm text-default-secondary hover:text-default-primary", BUTTON_ROLE_FOCUS_RING)}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
      aria-label={open ? t_i18n("Collapse") : t_i18n("Expand")}
      aria-expanded={open}
    >
      <ChevronRight className={cn("h-4 w-4 transition-transform", open && "rotate-90")} aria-hidden="true" />
    </button>
  );
}

/** The icon colour of a row action. */
const OPS_ROW_ACTION_TONE = {
  default: "",
  success: "text-feedback-success-primary",
  danger: "text-feedback-error-primary",
} as const;

/**
 * One icon action of the actions column (resolve, delete...): the design-system
 * Button at one size (28 px) with its focus ring, always visible, named by
 * `label`, which is also its tooltip. `busy` shows a spinner and disables it;
 * the tooltip wrapper is a tab stop only while the button is disabled.
 * An action that unfolds a panel under the row (Answer on Executions) passes
 * `expanded` and `controls` (the panel's id), so assistive tech hears that it
 * opens and closes that panel, as `OpsExpandToggle` does.
 */
export function OpsRowAction({
  icon: Icon,
  label,
  onClick,
  tone = "default",
  disabled = false,
  busy = false,
  expanded,
  controls,
}: {
  icon: ElementType;
  label: string;
  onClick: () => void;
  tone?: keyof typeof OPS_ROW_ACTION_TONE;
  disabled?: boolean;
  busy?: boolean;
  expanded?: boolean;
  controls?: string;
}) {
  return (
    <Tip content={label} wrap="auto">
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        aria-label={label}
        aria-expanded={expanded}
        aria-controls={controls}
        onClick={onClick}
        disabled={disabled || busy}
        aria-busy={busy || undefined}
      >
        {busy && <Spinner size="sm" tone="inherit" />}
        {!busy && <Icon className={cn("h-3.5 w-3.5", OPS_ROW_ACTION_TONE[tone])} aria-hidden="true" />}
      </Button>
    </Tip>
  );
}

/**
 * The updated cell: the relative time in the app language, right aligned,
 * the absolute time in the viewer's time zone in its tooltip. Render it under
 * `useTick()` (the tab already does) to keep it fresh. `completed` says the
 * time is when the row ended (a done or cancelled work item), not its last
 * update.
 */
export function OpsUpdated({ at, testId, completed = false }: { at: string | null | undefined; testId?: string; completed?: boolean }) {
  const { t_i18n, intl, locale } = useFormatter();
  const { user } = useAuth();
  if (!at) return <span className={OPS_UPDATED_COLUMN} aria-hidden="true" />;
  const absolute = formatInZone(at, viewerTimeZone(user?.timezone), locale);
  return (
    <Tip content={completed ? t_i18n("Completed {time}", { time: absolute }) : t_i18n("Updated {time}", { time: absolute })}>
      <span
        tabIndex={0}
        className={cn(OPS_UPDATED_COLUMN, "truncate content-compact text-default-secondary", TIP_WRAP_FOCUS_RING)}
        data-testid={testId}
      >
        {formatTimeAgo(intl, at)}
      </span>
    </Tip>
  );
}

/**
 * The optional chips of a row's line 1, one line high. A chip that does not
 * fit whole wraps onto a second line that is never shown, and that wrapped
 * chip is made inert: out of the Tab order and out of the accessibility tree,
 * so focus never lands on a chip nobody sees and never scrolls the strip
 * (never flex-wrap plus overflow-hidden alone for focusable children). A hidden chip also leaves the flow (`display: none`)
 * and the strip stops shrinking, so the strip is exactly as wide as the chips
 * it shows and the title gets the room back (a hidden chip that kept its flex
 * basis would leave the strip blank while the title is cut).
 * The fit is measured on every render and whenever the line resizes: every
 * chip is put back, the wrap is read, then the wrapped chips are taken out.
 * The facts the chips carry must also be in the row's hover card.
 *
 * Every badge in the strip is one line high, border included (`h-5 py-0`,
 * border-box): a badge at its own padding is its line height
 * + 2 x 2 px of padding + 2 x 1 px of border, over 20 px, and the strip's
 * `overflow-hidden` cut its bottom pixel, the bottom edge of an outline chip
 * (#9774). The badge is sized here, once, wherever a tab nests it (a `Tip`
 * wrapper included), so no chip carries its own height.
 */
export function OpsChipStrip({ children, testId }: { children: ReactNode; testId?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const strip = ref.current;
    if (!strip) return;
    const fit = () => {
      const chips = Array.from(strip.children).filter(
        (child): child is HTMLElement => child instanceof HTMLElement && child.dataset.opsStripStart === undefined,
      );
      // Put every chip back and let the strip shrink, so the wrap is read on
      // the width the line gives it.
      for (const chip of chips) chip.style.display = "";
      strip.style.flexShrink = "";
      const top = strip.getBoundingClientRect().top;
      const lineHeight = strip.clientHeight;
      // Not laid out (a hidden tab, jsdom): nothing to measure, keep every chip.
      if (lineHeight <= 0) return;
      const wrapped = chips.map((chip) => chip.getBoundingClientRect().top - top >= lineHeight - 0.5);
      chips.forEach((chip, i) => {
        const hidden = wrapped[i];
        chip.toggleAttribute("inert", hidden);
        chip.dataset.opsHidden = hidden ? "true" : "false";
        if (hidden) chip.style.display = "none";
      });
      // The strip now holds only what it shows: it keeps that width and the
      // title takes the rest.
      if (wrapped.some(Boolean)) strip.style.flexShrink = "0";
      strip.scrollTop = 0;
    };
    fit();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(fit);
    // The line, not the strip: a strip sized to its chips does not resize when
    // the window widens, the line does.
    observer.observe(strip.parentElement ?? strip);
    return () => observer.disconnect();
  });
  return (
    <span
      ref={ref}
      className={cn(
        "flex h-5 min-w-0 shrink flex-wrap content-start items-center gap-x-1.5 overflow-hidden [&>*]:shrink-0",
        "[&_[data-slot=badge]]:h-5 [&_[data-slot=badge]]:py-0",
      )}
      data-testid={testId}
      onScroll={(e) => {
        e.currentTarget.scrollTop = 0;
      }}
    >
      {/* Zero width: even the first chip wraps when it does not fit whole. */}
      <span className="-mr-1.5 h-5 w-0" aria-hidden="true" data-ops-strip-start="" />
      {children}
    </span>
  );
}

/** The height a row of an Operations list takes on screen: `OPS_ROW_HEIGHT` (64 px) plus the 8 px gap between cards. */
const OPS_ROW_PITCH_PX = 72;

/**
 * How many skeleton rows fill the window under a list's top, capped at the
 * page size: never a fixed 3 or 4 blocks (dev:skeleton-fidelity-sweep, #11345).
 */
export function opsSkeletonRowCount(pageSize: number, viewportHeight?: number): number {
  const height = viewportHeight ?? (typeof window === "undefined" ? 0 : window.innerHeight);
  const fit = Math.ceil(height / OPS_ROW_PITCH_PX);
  return Math.max(1, Math.min(pageSize, fit || pageSize));
}

/**
 * One Operations row on its way: the same card, the same padding and the same
 * `OpsRowGrid` as a loaded row, every slot greyed in place (the selection box
 * and the status icon, the title then the status chip, the summary line, the
 * two meta tokens, the updated time, one square per resting action). A loaded
 * row lands on it without moving (#11345).
 */
export function OpsRowSkeleton({
  actionsClassName,
  actions = 1,
  lead = 2,
  meta = true,
  index = 0,
}: {
  /** The tab's actions width, the one its rows pass to `OpsRowGrid`. */
  actionsClassName: string;
  /** Resting icon actions of a row (28 px squares). */
  actions?: number;
  /** Lead slots: 2 = the selection box then the status icon. */
  lead?: number;
  /** The meta column (from lg); false on a tab whose rows draw none. */
  meta?: boolean;
  /** The row's place in the list, to vary the title width as real titles vary. */
  index?: number;
}) {
  return (
    <Card data-testid="ops-row-skeleton" className={cn(OPS_ROW_CARD, "overflow-hidden")}>
      <CardContent className="p-0">
        <div className={cn(OPS_ROW_HEIGHT, "px-4 py-2.5")}>
          <OpsRowGrid
            actionsClassName={actionsClassName}
            lead={Array.from({ length: lead }).map((_, i) => (
              <Skeleton key={i} className={cn("h-4 w-4", i === 0 ? "rounded-[3px]" : "rounded-full")} />
            ))}
            main={
              <>
                <div className="flex h-5 min-w-0 items-center gap-2">
                  <Skeleton className={cn("h-3.5", index % 3 === 0 ? "w-2/5" : index % 3 === 1 ? "w-1/3" : "w-1/2")} />
                  <Skeleton className="h-5 w-16" />
                </div>
                <div className="flex h-4 items-center">
                  <Skeleton className={cn("h-3", index % 2 === 0 ? "w-3/5" : "w-1/2")} />
                </div>
              </>
            }
            meta={
              meta ? (
                <>
                  <div className="flex h-4 items-center"><Skeleton className="h-3 w-28" /></div>
                  <div className="flex h-4 items-center"><Skeleton className="h-3 w-20" /></div>
                </>
              ) : undefined
            }
            updated={
              <span className={cn(OPS_UPDATED_COLUMN, "flex h-4 items-center justify-end")}>
                <Skeleton className="h-3 w-12" />
              </span>
            }
            actions={Array.from({ length: actions }).map((_, i) => (
              <Skeleton key={i} className="h-7 w-7" />
            ))}
          />
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * An Operations list on its way, announced as one `role="status"` region: as
 * many `OpsRowSkeleton` as fill the window (capped at the page size), stacked
 * with the loaded list's gap. The list's `ListControlBar` stays mounted above
 * it with its select-all box (`loading`), so the header row does not pop in.
 */
export function OpsListSkeleton({
  label,
  pageSize,
  rows,
  testId = "list-skeleton",
  ...row
}: {
  /** What is loading, already translated. */
  label: string;
  /** The list's page size: the most rows drawn. */
  pageSize: number;
  /** A fixed row count (tests); default: the window's worth. */
  rows?: number;
  testId?: string;
} & Omit<Parameters<typeof OpsRowSkeleton>[0], "index">) {
  const count = rows ?? opsSkeletonRowCount(pageSize);
  return (
    <div role="status" aria-busy="true" aria-label={label} data-testid={testId} className="space-y-2">
      {Array.from({ length: count }).map((_, i) => (
        <OpsRowSkeleton key={i} index={i} {...row} />
      ))}
    </div>
  );
}

/**
 * The grid of one row, inside the tab's own surface (a card, a table row).
 * Every slot is one column of fixed width except `main`; `actionsClassName`
 * sets the tab's actions width. Every slot is vertically centred on the row
 * (`OPS_ROW_ALIGN`).
 */
export function OpsRowGrid({
  lead,
  main,
  meta,
  updated,
  actions,
  actionsClassName,
  actionsTestId,
}: {
  lead: ReactNode;
  main: ReactNode;
  meta?: ReactNode;
  updated: ReactNode;
  actions: ReactNode;
  actionsClassName: string;
  actionsTestId?: string;
}) {
  return (
    <div className={cn("flex w-full min-w-0 gap-3", OPS_ROW_ALIGN)} data-testid="ops-row-grid">
      <div className="flex shrink-0 items-center gap-3">{lead}</div>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">{main}</div>
      <div className={cn(OPS_META_COLUMN, "min-w-0 flex-col gap-0.5")} data-testid="ops-meta">
        {meta}
      </div>
      <div className="flex shrink-0 items-center">{updated}</div>
      <div
        className={cn("flex shrink-0 items-center justify-end gap-1", actionsClassName)}
        data-testid={actionsTestId}
        onClick={(e) => e.stopPropagation()}
      >
        {actions}
      </div>
    </div>
  );
}
