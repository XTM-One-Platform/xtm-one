// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, memo } from "react";
import {
  Save,
  Zap,
  Key,
  AlertTriangle,
  BarChart3,
  ShieldAlert,
  ShieldCheck,
  ExternalLink,
  KeyRound,
  CheckCircle2,
  Clock,
  Crown,
  LifeBuoy,
  Info,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { QuotaEditor } from "@/components/ui/quota-editor";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { Tip } from "@/components/ui/tooltip";
import { LicenseActivationDialog } from "@/components/ui/license-activation-dialog";
import { SupportCodeDialog } from "@/components/ui/support-code-dialog";
import { EditionBadge } from "@/components/ui/enterprise-upsell";
import {
  usePlatformQuotas,
  useUpdatePlatformQuotas,
  useAvailableModels,
  useQuotaMonitoring,
  useAgentFailurePool,
} from "@/hooks/api/useSettingsApi";
import { useToast } from "@/components/ui/toast";
import {
  useIsXtmOne, useAgenticSource, useAgenticQuotas, useAgenticAIEnabled, useIsCommunityEdition, usePlatformConfig,
} from "@/hooks/usePlatformConfig";
import { useAuth } from "@/hooks/useAuth";
import { cn, formatCompactNumber, formatDate, formatShortDate, QUOTA_NOT_APPLICABLE_LABEL } from "@/lib/utils";
import { FILIGRAN_CONTACT_URL, XTM_ONE_TRIAL_URL } from "@/lib/constants";
import { NOT_RECORDED_REFUND_CAUSE, formatFailureCause } from "@/lib/failure-causes";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledNotice, AiDisabledTip } from "@/components/ui/ai-disabled";
import { useChartTheme } from "@/hooks/useColorMode";
import { useMeasuredBox } from "@/components/presentation/measured-box";
import type { AgentFailurePoolCauseCount, QuotaTypeConfig, QuotaSummary, QuotaHistoryEntry, QuotaConsumer, QuotaOverQuotaUser } from "@/types";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const QUOTA_TYPE_META: Record<string, { label: string; icon: React.ElementType; color: string; barColor: string }> = {
  agentic: { label: "Agentic Executions", icon: Zap, color: "text-accent", barColor: "bg-accent" },
  llm_proxy: { label: "LLM Proxy Requests", icon: Key, color: "text-feedback-success-primary", barColor: "bg-feedback-success-primary" },
};

const PERIOD_LABELS: Record<string, string> = { daily: "Today", monthly: "This Month", yearly: "This Year" };

const BAR_COLORS: Record<string, { stroke: string; stop: string }> = {
  agentic: { stroke: "var(--color-accent)", stop: "var(--color-accent)" },
  llm_proxy: { stroke: "var(--color-feedback-success-primary)", stop: "var(--color-feedback-success-primary)" },
};

// ---------------------------------------------------------------------------
// Shared radial gauge SVG
// ---------------------------------------------------------------------------

function RadialGauge({ pct, strokeColor, size, strokeWidth, children }: {
  pct: number; strokeColor: string; size: number; strokeWidth: number; children: React.ReactNode;
}) {
  const r = 40, c = 2 * Math.PI * r;
  const offset = c - (pct / 100) * c;
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg viewBox="0 0 100 100" className="w-full h-full -rotate-90">
        <circle cx="50" cy="50" r={r} fill="none" stroke="white" strokeOpacity={0.06} strokeWidth={strokeWidth} />
        {pct > 0 && (
          <circle
            cx="50" cy="50" r={r} fill="none"
            stroke={strokeColor} strokeWidth={strokeWidth} strokeLinecap="round"
            strokeDasharray={c} strokeDashoffset={offset}
            className="transition-all duration-700"
          />
        )}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        {children}
      </div>
    </div>
  );
}

function resolveStrokeColor(type: string, pct: number) {
  if (pct >= 100) return "var(--color-feedback-error-primary)";
  if (pct >= 80) return "hsl(45,93%,47%)";
  return BAR_COLORS[type]?.stroke ?? "var(--color-accent)";
}

// ---------------------------------------------------------------------------
// Wide progress card (Copilot mode / single quota)
// ---------------------------------------------------------------------------

function QuotaProgressCard({ quota, ceOnly = false }: { quota: QuotaSummary; ceOnly?: boolean }) {
  const { t_i18n } = useFormatter();
  const meta = QUOTA_TYPE_META[quota.type];
  if (!meta) return null;
  const Icon = meta.icon;
  const hasLimit = quota.limit != null && quota.limit > 0;
  const pct = hasLimit ? Math.min((quota.current_usage / quota.limit!) * 100, 100) : 0;
  const isWarning = !ceOnly && pct >= 80 && pct < 100;
  const isDanger = !ceOnly && pct >= 100;
  const color = resolveStrokeColor(quota.type, pct);

  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardContent className="flex items-center gap-5 pt-5 pb-4">
        <RadialGauge pct={ceOnly ? 0 : hasLimit ? pct : 0} strokeColor={color} size={96} strokeWidth={6}>
          {!ceOnly && hasLimit ? (
            <span className={cn("text-lg font-bold tabular-nums", isDanger ? "text-feedback-error-primary" : isWarning ? "text-feedback-warning-primary" : meta.color)}>
              {Math.round(pct)}%
            </span>
          ) : (
            <Icon className={cn("h-6 w-6", ceOnly ? "text-default-secondary" : meta.color)} />
          )}
        </RadialGauge>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n(meta.label)}</p>
            {!ceOnly && (isDanger || isWarning) && (
              <Tip content={isDanger ? t_i18n("Platform usage has exceeded the limit") : t_i18n("Platform usage is approaching the limit")}>
                <AlertTriangle className={cn("h-3 w-3", isDanger ? "text-feedback-error-primary" : "text-feedback-warning-primary")} />
              </Tip>
            )}
          </div>
          <p className={cn("text-3xl font-bold mt-1 tabular-nums", ceOnly ? "text-default-secondary" : meta.color)}>
            {ceOnly ? QUOTA_NOT_APPLICABLE_LABEL : <AnimatedNumber value={quota.current_usage} />}
          </p>
          <p className="content-caption text-default-secondary mt-1">
            {ceOnly ? (
              <>{t_i18n(PERIOD_LABELS[quota.period] ?? quota.period)} &middot; {t_i18n("Agentic execution is disabled on Community Edition")}</>
            ) : hasLimit ? (
              <>{t_i18n(PERIOD_LABELS[quota.period] ?? quota.period)} &middot; {t_i18n("{count} platform limit", { count: formatCompactNumber(quota.limit!) })}</>
            ) : (
              <>{t_i18n(PERIOD_LABELS[quota.period] ?? quota.period)} &middot; {t_i18n("No platform limit")}</>
            )}
          </p>
        </div>

      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Compact gauge card (XTM One mode — 3 side-by-side)
// ---------------------------------------------------------------------------

