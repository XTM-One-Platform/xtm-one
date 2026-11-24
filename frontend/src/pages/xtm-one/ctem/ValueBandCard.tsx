// SPDX-License-Identifier: Apache-2.0

/**
 * The value band of the CTEM command center (C2 slice V1, design
 * dev-docs/ctem-command-center-v2.md section 4.1): what the page is worth in
 * five seconds, fleets off first.
 *
 * - Headline: the ready briefing's headline with its author and age, else a
 *   deterministic sentence from the posture (score, stages covered,
 *   known-exploited exposures). Never a model call: the briefing is a run that
 *   already happened.
 * - Posture: the gauge (opens "How the Exposure Posture is computed"), the
 *   period trend and its delta, last synced and Force sync.
 * - Risk: known-exploited exposures on the connected sources, or the way to
 *   connect OpenCTI. Never a sample figure.
 * - Value: hours saved and net value over 30 days from the Value page's own
 *   report (`useValueReport`, one cache entry), one named figure per row,
 *   "Estimated" and "Default rate" read from the labels the API sends
 *   (`shownLabel`). With no record the tile says so and counts the agentic
 *   runs of the last 30 days, never 0 h / $0. AI locked: the lock reason and
 *   its one way forward.
 * - Actions: the agentic runs of the last 30 days (the same count as the
 *   "Agentic executions" card) against what waits for the reader; with AI
 *   locked, why nothing runs instead of a 0.
 *
 * The period delta is `periodChange`, the one the hero reads too.
 *
 * Reading the band starts no run, calls no model and uses no quota.
 */
import { VALUE_METHODOLOGY_PATH, valuePath, waitingForMePath } from "@/lib/task-routes";
import { Link, useNavigate } from "react-router-dom";
import { ArrowDownRight, ArrowUpRight, RefreshCw, ShieldAlert, TrendingUp, Zap } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { AiLockedFooter } from "@/components/ui/enterprise-upsell";
import { CompactFigure } from "@/components/ui/compact-figure";
import { RoiFigure } from "@/components/roi/RoiFigure";
import { useValueReport } from "@/hooks/api/useRoiReportApi";
import { useWorkItemsWaitingForMe } from "@/hooks/api/useWorkItemApi";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import { exactCount, formatCount } from "@/lib/number-format";
import { ROI_METRIC_NAMES, shownLabel } from "@/lib/roi-figure";
import { roiColumnMetric } from "@/lib/roi-summary";
import { cn } from "@/lib/utils";
import type { CtemBand } from "@/types";
import { MiniTrend, PostureGauge } from "./ctem-viz";
import { periodChange } from "./trend-window";

export interface ValueBandProps {
  /** Live posture score, `null` while none is built. */
  score: number | null;
  band: CtemBand;
  coveredStages: number;
  totalStages: number;
  /** One composite score per recorded day, oldest first (`null` on a day with no score). */
  history: (number | null)[];
  /** Known-exploited exposures on the connected sources; `null` when OpenCTI is not connected. */
  kev: number | null;
  /** The ready briefing's headline and who wrote it, when one exists. */
  briefing: { headline: string; agentName?: string; generatedAt?: string } | null;
  syncedAt?: string;
  syncing: boolean;
  onForceSync: () => void;
  onExplain: () => void;
  onConnectOpenCti: () => void;
  /** Agentic runs of the last 30 days (dashboard stats, the "Agentic executions" card's count). */
  runsThisMonth: number;
  aiLockReason: AiLockReason | null;
  isAdmin: boolean;
  /** The page's greeting, the band's title (C2 V7, parity W1). */
  greeting?: string;
  /** The product status pills, beside the greeting (C2 V7, parity W7). */
  products?: React.ReactNode;
}

function Tile({ title, icon: Icon, children, testId }: { title: string; icon: typeof Zap; children: React.ReactNode; testId: string }) {
  return (
    <section className="flex min-w-0 flex-col gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3" data-testid={testId}>
      <h3 className="flex items-center gap-1.5 content-compact font-medium text-default-secondary">
        <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden /> {title}
      </h3>
      {children}
    </section>
  );
}

