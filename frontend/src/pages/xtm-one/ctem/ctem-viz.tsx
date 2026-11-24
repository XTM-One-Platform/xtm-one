// SPDX-License-Identifier: Apache-2.0

/**
 * Small, dependency-free SVG/CSS visualization primitives for the CTEM
 * command center. Design-system compliant: dynamic geometry/colors use inline
 * style (as the dashboard graph components do), tracks use `currentColor` so
 * they flip with the theme, and accent colors come from constants.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { InfoPopover } from "@/components/ui/info-popover";
import { MetricInfo } from "@/components/ui/metric-info";
import { ORGANIZATION_SETUP_PATH } from "@/components/layout/OrganizationSetupPrompt";
import { useAuth } from "@/hooks/useAuth";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { Tip } from "@/components/ui/tooltip";
import { cn, formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { CTEM_BAND_CONFIG, CTEM_EXPECTATION_BUCKETS, PROVIDER_LOGOS } from "@/lib/constants";
import type { CtemBand, CtemSeverityBucket } from "@/types";

const SEVERITY_ORDER = ["critical", "high", "medium", "low", "none", "unknown"];
const SEVERITY_COLOR: Record<string, string> = {
  critical: "var(--color-feedback-error-primary)",
  high: "var(--color-feedback-warning-primary)",
  medium: "var(--color-feedback-alert-primary)",
  low: "var(--color-feedback-success-primary)",
  none: "#475569",
  unknown: "#475569",
};

/** A severity as the page names it, read by `t_i18n(SEVERITY_LABELS[severity])` (OpenCTI stores CRITICAL, HIGH...). @i18n-keys */
const SEVERITY_LABELS = {
  critical: "Critical (severity)",
  high: "High (severity)",
  medium: "Medium (severity)",
  low: "Low (severity)",
  none: "None (severity)",
  unknown: "Unknown (severity)",
} as const satisfies Record<string, string>;

/** The catalog key of a severity label as the data carries it; an unlisted one reads Unknown. */
export function severityLabelKey(label: string | null | undefined): string {
  const key = (label || "unknown").toLowerCase();
  return key in SEVERITY_LABELS ? SEVERITY_LABELS[key as keyof typeof SEVERITY_LABELS] : SEVERITY_LABELS.unknown;
}

/** Inner edge, in px from the centre, of the qualitative ring a
 *  PostureGauge of this size and stroke draws (#11464). */
export function gaugeInnerEdge(size: number, strokeWidth: number): number {
  const radius = size / 2 - strokeWidth;
  const innerStroke = Math.max(3, Math.round(strokeWidth / 2));
  const innerRadius = radius - strokeWidth / 2 - innerStroke / 2 - 3;
  return innerRadius - innerStroke / 2;
}

/** Below this inner edge the 36 px number and the band caption (measured
 *  reach 37.6 px from the centre, plus 2 px of air) would be painted on the
 *  qualitative ring: the gauge then drops the caption (the labelled score
 *  pair beside it gives the band) and draws a 24 px number: "100" is a
 *  48 x 32 line box (28.8 px from the centre) around 43 x 19 of glyph ink
 *  (23.5 px), both inside the 30.5 px edge of the stage page gauge. */
export const GAUGE_FULL_CENTER_MIN_EDGE = 40;

/** Radial Exposure Posture gauge with the score + band in the center. With
 *  `qualitative` (#11464), a thinner inner ring draws the score read against
 *  the organization context, in its own band colour, under the outer
 *  (quantitative) one; the center keeps the quantitative number. */
