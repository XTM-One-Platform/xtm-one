// SPDX-License-Identifier: Apache-2.0

/**
 * Stats and coverage of the CTEM command center (C2 slice V5, redrawn by
 * #11547 slice S2d, design note dev-docs/ctem-stage-viz.md section 4, row
 * stats_coverage, R3-B2, R4-B5 and R4-B6): three tiles, one row at full width,
 * two columns at 1080.
 *
 * - Score by week: a heat map of the five stages (rows, labelled on the left,
 *   each opening its stage page) against the last 8 weeks of the posture
 *   history (columns, the first and the last dated on top), each cell the
 *   week's mean score in its band colour, with a five-step key under it
 *   (Critical to Strong). Every cell says its stage, week, score and band in
 *   its tooltip and to a screen reader: no band is told by colour alone.
 * - Change over the period: one signed bar per stage from a labelled zero
 *   line, its value printed at the bar end, green when the score went up, red
 *   when it went down.
 * - Findings: the vertical's three headline figures, compact (1.3K, the exact
 *   value on hover, rule 12), each opening the in-app stage page that holds it
 *   (#11324), with its 30-day sparkline at full row width.
 *
 * The ATT&CK tested vs untested part is dropped: the strip's Discovery column
 * carries that coverage, and the Discovery page its trend (R3-B2). When a
 * figure is module-wide while the organization context could tailor it
 * (`generic`), the shared notice says so. Nothing here starts a run, calls a
 * model or uses quota.
 */
import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { BarChart3, ChevronRight, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CompactFigure } from "@/components/ui/compact-figure";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { CTEM_PRODUCT_INFO, CTEM_STAGE_KEYS } from "@/lib/constants";
import type { CoverageHistoryPoint, CoverageModule, ExposureStats } from "@/lib/ctem-coverage-stats";
import { cn, formatCompactNumber } from "@/lib/utils";
import { GenericContextNotice } from "./GenericContextNotice";
import { SCORE_BANDS, scoreBandStyle, stageName, weeklyHeat } from "./stats-coverage";

/** One headline figure of the vertical, traceable to its module. */
interface StatsKpi {
  label: string;
  value: number | null;
  unit?: string;
  /** The CTEM stage page that holds the figure, when the module is connected. */
  stage?: string;
  /** The module to connect when it is not (the figure is then not shown). */
  connect?: CoverageModule | null;
  /** The figure's recorded values over the last 30 days, oldest first. */
  series?: readonly (number | null)[];
}

export interface StatsCoverageProps {
  /** The posture history (`GET /ctem-metrics/posture/history`), oldest first. */
  history: readonly CoverageHistoryPoint[] | null;
  stats: ExposureStats | null;
  findings: readonly StatsKpi[];
  onConnect: (module: CoverageModule) => void;
  /** A figure is module-wide while the organization context could tailor it:
   *  the card shows the shared notice with the setup call to action. */
  generic?: boolean;
  /** The height its content needs, so the page sizes the card to it. */
  onContentHeight?: (px: number) => void;
}

const TILE = "flex min-w-0 flex-col gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3";
const UP = "var(--color-feedback-success-primary)";
const DOWN = "var(--color-feedback-error-primary)";

/** A 30-day line at the full width of its row; nothing is drawn under two recorded values. */
function FindingSparkline({ values, label }: { values: readonly (number | null)[]; label: string }) {
  const { t_i18n } = useFormatter();
  const real = values.filter((v): v is number => typeof v === "number");
  if (real.length < 2) {
    // Days recorded but the figure missing from them is said as such, never "after 2 days" (UX round 1 on #11631, B1).
    return (
      <span className="block content-caption text-default-secondary" data-testid="ctem-finding-no-trend">
        {values.length < 2 ? t_i18n("Trend after 2 recorded days") : t_i18n("Too few days with this figure for a trend")}
      </span>
    );
  }
  const lo = Math.min(...real);
  const hi = Math.max(...real);
  const span = hi - lo || 1;
  const W = 100;
  const H = 20;
  const x = (i: number) => (i / (real.length - 1)) * W;
  const y = (v: number) => H - 2 - ((v - lo) / span) * (H - 4);
  const points = real.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <span role="img" aria-label={t_i18n("{label}: last 30 days", { label })} className="block h-5 w-full" data-testid="ctem-finding-sparkline">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-full w-full overflow-visible" aria-hidden>
        <polyline
          points={points}
          fill="none"
          stroke="var(--color-accent)"
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    </span>
  );
}

