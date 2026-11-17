// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";
import { AlertTriangle, Clock3, Coins, Gauge, Shapes, Sigma, type LucideIcon } from "lucide-react";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { Card } from "@/components/ui/card";
import { DiffBadge } from "@/components/ui/diff-badge";
import { MaskedCost } from "@/components/ui/masked-cost";
import { Skeleton } from "@/components/ui/skeleton";
import { Sparkline } from "@/components/ui/sparkline";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { formatCount, formatLatency, formatPercent, formatScore, formatUsd } from "@/pages/analytics/format";
import type { TraceAnalyticsSummary } from "@/types";

interface KpiProps {
  icon: LucideIcon;
  label: string;
  current: number | null;
  previous: number | null;
  formatter: (value: number) => string;
  previousLabel: string;
  invertColor?: boolean;
  isPercent?: boolean;
  /** Replaces the figure (a masked cost). */
  valueOverride?: ReactNode;
  hideDelta?: boolean;
  detail?: ReactNode;
  spark?: number[];
  sparkClassName?: string;
}

function Kpi({
  icon: Icon,
  label,
  current,
  previous,
  formatter,
  previousLabel,
  invertColor = false,
  isPercent = false,
  valueOverride,
  hideDelta = false,
  detail,
  spark,
  sparkClassName = "text-accent",
}: KpiProps) {
  const { t_i18n } = useFormatter();
  return (
    <Card className="flex flex-col gap-1 p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="truncate content-caption font-medium uppercase tracking-wider text-default-secondary">{t_i18n(label)}</p>
        <Icon className="h-4 w-4 shrink-0 text-default-disabled" aria-hidden />
      </div>
      <p className="text-2xl font-bold tabular-nums">
        {valueOverride ?? (current === null ? "-" : <AnimatedNumber value={current} formatter={formatter} />)}
      </p>
      <div className="flex min-h-5 flex-wrap items-center gap-x-2 gap-y-1">
        {!hideDelta && current !== null && previous !== null && (
          <DiffBadge
            current={current}
            previous={previous}
            label={t_i18n(previousLabel)}
            isPercent={isPercent}
            invertColor={invertColor}
            formatter={formatter}
          />
        )}
        {detail && <span className="content-caption text-default-secondary">{detail}</span>}
      </div>
      {spark && spark.length > 1 && <Sparkline values={spark} className={cn("mt-1", sparkClassName)} />}
    </Card>
  );
}

interface KpiStripProps {
  summary: TraceAnalyticsSummary | undefined;
  costDataAvailable: boolean;
  previousLabel: string;
  /** Daily totals for the sparklines, oldest first. */
  costSpark: number[];
  traceSpark: number[];
}

export function KpiStrip({ summary, costDataAvailable, previousLabel, costSpark, traceSpark }: KpiStripProps) {
  const { t_i18n } = useFormatter();
  if (!summary) {
    return (
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6">
        {Array.from({ length: 6 }).map((_, index) => (
          <Skeleton key={index} className="h-[132px] w-full rounded-sm" />
        ))}
      </div>
    );
  }
  const perTrace = summary.trace_count > 0 ? summary.total_cost / summary.trace_count : null;
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6 stagger-children">
      <Kpi
        icon={Coins}
        label="Total Cost"
        current={summary.total_cost}
        previous={summary.previous.total_cost}
        formatter={formatUsd}
        previousLabel={previousLabel}
        invertColor
        valueOverride={costDataAvailable ? undefined : <MaskedCost className="inline-flex" />}
        hideDelta={!costDataAvailable}
        detail={
          costDataAvailable && perTrace !== null
            ? t_i18n("{cost} per trace", { cost: formatUsd(perTrace) })
            : undefined
        }
        spark={costDataAvailable ? costSpark : undefined}
      />
      <Kpi
        icon={Shapes}
        label="Traces"
        current={summary.trace_count}
        previous={summary.previous.trace_count}
        formatter={formatCount}
        previousLabel={previousLabel}
        spark={traceSpark}
        sparkClassName="text-filigran-ia-primary"
      />
      <Kpi
        icon={Sigma}
        label="Total Tokens"
        current={summary.total_tokens}
        previous={summary.previous.total_tokens}
        formatter={formatCount}
        previousLabel={previousLabel}
      />
      <Kpi
        icon={Clock3}
        label="Avg Latency"
        current={summary.avg_duration_ms}
        previous={summary.previous.avg_duration_ms}
        formatter={formatLatency}
        previousLabel={previousLabel}
        invertColor
      />
      <Kpi
        icon={AlertTriangle}
        label="Error Rate"
        current={summary.error_rate * 100}
        previous={summary.previous.error_rate * 100}
        formatter={formatPercent}
        previousLabel={previousLabel}
        invertColor
        isPercent
      />
      <Kpi
        icon={Gauge}
        label="Avg Feedback (0-1)"
        current={summary.avg_feedback_score ?? null}
        previous={summary.previous.avg_feedback_score ?? null}
        formatter={formatScore}
        previousLabel={previousLabel}
      />
    </div>
  );
}
