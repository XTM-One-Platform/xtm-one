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

import { useMemo } from "react";

import { useToolGateCatalog } from "@/hooks/api/useHitlWhitelist";
import { useToolApprovals } from "@/hooks/api/useToolApprovals";
import { approvalsShown } from "@/lib/hitl";

/**
 * The gate state of one row of the tools list, for the cards and the tables.
 *
 * Builtin rows are *groups* ("Work Items" is six tool names), so they can only
 * answer in aggregate — "2 of 6 ask first". A custom row is one tool and answers
 * yes or no. Keeping both in one lookup means the two renderings cannot drift.
 *
 * Counted FOR THE READER, like the approvals list's own headers. The
 * catalogue's `gates` is the deployment's answer and deliberately excludes one
 * person's standing approvals — right for a list describing a deployment, and
 * wrong for a badge that says "asks first" with nobody named. Folded in here
 * rather than at each badge, which is why this hook exists.
 *
 * Empty while nothing asks: a tool's place on the allowed list only matters on
 * an agent that asks before running tools, so a badge saying "Asks first" on a
 * platform where no agent asks described a behaviour nobody gets. Given an
 * agent's own `approve_tool_calls`, it answers for that agent instead.
 */
export function useToolGateSummary(options?: {
  /** The agent's own setting (null = follows the platform default). */
  agentApproval?: boolean | null;
  /** Whether that agent is enabled: a disabled agent asks nothing. */
  agentEnabled?: boolean;
}) {
  const { data } = useToolGateCatalog();
  const { data: mine } = useToolApprovals();
  const forAgent = options !== undefined && "agentApproval" in options;
  const agentApproval = options?.agentApproval;
  const agentEnabled = options?.agentEnabled;
  return useMemo(() => {
    const byGroup = new Map<string, { gating: number; total: number }>();
    const byName = new Map<string, boolean>();
    const active = approvalsShown(
      mine,
      forAgent ? { approveToolCalls: agentApproval, enabled: agentEnabled } : undefined,
    );
    if (!active) return { byGroup, byName, loaded: !!data && !!mine, active };
    const approved = new Set((mine?.preferences ?? []).map((p) => p.tool_key));
    const muted = mine?.approve_all_tools ?? false;
    for (const tool of data?.tools ?? []) {
      const pauses = tool.gates && !muted && !approved.has(tool.tool_key);
      byName.set(tool.tool_key, pauses);
      if (!tool.group) continue;
      const entry = byGroup.get(tool.group) ?? { gating: 0, total: 0 };
      entry.total += 1;
      if (pauses) entry.gating += 1;
      byGroup.set(tool.group, entry);
    }
    return { byGroup, byName, loaded: !!data, active };
  }, [data, mine, forAgent, agentApproval, agentEnabled]);
}