export function PostureGauge({
  score,
  band,
  size = 168,
  strokeWidth = 12,
  caption,
  qualitative,
}: {
  score: number | null;
  band: CtemBand;
  size?: number;
  strokeWidth?: number;
  /** The line under the score; the band label when omitted, none when null
   *  (the page says why there is no score elsewhere, in one statement). */
  caption?: string | null;
  /** The qualitative score as an inner ring; null score = an empty track. */
  qualitative?: { score: number | null; band: CtemBand };
}) {
  const radius = size / 2 - strokeWidth;
  const circumference = 2 * Math.PI * radius;
  const value = score == null ? 0 : Math.max(0, Math.min(100, score));
  const offset = circumference * (1 - value / 100);
  const { t_i18n } = useFormatter();
  const conf = CTEM_BAND_CONFIG[band] ?? CTEM_BAND_CONFIG.unknown;
  const innerStroke = Math.max(3, Math.round(strokeWidth / 2));
  const innerRadius = radius - strokeWidth / 2 - innerStroke / 2 - 3;
  const innerCircumference = 2 * Math.PI * innerRadius;
  const innerValue = qualitative?.score == null ? 0 : Math.max(0, Math.min(100, qualitative.score));
  const innerRing = (CTEM_BAND_CONFIG[qualitative?.band ?? "unknown"] ?? CTEM_BAND_CONFIG.unknown).ring;
  const compactCenter = !!qualitative && gaugeInnerEdge(size, strokeWidth) < GAUGE_FULL_CENTER_MIN_EDGE;

  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} data-testid="ctem-posture-gauge">
      <svg width={size} height={size} className="-rotate-90">
        {qualitative && (
          <g data-testid="ctem-gauge-qualitative">
            <circle
              cx={size / 2}
              cy={size / 2}
              r={innerRadius}
              fill="none"
              stroke="currentColor"
              strokeWidth={innerStroke}
              className="text-default-disabled"
              strokeDasharray="2 4"
            />
            {qualitative.score != null && (
              <circle
                cx={size / 2}
                cy={size / 2}
                r={innerRadius}
                fill="none"
                stroke={innerRing}
                strokeOpacity={0.85}
                strokeWidth={innerStroke}
                strokeLinecap="round"
                strokeDasharray={innerCircumference}
                strokeDashoffset={innerCircumference * (1 - innerValue / 100)}
                style={{ transition: "stroke-dashoffset 1s ease-out" }}
              />
            )}
          </g>
        )}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-default-disabled"
        />
        {score != null && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={conf.ring}
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            style={{ transition: "stroke-dashoffset 1s ease-out" }}
          />
        )}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        {score == null ? (
          <span className={cn(compactCenter ? "text-xl" : "text-3xl", "font-bold text-default-secondary")}>--</span>
        ) : (
          <span
            className={cn(compactCenter ? "text-2xl" : "text-4xl", "font-bold tabular-nums font-sans-geologica")}
            data-testid="ctem-gauge-score"
            data-center={compactCenter ? "compact" : "full"}
          >
            <AnimatedNumber value={score} />
          </span>
        )}
        {caption !== null && !compactCenter && (
          <span
            data-testid="ctem-gauge-caption"
            className={cn("content-caption font-semibold uppercase tracking-wider mt-0.5", conf.color)}
          >
            {caption ?? t_i18n(conf.label)}
          </span>
        )}
      </div>
    </div>
  );
}

// A gently rising placeholder curve shown (greyed) until real history accrues, so
// every card always carries a trend line instead of an empty gap.
const FAKE_TREND = [34, 38, 36, 44, 47, 53, 51, 60];

/**
 * Responsive area-trend that FILLS its container (use inside a flex-1 wrapper to
 * absorb a card's empty space). Plots a metric's history (oldest -> newest) on a
 * fixed 0..yMax domain. With fewer than two real points it renders the greyed
 * `FAKE_TREND` placeholder so the line is always present. The line uses a
 * non-scaling stroke, so the stretch never distorts it.
 */
export function MiniTrend({
  values,
  color = "var(--color-accent)",
  yMax = 100,
}: {
  values: (number | null)[];
  color?: string;
  yMax?: number;
}) {
  const real = values.filter((v): v is number => v != null);
  const hasReal = real.length >= 2;
  const data = hasReal ? real : FAKE_TREND;
  const stroke = hasReal ? color : "#64748b";
  const n = data.length;
  const W = 100;
  const H = 40;
  const xAt = (i: number) => (n <= 1 ? W / 2 : (i / (n - 1)) * W);
  const yAt = (v: number) => H - 2 - (Math.max(0, Math.min(yMax, v)) / yMax) * (H - 4);
  const linePts = data.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
  return (
    <div className="w-full h-full" style={{ opacity: hasReal ? 1 : 0.45 }} aria-hidden="true">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full h-full overflow-visible">
        <polygon points={`0,${H} ${linePts} ${W},${H}`} fill={stroke} fillOpacity={0.12} />
        <polyline
          points={linePts}
          fill="none"
          stroke={stroke}
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    </div>
  );
}