export function StatsCoverageCard({
  history, stats, findings, onConnect, generic = false, onContentHeight,
}: StatsCoverageProps) {
  const { t_i18n, intl } = useFormatter();
  const nf = useNumberFormat();
  const navigate = useNavigate();
  const moduleName = (m: string) => CTEM_PRODUCT_INFO[m]?.name ?? m;
  const percent = (v: number) => intl.formatNumber(v / 100, { style: "percent", maximumFractionDigits: 0 });
  const signed = (v: number) => intl.formatNumber(v, { signDisplay: "exceptZero" });
  const weekLabel = (iso: string) =>
    intl.formatDate(new Date(`${iso}T00:00:00Z`), { day: "numeric", month: "short", timeZone: "UTC" });

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

  const heat = weeklyHeat(history, CTEM_STAGE_KEYS);
  const labelOf = new Map((stats?.stages ?? []).map((s) => [s.key, s.label]));
  const name = (key: string) => stageName(key, labelOf.get(key) ?? key, t_i18n);
  const deltas = (stats?.stages ?? []).map((s) => ({ key: s.key, change: s.change }));
  const maxAbs = Math.max(1, ...deltas.map((d) => Math.abs(d.change ?? 0)));

  return (
    <Card className="@container flex flex-col" data-testid="ctem-stats-coverage">
      <CardContent ref={contentRef} className="flex flex-col gap-3 p-4">
        <h2 className="min-h-7 content-base font-semibold flex items-center gap-2">
          <BarChart3 className="h-4 w-4 text-accent" aria-hidden /> {t_i18n("Stats and coverage")}
        </h2>
        {generic && (
          <GenericContextNotice
            title={t_i18n("These figures are module-wide")}
            adminText={t_i18n("Known-exploited counts every such vulnerability in OpenCTI, not only those of your software. Set up your organization context to count what affects you.")}
            readerText={t_i18n("Known-exploited counts every such vulnerability in OpenCTI, not only those of your software. An administrator can set up the organization context to count what affects you.")}
            testId="ctem-stats-generic"
          />
        )}
        <div className="grid grid-cols-1 gap-3 @2xl:grid-cols-2 @6xl:grid-cols-3">
          {/* ── Score by week: stages x the last 8 weeks */}
          <section className={TILE} aria-labelledby="ctem-stats-heat-title" data-testid="ctem-heat-map">
            <h3 id="ctem-stats-heat-title" className="content-compact font-medium">{t_i18n("Score by week")}</h3>
            {heat ? (
              <table className="w-full table-fixed content-caption">
                <caption className="sr-only">{t_i18n("Stage scores by week, last 8 weeks")}</caption>
                <colgroup>
                  <col className="w-24" />
                  {heat.weeks.map((w) => <col key={w} />)}
                </colgroup>
                <thead>
                  <tr className="text-default-secondary">
                    <th scope="col" className="sr-only">{t_i18n("Stage")}</th>
                    {heat.weeks.map((w, i) => (
                      <th key={w} scope="col" className="pb-1 text-center font-normal whitespace-nowrap" data-testid="ctem-heat-week">
                        {i === 0 || i === heat.weeks.length - 1 ? (
                          weekLabel(w)
                        ) : (
                          <span className="sr-only">{weekLabel(w)}</span>
                        )}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {heat.rows.map((row) => (
                    <tr key={row.key} data-stage={row.key}>
                      <th scope="row" className="py-0.5 pr-2 text-left font-normal">
                        <button
                          type="button"
                          onClick={() => navigate(`/ctem/${row.key}`)}
                          aria-label={t_i18n("Open the {stage} stage", { stage: name(row.key) })}
                          className={cn("block max-w-full truncate rounded-sm text-left content-compact hover:underline", TIP_WRAP_FOCUS_RING)}
                        >
                          {name(row.key)}
                        </button>
                      </th>
                      {row.cells.map((cell, i) => {
                        const week = weekLabel(heat.weeks[i]);
                        const style = cell ? scoreBandStyle(cell.band) : null;
                        const sentence = cell && style
                          ? t_i18n("{stage}, week of {date}: {score}, {band}", {
                              stage: name(row.key), date: week, score: nf.count(cell.score), band: t_i18n(style.label),
                            })
                          : t_i18n("{stage}, week of {date}: no score", { stage: name(row.key), date: week });
                        return (
                          <td key={heat.weeks[i]} className="px-0.5 py-0.5">
                            <Tip content={sentence}>
                              <span
                                tabIndex={0}
                                data-band={cell?.band ?? "none"}
                                className={cn(
                                  "block h-5 w-full rounded-sm",
                                  !style && "border border-dashed border-current text-default-secondary",
                                  TIP_WRAP_FOCUS_RING,
                                )}
                                style={style ? { backgroundColor: style.fill } : undefined}
                              >
                                <span className="sr-only">{sentence}</span>
                              </span>
                            </Tip>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="content-compact text-default-secondary">{t_i18n("Scores by week after the first recorded day")}</p>
            )}
            <ul className="flex flex-wrap gap-x-3 gap-y-1 content-caption text-default-secondary" data-testid="ctem-heat-legend">
              {SCORE_BANDS.map((band) => {
                const style = scoreBandStyle(band);
                return (
                  <li key={band} className="inline-flex items-center gap-1.5">
                    <span aria-hidden className="h-3 w-3 rounded-sm" style={{ backgroundColor: style.fill }} />
                    {t_i18n(style.label)}
                  </li>
                );
              })}
              <li className="inline-flex items-center gap-1.5">
                <span aria-hidden className="h-3 w-3 rounded-sm border border-dashed border-current" />
                {t_i18n("No data")}
              </li>
            </ul>
          </section>

          {/* ── Change over the period: one signed bar per stage */}
          <section className={TILE} aria-labelledby="ctem-stats-deltas-title" data-testid="ctem-exposure-deltas">
            <h3 id="ctem-stats-deltas-title" className="content-compact font-medium">{t_i18n("Change over the period")}</h3>
            {stats ? (
              <>
                <ul className="flex flex-col gap-1.5" data-testid="ctem-stage-deltas">
                  {deltas.map((d) => {
                    const share = d.change === null ? 0 : (Math.abs(d.change) / maxAbs) * 50;
                    const up = (d.change ?? 0) > 0;
                    const label = d.change === null
                      ? t_i18n("{stage}: not measured over the period", { stage: name(d.key) })
                      : t_i18n("{stage}: {change} over the period", { stage: name(d.key), change: signed(d.change) });
                    return (
                      <li key={d.key} className="grid grid-cols-[6rem_minmax(0,1fr)] items-center gap-2 content-caption" data-testid={`ctem-delta-${d.key}`}>
                        <span className="truncate text-default-secondary">{name(d.key)}</span>
                        <span className="relative block h-5" role="img" aria-label={label}>
                          <span aria-hidden className="absolute inset-y-0 left-1/2 w-px bg-[var(--border-elevation-subtle)]" />
                          {d.change !== null && d.change !== 0 && (
                            <span
                              aria-hidden
                              className="absolute top-1 bottom-1 rounded-sm"
                              style={{
                                backgroundColor: up ? UP : DOWN,
                                width: `${share}%`,
                                ...(up ? { left: "50%" } : { right: "50%" }),
                              }}
                            />
                          )}
                          <span
                            aria-hidden
                            className="absolute inset-y-0 flex items-center font-semibold tabular-nums text-default-primary"
                            style={
                              d.change === null
                                ? { left: "calc(50% + 4px)" }
                                : up
                                  ? { left: `calc(${50 + share}% + 4px)` }
                                  : d.change < 0
                                    ? { right: `calc(${50 + share}% + 4px)` }
                                    : { left: "calc(50% + 4px)" }
                            }
                            data-testid="ctem-delta-value"
                          >
                            {d.change === null ? "--" : signed(d.change)}
                          </span>
                        </span>
                      </li>
                    );
                  })}
                </ul>
                <span className="grid grid-cols-[6rem_minmax(0,1fr)] gap-2 content-caption text-default-secondary" aria-hidden>
                  <span />
                  <span className="text-center">0</span>
                </span>
              </>
            ) : (
              <p className="content-compact text-default-secondary">{t_i18n("Stats not read yet")}</p>
            )}
          </section>

          {/* ── Findings: the vertical's three headline figures, each with its trend */}
          <section className={TILE} data-testid="ctem-findings">
            <h3 className="content-compact font-medium">{t_i18n("Findings")}</h3>
            <ul className="flex flex-col gap-2">
              {findings.map((kpi) => {
                const connect = kpi.connect ?? null;
                // A count of 1,000 or more is compact (1.3K), its exact value on hover (rule 12).
                const value =
                  kpi.value === null ? null : kpi.unit === "%" ? percent(kpi.value) : formatCompactNumber(kpi.value);
                const exact = kpi.value === null ? "" : kpi.unit === "%" ? percent(kpi.value) : nf.exactCount(kpi.value);
                const label = t_i18n(kpi.label);
                return (
                  <li key={kpi.label} data-testid="ctem-finding" className="flex min-w-0 flex-col gap-1">
                    <span
                      className={cn(
                        "flex min-h-9 gap-2 content-compact",
                        // A missing module puts "Connect <module>" on its own line under the label: nothing is cut.
                        connect ? "flex-col items-start" : "items-center justify-between",
                      )}
                    >
                      <span className="min-w-0 break-words text-default-secondary">{label}</span>
                      {connect ? (
                        <Button type="button" variant="outline" size="sm" className="h-9 shrink-0 gap-1.5" onClick={() => onConnect(connect)}>
                          <Plus className="h-3.5 w-3.5" aria-hidden />
                          {t_i18n("Connect {name}", { name: moduleName(connect) })}
                        </Button>
                      ) : value === null ? (
                        <span className="text-default-secondary">{t_i18n("Not measured yet")}</span>
                      ) : kpi.stage ? (
                        <button
                          type="button"
                          onClick={() => navigate(`/ctem/${kpi.stage}`)}
                          aria-label={t_i18n("{label} {value}: open the {stage} stage", {
                            label, value: exact, stage: stageName(kpi.stage, kpi.stage, t_i18n),
                          })}
                          className={cn("inline-flex shrink-0 items-center gap-1 rounded-sm content-base font-semibold hover:underline", TIP_WRAP_FOCUS_RING)}
                        >
                          <CompactFigure exact={exact}>{value}</CompactFigure>
                          <ChevronRight className="h-4 w-4 text-default-secondary" aria-hidden />
                        </button>
                      ) : (
                        <CompactFigure exact={exact} className="shrink-0 content-base font-semibold" testId="ctem-finding-value">
                          {value}
                        </CompactFigure>
                      )}
                    </span>
                    {!connect && value !== null && <FindingSparkline values={kpi.series ?? []} label={label} />}
                  </li>
                );
              })}
            </ul>
          </section>
        </div>
      </CardContent>
    </Card>
  );
}