function CompactGaugeCard({ quota, ceOnly = false }: { quota: QuotaSummary; ceOnly?: boolean }) {
  const { t_i18n } = useFormatter();
  const hasLimit = quota.limit != null && quota.limit > 0;
  const pct = hasLimit ? Math.min((quota.current_usage / quota.limit!) * 100, 100) : 0;
  const isWarning = !ceOnly && pct >= 80 && pct < 100;
  const isDanger = !ceOnly && pct >= 100;
  const color = resolveStrokeColor(quota.type, pct);
  const periodLabel = PERIOD_LABELS[quota.period] ?? quota.period;

  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardContent className="flex flex-col items-center py-5 gap-3">
        <RadialGauge pct={ceOnly ? 0 : hasLimit ? pct : 0} strokeColor={color} size={80} strokeWidth={5}>
          {!ceOnly && hasLimit ? (
            <span className={cn(
              "text-base font-bold tabular-nums",
              isDanger ? "text-feedback-error-primary" : isWarning ? "text-feedback-warning-primary" : "text-accent",
            )}>
              {Math.round(pct)}%
            </span>
          ) : (
            <span className="content-compact text-default-secondary">{ceOnly ? "—" : "\u221E"}</span>
          )}
        </RadialGauge>

        <div className="text-center min-w-0">
          <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n(periodLabel)}</p>
          <p className={cn(
            "text-2xl font-bold mt-0.5 tabular-nums",
            ceOnly ? "text-default-secondary" : "text-accent",
          )}>
            {ceOnly ? QUOTA_NOT_APPLICABLE_LABEL : <AnimatedNumber value={quota.current_usage} />}
          </p>
          <p className="content-caption text-default-secondary mt-0.5">
            {ceOnly
              ? t_i18n("Agentic execution is disabled")
              : hasLimit
                ? t_i18n("{count} platform limit", { count: formatCompactNumber(quota.limit!) })
                : t_i18n("No platform limit")}
          </p>
        </div>

        {!ceOnly && hasLimit && (isDanger || isWarning) && (
          <div className="flex flex-col items-center gap-0.5">
            {isDanger && (
              <span className="content-caption text-feedback-error-primary flex items-center gap-0.5">
                <AlertTriangle className="h-2.5 w-2.5" /> {t_i18n("Over limit")}
              </span>
            )}
            {isWarning && !isDanger && (
              <span className="content-caption text-feedback-warning-primary flex items-center gap-0.5">
                <AlertTriangle className="h-2.5 w-2.5" /> {t_i18n("Near limit")}
              </span>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Historical bar chart (SVG, matches AI Adoption TimeSeriesChart style)
// ---------------------------------------------------------------------------

const HISTORY_RANGES = [
  { value: 9999, label: "All" }, { value: 365, label: "1Y" }, { value: 90, label: "3M" },
  { value: 30, label: "30D" }, { value: 7, label: "7D" },
] as const;
type HistoryRange = (typeof HISTORY_RANGES)[number]["value"];

const HISTORY_INTERVALS = [
  { value: "1d", label: "1D" }, { value: "1w", label: "1W" }, { value: "1m", label: "1M" },
] as const;
type HistoryInterval = (typeof HISTORY_INTERVALS)[number]["value"];

function SwitcherBar<T extends string | number>({ options, value, onChange }: {
  options: readonly { value: T; label: string }[]; value: T; onChange: (v: T) => void;
}) {
  return (
    <div className="flex items-center gap-px rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-px">
      {options.map((o) => (
        <button key={String(o.value)} onClick={() => onChange(o.value)} className={cn("px-1.5 py-0.5 content-caption font-medium rounded-[3px] transition-all duration-200 leading-tight", value === o.value ? "bg-accent/20 text-accent shadow-[0_0_6px_color-mix(in_srgb,_var(--color-accent)_calc(0.15_*_100%),_transparent)]" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover")}>{o.label}</button>
      ))}
    </div>
  );
}

function fmtPeriod(p: string, interval: HistoryInterval) {
  if (interval === "1m" || p.length === 7) {
    const [y, m] = p.split("-");
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return `${months[parseInt(m, 10) - 1]} ${y}`;
  }
  if (p.length === 10) {
    return formatShortDate(new Date(p + "T00:00:00"));
  }
  if (p.length === 4) return p;
  return p;
}

function filterHistory(history: QuotaHistoryEntry[], days: number): QuotaHistoryEntry[] {
  if (days >= 9999) return history;
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - days);
  const cutoffStr = cutoff.toISOString().slice(0, 10);
  return history.filter((h) => (h.period ?? "") >= cutoffStr);
}

function aggregateHistory(data: QuotaHistoryEntry[], interval: HistoryInterval): QuotaHistoryEntry[] {
  if (interval === "1d" || data.length === 0) return data;
  const buckets: Record<string, { agentic: number; llm_proxy: number }> = {};
  for (const d of data) {
    const p = d.period ?? "";
    let key: string;
    if (interval === "1w") {
      const dt = new Date(p.length >= 10 ? p + "T00:00:00" : p + "-01T00:00:00");
      const mon = new Date(dt);
      mon.setDate(mon.getDate() - ((mon.getDay() + 6) % 7));
      key = mon.toISOString().slice(0, 10);
    } else {
      key = p.length >= 7 ? p.slice(0, 7) : p;
    }
    if (!buckets[key]) buckets[key] = { agentic: 0, llm_proxy: 0 };
    buckets[key].agentic += d.agentic ?? 0;
    buckets[key].llm_proxy += d.llm_proxy ?? 0;
  }
  return Object.entries(buckets)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([period, v]) => ({ period, agentic: v.agentic, llm_proxy: v.llm_proxy }));
}

const HistoryChart = memo(function HistoryChart({ history }: { history: QuotaHistoryEntry[] }) {
  const { t_i18n } = useFormatter();
  const [range, setRange] = useState<HistoryRange>(30);
  const [interval, setInterval] = useState<HistoryInterval>("1d");
  const filtered = aggregateHistory(filterHistory(history, range), interval);

  const hasAgentic = history.some((h) => (h.agentic ?? 0) > 0);
  const hasLLMProxy = history.some((h) => (h.llm_proxy ?? 0) > 0);

  // The chart is drawn at its container's pixel width, so the viewBox unit is
  // one CSS px and `fontSize={10}` renders the ticks at the caption size
  // (10 px) at every width. 700 is only the first-paint fallback.
  const hostRef = useRef<HTMLDivElement>(null);
  const box = useMeasuredBox(hostRef);
  const W = box?.width ?? 700, H = 164;
  const PAD = { top: 8, right: 12, bottom: 44, left: 40 };
  // The rotated x labels start below the zero tick of the y axis so the two
  // never intersect.
  const X_LABEL_OFFSET = 18;
  const cW = W - PAD.left - PAD.right;
  const cH = H - PAD.top - PAD.bottom;

  const maxVal = Math.max(
    ...filtered.map((h) => Math.max(h.agentic ?? 0, h.llm_proxy ?? 0)),
    1,
  );

  const minSlots = interval === "1d" ? (range === 7 ? 7 : range === 30 ? 30 : 12) : Math.max(filtered.length, 6);
  const slots = Math.max(filtered.length, minSlots);
  const barGroupWidth = cW / slots;
  const maxBarW = 6;
  const barsInGroup = (hasAgentic ? 1 : 0) + (hasLLMProxy ? 1 : 0) || 1;
  const barGap = barsInGroup > 1 ? 1 : 0;
  const rawBarW = (barGroupWidth * 0.4 - barGap * (barsInGroup - 1)) / barsInGroup;
  const singleBarW = Math.min(rawBarW, maxBarW);
  const groupContentW = singleBarW * barsInGroup + barGap * (barsInGroup - 1);

  const yTicks = 4;
  // One x label per 40 px at most, so a narrow chart (332 px at 1024) does not
  // crowd its rotated labels.
  const xStep = Math.max(1, Math.ceil(filtered.length / Math.max(2, Math.floor(cW / 40))));
  const { gridStroke, tickFill } = useChartTheme();

  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 content-base font-semibold">
            <BarChart3 className="h-4 w-4 text-accent" />
            {t_i18n("Usage History")}
          </CardTitle>
          <div className="flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
            {hasAgentic && (
              <div className="flex items-center gap-1.5 mr-1">
                <div className="h-2 w-2 rounded-full bg-accent" />
                <span className="content-caption whitespace-nowrap text-default-secondary">{t_i18n("Agentic")}</span>
              </div>
            )}
            {hasLLMProxy && (
              <div className="flex items-center gap-1.5 mr-1">
                <div className="h-2 w-2 rounded-full bg-feedback-success-primary" />
                <span className="content-caption whitespace-nowrap text-default-secondary">{t_i18n("LLM Proxy")}</span>
              </div>
            )}
            <SwitcherBar options={HISTORY_RANGES} value={range} onChange={setRange} />
            <SwitcherBar options={HISTORY_INTERVALS} value={interval} onChange={setInterval} />
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div ref={hostRef} className="w-full">
        {filtered.length === 0 ? (
          <p className="content-compact text-default-secondary text-center py-6">{t_i18n("No data for selected period")}</p>
        ) : (
          <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="block w-full h-auto">
            {Array.from({ length: yTicks + 1 }, (_, i) => {
              const val = Math.round(maxVal * (1 - i / yTicks));
              const y = PAD.top + (i / yTicks) * cH;
              return (
                <g key={i}>
                  <line x1={PAD.left} y1={y} x2={PAD.left + cW} y2={y} stroke={gridStroke} />
                  <text x={PAD.left - 4} y={y + 3.5} textAnchor="end" fontSize={10} fill={tickFill}>
                    {formatCompactNumber(val)}
                  </text>
                </g>
              );
            })}

            {filtered.map((h, i) => {
              const cx = PAD.left + (i + 0.5) * barGroupWidth;
              const gx = cx - groupContentW / 2;
              let bOffset = 0;
              const bars: React.ReactNode[] = [];

              if (hasAgentic) {
                const val = h.agentic ?? 0;
                const barH = Math.max((val / maxVal) * cH, val > 0 ? 1 : 0);
                bars.push(
                  <rect key="a" x={gx + bOffset} y={PAD.top + cH - barH} width={singleBarW} height={barH} rx={1} fill="var(--color-accent)" fillOpacity={0.75} className="transition-all duration-500">
                    <title>Agentic: {formatCompactNumber(val)}</title>
                  </rect>,
                );
                bOffset += singleBarW + barGap;
              }
              if (hasLLMProxy) {
                const val = h.llm_proxy ?? 0;
                const barH = Math.max((val / maxVal) * cH, val > 0 ? 1 : 0);
                bars.push(
                  <rect key="l" x={gx + bOffset} y={PAD.top + cH - barH} width={singleBarW} height={barH} rx={1} fill="var(--color-feedback-success-primary)" fillOpacity={0.75} className="transition-all duration-500">
                    <title>LLM Proxy: {formatCompactNumber(val)}</title>
                  </rect>,
                );
              }

              // The last label shows only when it sits at least half a step
              // past the previous one, so the two never print over each other.
              const showLabel = i % xStep === 0 || (i === filtered.length - 1 && i % xStep >= Math.ceil(xStep / 2));
              return (
                <g key={i}>
                  {bars}
                  {showLabel && (
                    <text x={cx} y={PAD.top + cH + X_LABEL_OFFSET} textAnchor="end" fontSize={10} fill={tickFill} transform={`rotate(-30 ${cx} ${PAD.top + cH + X_LABEL_OFFSET})`}>
                      {fmtPeriod(h.period ?? "", interval)}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        )}
        </div>
      </CardContent>
    </Card>
  );
});

// ---------------------------------------------------------------------------
// Top consumers horizontal bar chart (matches AI Adoption HorizontalBarChart)
// ---------------------------------------------------------------------------

/**
 * Per-cause decomposition of one credit-back total.
 *
 * The two headline numbers say how much was forgiven; this says what was
 * forgiven, which is the part an admin can act on — "40 free credits" is one
 * provider outage or forty agents that cannot resolve a model, and those need
 * opposite responses.
 *
 * Capped at the top three, with the tail folded into one line so the tile
 * stays a tile. The entries already sum to their total (the API appends a
 * `not_recorded` remainder when its tally falls short), so the fold is a
 * display choice and never hides a discrepancy.
 */
function CauseBreakdown({ items }: { items?: AgentFailurePoolCauseCount[] }) {
  const { t_i18n } = useFormatter();
  if (!items || items.length === 0) return null;

  const TOP = 3;
  const top = items.slice(0, TOP);
  const restCount = items.slice(TOP).reduce((sum, item) => sum + item.count, 0);

  return (
    <ul className="mt-1.5 space-y-0.5 border-t border-elevation-subtle-soft pt-1.5">
      {top.map((item) => (
        <li key={item.cause} className="flex items-baseline justify-between gap-2 content-caption">
          <span
            className={cn(
              "truncate",
              // The remainder is bookkeeping, not a finding — muted so it never
              // reads as the leading cause of anything.
              item.cause === NOT_RECORDED_REFUND_CAUSE
                ? "text-default-secondary italic"
                : "text-default-secondary",
            )}
          >
            {formatFailureCause(item.cause)}
          </span>
          <span className="tabular-nums text-default-secondary shrink-0">{item.count}</span>
        </li>
      ))}
      {restCount > 0 && (
        <li className="flex items-baseline justify-between gap-2 content-caption text-default-secondary">
          <span>{t_i18n("Other causes")}</span>
          <span className="tabular-nums shrink-0">{restCount}</span>
        </li>
      )}
    </ul>
  );
}


function TopConsumersCard({ type, consumers }: { type: string; consumers: QuotaConsumer[] }) {
  const { t_i18n } = useFormatter();
  const meta = QUOTA_TYPE_META[type];
  if (!meta) return null;
  const Icon = meta.icon;
  const maxVal = Math.max(...consumers.map((c) => c.count), 1);

  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base font-semibold">
          <Icon className="h-4 w-4 text-accent" />
          {t_i18n("Top Consumers")} &middot; {t_i18n(meta.label)}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2.5">
        {consumers.length === 0 ? (
          <p className="content-compact text-default-secondary text-center py-4">{t_i18n("No data")}</p>
        ) : (
          consumers.map((c, i) => (
            <div key={i} className="space-y-1">
              <div className="flex items-center justify-between">
                <span className="content-compact font-medium truncate max-w-[70%]">{c.name || c.email}</span>
                <span className="content-compact text-default-secondary tabular-nums">{formatCompactNumber(c.count)}</span>
              </div>
              <div className="h-1.5 w-full rounded-full bg-elevation-hover">
                <div
                  className={cn("h-1.5 rounded-full transition-all duration-700", meta.barColor)}
                  style={{ width: `${(c.count / maxVal) * 100}%` }}
                />
              </div>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Users over / near quota card
// ---------------------------------------------------------------------------

function OverQuotaCard({ overQuotaUsers, isXtmOne }: { overQuotaUsers: Record<string, QuotaOverQuotaUser[]>; isXtmOne: boolean }) {
  const { t_i18n } = useFormatter();
  const agenticUsers = overQuotaUsers.agentic ?? [];
  const llmProxyUsers = overQuotaUsers.llm_proxy ?? [];
  const allUsers = [
    ...agenticUsers.map((u) => ({ ...u, quotaType: "agentic" as const })),
    ...(!isXtmOne ? llmProxyUsers.map((u) => ({ ...u, quotaType: "llm_proxy" as const })) : []),
  ];
  const overCount = allUsers.filter((u) => u.status === "over").length;
  const nearCount = allUsers.filter((u) => u.status === "near").length;

  return (
    <Card className="transition-all duration-300 hover:bg-elevation-hover flex flex-col">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 content-base font-semibold">
          <ShieldAlert className="h-4 w-4 text-accent" />
          {t_i18n("Quota Status")}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex-1 flex flex-col">
        <div className="flex items-center gap-3 mb-4">
          {overCount > 0 ? (
            <Badge variant="destructive" className="content-caption gap-1">
              <AlertTriangle className="h-3 w-3" /> {t_i18n("{count} over quota", { count: overCount })}
            </Badge>
          ) : (
            <Badge variant="outline" className="content-caption text-feedback-success-primary border-feedback-success-primary/30 gap-1">
              {t_i18n("{count} over quota", { count: 0 })}
            </Badge>
          )}
          {nearCount > 0 && (
            <Badge variant="outline" className="content-caption text-feedback-warning-primary border-feedback-alert-primary/30 gap-1">
              {t_i18n("{count} near limit", { count: nearCount })}
            </Badge>
          )}
        </div>

        {allUsers.length === 0 ? (
          <div className="flex-1 flex items-center justify-center py-6">
            <p className="content-compact text-default-secondary text-center">{t_i18n("All users are within their quota limits.")}</p>
          </div>
        ) : (
          <div className="space-y-0 flex-1 overflow-y-auto max-h-[260px]">
            <div className="grid grid-cols-[1fr_70px_70px_50px] gap-x-3 px-2 py-1.5 border-b border-elevation-subtle-soft">
              <span className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("User")}</span>
              <span className="content-caption font-medium text-default-secondary uppercase tracking-wider text-right">{t_i18n("Usage")}</span>
              <span className="content-caption font-medium text-default-secondary uppercase tracking-wider text-right">{t_i18n("Limit")}</span>
              <span className="content-caption font-medium text-default-secondary uppercase tracking-wider text-right">{t_i18n("Status")}</span>
            </div>
            {allUsers.map((user, i) => {
              // A 0 limit is a hard stop: the user is at their limit.
              const pct = user.limit > 0 ? Math.round((user.usage / user.limit) * 100) : 100;
              const isOver = user.status === "over";
              const meta = QUOTA_TYPE_META[user.quotaType];
              const Icon = meta?.icon ?? Zap;
              return (
                <div
                  key={`${user.email}-${user.quotaType}-${i}`}
                  className={cn(
                    "grid grid-cols-[1fr_70px_70px_50px] gap-x-3 px-2 py-2 border-b border-elevation-subtle-soft transition-colors",
                    isOver ? "bg-feedback-error-primary/[0.04]" : "bg-feedback-alert-primary/[0.03]",
                  )}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <Tip content={meta ? t_i18n(meta.label) : user.quotaType}>
                      <Icon className={cn("h-3 w-3 shrink-0", meta?.color ?? "text-accent")} />
                    </Tip>
                    <div className="min-w-0">
                      <p className="content-compact font-medium truncate">{user.name}</p>
                      <p className="content-caption text-default-secondary truncate">{user.email}</p>
                    </div>
                  </div>
                  <span className="content-compact tabular-nums text-right self-center font-medium">
                    {formatCompactNumber(user.usage)}
                  </span>
                  <span className="content-compact tabular-nums text-right self-center text-default-secondary">
                    {formatCompactNumber(user.limit)}
                  </span>
                  <div className="flex items-center justify-end self-center">
                    <span className={cn(
                      "content-caption font-semibold tabular-nums",
                      isOver ? "text-feedback-error-primary" : "text-feedback-warning-primary",
                    )}>
                      {pct}%
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Agent failure pool
// ---------------------------------------------------------------------------
// A failed agentic run is refunded, up to a bounded per-agent allowance that
// renews once a year on the licence anniversary. The allowance's SIZE is an
// environment setting with no UI on purpose (it changes what is billable), so
// this card is read-only apart from one action: redeeming a Filigran support
// code, which recharges every pool before the anniversary.

function AgentFailurePoolCard() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const [codeDialogOpen, setCodeDialogOpen] = useState(false);
  const { data: pool, isLoading } = useAgentFailurePool({ enabled: isAdmin });

  if (!isAdmin || isLoading || !pool) return null;

  const windowSourceLabel =
    pool.window.source === "xtm_license"
      ? t_i18n("Renews on your Filigran license anniversary")
      : pool.window.source === "platform"
        ? t_i18n("Renews on this platform's anniversary (no license installed)")
        : t_i18n("Renews at the turn of the calendar year");

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2 content-base font-semibold">
              <LifeBuoy className="h-4 w-4 text-accent" />
              {t_i18n("Agent Failure Pool")}
            </CardTitle>
            <p className="content-compact text-default-secondary leading-relaxed max-w-2xl">
              {pool.enabled
                ? t_i18n("An agentic action is deducted from the user's action quota before the agent starts, so a run that fails has already spent one action. Each agent's first failures are credited back instead: after that, failures are deducted like successes, so an agent that always fails keeps spending the quota.")
                : t_i18n("Failed runs currently spend an agentic action just like successful ones. The pool is sized in the environment (AGENT_FAILURE_QUOTA_REFUND_ENABLED / AGENT_FAILURE_QUOTA_POOL) and is not configurable here.")}
            </p>
          </div>
          {pool.enabled && (
            <Badge variant="outline" className="shrink-0 content-caption text-accent border-accent/30">
              {t_i18n("{count} per agent · per year", { count: pool.pool_size })}
            </Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {!pool.enabled ? (
          <p className="content-compact text-default-secondary">
            {t_i18n("Set AGENT_FAILURE_QUOTA_REFUND_ENABLED=true (and a pool size above 0) to stop deducting actions for failed runs.")}
          </p>
        ) : (
          <>
            {/* Window + totals. Five tiles, because the two credit-back
                reasons must be readable apart: one is rationed, the other is
                free — an admin seeing a big number needs to know which. */}
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
              <div className="rounded-sm border border-elevation-subtle-soft px-3 py-2">
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Renews")}</p>
                <p className="content-base font-semibold mt-0.5">{formatShortDate(pool.window.end)}</p>
                <Tip content={windowSourceLabel}>
                  <p className="content-caption text-default-secondary truncate">
                    {t_i18n("in {count, plural, one {# day} other {# days}}", { count: pool.window.days_remaining })}
                  </p>
                </Tip>
              </div>
              <div className="rounded-sm border border-elevation-subtle-soft px-3 py-2">
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Credited back · agent failure")}</p>
                <p className="content-base font-semibold mt-0.5 tabular-nums text-feedback-success-primary">
                  {formatCompactNumber(pool.totals.failures_forgiven ?? 0)}
                </p>
                <p className="content-caption text-default-secondary">{t_i18n("from the agents' pools")}</p>
                {(pool.totals.stale_failures_forgiven ?? 0) > 0 && (
                  <p className="content-caption text-default-secondary">
                    {t_i18n("including {count} for deleted users and agents", { count: formatCompactNumber(pool.totals.stale_failures_forgiven ?? 0) })}
                  </p>
                )}
                <CauseBreakdown items={pool.forgiven_by_cause} />
              </div>
              <div className="rounded-sm border border-elevation-subtle-soft px-3 py-2">
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Credited back · no LLM call")}</p>
                <p className="content-base font-semibold mt-0.5 tabular-nums text-accent">
                  {formatCompactNumber(pool.totals.free_credits ?? 0)}
                </p>
                <p className="content-caption text-default-secondary">{t_i18n("free, outside the pools")}</p>
                {(pool.totals.stale_free_credits ?? 0) > 0 && (
                  <p className="content-caption text-default-secondary">
                    {t_i18n("including {count} for deleted users and agents", { count: formatCompactNumber(pool.totals.stale_free_credits ?? 0) })}
                  </p>
                )}
                <CauseBreakdown items={pool.free_credits_by_cause} />
              </div>
              <div className="rounded-sm border border-elevation-subtle-soft px-3 py-2">
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Agents affected")}</p>
                <p className="content-base font-semibold mt-0.5 tabular-nums">{pool.totals.agents_with_failures}</p>
              </div>
              <div className="rounded-sm border border-elevation-subtle-soft px-3 py-2">
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Pools exhausted")}</p>
                <p className={cn(
                  "content-base font-semibold mt-0.5 tabular-nums",
                  pool.totals.exhausted > 0 ? "text-feedback-warning-primary" : "",
                )}>
                  {pool.totals.exhausted}
                </p>
                <p className="content-caption text-default-secondary">{t_i18n("now spending actions on failures")}</p>
              </div>
            </div>

            <p className="content-compact text-default-secondary leading-relaxed">
              {t_i18n("Two reasons an action comes back. A run that failed on its own merits spends one slot of that agent's pool. A run that never reached a model (provider unreachable, down or not configured) or a run the platform abandoned (worker lost, deadline blown) is credited back for free, outside the pool: no LLM was called, and an incident on this side must not ration what the user gets.")}
            </p>

            {/* Nothing platform-wide caps agentic actions. A failed
                run then costs the user nothing, so the refund path leaves the
                pool alone — saying so here is what keeps an admin with
                unlimited quotas from reading "Spent" as the platform having
                started to ration them. Per-user limits can still exist and
                those users ARE pooled, hence "only applies to", not "off", and
                "cannot run out" is claimed only about everyone else. */}
            {pool.platform_quota_unlimited && (
              <div className="flex items-start gap-2 rounded-sm border border-accent/20 bg-accent/[0.03] px-3 py-2">
                <Info className="h-3.5 w-3.5 shrink-0 mt-0.5 text-accent" />
                <p className="content-compact text-default-secondary leading-relaxed">
                  {t_i18n("Agentic actions are unlimited on this platform: no license ceiling, no platform limit, no default per user. The pool only applies to users or groups that carry an agentic limit of their own; for everyone else a failed run costs nothing, so their pools are left untouched and cannot run out.")}
                </p>
              </div>
            )}

            {/* Per-agent consumption */}
            {pool.entries.length === 0 ? (
              <p className="content-compact text-default-secondary">
                {t_i18n("No agent has failed a run in this window.")}
              </p>
            ) : (
              <div className="space-y-0 overflow-y-auto max-h-[260px]">
                <div className="grid grid-cols-[1fr_90px_70px_90px] gap-x-3 px-2 py-1.5 border-b border-elevation-subtle-soft">
                  <span className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Agent")}</span>
                  <span className="content-caption font-medium text-default-secondary uppercase tracking-wider text-right">{t_i18n("Pool used")}</span>
                  <span className="content-caption font-medium text-default-secondary uppercase tracking-wider text-right">{t_i18n("Left")}</span>
                  {/* The label alone gave no hint that an explanation exists;
                      the icon is the affordance, wrapped on its own like the
                      other tooltip icons in this file. */}
                  <span className="flex items-center justify-end gap-1 content-caption font-medium text-default-secondary uppercase tracking-wider">
                    {t_i18n("Provider")}
                    <Tip content={t_i18n("Runs credited back for free because no model was ever called")}>
                      <Info className="h-3 w-3 shrink-0 opacity-70" />
                    </Tip>
                  </span>
                </div>
                {pool.entries.map((entry) => (
                  <div
                    key={`${entry.user_id}-${entry.agent_id}`}
                    className={cn(
                      "grid grid-cols-[1fr_90px_70px_90px] gap-x-3 px-2 py-2 border-b border-elevation-subtle-soft",
                      entry.exhausted && "bg-feedback-alert-primary/[0.03]",
                    )}
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5 min-w-0">
                        <p className="content-compact font-medium truncate">
                          {/* A counter can outlive its agent between the CASCADE
                              delete and the key's expiry — say so rather than
                              rendering an empty row. */}
                          {entry.agent_name ?? t_i18n("Deleted agent")}
                        </p>
                        {/* A platform agent's SCHEDULED runs never consume the
                            quota, so they can never appear here; this row comes
                            from a manual trigger, which is charged like any
                            other run. Said out loud, because an admin who knows
                            the agent is normally free would otherwise read this
                            as a bug. */}
                        {entry.agent_is_builtin && (
                          <Tip content={t_i18n("Platform agent: its scheduled runs do not consume the action quota. Only manually triggered runs do, and that is what this row counts.")}>
                            <Badge variant="outline" className="shrink-0 content-caption text-default-secondary border-elevation-subtle-soft">
                              {t_i18n("Platform")}
                            </Badge>
                          </Tip>
                        )}
                      </div>
                      <p className="content-caption text-default-secondary truncate">{entry.user_email ?? "—"}</p>
                    </div>
                    <span className={cn(
                      "content-compact tabular-nums text-right self-center font-medium",
                      entry.quota_unlimited && "text-default-secondary",
                    )}>
                      {entry.used} / {pool.pool_size}
                    </span>
                    <div className="flex items-center justify-end self-center">
                      {/* A user nothing caps is never pooled: the
                          slots shown are history, and "Spent" would be a quota
                          alarm about a quota that does not exist. */}
                      {entry.quota_unlimited ? (
                        <Tip content={t_i18n("This user's agentic quota is unlimited: failed runs cost them nothing, so the pool is not consumed for them. The slots shown were spent under a limit since lifted, or before this rule applied.")}>
                          <Badge variant="outline" className="content-caption text-default-secondary border-elevation-subtle-soft">
                            {t_i18n("Not applicable")}
                          </Badge>
                        </Tip>
                      ) : entry.exhausted ? (
                        <Badge variant="outline" className="content-caption text-feedback-warning-primary border-feedback-alert-primary/30">
                          {t_i18n("Spent")}
                        </Badge>
                      ) : (
                        <span className="content-compact tabular-nums text-feedback-success-primary">{entry.remaining}</span>
                      )}
                    </div>
                    <span className={cn(
                      "content-compact tabular-nums text-right self-center",
                      (entry.free_refunds ?? 0) > 0 ? "text-accent" : "text-default-disabled",
                    )}>
                      {(entry.free_refunds ?? 0) > 0 ? entry.free_refunds : "—"}
                    </span>
                  </div>
                ))}
                {pool.truncated && (
                  <p className="content-caption text-default-secondary px-2 pt-2">
                    {t_i18n("Only the most affected agents are listed.")}
                  </p>
                )}
              </div>
            )}

            {/* Recharge */}
            <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
              <p className="content-compact text-default-secondary">
                {t_i18n("Filigran support can issue a code that gives every agent a full failure allowance again before the renewal date.")}
              </p>
              <Button variant="outline" className="gap-2 shrink-0" onClick={() => setCodeDialogOpen(true)}>
                <LifeBuoy className="h-4 w-4" />
                {t_i18n("Enter a support code")}
              </Button>
            </div>

            {/* Codes already redeemed — the ledger that makes each one single-use */}
            {pool.redemptions.length > 0 && (
              <div className="space-y-1 border-t border-elevation-subtle-soft pt-3">
                <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">
                  {t_i18n("Support codes redeemed")}
                </p>
                {pool.redemptions.slice(0, 5).map((r) => (
                  <div key={r.code_id} className="flex items-center justify-between gap-3 content-caption">
                    <span className="truncate text-default-secondary">
                      {r.note || r.code_id}
                      {r.kind === "platform_quota_reset" && (
                        <span className="text-default-secondary">{` · ${t_i18n("Usage counters reset")}`}</span>
                      )}
                    </span>
                    <span className="shrink-0 tabular-nums text-default-secondary">
                      {r.redeemed_at ? formatShortDate(r.redeemed_at) : "—"}
                      {r.redeemed_by ? ` · ${r.redeemed_by}` : ""}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </CardContent>

      <SupportCodeDialog open={codeDialogOpen} onOpenChange={setCodeDialogOpen} />
    </Card>
  );
}


// ---------------------------------------------------------------------------
// License Status Banner
// ---------------------------------------------------------------------------

function LicenseStatusBanner() {
  const { t_i18n } = useFormatter();
  const config = usePlatformConfig();
  const isXtmOne = useIsXtmOne();
  const agenticAIEnabled = useAgenticAIEnabled();
  const agenticQuotas = useAgenticQuotas();
  const agenticSource = useAgenticSource();
  const { isAdmin } = useAuth();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [codeDialogOpen, setCodeDialogOpen] = useState(false);

  const xtmLicense = config?.xtm_license;
  // The badges below are `EditionBadge`: the edition in XTM One mode, the
  // licence state (Licensed / Community) in Copilot mode (#9448). Enterprise
  // Edition inherited from a connected EE platform runs on the included
  // allowance; the banner then invites a license for capacity.
  const onIncludedAllowance = isXtmOne && agenticSource === "ee_platform";

  // Licensed state
  if (agenticAIEnabled) {
    const customer = config?.license_customer || xtmLicense?.customer || t_i18n("Your organization");
    const licenseType = config?.license_type || xtmLicense?.license_type || null;
    const expiration = config?.license_expiration || xtmLicense?.expiration || null;
    const isExpiringSoon = xtmLicense?.expiration_prevention ?? false;

    return (
      <Card className="border-accent/20 bg-accent/[0.03]">
        <CardContent className="flex items-start gap-4 py-4">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent/10">
            <ShieldCheck className="h-5 w-5 text-accent" />
          </div>
          <div className="flex-1 min-w-0 space-y-2">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="content-base font-semibold">
                {isXtmOne ? "XTM One" : t_i18n("Enterprise Edition")}
              </span>
              <EditionBadge
                className="content-caption"
                icon={<CheckCircle2 className="h-2.5 w-2.5 mr-0.5" />}
              />
              {licenseType && (
                <Badge variant="outline" className="content-caption">
                  <Crown className="h-2.5 w-2.5 mr-0.5" /> {licenseType}
                </Badge>
              )}
              {expiration && (
                <Badge
                  variant="outline"
                  className={cn(
                    "content-caption",
                    isExpiringSoon && "text-feedback-warning-primary border-feedback-alert-primary/30",
                  )}
                >
                  <Clock className="h-2.5 w-2.5 mr-0.5" />
                  {t_i18n("Expires {date}", { date: formatDate(expiration) })}
                </Badge>
              )}
            </div>
            {onIncludedAllowance ? (
              <p className="content-compact text-default-secondary">
                {t_i18n("Enterprise Edition through your connected Enterprise Edition platform. Every capability is active; the included agentic allowance applies, shared across the deployment - install a Filigran license to carry your own capacity:")}
              </p>
            ) : (
              <p className="content-compact text-default-secondary">
                {t_i18n("Licensed to")} <span className="text-default-primary font-medium">{customer}</span>.
                {" "}{t_i18n("Agentic execution quotas:")}
              </p>
            )}
            <div className="flex items-center gap-4 pt-1">
              {[
                { label: "Daily", value: agenticQuotas.daily },
                { label: "Monthly", value: agenticQuotas.monthly },
                { label: "Yearly", value: agenticQuotas.yearly },
              ].map((t) => (
                <div key={t.label} className="flex items-center gap-1.5">
                  <Zap className="h-3 w-3 text-accent" />
                  <span className="content-compact font-medium text-accent tabular-nums">{t.value != null ? formatCompactNumber(t.value) : "∞"}</span>
                  <span className="content-caption text-default-secondary">/ {t_i18n(t.label).toLowerCase()}</span>
                </div>
              ))}
            </div>
            {/* The counters these ceilings are compared with belong to the
                license, so no admin action resets them; a Filigran support
                code can, e.g. for a proof of concept that used the allowance
                before it ended. The same dialog takes a pool recharge
                code too: the code says what it grants. */}
            {isAdmin && (
              <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
                <p className="content-compact text-default-secondary leading-relaxed">
                  {t_i18n("Filigran support can issue a code that resets this platform's usage counters, for example when a proof of concept used its allowance before it ended.")}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="content-compact gap-1.5 h-7 shrink-0"
                  onClick={() => setCodeDialogOpen(true)}
                >
                  <LifeBuoy className="h-3 w-3" /> {t_i18n("Enter a support code")}
                </Button>
              </div>
            )}
          </div>
        </CardContent>
        <SupportCodeDialog open={codeDialogOpen} onOpenChange={setCodeDialogOpen} />
      </Card>
    );
  }

  // Community Edition (XTM One) / no valid license (Copilot): AI is off, so
  // there is no ceiling to show - the retired "default fallback quotas" list
  // described the former Starter Pack, which is now Enterprise Edition above.
  return (
    <>
      <Card className="border-feedback-alert-primary/20 bg-feedback-alert-primary/[0.03]">
        <CardContent className="py-4 space-y-3">
          <div className="flex items-start gap-4">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-alert-primary/10">
              <AlertTriangle className="h-5 w-5 text-feedback-warning-primary" />
            </div>
            <div className="flex-1 min-w-0 space-y-2">
              <div className="flex items-center gap-2">
                <span className="content-base font-semibold">
                  {isXtmOne ? t_i18n("AI is not activated") : t_i18n("No Enterprise License")}
                </span>
                <EditionBadge className="content-caption" />
              </div>
              <p className="content-compact text-default-secondary">
                {isXtmOne
                  ? t_i18n("This deployment runs Community Edition: no agentic execution runs and there is no quota to allocate. Connect an Enterprise Edition OpenCTI, OpenAEV or OpenCRQ platform, or activate a Filigran license, to unlock agents, chat and automations - and this page.")
                  : t_i18n("Install a valid Filigran license to enable agentic execution and configure quotas.")}
              </p>
              <div className="flex items-center gap-2 pt-1">
                <Button
                  size="sm"
                  className="content-compact gap-1.5 h-7"
                  onClick={() => window.open(XTM_ONE_TRIAL_URL, "_blank", "noopener,noreferrer")}
                >
                  <ExternalLink className="h-3 w-3" /> {t_i18n("Start free trial")}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="content-compact gap-1.5 h-7"
                  onClick={() => window.open(FILIGRAN_CONTACT_URL, "_blank", "noopener,noreferrer")}
                >
                  <ExternalLink className="h-3 w-3" /> {t_i18n("Contact sales")}
                </Button>
                {isAdmin && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="content-compact gap-1.5 h-7 text-default-secondary"
                    onClick={() => setDialogOpen(true)}
                  >
                    <KeyRound className="h-3 w-3" /> {t_i18n("Activate License")}
                  </Button>
                )}
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      <LicenseActivationDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        platformName={config?.platform_name}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Main QuotasSection
// ---------------------------------------------------------------------------

export default function QuotasSection() {
  const { t_i18n } = useFormatter();
  const { data } = usePlatformQuotas();
  const { data: models } = useAvailableModels({ scope: "all" });
  const { data: monitoring, isFetching } = useQuotaMonitoring();
  const updateQuotas = useUpdatePlatformQuotas();
  const { toast } = useToast();
  const isXtmOne = useIsXtmOne();
  const agenticSource = useAgenticSource();
  const xtmQuotas = useAgenticQuotas();
  // Community Edition (no Filigran license, no EE platform) — every editable
  // widget on this page locks, and gauges render "Not Applicable" instead of
  // a misleading zero / "No platform limit", because agentic execution is
  // server-side disabled until a license or registered EE platform arrives.
  // See ``useIsCommunityEdition`` and the LicenseStatusBanner above which
  // already explains the state textually.
  const isCeOnly = useIsCommunityEdition();

  const serverLLMProxy = data?.quota_config?.llm_proxy ?? null;
  const serverAgentic = data?.quota_config?.agentic ?? null;

  // The platform-wide agentic cap is set by something the admin can't
  // edit when the cap comes from the Filigran license (``xtm_license``) or
  // from the included allowance (``ee_platform``). In both cases we want
  // the platform-wide limit input to be **disabled** — the cap is enforced
  // by the backend, the admin can only tune the per-user default. ``none``
  // and ``default`` sources leave the input editable so an admin can
  // install a custom platform-wide cap.
  const platformAgenticCapLocked =
    isXtmOne
    && (agenticSource === "xtm_license" || agenticSource === "ee_platform");
  const xtmPrimaryLimit = xtmQuotas.daily || xtmQuotas.monthly || xtmQuotas.yearly || 0;
  const xtmPrimaryPeriod: "daily" | "monthly" | "yearly" =
    xtmQuotas.daily ? "daily" : xtmQuotas.monthly ? "monthly" : "yearly";

  // Source-specific hint shown under the disabled platform-wide input —
  // the LicenseStatusBanner already explains *why* the field is locked,
  // so this is just a brief inline reminder that points there for
  // upgrade options.
  const platformAgenticHint = platformAgenticCapLocked
    ? agenticSource === "xtm_license"
      ? t_i18n("Defined by your Filigran license. This global cap applies across all users and cannot be modified: see the license banner above for details.")
      : t_i18n("Default agentic execution quotas apply because no Filigran license is installed. Activate a license to lift the cap or configure your own custom platform-wide limits.")
    : undefined;

  const mergePlatformLimit = (cfg: QuotaTypeConfig | null): QuotaTypeConfig | null => {
    if (!platformAgenticCapLocked || xtmPrimaryLimit <= 0) return cfg;
    return {
      ...cfg,
      platform_limit: xtmPrimaryLimit,
      period: cfg?.period ?? xtmPrimaryPeriod,
    };
  };

  const [localLLMProxy, setLocalLLMProxy] = useState<QuotaTypeConfig | null>(null);
  const [localAgentic, setLocalAgentic] = useState<QuotaTypeConfig | null>(null);
  const [dirty, setDirty] = useState(false);

  const llmProxy = dirty ? localLLMProxy : serverLLMProxy;
  const rawAgentic = dirty ? localAgentic : serverAgentic;
  const agentic = platformAgenticCapLocked ? mergePlatformLimit(rawAgentic) : rawAgentic;

  const handleSave = () => {
    // When the platform-wide cap is owned by the license / default
    // fallback, never let the admin's stale "platform_limit" reach
    // the backend — strip it on save.
    const saveAgentic = platformAgenticCapLocked
      ? { ...rawAgentic, platform_limit: undefined }
      : rawAgentic;
    updateQuotas.mutate(
      { llm_proxy: llmProxy, agentic: saveAgentic },
      {
        onSuccess: () => {
          toast({ variant: "success", title: t_i18n("Saved"), description: t_i18n("Platform quotas updated") });
          setDirty(false);
        },
        onError: () => toast({ variant: "error", title: t_i18n("Error"), description: t_i18n("Failed to update quotas") }),
      },
    );
  };

  const allQuotas = monitoring?.quotas ?? [];

  const pickPlatformQuota = (type: string): QuotaSummary | null => {
    const typed = allQuotas.filter((q) => q.type === type);
    const global = typed.find((q) => q.is_global);
    if (global) return global;
    const any = typed[0];
    if (!any) return null;
    return { ...any, limit: null, is_global: true };
  };

  // Sandbox has no row of its own — its platform-wide usage is folded into
  // the displayed agentic number instead (spec
  // 002-sandbox-quota-usage-tracking FR-013/FR-014, revised). The ledger,
  // per-type period resolution, and Prometheus /metrics all keep sandbox
  // and agentic fully distinct — only this display-layer number is merged,
  // and only when both are bucketed under the same period: this platform-wide
  // sandbox row always scans a fixed monthly bucket (FR-011, revised — no
  // configurable period of its own), so an admin-configured daily/yearly
  // agentic quota must not be summed with it (or vice versa).
  const sandboxQuota = pickPlatformQuota("sandbox");

  const agenticGaugeRaw = pickPlatformQuota("agentic");
  const agenticGauge = agenticGaugeRaw
    ? {
        ...agenticGaugeRaw,
        current_usage:
          agenticGaugeRaw.current_usage +
          (sandboxQuota && sandboxQuota.period === agenticGaugeRaw.period ? sandboxQuota.current_usage : 0),
      }
    : null;
  const llmProxyGauge = isXtmOne ? null : pickPlatformQuota("llm_proxy");

  const agenticQuotas = allQuotas
    .filter((q) => q.type === "agentic")
    .map((q) =>
      sandboxQuota && q.period === sandboxQuota.period
        ? { ...q, current_usage: q.current_usage + sandboxQuota.current_usage }
        : q,
    );
  // Show the 3-gauge daily/monthly/yearly layout whenever the source
  // provides per-period caps — that's the case for both the XTM
  // license (``xtm_license``) and the no-license fallback
  // (``ee_platform``). Without this, the EE-platform fallback would
  // render as a single gauge with "No platform limit" copy even though
  // the 30/100/300 ceilings are actively enforced.
  const isMultiGauge = isXtmOne && agenticQuotas.length > 1;

  const agenticConsumers = monitoring?.top_consumers?.agentic ?? [];
  const llmProxyConsumers = monitoring?.top_consumers?.llm_proxy ?? [];
  const overQuotaUsers = monitoring?.over_quota_users ?? {};
  const hasConsumers = agenticConsumers.length > 0 || llmProxyConsumers.length > 0;
  const aiOff = useAiActionDisabled();

  return (
    <div className="space-y-6">
      <AiDisabledNotice />
      {/* Header — source-aware copy so admins know *who* owns each
          number (license / default fallback / their own settings)
          without having to click into the editor cards. */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold font-sans-geologica">{t_i18n("Quotas")}</h2>
          <p className="content-base text-default-secondary">
            {isXtmOne
              ? agenticSource === "xtm_license"
                ? t_i18n("Platform-wide agentic execution quotas are defined by your Filigran license. You can set default per-user quotas below, and override them at the group or user level.")
                : agenticSource === "ee_platform"
                  ? t_i18n("Enterprise Edition through your connected platform: the included agentic allowance (30 daily · 100 monthly · 300 yearly) applies because no Filigran license is installed. The platform-wide cap is read-only; install a license to carry your own capacity. You can still set per-user defaults and group/user overrides below.")
                  : t_i18n("Community Edition: AI is not activated, so no agentic execution runs and the quota controls below are locked. Connect an Enterprise Edition platform or install a Filigran license to unlock them.")
              : t_i18n("Define platform-wide quotas and default per-user quotas applied to all users on the platform. Individual users or groups can override the per-user defaults. Resolution order: user override > group > platform default.")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isFetching && <Spinner size="sm" tone="inherit" className="text-accent" />}
          {/* Only the Save is an AI write. The period switchers, the history
              charts and the licence CTAs below are reads and licence actions —
              a blanket <fieldset disabled> over this section took those away
              too, including "Activate License" (FR-022). */}
          <AiDisabledTip>
            <Tip content={isCeOnly ? t_i18n("Activate a license or register an EE platform to configure quotas.") : ""} wrap={isCeOnly}>
              <Button
                onClick={handleSave}
                disabled={isCeOnly || !dirty || updateQuotas.isPending || aiOff}
                className="gap-2"
              >
                <Save className="h-4 w-4" />
                {t_i18n("Save")}
              </Button>
            </Tip>
          </AiDisabledTip>
        </div>
      </div>

      {/* License status banner */}
      <LicenseStatusBanner />

      {/* Quota editor (definition) */}
      <QuotaEditor
        llmProxy={llmProxy}
        agentic={agentic}
        onChangeLLMProxy={(v) => { if (!dirty) setLocalAgentic(rawAgentic); setLocalLLMProxy(v); setDirty(true); }}
        onChangeAgentic={(v) => {
          if (!dirty) setLocalLLMProxy(serverLLMProxy);
          if (platformAgenticCapLocked) {
            // The platform-wide cap is owned by the license / default
            // fallback — never let the admin's edit of the per-user
            // default sneak a platform_limit change through.
            const { platform_limit: _drop, ...rest } = v ?? {};
            setLocalAgentic(Object.keys(rest).length ? rest as QuotaTypeConfig : null);
          } else {
            setLocalAgentic(v);
          }
          setDirty(true);
        }}
        availableModels={models}
        hideLLMProxy={isXtmOne}
        showPlatformLimit
        agenticPlatformLimitDisabled={platformAgenticCapLocked}
        agenticPlatformLimitHint={platformAgenticHint}
        disabled={isCeOnly}
        ceOnly={isCeOnly}
        notices={data?.notices}
      />

      {/* Agent failure pool (env-sized; recharged with a support code) */}
      <AgentFailurePoolCard />

      {/* Monitoring section */}
      {monitoring && (
        <div className="space-y-6">
          <div>
            <h3 className="text-lg font-semibold font-sans-geologica">{t_i18n("Consumption Monitoring")}</h3>
            <p className="content-base text-default-secondary">
              {isXtmOne && agenticSource === "xtm_license"
                ? t_i18n("Real-time agentic usage against the daily, monthly and yearly caps from your Filigran license.")
                : isXtmOne && agenticSource === "ee_platform"
                  ? t_i18n("Real-time agentic usage against the included 30 daily · 100 monthly · 300 yearly allowance. Install a Filigran license to carry your own capacity.")
                  : t_i18n("Real-time quota usage across all platform users.")}
            </p>
          </div>

          {/* KPI progress cards */}
          {isMultiGauge ? (
            <div className="grid gap-4 sm:grid-cols-3 stagger-children">
              {agenticQuotas.map((q, i) => (
                <CompactGaugeCard key={`${q.period}-${q.is_global}-${i}`} quota={q} ceOnly={isCeOnly} />
              ))}
            </div>
          ) : (
            <div className={cn(
              "grid gap-4 stagger-children",
              isXtmOne ? "grid-cols-1" : "sm:grid-cols-2",
            )}>
              {agenticGauge && <QuotaProgressCard key="agentic-platform" quota={agenticGauge} ceOnly={isCeOnly} />}
              {llmProxyGauge && <QuotaProgressCard key="llm-platform" quota={llmProxyGauge} ceOnly={isCeOnly} />}
            </div>
          )}

          {/* Historical bar chart + Users over quota (50/50) */}
          <div className="grid gap-4 sm:grid-cols-2">
            <HistoryChart history={monitoring.history ?? []} />
            <OverQuotaCard overQuotaUsers={overQuotaUsers} isXtmOne={isXtmOne} />
          </div>

          {/* Top consumers */}
          {hasConsumers && (
            <div className={cn(
              "grid gap-4 stagger-children",
              isXtmOne ? "grid-cols-1" : "sm:grid-cols-2",
            )}>
              {agenticConsumers.length > 0 && (
                <TopConsumersCard type="agentic" consumers={agenticConsumers} />
              )}
              {!isXtmOne && llmProxyConsumers.length > 0 && (
                <TopConsumersCard type="llm_proxy" consumers={llmProxyConsumers} />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