/**
 * One bar per severity in the fixed critical-to-low order, in the feedback tones:
 * the one severity distribution of the CTEM pages (Scoping and Prioritization).
 */
export function SeverityBars({ buckets, testId }: { buckets: CtemSeverityBucket[]; testId?: string }) {
  const { t_i18n } = useFormatter();
  const normalized = buckets
    .map((b) => ({ label: (b.label || "unknown").toLowerCase(), value: b.value || 0 }))
    .filter((b) => b.value > 0)
    .sort((a, b) => SEVERITY_ORDER.indexOf(a.label) - SEVERITY_ORDER.indexOf(b.label));
  if (normalized.length === 0) return null;
  const max = Math.max(...normalized.map((b) => b.value));
  return (
    <div className="space-y-1" data-testid={testId}>
      {normalized.map((b) => (
        <div key={b.label} className="flex items-center gap-2">
          <span className="w-20 truncate content-caption text-default-secondary shrink-0">{t_i18n(severityLabelKey(b.label))}</span>
          <div className="flex-1 h-2 rounded-full bg-elevation-hover overflow-hidden">
            <div
              className="h-full rounded-full"
              style={{
                width: `${Math.max(4, (b.value / max) * 100)}%`,
                backgroundColor: SEVERITY_COLOR[b.label] ?? SEVERITY_COLOR.unknown,
              }}
            />
          </div>
          <span className="w-10 text-right text-[11px] tabular-nums font-medium">{b.value}</span>
        </div>
      ))}
    </div>
  );
}

/** Compact donut for OpenAEV expectation-result buckets with success% center. */
export function ValidationDonut({
  buckets,
  size = 88,
}: {
  buckets: Record<string, number>;
  size?: number;
}) {
  const ordered = CTEM_EXPECTATION_BUCKETS.map((b) => ({
    ...b,
    value: buckets[b.id] || 0,
  })).filter((b) => b.value > 0);
  const total = ordered.reduce((sum, b) => sum + b.value, 0);
  const hole = size * 0.62;
  // Scale the centre % down on small donuts so "100%" never overflows the hole.
  const centerText = size < 72 ? "content-compact" : "content-base";
  // No results yet for this expectation type: render a greyed placeholder ring
  // (with a 0% center) instead of nothing, so a connected-but-empty OpenAEV
  // still shows the full donut set - mirroring OpenAEV's own home dashboard. The
  // ring thickness matches a real donut's so the placeholder lines up with it.
  if (!total) {
    return (
      <div
        className="relative rounded-full shrink-0 border-elevation-subtle-soft"
        style={{ width: size, height: size, borderStyle: "solid", borderWidth: Math.round((size - hole) / 2) }}
      >
        <div className="absolute inset-0 flex items-center justify-center">
          <span className={cn(centerText, "font-bold tabular-nums text-default-secondary")}>0%</span>
        </div>
      </div>
    );
  }
  let acc = 0;
  const stops = ordered.map((b) => {
    const start = (acc / total) * 360;
    acc += b.value;
    const end = (acc / total) * 360;
    return `${b.color} ${start}deg ${end}deg`;
  });
  const successPct = Math.round(((buckets.SUCCESS || 0) / total) * 100);
  return (
    <div
      className="relative rounded-full shrink-0"
      style={{ width: size, height: size, background: `conic-gradient(${stops.join(",")})` }}
    >
      <div
        className="absolute rounded-full layer-1 bg-elevation-default flex items-center justify-center"
        style={{ inset: (size - hole) / 2 }}
      >
        <span className={cn(centerText, "font-bold tabular-nums")}>{successPct}%</span>
      </div>
    </div>
  );
}

/** Inline legend dot used next to donuts / bars. */
export function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1 content-caption text-default-secondary">
      <span className={cn("h-2 w-2 rounded-full")} style={{ backgroundColor: color }} />
      {label}
    </span>
  );
}

// ── Skeleton placeholders (so the dashboard reads as "alive" before data) ─────

