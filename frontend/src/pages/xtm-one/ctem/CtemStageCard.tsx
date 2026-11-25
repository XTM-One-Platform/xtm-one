// SPDX-License-Identifier: Apache-2.0

/**
 * A single CTEM lifecycle stage tile - the heart of the unified dashboard.
 *
 * Always shows rich content: real metrics when available, otherwise a greyed
 * "sample" preview of what the stage will contain - with an "Install ... to go
 * live" CTA for blind spots (missing product) or a "collecting" note while live
 * data lands. Fuses the exposure data with the agents working the stage.
 */
import { memo, useEffect, useRef } from "react";
import { ScanEye, Radar, ShieldAlert, FlaskConical, Wrench, Bot, Plus, ExternalLink, Sparkles, RefreshCw, Lightbulb, type LucideIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import { DiffBadge } from "@/components/ui/diff-badge";
import { MetricInfo } from "@/components/ui/metric-info";
import { SampleBadge } from "@/components/ui/sample-badge";
import { cn, safeExternalUrl, exactMetricValue, formatMetricValue } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { StageSignInHint } from "./stage-sign-in-hint";
import { CTEM_BAND_CONFIG, CTEM_BAND_SCALE_TEXT, CTEM_PRODUCT_INFO, CTEM_STAGE_CONFIG, CTEM_EXPECTATION_BUCKETS, CTEM_EXPECTATION_TYPE_ORDER, ctemExpectationLabel, ctemExpectationOrder, type CtemStageKey } from "@/lib/constants";
import { useCtemStageAssessment, useRefreshStageAssessment } from "@/hooks/api/useCtemMetricsApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import type { CtemStage, CtemFact, CtemVertical } from "@/types";
import { SeverityBars, ValidationDonut, LegendDot, MiniMeter, MiniTrend } from "./ctem-viz";
import { stageDensity } from "./widget-density";
import { useWidgetSize } from "@/components/dashboard-grid/widget-size-context";

/** True only when the stage's dedicated chart will actually render (it has the
 *  data for it). An OpenCTI-only Validation/Prioritization has no chart, so the
 *  card falls back to a headline % meter and never looks empty. */
function hasDedicatedViz(stage: CtemStage): boolean {
  if (stage.key === "prioritization") return (stage.viz.severity_distribution?.length ?? 0) > 0;
  if (stage.key === "validation") {
    // OpenAEV connected (the stage has real data, even if every result is 0) ->
    // always show the donut set (placeholders for empty types), mirroring the
    // OpenAEV home which always renders its Prevention / Detection / Vulnerability
    // / Human-response donuts. Only an unconnected blind spot falls back to the
    // headline % meter / greyed sample.
    if (stage.covered) return true;
    const b = stage.viz.expectation_buckets ?? {};
    return Object.values(b).some((bucket) =>
      Object.values(bucket ?? {}).some((v) => (v ?? 0) > 0),
    );
  }
  return false;
}

/** Why connecting OpenAEV strengthens a stage OpenCTI already covers. */
const ENRICH_HINT: Record<string, string> = {
  scoping: "for live agent coverage",
  discovery: "to validate detections",
  prioritization: "to confirm exploitability",
  validation: "to prove it with BAS",
  remediation: "to re-test fixes",
};

const STAGE_ICONS: Record<string, LucideIcon> = {
  scoping: ScanEye,
  discovery: Radar,
  prioritization: ShieldAlert,
  validation: FlaskConical,
  remediation: Wrench,
};

type DisplayFact = CtemFact & { placeholder?: boolean };

function StageFacts({ facts, exclude, limit }: { facts: DisplayFact[]; exclude?: string; limit?: number }) {
  const kept = exclude ? facts.filter((f) => f.label !== exclude) : facts;
  const shown = limit != null ? kept.slice(0, limit) : kept;
  return (
    // Three columns once the card is wide enough: the same facts, less height.
    <div className="grid grid-cols-2 @lg:grid-cols-3 gap-x-4 gap-y-2">
      {shown.map((fct) => (
        <div key={fct.label} className={cn("min-w-0", fct.placeholder && "opacity-50")}>
          <div className="flex items-center gap-1 min-w-0">
            <p className="content-caption text-default-secondary truncate">{fct.label}</p>
            <MetricInfo label={fct.label} />
          </div>
          <p className="content-base font-semibold tabular-nums" title={exactMetricValue(fct.value)}>
            {formatMetricValue(fct.value)}
            {fct.unit && <span className="text-[11px] text-default-secondary ml-0.5">{fct.unit}</span>}
          </p>
        </div>
      ))}
    </div>
  );
}

function StageViz({ stage }: { stage: CtemStage }) {
  if (stage.key === "prioritization" && (stage.viz.severity_distribution?.length ?? 0) > 0) {
    return (
      <div className="pt-1">
        <SeverityBars buckets={stage.viz.severity_distribution ?? []} />
      </div>
    );
  }
  if (stage.key === "validation" && (stage.covered || stage.viz.expectation_buckets)) {
    const buckets = stage.viz.expectation_buckets ?? {};
    // Always render the canonical OpenAEV expectation donuts (Prevention,
    // Detection, Vulnerability, Human response) - the same set the OpenAEV home
    // shows - plus any extra/future types present. Empty types render a greyed
    // placeholder donut (via ValidationDonut), so a connected-but-empty OpenAEV
    // shows the full set instead of nothing.
    const types = Array.from(
      new Set([...CTEM_EXPECTATION_TYPE_ORDER, ...Object.keys(buckets)]),
    ).sort((a, b) => ctemExpectationOrder(a) - ctemExpectationOrder(b));
    return (
      <div className="pt-1 space-y-1.5">
        <div className="grid grid-cols-4 gap-x-1 gap-y-1 justify-items-center">
          {types.map((t) => (
            <div key={t} className="flex flex-col items-center gap-0.5">
              <ValidationDonut buckets={buckets[t] ?? {}} size={58} />
              <span className="text-[9px] text-default-secondary text-center leading-tight">{ctemExpectationLabel(t)}</span>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap justify-center gap-x-2 gap-y-0.5">
          {CTEM_EXPECTATION_BUCKETS.map((b) => (
            <LegendDot key={b.id} color={b.color} label={b.label} />
          ))}
        </div>
      </div>
    );
  }
  return null;
}

/** Statuses on which the assessment strip has nothing to show and never runs. */
const CTEM_AI_LOCKED_STATUSES: ReadonlySet<string> = new Set(["ee_locked", "ai_disabled", "no_model", "unavailable"]);

/** EE-only AI assessment strip for a live stage card: the seeded stage analyst's
 *  latest per-user, vertical-scoped read + recommended action, with a refresh. */
function StageAssessmentStrip({ stage, vertical }: { stage: string; vertical: CtemVertical }) {
  const { t_i18n } = useFormatter();
  const { data } = useCtemStageAssessment(stage, vertical);
  const refresh = useRefreshStageAssessment();
  // AI off: no generation on landing and the refresh control disabled.
  const aiOff = useAiActionDisabled();
  // Auto-generate this stage's assessment the first time the user lands on a
  // vertical without one (status "pending" = EE available, nothing in flight, no
  // assessment for THIS vertical yet). Mirrors the exposure-briefing auto-gen so
  // EVERY empty phase populates on landing, not just the briefing. Deduped per
  // vertical for this stage instance; the backend also dedupes in-flight runs and
  // exempts the first per-vertical run from the agentic quota.
  const autoGenerated = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (aiOff || data?.status !== "pending") return;
    if (autoGenerated.current.has(vertical)) return;
    autoGenerated.current.add(vertical);
    refresh.mutate({ stage, vertical });
  }, [stage, vertical, data?.status, refresh, aiOff]);
  if (!data || CTEM_AI_LOCKED_STATUSES.has(data.status)) return null;
  const a = data.assessment;
  // The backend returns an explicit ``generating`` flag (true while a run is in
  // flight) and mirrors it in ``status`` with precedence ("generating" outranks a
  // cached "ready"), so the boolean is the single source of truth here.
  const generating = !!data.generating;
  // The newest run was skipped before any model call: the member has no
  // account on these modules yet and signs in to them once (#2042).
  const unsignedModules = data.status === "no_module_account" ? data.no_module_account ?? [] : [];
  return (
    // Fixed height + fixed line-clamps so the AI assessment strip is identical
    // across every card in a row (the card body above grows to fill, pinning the
    // strip + footer to a consistent baseline).
    <div className="rounded-sm bg-accent/[0.04] ring-1 ring-accent/15 p-2.5 flex flex-col gap-1.5 h-[7.5rem]">
      <div className="flex items-center justify-between gap-2 shrink-0">
        <span className="inline-flex items-center gap-1 content-caption font-medium text-accent/80">
          <Sparkles className="h-3 w-3" /> {t_i18n("AI assessment")}
        </span>
        <Tip content={t_i18n("Regenerate this stage assessment (just for you)")}>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              refresh.mutate({ stage, vertical });
            }}
            disabled={refresh.isPending || generating || aiOff}
            className="text-default-secondary hover:text-accent transition-colors disabled:opacity-50"
            aria-label="Refresh assessment"
          >
            <RefreshCw className={cn("h-3 w-3", (refresh.isPending || generating) && "animate-spin")} />
          </button>
        </Tip>
      </div>
      {a ? (
        <>
          <p className="text-[11px] font-medium leading-snug line-clamp-1">{a.headline}</p>
          <p className="content-caption text-default-secondary leading-relaxed line-clamp-2">{a.assessment}</p>
          {a.recommended_action && (
            <p className="content-caption text-accent/80 flex items-start gap-1 mt-auto">
              <Lightbulb className="h-3 w-3 mt-px shrink-0" />
              <span className="line-clamp-1">{a.recommended_action}</span>
            </p>
          )}
        </>
      ) : !generating && data.status === "no_module_account" ? (
        <StageSignInHint modules={unsignedModules} />
      ) : (
        <p className="content-caption text-default-secondary my-auto">
          {generating
            ? t_i18n("Generating your assessment...")
            : t_i18n("No assessment yet - generate one for this vertical.")}
        </p>
      )}
    </div>
  );
}

