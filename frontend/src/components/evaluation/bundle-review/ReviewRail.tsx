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

// The rail: the reviewed answer's Entity F1 on each object type (a click filters every
// view on that type) and the status filters of the mode.

import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { FILTER_KEYS, f1, formatScore, typeCounts } from "@/lib/evaluation-bundle-review";
import { cn } from "@/lib/utils";
import { ALONE_FILTER_LABELS, FILTER_LABELS, STATUS_COLOR, STATUS_DOT } from "./labels";
import type { ReviewContext } from "./context";
import type { DisplayStatus } from "@/lib/evaluation-bundle-review";

/** A 0-100 bar. */
function ScoreBar({ value, color }: { value: number; color: string }) {
  return (
    <svg viewBox="0 0 100 4" preserveAspectRatio="none" className="h-1 w-full" aria-hidden>
      <rect width="100" height="4" rx="2" style={{ fill: "var(--border-elevation-subtle)" }} />
      <rect width={Math.max(0, Math.min(100, value))} height="4" rx="2" style={{ fill: color }} />
    </svg>
  );
}

export function ReviewRail({
  ctx,
  onType,
  onToggle,
  onClear,
}: {
  ctx: ReviewContext;
  onType: (type: string | null) => void;
  onToggle: (key: string) => void;
  onClear: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { model, pair, whatIf, filters, mode } = ctx;
  const keys = FILTER_KEYS[mode];
  const labels: Record<string, string> = mode === "alone" ? ALONE_FILTER_LABELS : FILTER_LABELS;
  const filtered = !!filters.type || filters.hidden.size > 0;
  return (
    <nav aria-label={t_i18n("Filters")} className="space-y-4">
      <section>
        <h3 className="content-caption font-medium uppercase tracking-wider text-default-secondary mb-2">
          {t_i18n("Entity F1 by type")}
        </h3>
        <ul className="space-y-1">
          {model.types.map((type) => {
            const c = typeCounts(model, pair.under, whatIf, type);
            const empty = c.tp + c.fp + c.fn === 0;
            const v = f1(c);
            const tone: DisplayStatus = empty ? "same" : v >= 99.9 ? "tp" : v <= 0.1 ? "fp" : "fn";
            return (
              <li key={type}>
                <button
                  type="button"
                  aria-pressed={filters.type === type}
                  onClick={() => onType(filters.type === type ? null : type)}
                  title={
                    t_i18n("TP {tp} · FP {fp} · FN {fn}", { tp: c.tp, fp: c.fp, fn: c.fn }) +
                    (ctx.comparator ? ` · ${ctx.comparator.name} ${formatScore(f1(typeCounts(ctx.fullModel, ctx.comparator.key, whatIf, type)))}` : "")
                  }
                  className={cn(
                    "w-full rounded-sm px-2 py-1 text-left transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
                    filters.type === type ? "bg-elevation-highlight" : "hover:bg-elevation-hover",
                  )}
                >
                  <span className="flex items-center justify-between gap-2 content-compact">
                    <span className="truncate font-mono-plex">{type}</span>
                    <span className="tabular-nums text-default-secondary">{empty ? "—" : formatScore(v)}</span>
                  </span>
                  <ScoreBar value={empty ? 0 : v} color={STATUS_COLOR[tone]} />
                </button>
              </li>
            );
          })}
        </ul>
      </section>
      <section>
        <h3 className="content-caption font-medium uppercase tracking-wider text-default-secondary mb-2">{t_i18n("Status")}</h3>
        <div role="group" aria-label={t_i18n("Status")} className="flex flex-wrap gap-1">
          {keys.map((k) => {
            const on = !filters.hidden.has(k);
            return (
              <button
                key={k}
                type="button"
                aria-pressed={on}
                onClick={() => onToggle(k)}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-sm border border-elevation-subtle px-2 py-0.5 content-compact transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
                  on ? "text-default-primary" : "text-default-disabled line-through",
                )}
              >
                <span aria-hidden className={cn("h-2 w-2 rounded-full", STATUS_DOT[k as DisplayStatus])} />
                {t_i18n(labels[k])}
              </button>
            );
          })}
        </div>
      </section>
      <p className="content-compact text-default-secondary">
        {filtered ? (
          <>
            {filters.type && filters.hidden.size > 0
              ? t_i18n("Filtered on type {type}, {shown} of {total} statuses.", { type: filters.type, shown: keys.length - filters.hidden.size, total: keys.length })
              : filters.type
                ? t_i18n("Filtered on type {type}.", { type: filters.type })
                : t_i18n("{shown} of {total} statuses shown.", { shown: keys.length - filters.hidden.size, total: keys.length })}{" "}
            <Button variant="link" size="sm" className="h-auto p-0" onClick={onClear}>
              {t_i18n("Clear filters")}
            </Button>
          </>
        ) : (
          t_i18n("Click a type to filter every view.")
        )}
      </p>
    </nav>
  );
}