// ── Posture-core orbit ────────────────────────────────────────────────────────
/** The Exposure Posture gauge as the center, with connected integration /
 *  product logos orbiting it - the "live" hero ring. */
export function PostureOrbit({
  score,
  band,
  logos,
  integrationCount,
  size = 230,
  onExplain,
  qualitative,
}: {
  score: number | null;
  band: CtemBand;
  logos: string[];
  integrationCount: number;
  size?: number;
  /** The qualitative posture, drawn as the gauge's inner ring (#11464). */
  qualitative?: { score: number | null; band: CtemBand };
  /** When set, the orbit becomes clickable and calls this to explain the score.
   *  No in-orbit "?" - a cursor + hover hint + a hover ring signal it instead, so
   *  the orbit stays clean. */
  onExplain?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const visible = logos
    .map((p) => ({ p, src: PROVIDER_LOGOS[p] }))
    .filter((e): e is { p: string; src: string } => Boolean(e.src))
    .slice(0, 10);
  const center = size / 2;
  const radius = center - 15;
  const angleStep = 360 / Math.max(visible.length, 1);
  const orbit = (
    <div
      className={cn("relative shrink-0 mx-auto", onExplain && "cursor-pointer group")}
      style={{ width: size, height: size }}
      role={onExplain ? "button" : undefined}
      tabIndex={onExplain ? 0 : undefined}
      onClick={onExplain}
      onKeyDown={
        onExplain
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onExplain();
              }
            }
          : undefined
      }
    >
      <div className="absolute inset-0 rounded-full border border-accent/10 animate-[orbit_60s_linear_infinite] motion-reduce:animate-none transition-colors group-hover:border-accent/30" />
      <div className="absolute inset-6 rounded-full border border-accent/5" />
      <div className="absolute inset-0 flex items-center justify-center">
        <PostureGauge score={score} band={band} size={size - 78} qualitative={qualitative} />
      </div>
      <div className="absolute inset-0 animate-[orbit_40s_linear_infinite] motion-reduce:animate-none">
        {visible.map(({ p, src }, i) => {
          const angle = (angleStep * i - 90) * (Math.PI / 180);
          const x = center + radius * Math.cos(angle) - 14;
          const y = center + radius * Math.sin(angle) - 14;
          return (
            <div
              key={p}
              className="absolute w-7 h-7 rounded-full layer-1 bg-elevation-default border border-elevation-subtle-soft flex items-center justify-center animate-counter-orbit motion-reduce:animate-none"
              style={{ left: x, top: y }}
            >
              <img
                src={src}
                alt={p}
                className="h-4 w-4 object-contain"
                onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
              />
            </div>
          );
        })}
      </div>
      {integrationCount > 0 && (
        <div className="absolute -bottom-1 left-1/2 -translate-x-1/2 bg-accent/20 text-accent text-[9px] font-bold rounded-full px-2.5 py-0.5 border border-accent/30 backdrop-blur-xs whitespace-nowrap">
          {t_i18n("{count} connected", { count: formatCompactNumber(integrationCount) })}
        </div>
      )}
    </div>
  );
  return onExplain ? (
    <Tip content={t_i18n("Click to see how the Exposure Posture is computed")}>{orbit}</Tip>
  ) : (
    orbit
  );
}

/** One reading of a CTEM score for the `ScoreRingKey`: the score, its band
 *  and, when two recorded days give one, its change over the period. */
export interface ScoreReading {
  score: number | null;
  band: CtemBand;
  delta?: number | null;
}

/** A 12 px picture of the gauge with one of its two rings lit: the thick
 *  outer arc (quantitative) or the thin inner one (qualitative). Dashed when
 *  the reading has no score. */
function RingSwatch({ ring, color, dashed }: { ring: "outer" | "inner"; color: string; dashed: boolean }) {
  const lit = dashed ? "currentColor" : color;
  return (
    <svg width={12} height={12} viewBox="0 0 12 12" className="shrink-0 text-default-disabled" aria-hidden="true" data-ring={ring}>
      <circle cx={6} cy={6} r={4.75} fill="none" strokeWidth={2.5} stroke={ring === "outer" ? lit : "currentColor"} strokeDasharray={ring === "outer" && dashed ? "1.5 1.5" : undefined} />
      <circle cx={6} cy={6} r={1.75} fill="none" strokeWidth={1.25} stroke={ring === "inner" ? lit : "currentColor"} strokeDasharray={ring === "inner" && dashed ? "1 1" : undefined} />
    </svg>
  );
}

