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
 * Layout check of a deck — the side panel behind the toolbar's
 * `Layout check` button, for decks and pages alike.
 *
 * `validate_spec` guarantees a deck is well-formed; whether six cards and a
 * callout FIT on the slide is a property of pixels, and only a render knows.
 * The platform renders the deck in the same headless browser the export uses
 * and measures every slide: a body that had to be shrunk to fit (`fit.ts`
 * publishes the ratio), text cut by its own box, a one-line label running past
 * its line, anything painted off the canvas. The agent gets this report in the
 * result of every save it makes; this panel is the same report for a human —
 * the deck's author checking before presenting, a reviewer about to comment.
 *
 * The check runs when the panel opens (opening it IS asking) and again on
 * request. A save landing while it is open (the studio, a collaborator) marks
 * the report stale rather than re-rendering on its own: the agent's save has
 * just run the same check, and a second render per revision is a cost the
 * user should choose (a re-check of the revision the save measured is served
 * from the backend's cache, not rendered again). Each finding names its slide
 * by its STABLE id; clicking one puts that slide on the stage wherever it has
 * moved since, and a slide that is gone is shown as such rather than sending
 * the reader to whatever now sits at its old position. `Fix with my assistant`
 * hands the findings to the studio as a primed message, so fixing is one click
 * from seeing.
 *
 * The check's LIFECYCLE is held by the viewer, not by this panel: a
 * render takes seconds, and closing the panel to look at the deck while it
 * runs must not lose it. `useLayoutCheckRun` (its own file) is that state —
 * the viewer calls it, spins the toolbar button while a check is in flight
 * (panel open or not) and hands the run to this panel, which asks for a check
 * when it opens with nothing current to show (no report, a stale one, a
 * skipped one) and shows the report that arrived while it was closed instead
 * of rendering the deck a second time.
 */
import { useEffect, useMemo, useRef } from "react";
import {
  AArrowDown,
  AlertTriangle,
  AlignVerticalJustifyStart,
  ArrowDownToLine,
  Calculator,
  CalendarClock,
  ChartColumn,
  ChartColumnDecreasing,
  CheckCircle2,
  CircleDot,
  Columns2,
  Columns4,
  ImageMinus,
  Minimize2,
  Network,
  Quote,
  RefreshCw,
  RotateCcw,
  Rows3,
  Ruler,
  Scissors,
  Shrink,
  Sparkles,
  Spline,
  Tags,
  UnfoldVertical,
  UsersRound,
  WrapText,
  X,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn, pluralize } from "@/lib/utils";
import type {
  Presentation,
  PresentationLayoutCheck,
  PresentationLayoutFinding,
  PresentationLayoutFindingKind,
  PresentationLayoutSeverity,
} from "@/types";

import { DECK_KEYS_OFF_ATTR } from "./deck-keys";
import type { LayoutCheckRun } from "./useLayoutCheckRun";
import { Spinner } from "@filigran/design-system";

/** `MAX_REPORTED_FINDINGS` in `services/presentation_layout.py` — the primed
 *  message keeps the same bound so the agent reads the same list. */
const MAX_PRIMED_FINDINGS = 8;

/** What the audience sees go wrong, one label per `FindingKind`. The table is
 *  read by `validate:i18n` (every label is a key of the 4 catalogs), which a
 *  `t_i18n(kind.label)` call alone would hide. @i18n-keys */
const KIND_LABELS = {
  fit: "Shrunk to fit",
  clipped: "Cut off",
  nowrap: "Runs past its line",
  overflow: "Off the canvas",
  sparse: "Leaves the bottom empty",
  logo: "Logos too small",
  lines: "Statement too long",
  columns: "Columns wrapped",
  chart: "Chart too short",
  values: "Chart values hidden",
  rotated: "Chart values rotated",
  shrunk: "Chart values shrunk",
  size: "Drawn below its size",
  org_scale: "Org chart too large to read",
  org_width: "Org chart level too wide",
  org_label: "Org chart label cut",
  org_siblings: "Org chart cards uneven",
  org_crossing: "Line through a seat",
  org_bubble: "Bubble label overlaps",
  org_counts: "Counts do not add up",
  org_crowded: "Org chart crowded",
  org_undated: "Planned seats undated",
  org_hint: "Draw it as an org chart",
  unsettled: "Layout did not settle",
  map_overlap: "Map labels overlap",
  zone_chips: "Zone chips do not fit",
  arch_row: "Architecture row uneven",
  uneven: "Columns end apart",
  floating: "Lone block floats at the top",
  top_heavy: "Body sits in the top",
  wrapped: "Title wraps to a second line",
  wrap_title: "Slide, panel or chart title wraps",
  wrap_legend: "Chart legend wraps",
  unfilled: "Does not fill the room it asked for",
} as const satisfies Record<PresentationLayoutFindingKind, string>;

/** The icon drawn beside each `FindingKind`. */
const KIND_ICONS: Record<PresentationLayoutFindingKind, LucideIcon> = {
  fit: Minimize2,
  clipped: Scissors,
  nowrap: WrapText,
  overflow: ArrowDownToLine,
  sparse: UnfoldVertical,
  logo: ImageMinus,
  lines: Quote,
  columns: Columns4,
  chart: ChartColumnDecreasing,
  values: ChartColumn,
  rotated: RotateCcw,
  shrunk: AArrowDown,
  size: Shrink,
  org_scale: Minimize2,
  org_width: Ruler,
  org_label: WrapText,
  org_siblings: Rows3,
  org_crossing: Spline,
  org_bubble: CircleDot,
  org_counts: Calculator,
  org_crowded: UsersRound,
  org_undated: CalendarClock,
  org_hint: Network,
  unsettled: Network,
  map_overlap: CircleDot,
  zone_chips: Tags,
  arch_row: Rows3,
  uneven: Columns2,
  floating: AlignVerticalJustifyStart,
  top_heavy: AlignVerticalJustifyStart,
  wrapped: WrapText,
  wrap_title: WrapText,
  wrap_legend: WrapText,
  unfilled: UnfoldVertical,
};

/** Badge, ring and icon colour per severity; `rank` orders them worst first
 *  (the backend's `_SEVERITY_ORDER`) so a slide's row wears its worst one. */
const SEVERITY_CONFIG: Record<
  PresentationLayoutSeverity,
  { badge: "warning" | "destructive" | "cyan"; label: string; ring: string; icon: string; rank: number }
> = {
  overloaded: { badge: "destructive", label: "Overloaded", ring: "border-feedback-error-primary/50", icon: "text-feedback-error-primary", rank: 0 },
  dense: { badge: "warning", label: "Dense", ring: "border-feedback-alert-primary/40", icon: "text-feedback-warning-primary", rank: 1 },
  advisory: { badge: "cyan", label: "Advisory", ring: "border-accent/30", icon: "text-accent", rank: 2 },
};

/** One slide's findings, in deck order — what the list is grouped by. */
interface SlideGroup {
  /** Position at the time of the check (the report's own numbering). */
  index: number;
  number: number;
  /** The slide's stable id when the render published one. */
  slideId: string | null;
  layout: string | null;
  title: string;
  findings: PresentationLayoutFinding[];
  worst: PresentationLayoutSeverity;
}

function groupBySlide(findings: PresentationLayoutFinding[]): SlideGroup[] {
  const groups = new Map<number, SlideGroup>();
  for (const finding of findings) {
    const group = groups.get(finding.slide_index) ?? {
      index: finding.slide_index,
      number: finding.slide_number,
      slideId: finding.slide_id,
      layout: finding.layout,
      title: finding.title,
      findings: [],
      worst: finding.severity,
    };
    group.findings.push(finding);
    if (SEVERITY_CONFIG[finding.severity].rank < SEVERITY_CONFIG[group.worst].rank) group.worst = finding.severity;
    groups.set(finding.slide_index, group);
  }
  return [...groups.values()].sort((a, b) => a.index - b.index);
}

/**
 * Where the slide a group describes is NOW: its stable id resolved against the
 * current spec (a reorder since the check moves it, and the row follows), the
 * recorded position for a slide the render gave no id, `null` for a slide that
 * is gone — never the unrelated slide that now occupies its old position.
 */
function currentSlideIndex(group: Pick<SlideGroup, "index" | "slideId">, slides: { id?: string }[]): number | null {
  if (group.slideId) {
    const index = slides.findIndex((slide) => slide.id === group.slideId);
    return index >= 0 ? index : null;
  }
  return group.index < slides.length ? group.index : null;
}

/**
 * The findings as a message for the studio — the same shape as the
 * `Layout check` section the agent reads in its own tool results, so the
 * conversation and the tool speak of the same slides in the same words.
 */
function primedMessage(
  report: PresentationLayoutCheck,
  unit: string,
  intro: string,
  more: (hidden: number) => string,
): string {
  const lines = report.findings.slice(0, MAX_PRIMED_FINDINGS).map((finding) => {
    let where = `${unit} ${finding.slide_number}`;
    if (finding.layout) where += ` [${finding.layout}]`;
    if (finding.title) where += ` "${finding.title}"`;
    return `- ${where} (${finding.severity}): ${finding.message}`;
  });
  const hidden = report.findings.length - MAX_PRIMED_FINDINGS;
  if (hidden > 0) lines.push(`- ${more(hidden)}`);
  return `${intro}\n${lines.join("\n")}`;
}

export function PresentationLayoutPanel({
  presentation,
  check,
  activeIndex,
  onSelectSlide,
  onFixWithAssistant,
  onClose,
  className,
}: {
  presentation: Presentation;
  /** The deck's check, held by the viewer (`useLayoutCheckRun`). */
  check: LayoutCheckRun;
  /** 0-based index of the slide / page on the stage — its group is marked. */
  activeIndex: number;
  onSelectSlide?: (index: number) => void;
  /**
   * Open the studio with this message in the composer (managers only — a
   * reader's assistant cannot save the deck). Absent: no button.
   */
  onFixWithAssistant?: (message: string) => void;
  onClose: () => void;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const isPage = presentation.kind === "page";
  const unit = isPage ? "page" : "slide";
  const positionLabel = (position: number) =>
    isPage ? t_i18n("Page {position}", { position }) : t_i18n("Slide {position}", { position });
  const untitled = isPage ? t_i18n("Untitled page") : t_i18n("Untitled slide");

  const { report, isPending, checkedRevision, stale, run: runCheck } = check;

  // Opening the panel is asking for the check — once, on mount, and only when
  // there is nothing current to show. A check still running from before the
  // panel was closed, or a measured report for this revision, is shown as it
  // is rather than rendering the deck a second time; no report, a stale one or
  // a skipped one is a reason to ask.
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (isPending || (report !== null && !report.skipped && !stale)) return;
    runCheck();
  }, [isPending, report, runCheck, stale]);

  const groups = useMemo(() => (report ? groupBySlide(report.findings) : []), [report]);
  const overloaded = report?.findings.filter((f) => f.severity === "overloaded").length ?? 0;
  // The header wears the worst severity in the report: a deck whose only
  // findings are advisory is not told it has a problem in amber.
  const worstOverall = groups.reduce<PresentationLayoutSeverity>(
    (worst, group) => (SEVERITY_CONFIG[group.worst].rank < SEVERITY_CONFIG[worst].rank ? group.worst : worst),
    "advisory",
  );
  const slides = presentation.spec.slides;
  const goneLabel = isPage
    ? t_i18n("This page is no longer in the presentation")
    : t_i18n("This slide is no longer in the presentation");

  const fixMessage =
    report && report.findings.length > 0 && onFixWithAssistant
      ? primedMessage(
          report,
          unit,
          t_i18n("Fix the layout of this presentation. The layout check found these issues; fix them and save again:"),
          (hidden) => t_i18n("...and {count} more; fix the above and save, the next check lists the rest.", { count: hidden }),
        )
      : null;

  return (
    <aside
      className={cn(
        "flex h-full w-full min-w-0 flex-col border-l border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm",
        className,
      )}
      aria-label={t_i18n("Layout check")}
      data-testid="presentation.layoutPanel"
      {...DECK_KEYS_OFF_ATTR}
    >
      <div className="flex items-center justify-between border-b border-elevation-subtle-soft px-4 py-3">
        <div>
          <h2 className="content-base font-semibold">{t_i18n("Layout check")}</h2>
          <p className="content-compact text-default-secondary">
            {isPage
              ? t_i18n("Each page rendered and measured: what the reader would see cut off or crowded.")
              : t_i18n("Each slide rendered and measured: what the audience would see cut off or crowded.")}
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label={t_i18n("Close")}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {isPending ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
            <Spinner size="md" tone="inherit" className="text-accent" />
            <p className="text-[12.5px] text-default-secondary">
              {t_i18n("Rendering {count} {unit}...", {
                count: presentation.slide_count,
                unit: t_i18n(pluralize(presentation.slide_count, unit)),
              })}
            </p>
          </div>
        ) : check.isError ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
            <AlertTriangle className="h-6 w-6 text-feedback-warning-primary" aria-hidden="true" />
            <p className="text-[12.5px] text-default-secondary">
              {check.error instanceof Error && check.error.message
                ? check.error.message
                : t_i18n("The layout check could not run.")}
            </p>
          </div>
        ) : !report ? null : report.skipped ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
            <Ruler className="h-6 w-6 text-default-secondary" aria-hidden="true" />
            <p className="text-[12.5px] font-medium text-default-primary">{t_i18n("Not measured")}</p>
            <p className="text-[12px] text-default-secondary">{report.skipped}</p>
          </div>
        ) : report.findings.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
            <CheckCircle2 className="h-6 w-6 text-feedback-success-primary" aria-hidden="true" />
            <p className="text-[12.5px] font-medium text-default-primary">
              {t_i18n("Every one of the {count} {unit} fits its canvas.", {
                count: report.slide_count,
                unit: t_i18n(pluralize(report.slide_count, unit)),
              })}
            </p>
            <p className="text-[12px] text-default-secondary">
              {/* A body shrunk by a few percent is not reported (below the
                  dense threshold), so "nothing was shrunk" would be untrue. */}
              {t_i18n("Nothing is cut off and nothing is crowded enough to need a change.")}
            </p>
          </div>
        ) : (
          <>
            <div className="mb-3 flex items-center gap-2 text-[12px]">
              <AlertTriangle
                className={cn("h-4 w-4 shrink-0", SEVERITY_CONFIG[worstOverall].icon)}
                aria-hidden="true"
              />
              <span className="text-default-primary">
                {t_i18n("{count} {issues} on {slides} {unit}", {
                  count: report.findings.length,
                  issues: t_i18n(pluralize(report.findings.length, "issue")),
                  slides: groups.length,
                  unit: t_i18n(pluralize(groups.length, unit)),
                })}
              </span>
              {overloaded > 0 && (
                <Badge variant="destructive" className="ml-auto px-1.5 py-0">
                  {t_i18n("{count} overloaded", { count: overloaded })}
                </Badge>
              )}
            </div>
            <ol className="space-y-2">
              {groups.map((group) => {
                // The slide as it is NOW: moved since the check, the row
                // follows it; removed, the row says so and goes nowhere.
                const target = currentSlideIndex(group, slides);
                const gone = target === null;
                const isActive = !gone && target === activeIndex;
                const severity = SEVERITY_CONFIG[group.worst];
                const row = (
                  <button
                    type="button"
                    onClick={() => {
                      if (!gone) onSelectSlide?.(target);
                    }}
                    disabled={gone}
                    aria-current={isActive || undefined}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-t-sm px-3 py-2 text-left transition-colors",
                      gone ? "cursor-not-allowed opacity-60" : "hover:bg-elevation-hover",
                    )}
                  >
                    <span className="shrink-0 rounded-sm bg-elevation-hover px-1.5 py-0.5 font-mono-plex content-compact tabular-nums text-default-primary">
                      {positionLabel(gone ? group.number : target + 1)}
                    </span>
                    {group.layout && (
                      <span className="shrink-0 font-mono-plex content-compact text-default-secondary">{group.layout}</span>
                    )}
                    <span className="min-w-0 flex-1 truncate text-[12.5px] text-default-secondary">
                      {group.title || untitled}
                    </span>
                    <Badge variant={severity.badge} className="shrink-0 px-1.5 py-0">
                      {t_i18n(severity.label)}
                    </Badge>
                  </button>
                );
                return (
                  <li key={group.slideId ?? `at-${group.index}`}>
                    <div
                      className={cn(
                        "rounded-sm border bg-elevation-default",
                        isActive ? "border-accent/50 shadow-[0_0_0_1px_color-mix(in_srgb,_var(--color-accent)_calc(0.25_*_100%),_transparent)]" : severity.ring,
                      )}
                    >
                      {gone ? (
                        <Tip content={goneLabel} wrap wrapClassName="flex w-full">
                          {row}
                        </Tip>
                      ) : (
                        row
                      )}
                      <ul className="space-y-1.5 border-t border-elevation-subtle-soft px-3 py-2">
                        {group.findings.map((finding, i) => {
                          const known = finding.kind in KIND_LABELS;
                          const kindLabel = KIND_LABELS[known ? finding.kind : "overflow"];
                          const KindIcon = KIND_ICONS[known ? finding.kind : "overflow"];
                          return (
                            <li key={`${finding.kind}-${i}`} className="flex gap-2">
                              <Tip content={t_i18n(kindLabel)}>
                                <span
                                  className={cn(
                                    "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center",
                                    SEVERITY_CONFIG[finding.severity].icon,
                                  )}
                                >
                                  <KindIcon className="h-3.5 w-3.5" aria-hidden="true" />
                                </span>
                              </Tip>
                              <p className="text-[12px] leading-snug text-default-secondary">
                                {finding.kind === "fit" && finding.scale !== null && (
                                  <span className="mr-1.5 rounded-sm bg-elevation-hover px-1 font-mono-plex content-compact tabular-nums">
                                    {Math.round(finding.scale * 100)}%
                                  </span>
                                )}
                                {finding.message}
                              </p>
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  </li>
                );
              })}
            </ol>
          </>
        )}
      </div>

      {/* The revision label never truncates: in a narrow panel (341 px at
          1024) the actions wrap to a second row under it, and the actions
          wrap among themselves when a long label (fr, es) does not fit one
          row, so no button runs past the panel edge. */}
      <div
        className="flex flex-wrap items-center justify-between gap-2 border-t border-elevation-subtle-soft px-4 py-3"
        data-testid="presentation.layoutPanel.footer"
      >
        <span
          className="content-compact tabular-nums text-default-secondary"
          data-testid="presentation.layoutPanel.revision"
        >
          {stale
            ? t_i18n("Changed since this check (revision {revision})", { revision: presentation.revision })
            : report && !isPending && checkedRevision !== null
              ? t_i18n("Checked revision {revision}", { revision: checkedRevision })
              : t_i18n("Revision {revision}", { revision: presentation.revision })}
        </span>
        <div
          className="ml-auto flex min-w-0 max-w-full flex-wrap items-center justify-end gap-2"
          data-testid="presentation.layoutPanel.actions"
        >
          <Tip content={t_i18n("Render the presentation again and measure it")}>
            <Button
              variant={stale ? "default" : "ghost"}
              size="sm"
              className="gap-1.5"
              onClick={runCheck}
              disabled={isPending}
              data-testid="presentation.layoutPanel.check" aria-busy={isPending || undefined}>
              {isPending && <Spinner size="sm" tone="inherit" />}{!isPending && <RefreshCw className="h-3.5 w-3.5" />}
              {t_i18n("Check again")}
            </Button>
          </Tip>
          {fixMessage && onFixWithAssistant && (
            <Tip content={t_i18n("Open the studio with these findings ready to send")}>
              <Button
                size="sm"
                className="gap-1.5"
                onClick={() => onFixWithAssistant(fixMessage)}
                disabled={isPending}
                data-testid="presentation.layoutPanel.fix"
              >
                <Sparkles className="h-3.5 w-3.5" />
                {t_i18n("Fix with my assistant")}
              </Button>
            </Tip>
          )}
        </div>
      </div>
    </aside>
  );
}
