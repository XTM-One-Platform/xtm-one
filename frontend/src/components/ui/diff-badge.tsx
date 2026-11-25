// SPDX-License-Identifier: Apache-2.0

import { TrendingUp, TrendingDown, Equal } from "lucide-react";
import { cn, formatCompactNumber } from "@/lib/utils";
import { Tip } from "@/components/ui/tooltip";

/**
 * DiffBadge — shows +N / -N / = compared to a previous period.
 *
 * Shared by the dashboards to keep their period comparisons visually consistent.
 * Shape: the chip geometry (`rounded-sm`, a 1 px edge, `px-2`, #1333). The
 * edge is transparent: the tint is the boundary, and the edge keeps the box
 * the same size as an outlined Badge beside it.
 */
export function DiffBadge({
  current,
  previous,
  label,
  isPercent = false,
  invertColor = false,
  formatter,
}: {
  current: number;
  previous: number;
  label: string;
  isPercent?: boolean;
  invertColor?: boolean;
  /**
   * Optional value formatter (the same one used for the KPI value). When set,
   * the diff magnitude and the tooltip's previous value are rendered through
   * it so a cost diff reads "+$552.06", a latency diff "+235ms", etc. The +/-
   * sign is added OUTSIDE the formatter so currency symbols and units stay
   * attached to the magnitude. Without it, diffs fall back to compact rounding.
   */
  formatter?: (value: number) => string;
}) {
  const diff = isPercent ? +(current - previous).toFixed(1) : current - previous;
  const isPositive = diff > 0;
  const isNeutral = diff === 0;
  // When invertColor is true, a positive diff is bad (red) and a negative diff is good (green).
  const isGood = invertColor ? !isPositive : isPositive;

  const formatValue = (value: number) =>
    formatter ? formatter(value) : isPercent ? `${value}%` : formatCompactNumber(value);

  return (
    <Tip content={`Was ${formatValue(previous)} ${label}`}>
      <span
        className={cn(
          "inline-flex items-center gap-0.5 content-caption font-medium rounded-sm border border-transparent px-2 py-0.5",
          isNeutral
            ? "bg-elevation-hover text-default-secondary"
            : isGood
            ? "bg-feedback-success-primary/10 text-feedback-success-primary"
            : "bg-feedback-error-primary/10 text-feedback-error-primary",
        )}
      >
        {isNeutral ? (
          <Equal className="h-2.5 w-2.5" />
        ) : isPositive ? (
          <TrendingUp className="h-2.5 w-2.5" />
        ) : (
          <TrendingDown className="h-2.5 w-2.5" />
        )}
        {!isNeutral && `${isPositive ? "+" : "-"}${formatValue(Math.abs(diff))}`}
      </span>
    </Tip>
  );
}
