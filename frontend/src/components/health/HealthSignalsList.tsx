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
 * The signals currently crossed (#8261): what was read, and whether a cycle started.
 * A bounded list (one row per signal kind and target): client-side search, filters and sort
 * on the shared data table (#9378, slice 6).
 */
import { Activity } from "lucide-react";
import { useCallback, useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { EmptyState } from "@/components/ui/empty-state";
import { useHealthName, useHealthSignals } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { HEALTH_SIGNAL_LABELS, healthRefusalKey, healthTargetHref, labelOf } from "@/lib/health";
import { cn } from "@/lib/utils";
import type { HealthSignal } from "@/types";

function reading(signal: HealthSignal): string {
  const value = signal.last_value?.value;
  const baseline = signal.last_value?.baseline;
  if (value === undefined || value === null) return "-";
  return baseline === undefined || baseline === null ? String(value) : `${baseline} → ${value}`;
}

/** English label of the kind of object a signal reads. */
function targetLabel(signal: HealthSignal): string {
  return signal.target_type === "flow" ? "Flow" : signal.target_type === "assignment" ? "Assignment" : "Agent";
}

/** English wording of a target that no longer exists (the API answers no name for it). */
function missingTargetLabel(signal: HealthSignal): string {
  return signal.target_type === "flow"
    ? "Flow no longer exists"
    : signal.target_type === "assignment"
      ? "Assignment no longer exists"
      : "Agent no longer exists";
}

/** Whether the signal started a cycle, as the Cycle filter reads it. */
const cycleState = (signal: HealthSignal) => (signal.cycle_id ? "started" : "not_started");

/**
 * Column widths in px under the fixed layout (UX round 2 on #10066, B1): Target takes what is
 * left and cuts a long name with its tooltip, so the table never scrolls sideways at 1024 and
 * every row keeps one line.
 */
const HEALTH_SIGNAL_COLUMN_WIDTHS = { signal: 144, reading: 112, cycle: 176, fired: 152 } as const;

export function HealthSignalsList() {
  const { t_i18n } = useFormatter();
  const healthName = useHealthName();
  const { data: signals = [], isLoading, isError, refetch } = useHealthSignals();

  const cycleLabel = useCallback(
    (signal: HealthSignal) =>
      signal.cycle_id
        ? t_i18n("Started")
        : t_i18n(healthRefusalKey({ detail: { code: signal.refusal } }), { name: healthName }),
    [t_i18n, healthName],
  );

  /** The target's name, or the named state of a target that no longer exists; never a raw id. */
  const targetName = useCallback(
    (signal: HealthSignal) => signal.target_name || t_i18n(missingTargetLabel(signal)),
    [t_i18n],
  );

  const columns = useMemo<DataTableColumn<HealthSignal>[]>(
    () => [
      {
        key: "signal",
        header: "Signal",
        width: HEALTH_SIGNAL_COLUMN_WIDTHS.signal,
        cell: (signal) => {
          const label = t_i18n(labelOf(HEALTH_SIGNAL_LABELS, signal.kind));
          return (
            <span className="block truncate whitespace-nowrap" title={label}>
              {label}
            </span>
          );
        },
        sortValue: (signal) => t_i18n(labelOf(HEALTH_SIGNAL_LABELS, signal.kind)),
        csv: (signal) => t_i18n(labelOf(HEALTH_SIGNAL_LABELS, signal.kind)),
      },
      {
        key: "target",
        header: "Target",
        // Plain text: the row opens the target (rowHref), no cell draws a link (#12070).
        cell: (signal) => {
          return (
            <span className="flex min-w-0 items-center gap-2">
              <span
                className={cn("block min-w-0 truncate", !signal.target_name && "text-default-secondary")}
                title={targetName(signal)}
              >
                {targetName(signal)}
              </span>
              {signal.target_name ? (
                <span className="shrink-0 whitespace-nowrap text-xs text-default-secondary">{t_i18n(targetLabel(signal))}</span>
              ) : null}
            </span>
          );
        },
        sortValue: (signal) => targetName(signal),
        // A target that no longer exists already names its type (A1 of UX round 2).
        csv: (signal) => (signal.target_name ? `${targetName(signal)} (${t_i18n(targetLabel(signal))})` : targetName(signal)),
      },
      {
        key: "reading",
        header: "Reading",
        hideable: true,
        width: HEALTH_SIGNAL_COLUMN_WIDTHS.reading,
        cell: (signal) => (
          <span className="block truncate whitespace-nowrap font-mono-plex" title={reading(signal)}>
            {reading(signal)}
          </span>
        ),
        csv: (signal) => reading(signal),
      },
      {
        key: "cycle",
        header: "Cycle",
        hideable: true,
        width: HEALTH_SIGNAL_COLUMN_WIDTHS.cycle,
        cell: (signal) => (
          <Badge variant={signal.cycle_id ? "cyan" : "warning"} className="max-w-full whitespace-nowrap" title={cycleLabel(signal)}>
            <span className="truncate">{cycleLabel(signal)}</span>
          </Badge>
        ),
        sortValue: (signal) => cycleLabel(signal),
        csv: (signal) => cycleLabel(signal),
      },
      {
        key: "fired",
        header: "Fired",
        hideable: true,
        width: HEALTH_SIGNAL_COLUMN_WIDTHS.fired,
        cell: (signal) => <DateTimeCell value={signal.fired_at} />,
        sortValue: (signal) => signal.fired_at ?? "",
        csv: (signal) => signal.fired_at ?? "",
      },
    ],
    [t_i18n, cycleLabel, targetName],
  );

  const filters = useMemo<DataTableFilter<HealthSignal>[]>(
    () => [
      {
        key: "signal",
        label: "Signal",
        values: (signal) => [signal.kind],
        options: Object.entries(HEALTH_SIGNAL_LABELS).map(([value, label]) => ({ value, label: t_i18n(label) })),
      },
      {
        key: "target",
        label: "Target",
        values: (signal) => [signal.target_type],
        options: [
          { value: "agent", label: t_i18n("Agent") },
          { value: "flow", label: t_i18n("Flow") },
          { value: "assignment", label: t_i18n("Assignment") },
        ],
      },
      {
        key: "cycle",
        label: "Cycle",
        values: (signal) => [cycleState(signal)],
        options: [
          { value: "started", label: t_i18n("Started") },
          { value: "not_started", label: t_i18n("Not started") },
        ],
      },
    ],
    [t_i18n],
  );

  if (!isLoading && !isError && !signals.length) {
    return (
      <EmptyState
        icon={Activity}
        title={t_i18n("No signal is crossed")}
        description={t_i18n("Signals are read without any model call, every 15 minutes for feedback and failures, hourly for cost and models.")}
      />
    );
  }
  return (
    <DataTable
      rows={signals}
      columns={columns}
      rowKey={(signal) => `${signal.kind}-${signal.target_id}`}
      rowHref={(signal) => (signal.target_name ? (healthTargetHref(signal.target_type, signal.target_id) ?? "") : "")}
      defaultSort={{ key: "fired", direction: "desc" }}
      layout="fixed"
      searchFields={(signal) => [
        t_i18n(labelOf(HEALTH_SIGNAL_LABELS, signal.kind)),
        targetName(signal),
        t_i18n(targetLabel(signal)),
        cycleLabel(signal),
      ]}
      searchPlaceholder={t_i18n("Search...")}
      filters={filters}
      exportName="concierge-signals"
      urlKey="cs_"
      storageKey="concierge-signals"
      isLoading={isLoading}
      isError={isError}
      errorLabel={t_i18n("{name}'s signals could not be loaded.", { name: healthName })}
      onRetry={() => void refetch()}
      emptyLabel={t_i18n("No signal is crossed")}
    />
  );
}
