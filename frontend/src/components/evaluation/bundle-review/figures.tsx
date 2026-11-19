/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

// The small figures of the run review: a change against what the run is compared with,
// a 0-100 bar, and a figure tile (label, value, change, a line under it).

import type { ReactNode } from "react";
import { useFormatter } from "@/i18n";
import { formatScore } from "@/lib/evaluation-bundle-review";
import { cn } from "@/lib/utils";

/** The change from `was` to `now`: green up, red down, neutral within 0.05. */
export function Delta({ now, was, vs }: { now: number | null | undefined; was: number | null | undefined; vs: string }) {
  const { t_i18n } = useFormatter();
  if (now == null || was == null) return null;
  const d = now - was;
  const flat = Math.abs(d) < 0.05;
  const sign = flat ? "±" : d > 0 ? "+" : "−";
  const tip = t_i18n("vs {label} ({was})", { label: vs, was: formatScore(was) });
  return (
    <span
      title={tip}
      aria-label={`${sign}${formatScore(Math.abs(d))} ${tip}`}
      className={cn(
        "rounded-sm px-1 content-caption font-semibold tabular-nums",
        flat ? "text-default-secondary" : d > 0 ? "text-feedback-success-primary bg-feedback-success-primary/10" : "text-feedback-error-primary bg-feedback-error-primary/10",
      )}
    >
      {sign}
      {formatScore(Math.abs(d))}
    </span>
  );
}

/** A 0-100 bar in a status colour (a CSS variable). */
export function Bar({ value, color = "var(--color-accent)" }: { value: number | null | undefined; color?: string }) {
  const v = Math.max(0, Math.min(100, value ?? 0));
  return (
    <svg viewBox="0 0 100 4" preserveAspectRatio="none" className="h-1 w-full" aria-hidden>
      <rect width="100" height="4" rx="2" style={{ fill: "var(--border-elevation-subtle)" }} />
      <rect width={v} height="4" rx="2" style={{ fill: color }} />
    </svg>
  );
}

/** A figure tile: its label, its value with what the value changed by, a line under it. */
export function Tile({
  label,
  value,
  was,
  delta,
  sub,
  highlight,
  children,
}: {
  label: string;
  value?: string;
  /** The official figure the what-if replaced, struck through. */
  was?: string | null;
  delta?: ReactNode;
  sub?: ReactNode;
  highlight?: boolean;
  children?: ReactNode;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1 rounded-sm border border-elevation-subtle bg-elevation-default p-3", highlight && "border-dashed border-feedback-success-primary")}>
      <span className="content-caption font-medium uppercase tracking-wider text-default-secondary">{label}</span>
      {value !== undefined && (
        <span className="flex flex-wrap items-baseline gap-2">
          <span className="content-base-bold tabular-nums">{value}</span>
          {was && <s className="content-caption tabular-nums text-default-secondary">{was}</s>}
          {delta}
        </span>
      )}
      {children}
      {sub && <span className="content-caption text-default-secondary">{sub}</span>}
    </div>
  );
}
