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

/**
 * The Status filter of an Operations list, on the toolbar itself (#9378 slices
 * 8f and 8e): a one-click "Running" chip, then a multi-select Status pill with
 * the count of each status under the list's other filters. Executions and
 * Background Tasks draw it with their own statuses and test ids.
 *
 * Controlled: the page keeps the selection in the URL (`?status=a,b`).
 */

import { useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { FilterClearButton, FilterPillPanel, FilterPillTrigger, filterPillClass, useFilterPopover } from "@/components/ui/filter-pill";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/** The Status pill's glyph, from the design system's icon set (no direct lucide-react import). */
function StatusPillIcon({ className }: { className?: string }) {
  return <Icon name="list-checks" size={16} className={className} aria-hidden />;
}

/** The statuses a URL names (`?status=a,b`, else a legacy single value), in the list's order; unknown values dropped. */
export function statusesFromParam<S extends string>(statuses: readonly S[], status: string | null, legacy?: string | null): S[] {
  const raw = status ? status.split(",") : legacy ? [legacy] : [];
  return statuses.filter((s) => raw.some((value) => value.trim() === s));
}

interface Props<S extends string> {
  /** Every status the pill offers, in the order of a record's life; `running` gets the chip. */
  statuses: readonly S[];
  /** The translated label of each status. */
  labels: Record<S, string>;
  value: S[];
  onChange: (next: S[]) => void;
  /** Records per status under the other filters; no number while unknown. */
  counts?: Record<string, number>;
  /** Below 1440 px: the Running chip shows its count only beside the dot. */
  compact?: boolean;
  /** Prefix of the test ids: `<prefix>-status-filter`, `-running-chip`, `-status-pill`, `-status-<status>`. */
  testIdPrefix: string;
}

export function OperationsStatusFilter<S extends string>({ statuses, labels, value, onChange, counts, compact = false, testIdPrefix }: Props<S>) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useFilterPopover(ref, open, setOpen);

  const running = statuses.find((s) => s === "running");
  const runningOnly = !!running && value.length === 1 && value[0] === running;
  const runningCount = counts?.running;
  const toggle = (status: S) => onChange(statuses.filter((s) => (s === status ? !value.includes(s) : value.includes(s))));

  return (
    <div className="flex shrink-0 items-center gap-2" data-testid={`${testIdPrefix}-status-filter`}>
      {running && (
        <button
          type="button"
          aria-pressed={runningOnly}
          data-testid={`${testIdPrefix}-running-chip`}
          className={filterPillClass(runningOnly)}
          onClick={() => onChange(runningOnly ? [] : [running])}
        >
          <span className={cn("h-1.5 w-1.5 rounded-full", runningCount ? "bg-accent animate-pulse" : "bg-elevation-highlight")} aria-hidden />
          {!compact && labels[running]}
          {runningCount !== undefined && <span className="tabular-nums">{runningCount}</span>}
          {compact && <span className="sr-only">{labels[running]}</span>}
        </button>
      )}
      <div ref={ref} className="relative">
        <FilterPillTrigger
          icon={StatusPillIcon}
          label={t_i18n("Status")}
          open={open}
          count={value.length}
          aria-haspopup="true"
          data-testid={`${testIdPrefix}-status-pill`}
          onClick={() => setOpen(!open)}
        />
        {open && (
          <FilterPillPanel role="group" aria-label={t_i18n("Status filter")} className="w-60 p-1">
            {statuses.map((status) => {
              const selected = value.includes(status);
              const n = counts?.[status] ?? (counts ? 0 : undefined);
              return (
                <button
                  type="button"
                  key={status}
                  aria-pressed={selected}
                  data-testid={`${testIdPrefix}-status-${status}`}
                  onClick={() => toggle(status)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-sm px-2.5 py-1.5 text-left content-compact transition-colors",
                    selected ? "bg-accent/15 text-accent" : "hover:bg-elevation-hover",
                  )}
                >
                  <span className="min-w-0 flex-1 truncate">{labels[status]}</span>
                  {n !== undefined && <span className="tabular-nums text-default-secondary">{n}</span>}
                  <Icon name="check" size={16} className={cn("h-3.5 w-3.5 shrink-0", !selected && "invisible")} aria-hidden />
                </button>
              );
            })}
            {value.length > 0 && (
              <div className="mt-1 border-t border-elevation-subtle-soft pt-1">
                <FilterClearButton onClick={() => onChange([])}>{t_i18n("Clear")}</FilterClearButton>
              </div>
            )}
          </FilterPillPanel>
        )}
      </div>
    </div>
  );
}
