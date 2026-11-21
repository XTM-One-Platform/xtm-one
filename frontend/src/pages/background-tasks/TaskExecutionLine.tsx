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
 * The execution line of a pending or running background task (line 2 of its
 * Operations row, one line that never wraps). Its parts sit in an
 * `OpsChipStrip`: a part that does not fit whole is hidden whole and made
 * inert, never shrunk under its label or painted over its neighbour, and the
 * row's hover card names every id whole. The parts come in priority order, so
 * the last ones go first on a narrow row: the "No live execution" flag, the
 * state, the last progress, then the run and the orchestration ids (the run is
 * also the row's meta token). The
 * state of its orchestration execution, the run it belongs to (a link to the
 * run page), the execution id and its last progress, read from the fields the
 * list endpoint adds.
 *
 * A task whose execution is missing or no longer pending / running carries the
 * "No live execution" flag: the rule of the `list_background_tasks` tool line
 * (`task_liveness_suffix`), the zombie the sweep settles.
 */
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { OpsChipStrip } from "@/components/ui/operations-list";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import type { SubAgentTask } from "@/types";
import { hasNoLiveExecution, OPEN_TASK_STATUSES } from "@/pages/background-tasks/liveness";
import { formatTimeAgo } from "@/lib/relative-time";

/** What the execution state reads as. @i18n-keys */
const EXECUTION_STATE_LABELS = {
  none: "No execution",
  pending: "Execution pending",
  running: "Execution running",
  completed: "Execution completed",
  failed: "Execution failed",
  dead_letter: "Execution dead-lettered",
  cancelled: "Execution cancelled",
} as const;

export function TaskExecutionLine({ task, nowMs }: { task: SubAgentTask; nowMs?: number }) {
  const { t_i18n, intl } = useFormatter();
  if (!OPEN_TASK_STATUSES.has(task.status) || !task.execution_state) return null;
  const zombie = hasNoLiveExecution(task);
  const stateLabel: string | undefined =
    EXECUTION_STATE_LABELS[task.execution_state as keyof typeof EXECUTION_STATE_LABELS];
  const runId = task.assignment_run_id;
  const progress = task.last_progress_at;
  return (
    <div
      className="flex min-w-0 items-center overflow-hidden whitespace-nowrap content-compact text-default-secondary"
      data-testid="task-execution-line"
      data-no-live-execution={zombie ? "true" : undefined}
    >
      {/* Every part is whole or hidden whole (OpsChipStrip): a part never
          shrinks under its label into the next one. Priority order: the parts
          at the end go first on a narrow row; the hover card holds them all. */}
      <OpsChipStrip testId="task-execution-parts">
        {zombie && (
          <Tip
            content={t_i18n(
              "This task is still marked in flight but no execution is running it. The recovery sweep settles it; kill it to settle it now.",
            )}
          >
            <Badge variant="destructive" tabIndex={0} className="gap-1 content-compact cursor-default focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus">
              <Icon name="triangle-alert" size={16} />
              {t_i18n("No live execution")}
            </Badge>
          </Tip>
        )}
        {/* A zombie with no execution at all: the flag already says it, so the
            state ("No execution") is not repeated next to it. */}
        {!(zombie && task.execution_state === "none") && (
          <span>{stateLabel ? t_i18n(stateLabel) : task.execution_state}</span>
        )}
        {progress && (
          <span title={intl.formatDate(progress, { dateStyle: "short", timeStyle: "short" })}>
            {t_i18n("Last progress {when}", { when: formatTimeAgo(intl, progress, nowMs) })}
          </span>
        )}
        <span className="inline-flex items-center gap-1" data-testid="task-execution-run">
          <span>{t_i18n("Run")}</span>
          {runId ? (
            <Link
              to={`/operations/executions/${runId}`}
              onClick={(e) => e.stopPropagation()}
              className="font-mono text-accent hover:underline rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              title={runId}
            >
              {runId.slice(0, 8)}
            </Link>
          ) : (
            <span>{t_i18n("none")}</span>
          )}
        </span>
        {task.execution_id && (
          <span className="inline-flex items-center gap-1" data-testid="task-execution-orchestration">
            {/* "Orchestration", not "Execution": fr, es and ja translate "Run" and
                "Execution" the same way, so the two ids would read alike. */}
            <span>{t_i18n("Orchestration")}</span>
            <span className="font-mono" title={task.execution_id}>
              {task.execution_id.slice(0, 8)}
            </span>
          </span>
        )}
      </OpsChipStrip>
    </div>
  );
}
