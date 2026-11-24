// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
/**
 * The exposure story strip of the CTEM command center (the `lifecycle_band`
 * widget; dev-docs/ctem-stage-viz.md section 4, row `lifecycle_band`, slice
 * S2a of #11547). It replaces the row of text chips of #11464 with one flow
 * across the five CTEM stages and Governance & Risk.
 *
 * Each stage column, on one shared five-row subgrid so every row keeps one
 * baseline across the columns: the stage name; a 56 px concentric ring (outer
 * arc the measured, quantitative score; inner arc the score in your context,
 * the qualitative one; each in its band colour, never a colour per stage) with
 * the measured score as the big number; the band name and the 30-day score
 * delta; the stage's own metric as a small bullet bar with its delta; a 30-day
 * score sparkline. A click opens `/ctem/:stage`, which holds every figure.
 *
 * Honest states (section 2): a stage with no source draws dashed ring tracks,
 * "--" and one link naming the product to connect; a stage with a measured
 * score but no organization context keeps its outer arc and asks for the
 * context instead of drawing an inner arc. Governance & Risk reads the
 * posture: its two scores, its delta and its sparkline; there is no posture
 * target setting yet, so its bullet bar stays dashed and says so.
 *
 * It copies no stage hero (no attack-domain ring, matrix, funnel or area).
 * Reading the strip starts no run, calls no model and uses no quota: the
 * assessments are the stored ones, read through the cache Act now shares.
 * While the posture is rebuilt or a stage's assessment is regenerated, that
 * stage shows a spinner beside its name (#12183).
 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Spinner } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import { useCtemStageAssessments } from "@/hooks/api/useCtemMetricsApi";
import type { CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import {
  CTEM_BAND_CONFIG, CTEM_PRODUCT_INFO, CTEM_STAGE_CONFIG, CTEM_STAGE_KEYS, type CtemStageKey,
} from "@/lib/constants";
import { cn, safeExternalUrl } from "@/lib/utils";
import type { CtemBand, CtemStage, CtemVertical } from "@/types";
import { MiniTrend } from "./ctem-viz";
import { CtemScoreTip } from "./CtemScoreTip";
import { LifecycleHeader } from "./LifecycleHeader";
import { SignalCounter } from "./loop/SignalCounter";
import { assessmentBusy } from "./landing-refresh";
import { casesListHref } from "./cases-due";
import { historyWindow, seriesChange } from "./trend-window";

/** Days of the column sparkline and of every delta of the strip. */
const LIFECYCLE_TREND_DAYS = 30;

/** Where "Add organization context" leads: the same target as the other CTEM cards. */
export const ORGANIZATION_CONTEXT_SETUP = "/settings/organization?setup=1";

interface LifecycleHistoryPoint {
  date: string;
  posture_score?: number | null;
  stage_metrics: Record<string, Record<string, number | null> | undefined>;
}

/** The connect target of a stage with no source: its first missing module. */
type ConnectProduct = "opencti" | "openaev" | "opencrq";

export interface LifecycleBandProps {
  /** The live posture's stages; empty while none is built. */
  stages: readonly CtemStage[];
  /** The posture is built from a connected platform (Export CSV on). */
  live: boolean;
  covered: number;
  total: number;
  /** The posture history, oldest first. */
  history: readonly LifecycleHistoryPoint[];
  vertical: CtemVertical;
  aiLockReason: AiLockReason | null;
  /** OpenCRQ's active registration, when one exists. */
  opencrq: { url?: string | null } | null;
  onExport: () => void;
  onConnect: (product: ConnectProduct) => void;
  /** The height its content needs, so the page sizes the card to its columns. */
  onContentHeight?: (px: number) => void;
  /** No organization context saved: the qualitative slots ask for it (#11464). */
  contextMissing?: boolean;
  /** The overall posture, for the Governance & Risk column. */
  posture?: {
    score: number | null;
    band: CtemBand;
    qualitativeScore?: number | null;
    qualitativeBand?: CtemBand;
    /** The posture target of the organization profile; null when none is set. */
    target?: number | null;
  } | null;
  /** Today, for tests. */
  now?: Date;
  /** The posture behind every column is being rebuilt (#12183). */
  refreshing?: boolean;
  /**
   * The CTEM signals by state (CTEM-01-UI): a compact counter under the ring
   * key, each count opening the signal list of the loop trace. Absent outside
   * XTM One mode or before the first read.
   */
  signals?: {
    counts: Partial<Record<CtemSignalState, number>>;
    onOpen: (state: CtemSignalState | null) => void;
  } | null;
  /**
   * The open cases of each stage and those waiting for a decision (`GET /ctem-cases/overview`, #1621),
   * and the accepted risks for the Governance & Risk column: one footer line per column. Absent
   * before the first read or outside XTM One mode: no footer line.
   */
  cases?: {
    phases: Partial<Record<CtemStageKey, { open: number; waiting: number }>>;
    acceptedRisk: number;
  } | null;
}

