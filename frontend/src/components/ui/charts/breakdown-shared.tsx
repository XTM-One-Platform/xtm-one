import type { ReactNode } from "react";
import { MaskedCost } from "@/components/ui/masked-cost";
import { Tip } from "@/components/ui/tooltip";
import { cn, formatCompactCost } from "@/lib/utils";

/** A cost cell, on the compact scale of the LLM Analytics figures; the CSV keeps the raw amount. */
export function CostValue({ value, available }: { value: number; available: boolean }) {
  return available ? <>{formatCompactCost(value)}</> : <MaskedCost className="inline-flex" />;
}

/** Text whose explanation lives in a tooltip, reachable from the keyboard as well as on hover. */
export function HintText({ hint, className, children }: { hint: string; className?: string; children: ReactNode }) {
  return (
    <Tip content={hint}>
      <span
        tabIndex={0}
        className={cn("cursor-help rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus", className)}
      >
        {children}
      </span>
    </Tip>
  );
}

/** One row's share of the window's total cost: a bar plus the percentage. */
export function CostShare({ cost, totalCost, available }: { cost: number; totalCost: number; available: boolean }) {
  if (!available) return <MaskedCost className="inline-flex" />;
  const percent = totalCost > 0 ? Math.min(100, (cost / totalCost) * 100) : 0;
  return (
    <div className="flex items-center justify-end gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-elevation-hover" aria-hidden="true">
        <div className="h-full rounded-full bg-elevation-highlight" style={{ width: `${percent}%` }} />
      </div>
      <span className="w-11 text-right tabular-nums">
        {percent > 0 && percent < 0.1 ? "<0.1%" : `${percent.toFixed(1)}%`}
      </span>
    </div>
  );
}
