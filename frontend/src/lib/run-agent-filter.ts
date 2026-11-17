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
 * The `agent_ids` filter of the run feeds.
 *
 * An entity's Activity tab and run stats show the runs of the agents bound to
 * it. The server filters by those agents, so pagination and totals cover every
 * run of theirs, never the head of a capped platform-wide page. An empty list
 * is sent as `agent_ids=` and matches no run.
 */

/** Mirrors `MAX_RUN_FILTER_AGENT_IDS` in `backend/app/api/v1/assignments.py`. */
export const MAX_RUN_FILTER_AGENT_IDS = 150;

/**
 * The `agent_ids` value for a set of agent ids: deduplicated and sorted, so
 * the query key and the URL do not depend on the order the agents loaded in.
 * `undefined` = no agent filter.
 */
export function runAgentIdsParam(ids: Iterable<string> | undefined): string | undefined {
  if (ids === undefined) return undefined;
  return [...new Set(ids)].sort().join(",");
}

/** Whether a set of agents is too large for one run feed read to filter by. */
export function exceedsRunAgentFilter(ids: Iterable<string>): boolean {
  return new Set(ids).size > MAX_RUN_FILTER_AGENT_IDS;
}
