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
 * The pure parts of the runs Activity tabs (#9378 slice 4a): the server order of each
 * sortable column, the order an URL names (old Activity tab links included), the Type
 * column's label and the ROI review options shared with a fleet's runs list.
 */
import type { ExecutionRunSort, RunReviewFilter } from "@/hooks/api/useAssignmentApi";
import { TRIGGER_LABELS } from "@/lib/constants";
import type { SortDirection } from "@/lib/table-rows";
import type { AssignmentRunDetail } from "@/types";

export const RUN_DEFAULT_SORT = "started";
export const RUN_DEFAULT_DIR: SortDirection = "desc";

/** The server order of each sortable column and direction (`?sort=` of /assignments/runs/all). */
export const RUN_SORTS: Record<string, Record<SortDirection, ExecutionRunSort>> = {
  started: { desc: "newest", asc: "oldest" },
  status: { asc: "status", desc: "status_desc" },
  agent: { asc: "agent", desc: "agent_desc" },
  duration: { desc: "longest", asc: "shortest" },
};

/** The ROI review filter copy by segment, read through `t_i18n` by SegmentedFilter. @i18n-keys */
const REVIEW_COPY = {
  all: { label: "All", hint: "Every run." },
  unreviewed: { label: "Unreviewed", hint: "Runs whose ROI outcome nobody has reviewed yet." },
} as const satisfies Record<"all" | RunReviewFilter, { label: string; hint: string }>;

/** All / Unreviewed, shared by the Activity tabs and a fleet's runs list (#9837). */
export const REVIEW_OPTIONS = (["all", "unreviewed"] as const).map((key) => ({ key, ...REVIEW_COPY[key] }));

/**
 * The column and direction a URL names. `sort` and `dir` as the table writes them;
 * an Activity tab link written before the table (`?sort=oldest`, `?sort=status`,
 * `?sort=newest`, no `dir`) reads as the order it named.
 */
export function runSortFromParams(sort: string | null, dir: string | null): { key: string; direction: SortDirection } {
  if (sort === "oldest") return { key: RUN_DEFAULT_SORT, direction: "asc" };
  if (sort === "newest") return { key: RUN_DEFAULT_SORT, direction: "desc" };
  const key = sort && RUN_SORTS[sort] ? sort : RUN_DEFAULT_SORT;
  const direction: SortDirection = dir === "asc" ? "asc" : dir === "desc" ? "desc" : key === "status" ? "asc" : RUN_DEFAULT_DIR;
  return { key, direction };
}

/** What started the run, the Type column: an A2A partner, a delegation, else the trigger type. */
export function runTypeLabel(run: Pick<AssignmentRunDetail, "trigger_context" | "parent_run_id">): string {
  const context = run.trigger_context ?? {};
  if (context.source === "a2a") return "A2A";
  if (run.parent_run_id) return "Delegation";
  const type = typeof context.type === "string" ? context.type : "";
  if (type === "scheduled_work_item") return "Work item";
  return TRIGGER_LABELS[type] ?? (type ? "Other" : "Manual");
}

/** Seconds a run took, or null while it has not ended. */
export function runDurationSeconds(run: Pick<AssignmentRunDetail, "started_at" | "completed_at">): number | null {
  if (!run.started_at || !run.completed_at) return null;
  return (new Date(run.completed_at).getTime() - new Date(run.started_at).getTime()) / 1000;
}
