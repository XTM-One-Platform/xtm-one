// SPDX-License-Identifier: Apache-2.0

/**
 * The shape of a series in a KPI card: a line over a faint area, in the
 * text colour it is given (`currentColor`), no axis - the card's own figure
 * says the value.
 */
import { useId } from "react";
import { cn } from "@/lib/utils";

const WIDTH = 120;
const HEIGHT = 32;

export function Sparkline({ values, className }: { values: number[]; className?: string }) {
  const gradientId = useId();
  if (values.length < 2) return null;
  const max = Math.max(...values);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const x = (index: number) => (index / (values.length - 1)) * WIDTH;
  const y = (value: number) => HEIGHT - 2 - ((value - min) / span) * (HEIGHT - 4);
  const line = values.map((value, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none" className={cn("h-8 w-full", className)} aria-hidden>
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity={0.25} />
          <stop offset="100%" stopColor="currentColor" stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={`${line} L${WIDTH},${HEIGHT} L0,${HEIGHT} Z`} fill={`url(#${gradientId})`} />
      <path d={line} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