/** A signed whole number: "+4", "-2", "0". */
export function signedDelta(delta: number): string {
  return delta > 0 ? `+${delta}` : `${delta}`;
}

/**
 * The key of the gauge's two rings beside it (design note rule 8, SP1): one
 * line per reading, each its icon (`database` measured, `building-2` in your
 * context), a swatch of its ring position, the score in its band colour, the
 * band name and the period delta. The words "Quantitative" and "Qualitative"
 * and their definitions are in the icon's popover, never printed beside the
 * number. A qualitative score the context cannot give yet is the dashed
 * swatch, "--" and one link to the organization context (administrators), or
 * "No organization context"; never a made-up number. `size` sets the number
 * size: "lg" for a stage page header, "sm" for the command center hero.
 */
export function ScoreRingKey({
  quantitative,
  qualitative,
  contextMissing,
  size = "sm",
  className,
}: {
  quantitative: ScoreReading;
  qualitative: ScoreReading;
  /** No organization context saved: the qualitative line asks for it. */
  contextMissing?: boolean;
  size?: "sm" | "lg";
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const number = size === "lg" ? "text-3xl" : "text-2xl";
  const line = (
    testId: string,
    ring: "outer" | "inner",
    icon: "database" | "building-2",
    label: string,
    help: string,
    value: ScoreReading,
    missing: ReactNode,
  ) => {
    const conf = CTEM_BAND_CONFIG[value.band] ?? CTEM_BAND_CONFIG.unknown;
    const scored = value.score != null;
    const delta = scored && value.delta != null ? value.delta : null;
    return (
      <div className="flex min-w-0 items-center gap-2" data-testid={testId}>
        <InfoPopover
          label={t_i18n("{label}: definition", { label })}
          icon={<Icon name={icon} size={16} />}
          heading={label}
          headingClassName="content-caption"
          width={288}
          hoverOpens
          triggerTestId={`${testId}-definition`}
          triggerClassName={cn("inline-flex shrink-0 items-center justify-center rounded-sm text-default-secondary hover:text-accent transition-colors cursor-help", BUTTON_ROLE_FOCUS_RING)}
          popoverClassName="p-3"
        >
          <p className="content-caption text-default-secondary leading-relaxed">{help}</p>
        </InfoPopover>
        <RingSwatch ring={ring} color={conf.ring} dashed={!scored} />
        {scored ? (
          <>
            <span className={cn(number, "font-bold leading-none tabular-nums", conf.color)} data-testid={`${testId}-score`}>{value.score}</span>
            <span className={cn("content-caption font-medium", conf.color)}>{t_i18n(conf.label)}</span>
            {delta != null && (
              <span
                className={cn(
                  "content-caption font-medium tabular-nums",
                  delta > 0 ? "text-feedback-success-primary" : delta < 0 ? "text-feedback-error-primary" : "text-default-secondary",
                )}
                aria-label={t_i18n("{delta} since the start of the period", { delta: signedDelta(delta) })}
                data-testid={`${testId}-delta`}
              >
                {signedDelta(delta)}
              </span>
            )}
          </>
        ) : (
          <>
            <span className={cn(number, "font-bold leading-none text-default-secondary")}>--</span>
            {missing}
          </>
        )}
      </div>
    );
  };
  const contextLine = contextMissing ? (
    isAdmin ? (
      <Link to={ORGANIZATION_SETUP_PATH} className={cn("content-caption font-medium text-accent hover:underline rounded-sm", BUTTON_ROLE_FOCUS_RING)}>
        {t_i18n("Set up organization context")}
      </Link>
    ) : (
      <span className="content-caption text-default-secondary">{t_i18n("No organization context")}</span>
    )
  ) : null;
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)} data-testid="ctem-score-ring-key">
      {line(
        "ctem-score-quantitative",
        "outer",
        "database",
        t_i18n("Quantitative"),
        t_i18n("From the connected modules' metrics alone, whatever the organization."),
        quantitative,
        null,
      )}
      {line(
        "ctem-score-qualitative",
        "inner",
        "building-2",
        t_i18n("Qualitative"),
        t_i18n("The same metrics read against your organization context: primary assets, software, sector, PIRs and risk appetite. The more of it is filled, the more of the score holds."),
        qualitative,
        contextLine,
      )}
    </div>
  );
}