export function ValueBandCard(props: ValueBandProps) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const locked = props.aiLockReason !== null;
  const report = useValueReport(props.isAdmin ? "platform" : "mine", "30d", { enabled: !locked });
  const waiting = useWorkItemsWaitingForMe();
  const live = props.score != null;
  const delta = periodChange(props.score, props.history)?.change ?? null;
  const figures = report.data?.organization;
  const defaultRate = report.data ? shownLabel(report.data.rate_label) === "default" : false;
  const runsLine = t_i18n("{count, plural, one {# agentic run in the last 30 days} other {# agentic runs in the last 30 days}}", { count: props.runsThisMonth });
  const recorded = Boolean(figures && figures.runs > 0);
  const costMasked = Boolean(report.data?.cost_masked);
  const waitingCount = waiting.data?.count ?? 0;

  const sentence = props.briefing
    ? props.briefing.headline
    : live
      ? [
          t_i18n("Exposure posture {score}/100 across {covered} of {total} CTEM stages.", {
            score: props.score ?? 0,
            covered: props.coveredStages,
            total: props.totalStages,
          }),
          props.kev != null
            ? `${t_i18n("{count, plural, one {{shown} known-exploited exposure} other {{shown} known-exploited exposures}}", { count: props.kev, shown: formatCount(intl.locale, props.kev) })}.`
            : "",
        ].join(" ").trim()
      : t_i18n("No exposure posture yet. Connect OpenCTI or OpenAEV to build it.");

  return (
    <Card className="flex h-full flex-col overflow-hidden" data-testid="ctem-value-band">
      <CardContent className="@container flex h-full flex-col gap-3 p-4">
        {(props.greeting || props.products) && (
          <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2">
            {props.greeting && <h1 className="text-2xl font-bold font-sans-geologica tracking-tight">{props.greeting}</h1>}
            {props.products}
          </div>
        )}
        <div className="min-w-0">
          <p className="content-base font-semibold" data-testid="ctem-value-band-headline">{sentence}</p>
          {props.briefing?.agentName && (
            <p className="content-compact text-default-secondary">
              {t_i18n("By {agent}, {timeAgo}", {
                agent: props.briefing.agentName,
                timeAgo: props.briefing.generatedAt ? formatTimeAgo(intl, props.briefing.generatedAt) : "",
              })}
            </p>
          )}
        </div>
        <div className="grid flex-1 grid-cols-1 gap-3 @2xl:grid-cols-2 @5xl:grid-cols-4">
          <Tile title={t_i18n("Exposure posture")} icon={ShieldAlert} testId="ctem-value-band-posture">
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={props.onExplain}
                className="shrink-0 rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                aria-label={t_i18n("How the Exposure Posture is computed")}
              >
                <PostureGauge score={props.score} band={props.band} size={88} strokeWidth={8} />
              </button>
              <div className="min-w-0 flex-1 space-y-1">
                <div className="h-8">
                  <MiniTrend values={props.history} />
                </div>
                {delta != null && (
                  <p className={cn("flex items-center gap-1 content-compact tabular-nums", delta >= 0 ? "text-feedback-success-primary" : "text-feedback-error-primary")}>
                    {delta >= 0 ? <ArrowUpRight className="h-3.5 w-3.5" aria-hidden /> : <ArrowDownRight className="h-3.5 w-3.5" aria-hidden />}
                    {delta > 0 ? "+" : delta < 0 ? "-" : ""}
                    {t_i18n("{count, plural, one {# point} other {# points}} vs start of period", { count: Math.abs(delta) })}
                  </p>
                )}
                <p className="flex items-center gap-1 content-compact text-default-secondary">
                  <span className="min-w-0 break-words">
                    {props.syncing
                      ? t_i18n("Syncing...")
                      : props.syncedAt
                        ? t_i18n("Last synced {timeAgo}", { timeAgo: formatTimeAgo(intl, props.syncedAt) })
                        : t_i18n("Not synced yet")}
                  </span>
                  <Tip content={t_i18n("Force sync now: refetches your exposure data live (just for you)")}>
                    <Button type="button" variant="ghost" size="icon" className="h-9 w-9 shrink-0" onClick={props.onForceSync} disabled={props.syncing} aria-label={t_i18n("Force sync")}>
                      <RefreshCw className={cn("h-4 w-4", props.syncing && "animate-spin")} aria-hidden />
                    </Button>
                  </Tip>
                </p>
              </div>
            </div>
          </Tile>

          <Tile title={t_i18n("Risk")} icon={ShieldAlert} testId="ctem-value-band-risk">
            {props.kev != null ? (
              <>
                <p className="text-2xl font-bold tabular-nums leading-none">
                  <CompactFigure exact={exactCount(intl.locale, props.kev)}>{formatCount(intl.locale, props.kev)}</CompactFigure>
                </p>
                <p className="content-compact text-default-secondary">{t_i18n("Known-exploited exposures")}</p>
              </>
            ) : (
              <>
                <p className="content-compact text-default-secondary">{t_i18n("Connect OpenCTI to count known-exploited exposures.")}</p>
                <Button type="button" variant="outline" size="sm" className="self-start" onClick={props.onConnectOpenCti}>
                  {t_i18n("Connect OpenCTI")}
                </Button>
              </>
            )}
          </Tile>

          <Tile title={t_i18n("Value")} icon={TrendingUp} testId="ctem-value-band-value">
            {locked ? (
              <AiLockedFooter reason={props.aiLockReason} layout="stacked" />
            ) : report.isError ? (
              <div role="alert" className="flex flex-wrap items-center gap-2 content-compact text-feedback-error-primary">
                {t_i18n("The value could not be read.")}
                <Button type="button" variant="outline" size="sm" onClick={() => report.refetch()}>
                  {t_i18n("Retry")}
                </Button>
              </div>
            ) : !report.data || !figures ? (
              <div className="h-12 rounded-sm bg-elevation-hover" aria-busy="true" />
            ) : recorded ? (
              <>
                <dl className="grid grid-cols-1 gap-1.5">
                  {(["hours", roiColumnMetric(costMasked)] as const).map((metric) => (
                    <div key={metric} className="min-w-0">
                      <dt className="content-compact text-default-secondary">{t_i18n(ROI_METRIC_NAMES[metric])}</dt>
                      <dd className="min-w-0 content-base font-semibold tabular-nums">
                        <RoiFigure figures={figures} metric={metric} currency={report.data.currency} window="30d" costMasked={costMasked} layout="inline" wrap />
                      </dd>
                    </div>
                  ))}
                </dl>
                <p className="content-compact text-default-secondary" data-testid="ctem-value-band-value-basis">
                  {t_i18n("Last 30 days")}
                  {report.data.estimated ? ` · ${t_i18n("Estimated")}` : ""}
                  {defaultRate ? ` · ${t_i18n("Default rate")}` : ""}
                </p>
              </>
            ) : (
              <>
                <p className="content-compact">{t_i18n("No value recorded yet")}</p>
                <p className="content-compact text-default-secondary">{runsLine}</p>
              </>
            )}
            <Link to={recorded ? valuePath() : VALUE_METHODOLOGY_PATH} className="mt-auto content-compact text-accent hover:underline">
              {recorded ? t_i18n("See the value") : t_i18n("How value is computed")}
            </Link>
          </Tile>

          <Tile title={t_i18n("Actions")} icon={Zap} testId="ctem-value-band-actions">
            <p className="content-compact" data-testid="ctem-value-band-runs">
              {locked ? t_i18n("Agents cannot run until AI is available") : runsLine}
            </p>
            <button
              type="button"
              onClick={() => navigate(waitingForMePath())}
              className={cn("self-start content-compact hover:underline", waitingCount > 0 ? "text-feedback-warning-primary" : "text-default-secondary")}
            >
              {t_i18n("{count} waiting for you", { count: formatCount(intl.locale, waitingCount) })}
            </button>
          </Tile>
        </div>
      </CardContent>
    </Card>
  );
}
