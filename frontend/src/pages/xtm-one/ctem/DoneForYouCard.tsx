// SPDX-License-Identifier: Apache-2.0

/**
 * The Done for you timeline of the CTEM command center v2 (C2 slice V3, design
 * dev-docs/ctem-command-center-v2.md section 4.3): what the platform did over
 * the last 7 or 30 days, on a horizontal axis with one lane per source.
 *
 * Fleets off is the primary case: the Modules lane holds the events the page
 * already reads (syncs, registrations, health, validation) and the Automations
 * lane the runs of the built-in automations and the digests; the Fleets lane is
 * drawn only when a fleet ran in the period. The Outcomes lane holds what a run
 * recorded (actions taken, a digest published), linked to the run it came
 * from by a dashed line ("started" in the legend). More than three points in one
 * day of one lane become one count badge; a click lists that day under the
 * lanes. A point opens its run. Nothing here starts a run.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowUpRight, History, X } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { useFormatter } from "@/i18n";
import { formatDayIntl, formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import { activityText } from "./activity-text";
import { CtemWidgetHeader } from "./CtemWidgetHeader";
import {
  DFY_PERIODS, doneForYouCells, doneForYouPoints,
  type DfyCell, type DfyDigest, type DfyFleetRun, type DfyLane, type DfyPeriod, type DfyPoint, type DfyRun, type DfyTone,
} from "./done-for-you";
import type { ActivityEvent } from "./activity-feed";

/** The name of each lane. @i18n-keys */
const LANE_LABELS = {
  modules: "Modules",
  automations: "Automations",
  fleets: "Fleets",
  outcomes: "Outcomes",
} as const satisfies Record<DfyLane, string>;

/** The legend, one entry per colour. @i18n-keys */
const TONE_LABELS = {
  module: "Module event",
  automation: "Automation run",
  fleet: "Fleet run",
  outcome: "Outcome of a run",
  attention: "Needs attention",
} as const satisfies Record<DfyTone, string>;

const TONE_DOT: Record<DfyTone, string> = {
  module: "bg-feedback-info-primary",
  automation: "bg-accent",
  fleet: "bg-elevation-default ring-2 ring-inset ring-accent",
  outcome: "bg-feedback-success-primary",
  attention: "bg-feedback-warning-primary",
};

/** The words of a run's outcome. @i18n-keys */
const OUTCOME_LABELS = {
  ok: "Run outcome: succeeded",
  failed: "Run outcome: failed",
  interrupted: "Run outcome: interrupted",
  active: "Run outcome: running",
} as const;

const LANE_LABEL_WIDTH = "6.5rem";

/** Rows a day list shows before "N more" sends to the executions page, so the card never scrolls inside itself. */
export const DFY_DAY_LIST_MAX = 8;

/** The count badge: the attention tone when any of its points needs attention, else neutral. */
const CLUSTER_TONE = {
  attention: "bg-feedback-warning-primary/20 text-feedback-warning-tertiary",
  neutral: "bg-elevation-highlight text-default-primary",
} as const;

export interface DoneForYouCardProps {
  events: ActivityEvent[];
  runs: DfyRun[];
  digests: DfyDigest[];
  fleetRuns: DfyFleetRun[];
  aiLockReason: AiLockReason | null;
  /** The height its content needs, so the page sizes the card to its lanes. */
  onContentHeight?: (px: number) => void;
  /** Now, for tests. */
  now?: number;
}

