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

import type { SubAgentTask } from "@/types";

/** An execution state that still runs a task. */
const LIVE_EXECUTION_STATES: ReadonlySet<string> = new Set(["pending", "running"]);
/** A task status the list carries execution fields for. */
export const OPEN_TASK_STATUSES: ReadonlySet<string> = new Set(["pending", "running"]);

/**
 * Whether *task* is pending or running with no live execution behind it:
 * the rule of the `list_background_tasks` tool line
 * (`task_liveness_suffix`), the zombie the recovery sweep settles.
 */
export function hasNoLiveExecution(task: Pick<SubAgentTask, "status" | "execution_state">): boolean {
  if (!OPEN_TASK_STATUSES.has(task.status) || !task.execution_state) return false;
  return !LIVE_EXECUTION_STATES.has(task.execution_state);
}
