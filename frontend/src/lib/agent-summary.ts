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
 * Per-agent assignment / trigger summary derived from the flat assignment list.
 *
 * Used by the Agentic Flow Map tab to render trigger-type chips and an
 * assignment count on each graph node, and by the agent detail drawer.
 */

import type { Assignment, TriggerType } from "@/types";

export interface AgentSummary {
  /** Total assignments bound to the agent. */
  assignmentCount: number;
  /** How many of those assignments are enabled. */
  enabledCount: number;
  /** Distinct trigger types present on the agent, in canonical order. */
  triggerTypes: TriggerType[];
}

// Canonical display order for trigger chips.
const TRIGGER_ORDER: TriggerType[] = [
  "schedule",
  "webhook",
  "integration_event",
  "manual",
];

/**
 * Build a `{ agentId -> AgentSummary }` map from a flat assignment list.
 */
export function buildAgentSummaries(
  assignments: Assignment[],
): Record<string, AgentSummary> {
  const acc: Record<
    string,
    { count: number; enabled: number; types: Set<TriggerType> }
  > = {};

  for (const a of assignments) {
    const entry =
      acc[a.agent_id] ?? { count: 0, enabled: 0, types: new Set<TriggerType>() };
    entry.count += 1;
    if (a.enabled) entry.enabled += 1;
    entry.types.add(a.trigger_type);
    acc[a.agent_id] = entry;
  }

  const out: Record<string, AgentSummary> = {};
  for (const [agentId, e] of Object.entries(acc)) {
    out[agentId] = {
      assignmentCount: e.count,
      enabledCount: e.enabled,
      triggerTypes: TRIGGER_ORDER.filter((t) => e.types.has(t)),
    };
  }
  return out;
}