/** The footer line of a column: its open cases, linking to the Cases list on them (#1621). */
function CasesFooter({ href, text, muted, testId }: { href: string; text: string; muted: boolean; testId: string }) {
  return (
    <Link
      to={href}
      className={cn(
        "pointer-events-auto relative z-10 w-fit truncate content-caption hover:underline",
        muted ? "text-default-secondary" : "font-medium text-accent",
        BUTTON_ROLE_FOCUS_RING,
      )}
      data-testid={testId}
    >
      {text}
    </Link>
  );
}

/** The score series of one stage over the last `days` days (`null` on a day with none). */
export function stageTrend(history: readonly LifecycleHistoryPoint[], stage: string, days: number, today: Date): (number | null)[] {
  return historyWindow(history, days, today).map((h) => h.stage_metrics?.[stage]?.score ?? null);
}

/**
 * The metric each stage column draws as its bullet bar, read from the stage's
 * snapshot metrics (the same keys the stage score is computed from,
 * backend/app/services/ctem_scoring.py). The first key with a value wins.
 * Prioritization draws the critical share of the open findings, the
 * complement of the non-critical share its score reads.
 */
const STAGE_METRIC: Record<CtemStageKey, { label: string; keys: readonly string[]; complement?: boolean }> = {
  scoping: { label: "Breadth", keys: ["scope_breadth_pct", "scope_completeness_pct"] },
  discovery: { label: "Coverage", keys: ["threat_informed_coverage_pct", "detection_coverage_pct", "monitoring_depth_pct", "discovery_pct"] },
  prioritization: { label: "Critical", keys: ["noncritical_share_pct"], complement: true },
  validation: { label: "Prevented", keys: ["prevention_success_pct", "corroboration_pct"] },
  remediation: { label: "Closure", keys: ["case_closure_pct"] },
};

/** The stage metric on one snapshot, 0-100, or null when none of its keys is recorded. */
export function stageMetricValue(stage: CtemStageKey, metrics: Record<string, number | null> | undefined): number | null {
  const spec = STAGE_METRIC[stage];
  for (const key of spec.keys) {
    const v = metrics?.[key];
    if (typeof v === "number" && Number.isFinite(v)) {
      const pct = Math.max(0, Math.min(100, v));
      return spec.complement ? 100 - pct : pct;
    }
  }
  return null;
}

function bandOf(band: CtemBand | undefined | null) {
  return CTEM_BAND_CONFIG[band ?? "unknown"] ?? CTEM_BAND_CONFIG.unknown;
}

function connectTarget(stage: CtemStage | undefined): ConnectProduct | null {
  const m = stage?.missing_products?.[0];
  if (!m) return null;
  return (m === "openbas" ? "openaev" : m) as ConnectProduct;
}

/** A signed whole delta ("+4", "-2", "+0"): a zero keeps its sign so it reads as a change. */
function signed(n: number) {
  return n >= 0 ? `+${n}` : `${n}`;
}

const RING = 56;
const OUTER_R = 24;
const INNER_R = 16;
const STROKE = 5;