/** Labelled horizontal progress meter (0-100). Used to give every stage card a
 *  consistent headline visual so the lifecycle grid stays balanced. */
export function MiniMeter({
  label,
  value,
  color = "var(--color-accent)",
}: {
  label: string;
  value: number | null | undefined;
  color?: string;
}) {
  const v = value == null ? 0 : Math.max(0, Math.min(100, value));
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between content-caption">
        <span className="flex items-center gap-1 text-default-secondary">
          {label}
          <MetricInfo label={label} />
        </span>
        <span className="tabular-nums font-semibold">{value == null ? "--" : `${Math.round(value)}%`}</span>
      </div>
      <div className="h-1.5 rounded-full bg-elevation-hover overflow-hidden">
        <div className="h-full rounded-full transition-all duration-700" style={{ width: `${v}%`, backgroundColor: color }} />
      </div>
    </div>
  );
}

const TREND_COLORS = ["var(--color-accent)", "#a78bfa", "var(--color-feedback-success-primary)", "var(--color-feedback-alert-primary)", "var(--color-feedback-error-primary)", "#60a5fa", "#f472b6"];

export interface TrendSeries {
  key: string;
  label: string;
  values: (number | null)[];
  color?: string;
}

/**
 * Compact multi-series line chart for historical CTEM metrics (0..yMax). Connects
 * the available (non-null) points, renders a dot for a single point, and shows a
 * "building history" hint when there are fewer than two days of data (snapshots
 * accumulate one per day, so a fresh deployment starts almost empty).
 */
export function TrendChart({
  labels,
  series,
  height = 132,
  yMax = 100,
}: {
  labels: string[];
  series: TrendSeries[];
  height?: number;
  yMax?: number;
}) {
  const n = labels.length;
  const W = 600;
  const padX = 6;
  const padY = 6;
  const innerW = W - padX * 2;
  const innerH = height - padY * 2;
  const xAt = (i: number) => (n <= 1 ? W / 2 : padX + (i / (n - 1)) * innerW);
  const yAt = (val: number) => padY + innerH * (1 - Math.max(0, Math.min(yMax, val)) / yMax);
  const enriched = series.map((s, idx) => ({ ...s, color: s.color ?? TREND_COLORS[idx % TREND_COLORS.length] }));

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${height}`} className="w-full" style={{ height }} preserveAspectRatio="none">
        {[0, 25, 50, 75, 100].map((g) => (
          <line
            key={g}
            x1={padX}
            x2={W - padX}
            y1={yAt((g / 100) * yMax)}
            y2={yAt((g / 100) * yMax)}
            stroke="currentColor"
            className="text-default-primary/[0.06]"
            strokeWidth={1}
          />
        ))}
        {enriched.map((s) => {
          const pts = s.values
            .map((val, i) => (val == null ? null : ([xAt(i), yAt(val)] as [number, number])))
            .filter((p): p is [number, number] => p !== null);
          if (pts.length === 0) return null;
          if (pts.length === 1) {
            // A single day -> a short flat segment, NOT a circle (a circle is
            // stretched into a distorted ellipse by preserveAspectRatio="none").
            const [px, py] = pts[0];
            return (
              <line
                key={s.key}
                x1={px - 14}
                x2={px + 14}
                y1={py}
                y2={py}
                stroke={s.color}
                strokeWidth={2}
                strokeLinecap="round"
                vectorEffect="non-scaling-stroke"
              />
            );
          }
          return (
            <polyline
              key={s.key}
              points={pts.map(([px, py]) => `${px},${py}`).join(" ")}
              fill="none"
              stroke={s.color}
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          );
        })}
      </svg>
      <div className="mt-1.5 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {enriched.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1 content-caption text-default-secondary">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
        {n >= 2 && (
          <span className="text-[9px] text-default-secondary">
            {labels[0]} - {labels[n - 1]}
          </span>
        )}
      </div>
    </div>
  );
}
