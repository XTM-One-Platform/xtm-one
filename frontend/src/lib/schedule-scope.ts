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
 * The agents the Schedule view of the Agentic Flow map shows.
 *
 * The map's toolbar keeps a set of agents (the visibility segment and the
 * Filters dropdown, `keptAgentIds`; null when neither narrows anything, so
 * every agent the caller sees), and the Focus picker narrows it further to
 * the focused agents. The Schedule view counts its tiles, its timeline and
 * its inventory on the result: `null` means every agent.
 *
 * Pure module (no React, no DOM): unit-tested on vitest.
 */

export interface ScheduleScopeInput {
  /** The agents the segment and the filters keep; null = every agent. */
  keptAgentIds: ReadonlySet<string> | null;
  /** The focused agents; empty = no focus. */
  focusedIds: ReadonlySet<string>;
}

export function scheduleScope({ keptAgentIds, focusedIds }: ScheduleScopeInput): ReadonlySet<string> | null {
  if (focusedIds.size === 0) return keptAgentIds;
  const focused = [...focusedIds];
  return new Set(keptAgentIds ? focused.filter((id) => keptAgentIds.has(id)) : focused);
}