/** One arc of the concentric ring, as a stroke-dasharray over a full circle. */
function RingArc({ r, value, color, dashed }: { r: number; value: number | null; color: string; dashed?: boolean }) {
  const c = 2 * Math.PI * r;
  return (
    <>
      <circle
        cx={RING / 2}
        cy={RING / 2}
        r={r}
        fill="none"
        stroke="var(--border-elevation-subtle-soft)"
        strokeWidth={STROKE}
        strokeDasharray={dashed ? "3 3" : undefined}
      />
      {value != null && (
        <circle
          cx={RING / 2}
          cy={RING / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={`${(Math.max(0, Math.min(100, value)) / 100) * c} ${c}`}
          transform={`rotate(-90 ${RING / 2} ${RING / 2})`}
          className="motion-safe:transition-[stroke-dasharray] motion-safe:duration-700"
        />
      )}
    </>
  );
}

/** The concentric ring of a column: outer the measured score, inner the score in your context. */
function StoryRing({
  measured,
  measuredBand,
  qualitative,
  qualitativeBand,
  label,
}: {
  measured: number | null;
  measuredBand: CtemBand;
  qualitative: number | null;
  qualitativeBand: CtemBand;
  label: string;
}) {
  return (
    <span className="relative inline-flex h-14 w-14 items-center justify-center" role="img" aria-label={label} title={label} data-testid="ctem-story-ring">
      <svg width={RING} height={RING} viewBox={`0 0 ${RING} ${RING}`} aria-hidden className="absolute inset-0">
        <RingArc r={OUTER_R} value={measured} color={bandOf(measuredBand).ring} dashed={measured == null} />
        <RingArc r={INNER_R} value={qualitative} color={bandOf(qualitativeBand).ring} dashed={qualitative == null} />
      </svg>
      <span className={cn("relative content-base font-bold leading-none tabular-nums", measured != null ? bandOf(measuredBand).color : "text-default-secondary")} data-testid="ctem-story-score">
        {measured ?? "--"}
      </span>
    </span>
  );
}

/**
 * The two readings beside the ring, each with its label (#11689): the ring
 * alone never said which number was which. Measured in its band colour, then
 * In your context in its own band colour, "--" when the context gives none.
 */
function ReadingFigures({
  measured,
  measuredBand,
  qualitative,
  qualitativeBand,
}: {
  measured: number | null;
  measuredBand: CtemBand;
  qualitative: number | null;
  qualitativeBand: CtemBand;
}) {
  const { t_i18n } = useFormatter();
  if (measured == null) return null;
  const row = (testId: string, icon: "database" | "building-2", label: string, value: number | null, band: CtemBand) => (
    <span className="flex min-w-0 items-center gap-1 content-caption" data-testid={testId}>
      <Icon name={icon} size={16} className="shrink-0 text-default-secondary" aria-hidden />
      <span className="min-w-0 truncate text-default-secondary">{label}</span>
      <span className={cn("ml-auto shrink-0 font-semibold tabular-nums", value != null ? bandOf(band).color : "text-default-secondary")}>
        {value ?? "--"}
      </span>
    </span>
  );
  return (
    <span className="flex min-w-0 flex-1 flex-col gap-1" data-testid="ctem-story-figures">
      {row("ctem-story-measured", "database", t_i18n("Measured"), measured, measuredBand)}
      {row("ctem-story-context", "building-2", t_i18n("In your context"), qualitative, qualitativeBand)}
    </span>
  );
}

/** A small 0-100 bullet bar with its value and its 30-day delta, written "+4 / 30 d". */
function BulletBar({ label, value, delta, unit = "%" }: { label: string; value: number | null; delta: string | null; unit?: string }) {
  return (
    <span className="flex min-w-0 flex-col gap-1" data-testid="ctem-story-metric">
      <span className="flex min-w-0 items-baseline gap-1 content-caption text-default-secondary">
        <span className="min-w-0 truncate">{label}</span>
        {value != null && <span className="shrink-0 font-semibold tabular-nums text-default-primary">{Math.round(value)}{unit}</span>}
        {delta != null && <span className="shrink-0 tabular-nums">{delta}</span>}
      </span>
      <span
        className={cn(
          "block h-1.5 w-full overflow-hidden rounded-full",
          value == null ? "border border-dashed border-elevation-subtle-soft" : "bg-elevation-hover",
        )}
        aria-hidden
      >
        {value != null && <span className="block h-full rounded-full bg-accent" style={{ width: `${value}%` }} />}
      </span>
    </span>
  );
}

/**
 * The band and delta row reserves two caption lines in every column, so a
 * column that adds a link (organization context, OpenCRQ) never makes its tile
 * taller than its neighbours (one tile height per breakpoint).
 */
const DELTA_ROW = "pointer-events-none flex min-h-[2lh] min-w-0 flex-col content-caption";

/** Six subgrid rows: name, ring, band and delta, metric, sparkline, open cases (#1621). */
const COLUMN = "relative row-span-6 grid grid-rows-subgrid min-w-0 gap-y-2 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3";

function Column({ children, stage, onOpen, openLabel, summary, tip }: {
  children: ReactNode; stage: string; onOpen?: () => void; openLabel?: string;
  /** Every figure of the column in words: the button's accessible description. */
  summary: string;
  /** The structured hover card (CtemScoreTip); the summary when none is given. */
  tip?: ReactNode;
}) {
  const descId = useId();
  return (
    <li className={COLUMN} data-stage={stage}>
      {/* sr-only is absolutely positioned: it takes no cell of the subgrid. */}
      {onOpen && <span id={descId} className="sr-only" data-testid="ctem-story-summary">{summary}</span>}
      {onOpen && (
        // The whole column opens its page; the button sits under the content so
        // an inner link (connect, context) keeps its own target. Its tooltip and
        // its accessible description carry every figure with its definition, on
        // hover and on keyboard focus alike.
        <Tip content={tip ?? summary}>
          <button
            type="button"
            onClick={onOpen}
            aria-label={openLabel}
            aria-describedby={descId}
            className={cn("absolute inset-0 rounded-sm", BUTTON_ROLE_FOCUS_RING)}
          />
        </Tip>
      )}
      {children}
    </li>
  );
}

/** An inline action of the delta row: above the column's own button. */
function RowAction({ children, onClick, href }: { children: ReactNode; onClick?: () => void; href?: string }) {
  const cls = "pointer-events-auto relative z-10 w-fit content-caption font-medium text-accent hover:underline text-left";
  return href ? (
    <a href={href} className={cls}>
      {children}
    </a>
  ) : (
    <button type="button" onClick={onClick} className={cls}>
      {children}
    </button>
  );
}

export function LifecycleBandCard({
  stages, live, covered, total, history, vertical, aiLockReason, opencrq,
  onExport, onConnect, onContentHeight, posture = null, now, refreshing = false, signals = null, cases = null,
}: LifecycleBandProps) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  // Read once at mount: the render body stays pure.
  const [mountedAt] = useState(() => new Date());
  const today = now ?? mountedAt;
  const byKey = new Map(stages.map((s) => [s.key, s]));
  const coveredKeys = CTEM_STAGE_KEYS.filter((k) => byKey.get(k)?.covered);
  // The stored assessments only (no generation): the same cache entries as Act now.
  const assessments = useCtemStageAssessments(coveredKeys, vertical, aiLockReason === null);
  const pending = new Set(
    coveredKeys.filter((_, i) => {
      const a = assessments[i]?.data;
      return a?.status === "ready" && Boolean(a.assessment?.recommended_action?.trim());
    }),
  );
  const regenerating = new Set(coveredKeys.filter((_, i) => assessmentBusy(assessments[i]?.data)));

  // The content is measured, so the page sizes the card to its columns (no empty band).
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

  const crqUrl = opencrq ? safeExternalUrl(opencrq.url ?? undefined) : undefined;
  const window30 = historyWindow(history, LIFECYCLE_TREND_DAYS, today);
  const deltaText = (n: number | null) => (n == null ? null : t_i18n("{delta} / 30 d", { delta: signed(n) }));
  const changeHelp = t_i18n("Change over the last 30 days");
  const measuredHelp = t_i18n("From the connected modules' metrics alone, whatever the organization.");
  const contextHelp = t_i18n("The same metrics read against your organization context: crown jewels, tech stack, sector, PIRs and risk appetite. The more of it is filled, the more of the score holds.");
  const contextLink = <RowAction href={ORGANIZATION_CONTEXT_SETUP}>{t_i18n("Add organization context")}</RowAction>;

  const postureTrend = window30.map((h) => h.posture_score ?? null);
  const postureDelta = seriesChange(postureTrend).change;
  const postureQual = posture?.qualitativeScore ?? null;
  const postureTarget = posture?.target ?? null;
  const postureName = t_i18n("Governance & Risk");
  const postureRing = posture?.score == null
    ? t_i18n("{stage}: not configured", { stage: postureName })
    : postureQual == null
      ? t_i18n("{stage}: measured {measured}, no organization context, add organization context", { stage: postureName, measured: posture.score })
      : t_i18n("{stage}: measured {measured}, in your context {qualitative}", { stage: postureName, measured: posture.score, qualitative: postureQual });

  return (
    <Card className="@container flex flex-col" data-testid="ctem-lifecycle-band">
      <CardContent ref={contentRef} className="flex flex-col gap-3 p-4">
        <LifecycleHeader live={live} covered={covered} total={total} onExport={onExport} />
        {/* Signals first: they start the loop (#1189). Then the ring key, once for the strip, short labels on
            one row; the definitions are in the tooltips. Wide card: the key at the right end of the row; narrower:
            the signals on a line of their own, the key on the next one, both on the card's left edge. */}
        <div className="flex flex-wrap items-center gap-4 content-caption text-default-secondary" data-testid="ctem-story-key">
          {signals && (
            <span className="flex min-w-0 basis-full items-center gap-1 @5xl:basis-auto" data-testid="ctem-story-key-signals">
              {/* The signal sources are a tab of the Signals page the counter opens (#1263): no icon of their own here. */}
              <SignalCounter counts={signals.counts} onOpen={signals.onOpen} />
            </span>
          )}
          <span className="flex flex-wrap items-center gap-4 @5xl:ml-auto" data-testid="ctem-story-key-rings">
          <Tip content={measuredHelp}>
            <span className="inline-flex items-center gap-1.5" tabIndex={0}>
              <Icon name="database" size={16} /> {t_i18n("Outer ring: Measured (quantitative)")}
            </span>
          </Tip>
          <Tip content={contextHelp}>
            <span className="inline-flex items-center gap-1.5" tabIndex={0}>
              <Icon name="building-2" size={16} /> {t_i18n("Inner ring: In your context (qualitative)")}
            </span>
          </Tip>
          </span>
        </div>
        <div className="relative">
          {/* The track the stages sit on: one line through the rings' row. */}
          <div className="pointer-events-none absolute inset-x-3 top-[4.25rem] hidden h-px bg-[var(--border-elevation-subtle-soft)] @5xl:block" aria-hidden data-testid="ctem-story-track" />
          <ul
            className="relative grid grid-cols-2 gap-x-3 gap-y-3 @2xl:grid-cols-3 @5xl:grid-cols-6"
            style={{ gridTemplateRows: "repeat(6, auto)" }}
            data-testid="ctem-lifecycle-chips"
          >
            {CTEM_STAGE_KEYS.map((key, index) => {
              const stage = byKey.get(key);
              const stageKey = key as CtemStageKey;
              const name = t_i18n(CTEM_STAGE_CONFIG[stageKey].label);
              const isCovered = Boolean(stage?.covered);
              const measured = isCovered ? (stage?.score ?? null) : null;
              const qualitative = measured != null ? (stage?.qualitative_score ?? null) : null;
              const trend = window30.map((h) => h.stage_metrics?.[key]?.score ?? null);
              const hasTrend = trend.filter((v) => v !== null).length >= 2;
              const delta = seriesChange(trend).change;
              const metricSeries = window30.map((h) => stageMetricValue(stageKey, h.stage_metrics?.[key] ?? undefined));
              const recordedMetric = metricSeries.filter((v): v is number => v != null);
              const metric = isCovered && recordedMetric.length > 0 ? (recordedMetric[recordedMetric.length - 1] as number) : null;
              const metricDelta = isCovered ? seriesChange(metricSeries).change : null;
              const connect = isCovered ? null : connectTarget(stage);
              const noContext = measured != null && qualitative == null;
              const band = bandOf(stage?.band);
              const ringLabel = measured == null
                ? isCovered
                  ? t_i18n("{stage}: connected, not enough data to score", { stage: name })
                  : t_i18n("{stage}: not configured", { stage: name })
                : noContext
                  ? t_i18n("{stage}: measured {measured}, no organization context, add organization context", { stage: name, measured })
                  : t_i18n("{stage}: measured {measured}, in your context {qualitative}", { stage: name, measured, qualitative: qualitative ?? "--" });
              const metricLabel = t_i18n(STAGE_METRIC[stageKey].label);
              // The metric is the score's own input (Discovery's coverage is its
              // score): its change is the score's, said once, never twice.
              const sameAsScore = metric != null && measured != null && Math.round(metric) === measured && metricDelta === delta;
              const summary = [
                ringLabel,
                measured != null ? `${t_i18n(band.label)}${delta != null ? `, ${changeHelp}: ${signed(delta)}` : ""}` : null,
                metric != null
                  ? `${metricLabel} ${Math.round(metric)}%${!sameAsScore && metricDelta != null ? `, ${changeHelp}: ${signed(metricDelta)}` : ""}`
                  : null,
              ].filter(Boolean).join(". ");
              const tip = (
                <CtemScoreTip
                  title={name}
                  measured={{ score: measured, band: stage?.band }}
                  context={{ score: qualitative, band: stage?.qualitative_band }}
                  noContext={noContext}
                  delta={delta}
                  metric={metric != null ? { label: metricLabel, value: metric, delta: metricDelta, sameAsScore } : null}
                  emptyText={ringLabel}
                />
              );
              return (
                <Column key={key} stage={key} summary={summary} tip={tip} onOpen={() => navigate(`/ctem/${key}`)} openLabel={t_i18n("Open the {stage} stage", { stage: name })}>
                  <span className="pointer-events-none flex min-w-0 items-center gap-1.5">
                    <span className="min-w-0 truncate content-compact font-medium">{name}</span>
                    {(refreshing || regenerating.has(key)) && (
                      <span className="inline-flex shrink-0 text-accent" data-testid="ctem-lifecycle-refreshing">
                        <Spinner size="sm" tone="inherit" label={t_i18n("Refreshing...")} />
                      </span>
                    )}
                    {pending.has(key) && (
                      <span
                        role="img"
                        aria-label={t_i18n("A recommended action is pending")}
                        data-testid="ctem-lifecycle-pending"
                        className="h-2 w-2 shrink-0 rounded-full bg-accent"
                      />
                    )}
                    {index < CTEM_STAGE_KEYS.length - 1 && (
                      <Icon name="chevron-right" size={16} className="ml-auto hidden shrink-0 text-default-secondary @5xl:inline" />
                    )}
                  </span>
                  <span className="pointer-events-none relative flex min-w-0 items-center gap-3">
                    <StoryRing
                      measured={measured}
                      measuredBand={stage?.band ?? "unknown"}
                      qualitative={qualitative}
                      qualitativeBand={stage?.qualitative_band ?? "unknown"}
                      label={ringLabel}
                    />
                    <ReadingFigures
                      measured={measured}
                      measuredBand={stage?.band ?? "unknown"}
                      qualitative={qualitative}
                      qualitativeBand={stage?.qualitative_band ?? "unknown"}
                    />
                  </span>
                  <span className={DELTA_ROW} data-testid="ctem-story-delta">
                    {measured == null ? (
                      connect ? (
                        <RowAction onClick={() => onConnect(connect)}>
                          {t_i18n("Connect {name}", { name: CTEM_PRODUCT_INFO[connect]?.name ?? connect })}
                        </RowAction>
                      ) : (
                        <span className="content-caption text-default-secondary">
                          {isCovered ? t_i18n("Not enough data to score") : t_i18n("No data")}
                        </span>
                      )
                    ) : (
                      <>
                        <span className="pointer-events-none flex items-baseline gap-1.5 content-caption">
                          <span className={band.color}>{t_i18n(band.label)}</span>
                          {delta != null && (
                            <span className="tabular-nums text-default-secondary">{deltaText(delta)}</span>
                          )}
                        </span>
                        {noContext && contextLink}
                      </>
                    )}
                  </span>
                  <span className="pointer-events-none">
                    <BulletBar label={metricLabel} value={metric} delta={deltaText(metricDelta)} />
                  </span>
                  <span className="pointer-events-none block h-6 w-full" data-testid="ctem-lifecycle-trend-line">
                    {isCovered && hasTrend ? <MiniTrend values={trend} color={band.ring} /> : null}
                  </span>
                  <span className="flex min-h-[1lh] min-w-0">
                    {cases && (() => {
                      const open = cases.phases[stageKey]?.open ?? 0;
                      const waiting = cases.phases[stageKey]?.waiting ?? 0;
                      const text = open === 0
                        ? t_i18n("No open case")
                        : waiting > 0
                          ? t_i18n("{count, plural, one {# open case} other {# open cases}}, {waiting} waiting", { count: open, waiting })
                          : t_i18n("{count, plural, one {# open case} other {# open cases}}", { count: open });
                      return <CasesFooter href={casesListHref({ phase: key })} text={text} muted={open === 0} testId={`ctem-lifecycle-cases-${key}`} />;
                    })()}
                  </span>
                </Column>
              );
            })}
            <Column
              stage="governance"
              summary={[
                postureRing,
                posture?.score != null ? `${t_i18n(bandOf(posture.band).label)}${postureDelta != null ? `, ${changeHelp}: ${signed(postureDelta)}` : ""}` : null,
                postureTarget != null ? t_i18n("Posture target {value}", { value: postureTarget }) : t_i18n("No posture target yet"),
              ].filter(Boolean).join(". ")}
              tip={
                <CtemScoreTip
                  title={postureName}
                  measured={{ score: posture?.score ?? null, band: posture?.band }}
                  context={{ score: posture?.score != null ? postureQual : null, band: posture?.qualitativeBand }}
                  noContext={posture?.score != null && postureQual == null}
                  delta={postureDelta}
                  emptyText={postureRing}
                  footer={
                    <p className="content-caption text-default-secondary">
                      {postureTarget != null ? t_i18n("Posture target {value}", { value: postureTarget }) : t_i18n("No posture target yet")}
                    </p>
                  }
                />
              }
              onOpen={() => navigate("/ctem/governance")}
              openLabel={t_i18n("Open Governance & Risk")}
            >
              <span className="pointer-events-none flex min-w-0 items-center gap-1.5">
                <img src={CTEM_PRODUCT_INFO.opencrq.logo} alt="" className="h-3.5 w-3.5 shrink-0" />
                <span className="min-w-0 truncate content-compact font-medium">{postureName}</span>
              </span>
              <span className="pointer-events-none flex min-w-0 items-center gap-3">
                <StoryRing
                  measured={posture?.score ?? null}
                  measuredBand={posture?.band ?? "unknown"}
                  qualitative={posture?.score != null ? postureQual : null}
                  qualitativeBand={posture?.qualitativeBand ?? "unknown"}
                  label={postureRing}
                />
                <ReadingFigures
                  measured={posture?.score ?? null}
                  measuredBand={posture?.band ?? "unknown"}
                  qualitative={posture?.score != null ? postureQual : null}
                  qualitativeBand={posture?.qualitativeBand ?? "unknown"}
                />
              </span>
              <span className={DELTA_ROW} data-testid="ctem-story-delta">
                {posture?.score != null && (
                  <span className="flex items-baseline gap-1.5 content-caption">
                    <span className={bandOf(posture.band).color}>{t_i18n(bandOf(posture.band).label)}</span>
                    {postureDelta != null && <span className="tabular-nums text-default-secondary">{deltaText(postureDelta)}</span>}
                  </span>
                )}
                {posture?.score != null && postureQual == null ? contextLink : crqUrl && (
                  <a href={crqUrl} target="_blank" rel="noopener noreferrer" className="pointer-events-auto relative z-10 inline-flex h-[1lh] w-fit items-center gap-1 overflow-visible content-caption font-medium text-accent hover:underline">
                    <Icon name="external-link" size={16} className="shrink-0" /> {t_i18n("Open in OpenCRQ")}
                  </a>
                )}
              </span>
              <span className="pointer-events-none">
                {postureTarget != null ? (
                  <BulletBar label={t_i18n("Posture target")} value={postureTarget} delta={null} unit="" />
                ) : (
                  <BulletBar label={t_i18n("No posture target yet")} value={null} delta={null} />
                )}
              </span>
              <span className="pointer-events-none block h-6 w-full" data-testid="ctem-lifecycle-trend-line">
                {postureTrend.filter((v) => v !== null).length >= 2 ? <MiniTrend values={postureTrend} color={bandOf(posture?.band).ring} /> : null}
              </span>
              <span className="flex min-h-[1lh] min-w-0">
                {/* Cases carry no Governance phase: the column counts the risks accepted on them. */}
                {cases && (
                  <CasesFooter
                    href={casesListHref({ state: "accepted_risk" })}
                    text={cases.acceptedRisk === 0
                      ? t_i18n("No accepted risk")
                      : t_i18n("{count, plural, one {# accepted risk} other {# accepted risks}}", { count: cases.acceptedRisk })}
                    muted={cases.acceptedRisk === 0}
                    testId="ctem-lifecycle-cases-governance"
                  />
                )}
              </span>
            </Column>
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}
