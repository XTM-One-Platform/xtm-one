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

/** The rows of the fleet cost card: agents by cost, with their share of the most expensive. */
import type { FleetCost } from "@/hooks/api/useFleetApi";

interface CostRow {
  key: string;
  name: string;
  cost: number;
  runs: number;
  /** Bar width in percent of the most expensive agent (0 to 100). */
  share: number;
}

export function costRows(cost: FleetCost | undefined): CostRow[] {
  if (!cost) return [];
  const sorted = [...cost.agents].sort((a, b) => b.total_cost - a.total_cost || b.trace_count - a.trace_count);
  const top = sorted[0]?.total_cost ?? 0;
  return sorted.map((a, i) => ({
    key: a.agent_id ?? `unknown-${i}`,
    name: a.agent_name,
    cost: a.total_cost,
    runs: a.trace_count,
    share: top > 0 ? Math.round((a.total_cost / top) * 100) : 0,
  }));
}