function CtemStageCardInner({
  stage,
  sample,
  vertical,
  aiEnabled,
  openUrl,
  openLabel,
  onConnect,
  onOpenAgent,
  onOpen,
  scoreHistory,
}: {
  stage: CtemStage;
  sample?: CtemStage;
  vertical: CtemVertical;
  aiEnabled?: boolean;
  openUrl?: string | null;
  openLabel?: string;
  onConnect: (product: string) => void;
  onOpenAgent: (id: string) => void;
  /** Open the full per-stage overview page. Makes the whole card clickable. */
  onOpen?: () => void;
  /** Stage-score history (oldest -> newest) for the in-card sparkline. */
  scoreHistory?: (number | null)[];
}) {
  const { t_i18n } = useFormatter();
  const cfg = CTEM_STAGE_CONFIG[stage.key as CtemStageKey];
  const Icon = STAGE_ICONS[stage.key] ?? Bot;
  // Validate the (unvalidated-at-ingestion) platform_url before it reaches href.
  const safeOpenUrl = safeExternalUrl(openUrl);

  // Every card renders the SAME full layout: real values where we have them,
  // greyed sample placeholders for the gaps (a product not connected) - never an
  // empty card. The score is real when computed, otherwise the sample value.
  const scored = stage.score != null;
  // A covered stage (real facts) never borrows the sample score: it says it has
  // too little data to score, as the lifecycle band and the stage page do.
  const unscored = !scored && stage.covered;
  const scoreValue = scored ? stage.score : unscored ? null : sample?.score ?? null;
  const scoreBand = scored ? stage.band : unscored ? "unknown" : sample?.band ?? "unknown";
  const bandConf = CTEM_BAND_CONFIG[scoreBand] ?? CTEM_BAND_CONFIG.unknown;

  // Merge facts: the full expected set (the sample carries every row), overlaid
  // with the real value by label; any real fact the sample lacks is appended.
  const realByLabel = new Map(stage.facts.map((f) => [f.label, f]));
  const sampleFacts = sample?.facts ?? [];
  const sampleLabels = new Set(sampleFacts.map((f) => f.label));
  const displayFacts: DisplayFact[] = [
    ...sampleFacts.map((sf) => {
      const real = realByLabel.get(sf.label);
      return real ? { ...real, placeholder: false } : { ...sf, placeholder: true };
    }),
    ...stage.facts.filter((f) => !sampleLabels.has(f.label)).map((f) => ({ ...f, placeholder: false })),
  ];

  // Viz: a connected stage (covered - it has real facts, even if all zero) shows
  // its REAL chart, never the greyed sample. When that real chart has nothing to
  // plot yet (e.g. OpenAEV connected but no simulations run), the card falls
  // through to the headline-% meter below instead of greyed sample donuts/bars
  // that would contradict the real 0 facts. Only an unconnected blind spot (not
  // covered) falls back to the sample preview.
  const realHasViz = hasDedicatedViz(stage);
  const useSampleViz = !realHasViz && !stage.covered;
  const vizStage = useSampleViz ? sample ?? stage : stage;
  const vizPlaceholder = useSampleViz;
  // Non-chart stages get a headline-% meter (from the merged facts) so every card
  // has an equal-height visual.
  const headlineFact = !hasDedicatedViz(vizStage)
    ? displayFacts.find((f) => f.kind === "derived" && f.unit === "%" && typeof f.value === "number")
    : undefined;

  // How much of itself the card draws in its grid cell (`stageDensity`): the
  // score trend goes first, then the chart, the AI read and all but four facts.
  const density = stageDensity(useWidgetSize());

  const primaryMissing = stage.missing_products[0];
  const primaryMissingName = primaryMissing
    ? CTEM_PRODUCT_INFO[primaryMissing]?.name ?? primaryMissing
    : "";

  return (
    <Card
      className={cn(
        "overflow-hidden flex h-full flex-col transition-colors hover:border-accent/30",
        onOpen && "cursor-pointer hover:bg-elevation-hover",
      )}
      onClick={onOpen}
      role={onOpen ? "button" : undefined}
      tabIndex={onOpen ? 0 : undefined}
      onKeyDown={
        onOpen
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpen();
              }
            }
          : undefined
      }
    >
      <CardContent className="@container px-4 pt-4 pb-0 flex flex-1 flex-col gap-2.5">
        {/* Header */}
        <div className="flex items-start gap-2.5">
          <div className={cn("h-8 w-8 rounded-sm bg-elevation-hover ring-1 ring-[var(--border-elevation-subtle)] flex items-center justify-center shrink-0", cfg?.color)}>
            <Icon className="h-4 w-4" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <h3 className="content-base font-semibold truncate">{stage.label}</h3>
              <MetricInfo
                label={stage.label}
                title={`${stage.label} score`}
                source="Derived"
                content={`${stage.methodology} The score is ${CTEM_BAND_SCALE_TEXT}`}
              />
            </div>
            <p className="content-caption text-default-secondary truncate">{cfg?.tagline}</p>
          </div>
          {unscored && (
            <span className="shrink-0 text-right content-compact text-default-secondary" data-testid="ctem-stage-unscored">
              {t_i18n("Not enough data to score")}
            </span>
          )}
          {scoreValue != null && (
            <div className="text-right shrink-0">
              <div className="flex items-center justify-end gap-1.5">
                {scored && stage.prev_score != null && (
                  <DiffBadge current={stage.score!} previous={stage.prev_score} label={t_i18n("vs prev")} />
                )}
                <span
                  className={cn("text-2xl font-bold tabular-nums leading-none", scored ? bandConf.color : "text-default-secondary")}
                >
                  {scoreValue}
                </span>
              </div>
              {scored ? (
                <span className={cn("block text-[9px] uppercase tracking-wider mt-1", bandConf.color)}>
                  {bandConf.label}
                </span>
              ) : (
                <SampleBadge className="mt-1" />
              )}
            </div>
          )}
        </div>

        {/* Body: the full fact set + viz, always. Gaps (no real value) are greyed
            per element so the card layout is identical with one product or both. */}
        <div className="flex flex-1 flex-col gap-2.5">
          <StageFacts
            facts={displayFacts}
            exclude={density === "compact" ? undefined : headlineFact?.label}
            limit={density === "compact" ? 4 : density === "medium" ? 6 : undefined}
          />
          {headlineFact && density !== "compact" && (
            <div className={cn(headlineFact.placeholder && "opacity-50 grayscale")}>
              <MiniMeter label={headlineFact.label} value={headlineFact.value as number} color={bandConf.ring} />
            </div>
          )}
          {density !== "compact" && (
            <div className={cn(vizPlaceholder && "opacity-50 grayscale")}>
              <StageViz stage={vizStage} />
            </div>
          )}
          {/* Small, discrete score trend pinned to the bottom of the card - the
              mt-auto spacer absorbs any height difference between cards in a row
              (so it never grows into a giant chart). Greyed placeholder curve until
              two days of real history accrue. */}
          {density === "full" && (
            <div className="mt-auto pt-1">
              <p className="content-caption text-default-secondary mb-1">{t_i18n("Score trend")}</p>
              <div className="h-9">
                <MiniTrend values={scoreHistory ?? []} color={scored ? bandConf.ring : "#64748b"} />
              </div>
            </div>
          )}
        </div>

        {/* EE AI assessment - shown even on placeholder/sample cards so the
            analyst's read (e.g. "OpenAEV not connected, validation unproven") is
            never hidden. StageAssessmentStrip renders nothing when there is no
            assessment / EE is locked. */}
        {aiEnabled && density !== "compact" && <StageAssessmentStrip stage={stage.key} vertical={vertical} />}

        {/* Footer: owner + deep link, plus a connect hint for any missing product
            - vertically centered with a compact min-height so the strip stays
            consistent across a row without feeling heavy. */}
        <div className="border-t border-elevation-subtle-soft flex flex-col justify-center gap-1 min-h-[2.25rem]">
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              {stage.agent ? (
                <button
                  className="flex items-center gap-1.5 text-[11px] text-default-secondary hover:text-default-primary transition-colors w-full"
                  onClick={(e) => { e.stopPropagation(); onOpenAgent(stage.agent!.id); }}
                >
                  <Bot className="h-3 w-3 text-accent/70 shrink-0" />
                  <span className="truncate">{t_i18n("Owned by {name}", { name: stage.agent.name })}</span>
                </button>
              ) : (
                <span className="flex items-center gap-1.5 text-[11px] text-default-secondary">
                  <Bot className="h-3 w-3 shrink-0" /> {t_i18n("Stage analyst")}
                </span>
              )}
            </div>
            {safeOpenUrl && (
              <a
                href={safeOpenUrl}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="text-[11px] text-accent/80 hover:text-accent inline-flex items-center gap-1 shrink-0 no-underline"
              >
                {openLabel} <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
          {primaryMissing && (
            <button
              onClick={(e) => { e.stopPropagation(); onConnect(primaryMissing); }}
              className="flex items-center gap-1 content-caption text-accent/70 hover:text-accent transition-colors w-full"
            >
              <Plus className="h-2.5 w-2.5 shrink-0" />
              <span className="truncate">
                {t_i18n("Connect {name}", { name: primaryMissingName })} {t_i18n(ENRICH_HINT[stage.key] ?? "to complete this stage")}
              </span>
            </button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/** Memoized: with stable props from the command center, a stage card no longer
 *  re-renders on every posture / briefing / stats / runs poll tick - only when
 *  its own stage data, sample, score history or vertical actually changes. */
export const CtemStageCard = memo(CtemStageCardInner);
