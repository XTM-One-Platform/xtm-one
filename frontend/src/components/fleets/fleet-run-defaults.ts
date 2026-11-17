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
 * The fleet settings panel's "Run defaults": the
 * budget limits and the iteration cap the home members run on when they set
 * none of their own (`PUT /fleets/{id}` `budget_config` / `max_iterations`).
 * An empty field inherits the platform; `null` clears the default.
 */
import {
  type LimitsDraft,
  limitsDraft,
  limitsPayload,
  parseIterationCap,
} from "@/lib/run-budget-limits";
import type { AgentBudgetConfig } from "@/types";

/** Key-order-free, null-free form of a budget, so a re-save of the stored value is no change. */
function canonical(config: AgentBudgetConfig | null): string {
  const entries = Object.entries(config ?? {})
    .filter(([, v]) => v != null)
    .sort(([a], [b]) => a.localeCompare(b));
  return JSON.stringify(entries);
}

export interface RunDefaultsDraft {
  limits: LimitsDraft;
  /** The iteration cap as typed; empty = inherit the platform. */
  maxIterations: string;
}

export function runDefaultsDraft(fleet: {
  budget_config?: AgentBudgetConfig | null;
  max_iterations?: number | null;
}): RunDefaultsDraft {
  return {
    limits: limitsDraft(fleet.budget_config ?? null),
    maxIterations: fleet.max_iterations != null ? String(fleet.max_iterations) : "",
  };
}

/**
 * The run-default fields of the `PUT /fleets/{id}` body: only what changed,
 * `null` to clear. Keys the panel does not edit (the final-answer reserve, the
 * failsafe) stay as stored.
 */
export function runDefaultsChanges(
  fleet: { budget_config?: AgentBudgetConfig | null; max_iterations?: number | null },
  draft: RunDefaultsDraft,
): { budget_config?: AgentBudgetConfig | null; max_iterations?: number | null } {
  const stored = fleet.budget_config ?? null;
  const out: { budget_config?: AgentBudgetConfig | null; max_iterations?: number | null } = {};
  const budget = limitsPayload(draft.limits, stored);
  if (canonical(budget) !== canonical(stored)) {
    out.budget_config = budget;
  }
  const iterations = parseIterationCap(draft.maxIterations);
  if (iterations !== (fleet.max_iterations ?? null)) out.max_iterations = iterations;
  return out;
}
