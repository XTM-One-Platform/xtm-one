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
 * The four gated limits of a run budget, shared by the agent Budget tab and
 * the fleet settings panel's "Run defaults".
 *
 * A budget is a PARTIAL override: an empty field is not "0", it is "inherit".
 * The platform ceiling binds a non-admin (`budget_policy.ceiling_violations`
 * on the server); `blockedLimits` mirrors that rule so the refusal is shown
 * next to the field before the save is attempted.
 */
import { Clock, Coins, Hash } from "lucide-react";

import { MAX_ITERATIONS_CEILING } from "@/lib/constants";
import type { AgentBudgetConfig, BudgetLimitKey, BudgetPolicy } from "@/types";

interface BudgetLimit {
  key: BudgetLimitKey;
  label: string;
  icon: React.ElementType;
  help: string;
  /** The info bubble of the field: what, unit, example, who reads it, when empty. */
  info: string;
  step?: string;
  /** Renders a stored/inherited number the way that dimension reads. */
  format: (v: number) => string;
}

/** The four gated dimensions, in the order the docs present them. */
export const BUDGET_LIMITS: readonly BudgetLimit[] = [
  {
    key: "max_cost_usd",
    label: "Max cost (USD)",
    icon: Coins,
    step: "0.01",
    help: "Hard ceiling on the priced model spend of one run. Needs pricing for the model: with none known, cost is not enforced and the other limits still apply.",
    info: "Unit: US dollars per run. Example: 2.50. Read by the platform after each model call of a run. Left empty, the field inherits the value its placeholder shows.",
    format: (v) => `$${v}`,
  },
  {
    key: "max_total_tokens",
    label: "Max tokens",
    icon: Hash,
    help: "Hard ceiling on the tokens one run may consume, prompt and completion together.",
    info: "Unit: tokens per run, prompt and completion together. Example: 500000. Read by the platform after each model call of a run. Left empty, the field inherits the value its placeholder shows.",
    format: (v) => v.toLocaleString(),
  },
  {
    key: "max_tool_calls",
    label: "Max tool calls",
    icon: Hash,
    help: "Bounds parallel tool use, which Max iterations cannot: a single iteration can fan out into many calls.",
    info: "Unit: tool calls per run. Example: 200. Read by the platform before each tool call of a run. Left empty, the field inherits the value its placeholder shows.",
    format: (v) => v.toLocaleString(),
  },
  {
    key: "max_duration_seconds",
    label: "Max duration (seconds)",
    icon: Clock,
    help: "Wall-clock ceiling for one run. Combined with the run's own timeout: the tighter of the two applies.",
    info: "Unit: seconds of wall-clock time per run. Example: 900 for 15 minutes. Read by the platform throughout a run. Left empty, the field inherits the value its placeholder shows.",
    format: (v) => `${v.toLocaleString()}s`,
  },
];

/**
 * An empty input, "0", or anything unparseable all mean "inherit".
 *
 * Tokens, tool calls and seconds are whole numbers in the API schema, so a
 * typed `1.5` is floored here. Cost stays fractional. The positivity check
 * runs AFTER the floor: `0.5` tokens floors to `0`, which the API reads as
 * "no limit", so anything under 1 in a whole-number dimension is "inherit".
 */
export function parseLimit(raw: string, key: BudgetLimitKey): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) return null;
  const value = key === "max_cost_usd" ? parsed : Math.floor(parsed);
  return value > 0 ? value : null;
}

export type LimitsDraft = Record<BudgetLimitKey, string>;

export function limitsDraft(config: AgentBudgetConfig | null | undefined): LimitsDraft {
  const draft = {} as LimitsDraft;
  for (const { key } of BUDGET_LIMITS) {
    const value = config?.[key];
    draft[key] = value != null ? String(value) : "";
  }
  return draft;
}

/**
 * The budget to store: the limits of `draft` on top of what `stored` pins
 * outside them (final-answer reserve, failsafe), or `null` when nothing is
 * pinned at all, which clears the override.
 */
export function limitsPayload(
  draft: LimitsDraft,
  stored: AgentBudgetConfig | null | undefined = null,
): AgentBudgetConfig | null {
  const config: AgentBudgetConfig = {};
  for (const [k, v] of Object.entries(stored ?? {})) {
    if (v != null && !BUDGET_LIMITS.some((l) => l.key === k)) {
      (config as Record<string, unknown>)[k] = v;
    }
  }
  for (const { key } of BUDGET_LIMITS) {
    const value = parseLimit(draft[key], key);
    if (value !== null) config[key] = value;
  }
  return Object.keys(config).length > 0 ? config : null;
}

/**
 * The limits a save would be refused for, so the reason is shown next to the
 * field instead of a 403 after the attempt. Mirrors the server rule with its
 * escape hatch: a value an admin already stored is not re-litigated.
 */
export function blockedLimits(
  draft: LimitsDraft,
  policy: BudgetPolicy | undefined,
  stored: AgentBudgetConfig | null | undefined,
): Set<BudgetLimitKey> {
  const offending = new Set<BudgetLimitKey>();
  if (!policy || policy.can_exceed_ceiling) return offending;
  for (const { key } of BUDGET_LIMITS) {
    const requested = parseLimit(draft[key], key);
    const ceiling = policy.ceilings[key];
    if (requested === null || ceiling == null) continue;
    if (requested === stored?.[key]) continue;
    if (requested > ceiling) offending.add(key);
  }
  return offending;
}

/** An iteration cap typed in a field: empty or unusable is "inherit" (`null`), else clamped. */
export function parseIterationCap(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const parsed = Math.floor(Number(trimmed));
  if (!Number.isFinite(parsed) || parsed < 1) return null;
  return Math.min(parsed, MAX_ITERATIONS_CEILING);
}
