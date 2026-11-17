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
 * The Status filter of Operations > Executions, on the toolbar itself (#9378
 * slice 8f): a one-click "Running" chip, then a multi-select Status pill
 * (Running, Awaiting input, Completed, Failed, Cancelled) with the count of
 * each status under the list's other filters. The shared
 * `OperationsStatusFilter` draws it (Background Tasks takes the same, 8e).
 *
 * Controlled: the page keeps the selection in the URL (`?status=a,b`).
 */

import { useFormatter } from "@/i18n";
import type { RunStatusCounts } from "@/hooks/api/useRunStatusCounts";
import { OperationsStatusFilter, statusesFromParam } from "./OperationsStatusFilter";

/** The statuses the pill offers, in the order of a run's life. */
const EXECUTION_STATUSES = ["running", "awaiting_human", "completed", "failed", "cancelled"] as const;
export type ExecutionStatus = (typeof EXECUTION_STATUSES)[number];

/** The statuses a URL names: `?status=a,b`, else the legacy single `?filter=<status>`; unknown values dropped. */
export function statusesFromParams(status: string | null, legacyFilter: string | null): ExecutionStatus[] {
  return statusesFromParam(EXECUTION_STATUSES, status, legacyFilter);
}

interface Props {
  value: ExecutionStatus[];
  onChange: (next: ExecutionStatus[]) => void;
  /** Runs per status under the other filters; no number while unknown. */
  counts?: RunStatusCounts;
  /** Below 1440 px: the Running chip shows its count only beside the dot. */
  compact?: boolean;
}

export function ExecutionStatusFilter({ value, onChange, counts, compact = false }: Props) {
  const { t_i18n } = useFormatter();
  const labels: Record<ExecutionStatus, string> = {
    running: t_i18n("Running"),
    awaiting_human: t_i18n("Awaiting input"),
    completed: t_i18n("Completed"),
    failed: t_i18n("Failed"),
    cancelled: t_i18n("Cancelled"),
  };
  return (
    <OperationsStatusFilter
      statuses={EXECUTION_STATUSES}
      labels={labels}
      value={value}
      onChange={onChange}
      counts={counts}
      compact={compact}
      testIdPrefix="executions"
    />
  );
}