export function DoneForYouCard({ events, runs, digests, fleetRuns, aiLockReason, onContentHeight, now }: DoneForYouCardProps) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const [period, setPeriod] = useState<DfyPeriod>(7);
  const [open, setOpen] = useState<{ lane: DfyLane; day: number } | null>(null);
  // Read once when the card mounts: the axis does not move while it is open.
  const [mountedAt] = useState(() => Date.now());
  const at = now ?? mountedAt;

  const points = useMemo(() => doneForYouPoints({ events, runs, digests, fleetRuns }), [events, runs, digests, fleetRuns]);
  const { lanes, cells, count } = useMemo(() => doneForYouCells(points, period, at), [points, period, at]);
  const byLane = useMemo(() => {
    const map = new Map<DfyLane, Map<number, DfyCell>>();
    for (const cell of cells) {
      const days = map.get(cell.lane) ?? new Map<number, DfyCell>();
      days.set(cell.day, cell);
      map.set(cell.lane, days);
    }
    return map;
  }, [cells]);
  const openCell = open ? byLane.get(open.lane)?.get(open.day) ?? null : null;
  const pointsById = useMemo(() => new Map(points.map((p) => [p.id, p])), [points]);

  const dayDate = (day: number) => {
    const d = new Date(at);
    d.setHours(12, 0, 0, 0);
    d.setDate(d.getDate() - (period - 1 - day));
    return d;
  };
  const dayLabel = (day: number) =>
    day === period - 1
      ? t_i18n("Today")
      : period === 7
        ? intl.formatDate(dayDate(day), { weekday: "short" })
        : intl.formatDate(dayDate(day), { day: "numeric", month: "short" });
  const shownTick = (day: number) => period === 7 || day === period - 1 || (period - 1 - day) % 7 === 0;

  const describe = (point: DfyPoint): { title: string; detail: string } => {
    const s = point.subject;
    switch (s.kind) {
      case "event": {
        const text = activityText(s.event, t_i18n, (iso) => formatDayIntl(intl, iso));
        return { title: text.title, detail: text.detail };
      }
      case "run": {
        const outcome = s.outcome && s.outcome !== "other" ? t_i18n(OUTCOME_LABELS[s.outcome]) : "";
        return { title: s.name, detail: [s.agent, outcome].filter(Boolean).join(" · ") };
      }
      case "actions":
        return {
          title: t_i18n("{count, plural, one {# action taken} other {# actions taken}}", { count: s.count }),
          detail: t_i18n("Outcome of {run}", { run: s.name }),
        };
      case "digest":
        return { title: t_i18n("Digest published"), detail: t_i18n("Outcome of {run}", { run: s.name }) };
    }
  };

  const openPoint = (point: DfyPoint) => {
    if (point.runId) navigate(`/operations/executions/${point.runId}`, { state: { backTo: "/", backLabel: "Back to Command Center" } });
  };

  const tip = (point: DfyPoint) => {
    const { title, detail } = describe(point);
    const source = point.from ? pointsById.get(point.from) : undefined;
    return (
      <div className="max-w-xs space-y-0.5">
        <p className="content-compact font-semibold">{title}</p>
        {detail && <p className="text-xs">{detail}</p>}
        <p className="text-xs">{formatShortDateTimeIntl(intl, point.at)}</p>
        {source && <p className="text-xs">{t_i18n("Started by {run}", { run: describe(source).title })}</p>}
      </div>
    );
  };

  const dot = (point: DfyPoint) => {
    const { title } = describe(point);
    const common = cn("block h-3 w-3 shrink-0 rounded-full", TONE_DOT[point.tone]);
    return (
      <Tip key={point.id} content={tip(point)}>
        {point.runId ? (
          <button
            type="button"
            onClick={() => openPoint(point)}
            aria-label={t_i18n("{title}, {time}", { title, time: formatTimeAgo(intl, point.at) })}
            className={cn("relative z-10 flex h-6 w-6 items-center justify-center rounded-full hover:bg-elevation-hover", BUTTON_ROLE_FOCUS_RING)}
            data-tone={point.tone}
            data-point={point.id}
          >
            <span className={common} aria-hidden />
          </button>
        ) : (
          <span
            tabIndex={0}
            role="img"
            aria-label={t_i18n("{title}, {time}", { title, time: formatTimeAgo(intl, point.at) })}
            className={cn("relative z-10 flex h-6 w-6 items-center justify-center rounded-full", BUTTON_ROLE_FOCUS_RING)}
            data-tone={point.tone}
            data-point={point.id}
          >
            <span className={common} aria-hidden />
          </span>
        )}
      </Tip>
    );
  };

  // The content is measured, so the page sizes the card to its lanes (no empty band).
  const contentRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = contentRef.current;
    if (!node || !onContentHeight) return;
    const report = () => onContentHeight(Math.ceil(node.getBoundingClientRect().height) + 2);
    report();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(report);
    observer.observe(node);
    return () => observer.disconnect();
  }, [onContentHeight]);

  // The dashed links from a run to its outcome, between the dots drawn (a
  // point inside a count badge has no dot of its own: its day list names it).
  const tableRef = useRef<HTMLDivElement>(null);
  const [links, setLinks] = useState<{ id: string; x1: number; y1: number; x2: number; y2: number }[]>([]);
  const linkPairs = useMemo(() => {
    const drawn = new Set(cells.filter((c) => !c.clustered).flatMap((c) => c.points.map((p) => p.id)));
    return points.filter((p) => p.from && drawn.has(p.id) && drawn.has(p.from)).map((p) => [p.from as string, p.id] as const);
  }, [cells, points]);
  useLayoutEffect(() => {
    const table = tableRef.current;
    if (!table) return;
    const measure = () => {
      const box = table.getBoundingClientRect();
      const centre = (id: string) => {
        const node = [...table.querySelectorAll<HTMLElement>("[data-point]")].find((n) => n.dataset.point === id);
        if (!node) return null;
        const r = node.getBoundingClientRect();
        return { x: r.left - box.left + r.width / 2, y: r.top - box.top + r.height / 2 };
      };
      const next: { id: string; x1: number; y1: number; x2: number; y2: number }[] = [];
      for (const [from, to] of linkPairs) {
        const a = centre(from);
        const b = centre(to);
        if (a && b) next.push({ id: `${from}>${to}`, x1: a.x, y1: a.y, x2: b.x, y2: b.y });
      }
      setLinks(next);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(table);
    return () => observer.disconnect();
  }, [linkPairs]);

  const columns = `${LANE_LABEL_WIDTH} repeat(${period}, minmax(0, 1fr))`;
  const clusterTone = (cell: DfyCell): keyof typeof CLUSTER_TONE =>
    cell.points.some((p) => p.tone === "attention") ? "attention" : "neutral";
  const usedTones = new Set(points.map((p) => p.tone));

  return (
    <Card className="flex flex-col" data-testid="ctem-done-for-you">
      <CardContent ref={contentRef} className="@container flex flex-col gap-3 p-4">
        <CtemWidgetHeader
          icon={<History className="h-4 w-4" />}
          title={t_i18n("Done for you")}
          count={intl.formatNumber(count)}
          countTestId="ctem-done-for-you-count"
          actions={
            <>
            <SegmentedFilter
              size="md"
              selection="radio"
              ariaLabel={t_i18n("Done for you period")}
              testId="ctem-done-for-you-period"
              options={DFY_PERIODS.map((days) => ({
                key: String(days),
                label: t_i18n("{count, plural, one {# day} other {# days}}", { count: days }),
              }))}
              value={String(period)}
              onChange={(key) => {
                setPeriod(Number(key) as DfyPeriod);
                setOpen(null);
              }}
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-9 gap-1"
              aria-label={t_i18n("All executions")}
              onClick={() => navigate("/operations/executions")}
            >
              <span className="hidden @xl:inline">{t_i18n("All executions")}</span> <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
            </Button>
            </>
          }
        />

        <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-default-secondary" aria-label={t_i18n("Legend")}>
          {(Object.keys(TONE_LABELS) as DfyTone[])
            .filter((tone) => tone === "module" || tone === "automation" || usedTones.has(tone))
            .map((tone) => (
              <li key={tone} className="flex items-center gap-1.5">
                <span className={cn("h-2.5 w-2.5 rounded-full", TONE_DOT[tone])} aria-hidden />
                {t_i18n(TONE_LABELS[tone])}
              </li>
            ))}
          {linkPairs.length > 0 && (
            <li className="flex items-center gap-1.5">
              <svg width="16" height="4" aria-hidden className="text-default-secondary">
                <line x1="0" y1="2" x2="16" y2="2" stroke="currentColor" strokeWidth="1.5" strokeDasharray="3 2" />
              </svg>
              {t_i18n("Timeline link: started")}
            </li>
          )}
        </ul>

        <div ref={tableRef} role="table" aria-label={t_i18n("Done for you")} className="relative flex flex-col">
          {links.length > 0 && (
            <svg className="pointer-events-none absolute inset-0 h-full w-full text-default-secondary" aria-hidden data-testid="ctem-done-for-you-links">
              {links.map((l) => (
                <line key={l.id} x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2} stroke="currentColor" strokeWidth="1.5" strokeDasharray="3 2" />
              ))}
            </svg>
          )}
          <div role="row" className="grid h-6 items-end border-b border-elevation-subtle-soft pb-1" style={{ gridTemplateColumns: columns }}>
            {/* The header stays in the grid flow (a visually hidden node is absolutely positioned and would pull every day one column left). */}
            <span role="columnheader" data-testid="ctem-done-for-you-lane-header">
              <span className="sr-only">{t_i18n("Lane")}</span>
            </span>
            {Array.from({ length: period }, (_, day) => (
              <span
                key={day}
                role="columnheader"
                className="relative h-4 text-xs tabular-nums text-default-secondary"
                aria-label={intl.formatDate(dayDate(day), { weekday: "long", day: "numeric", month: "long" })}
              >
                {/* A tick is never cut: it is laid over its neighbours (blank on 30 days), the last one kept inside the card. */}
                {shownTick(day) && (
                  <span
                    aria-hidden
                    data-testid="ctem-done-for-you-tick"
                    className={cn("absolute bottom-0 whitespace-nowrap", day === period - 1 ? "right-0" : "left-1/2 -translate-x-1/2")}
                  >
                    {dayLabel(day)}
                  </span>
                )}
              </span>
            ))}
          </div>
          {lanes.map((lane) => (
            <div
              key={lane}
              role="row"
              data-lane={lane}
              className="grid min-h-9 items-center border-b border-elevation-subtle-soft last:border-b-0"
              style={{ gridTemplateColumns: columns }}
            >
              <span role="rowheader" className="truncate pr-2 text-xs font-medium text-default-secondary">
                {t_i18n(LANE_LABELS[lane])}
              </span>
              {Array.from({ length: period }, (_, day) => {
                const cell = byLane.get(lane)?.get(day);
                return (
                  <div key={day} role="cell" className="flex min-w-0 flex-wrap items-center justify-center gap-0.5 py-1">
                    {cell && cell.clustered ? (
                      <button
                        type="button"
                        onClick={() => setOpen(open?.lane === lane && open.day === day ? null : { lane, day })}
                        aria-expanded={open?.lane === lane && open.day === day}
                        aria-label={t_i18n("{count, plural, one {# item} other {# items}} on {day}, show them", {
                          count: cell.points.length,
                          day: intl.formatDate(dayDate(day), { weekday: "long", day: "numeric", month: "long" }),
                        })}
                        className={cn(
                          "flex h-6 min-w-6 items-center justify-center rounded-full px-1.5 text-xs font-semibold tabular-nums",
                          CLUSTER_TONE[clusterTone(cell)],
                          BUTTON_ROLE_FOCUS_RING,
                        )}
                        data-testid="ctem-done-for-you-cluster"
                        data-tone={clusterTone(cell)}
                      >
                        {intl.formatNumber(cell.points.length)}
                      </button>
                    ) : (
                      cell?.points.map(dot)
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>

        {count === 0 && (
          <p className="text-xs text-default-secondary">
            {t_i18n("Nothing recorded in this period yet. Module syncs, built-in automations and their outcomes show here as they happen.")}
          </p>
        )}

        {openCell && (
          <div className="rounded-sm border border-elevation-subtle-soft p-2" data-testid="ctem-done-for-you-day">
            <div className="flex items-center justify-between gap-2 pb-1">
              <p className="content-compact font-semibold">
                {t_i18n(LANE_LABELS[openCell.lane])}
                {" · "}
                {intl.formatDate(dayDate(openCell.day), { weekday: "long", day: "numeric", month: "long" })}
              </p>
              <Button type="button" variant="ghost" size="icon" className="h-9 w-9" onClick={() => setOpen(null)} aria-label={t_i18n("Close")}>
                <X className="h-4 w-4" aria-hidden />
              </Button>
            </div>
            <ul className="flex flex-col divide-y divide-[var(--border-elevation-subtle-soft)]">
              {[...openCell.points].reverse().slice(0, DFY_DAY_LIST_MAX).map((point) => {
                const { title, detail } = describe(point);
                return (
                  <li key={point.id} className="flex min-h-9 items-center gap-2 py-1">
                    <span className={cn("h-2.5 w-2.5 shrink-0 rounded-full", TONE_DOT[point.tone])} aria-hidden />
                    <div className="min-w-0 flex-1">
                      <p className="content-compact truncate">{title}</p>
                      {detail && <p className="truncate text-xs text-default-secondary">{detail}</p>}
                    </div>
                    <span className="shrink-0 text-xs tabular-nums text-default-secondary">{formatShortDateTimeIntl(intl, point.at)}</span>
                    {point.runId && (
                      <Button type="button" variant="outline" size="sm" className="h-9 shrink-0" onClick={() => openPoint(point)} aria-label={`${t_i18n("Open run")}: ${title}`}>
                        {t_i18n("Open run")}
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
            {openCell.points.length > DFY_DAY_LIST_MAX && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="mt-1 h-9 gap-1"
                onClick={() => navigate("/operations/executions")}
                data-testid="ctem-done-for-you-day-more"
              >
                {t_i18n("{count, plural, one {# more in Executions} other {# more in Executions}}", {
                  count: openCell.points.length - DFY_DAY_LIST_MAX,
                })}
                <ArrowUpRight className="h-3.5 w-3.5" aria-hidden />
              </Button>
            )}
          </div>
        )}

        {aiLockReason && <AiLockedFooter reason={aiLockReason} />}
      </CardContent>
    </Card>
  );
}
