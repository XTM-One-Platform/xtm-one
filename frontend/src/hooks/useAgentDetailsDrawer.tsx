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
 * useAgentDetailsDrawer - the agent definition drawer of an agent graph:
 * which agent it shows, and the `AgentFlowDrawer` element itself.
 *
 * One hook for every page drawing `AgentFlowGraph` (the Agentic Flow map and a
 * fleet's Graph tab), so the node's "i", Enter on a focused node and a menu
 * entry open the same drawer with the same content and the same close
 * behaviour wherever the graph is drawn. The page passes `show` as the graph's
 * `onShowDetails`, `selectedId` as its `selectedNodeId`, and renders `element`.
 *
 * The drawer lies over the canvas on a clear backdrop, never an opaque one:
 * that rule lives in `AgentFlowDrawer` and is not repeated here.
 */
import { useCallback, useMemo, useState, type ReactNode } from "react";

import { AgentFlowDrawer } from "@/components/ui/agent-flow-drawer";
import { useAssignments } from "@/hooks/api/useAssignmentApi";
import type { Agent } from "@/types";

export interface AgentDetailsDrawerOptions {
  /** The agents the reader sees: the drawer opens on one of them only. */
  agents: readonly Agent[] | undefined;
  /** Agents with a run or a chat in flight, for the drawer's status line. */
  activeAgentIds: Set<string>;
  /** The drawer's "Open agent" button. */
  onOpenAgent: (agentId: string) => void;
  /** A recent run's row; without it the rows are not links. */
  onOpenRun?: (runId: string) => void;
}

export interface AgentDetailsDrawer {
  /** Opens the drawer on an agent: the graph's `onShowDetails`. */
  show: (agentId: string) => void;
  close: () => void;
  /** The agent the drawer shows, when it is open: the graph's `selectedNodeId`. */
  selectedId: string | null;
  /** The drawer, to render once on the page. */
  element: ReactNode;
}

export function useAgentDetailsDrawer({
  agents,
  activeAgentIds,
  onOpenAgent,
  onOpenRun,
}: AgentDetailsDrawerOptions): AgentDetailsDrawer {
  const [agentId, setAgentId] = useState<string | null>(null);
  const { data: assignments } = useAssignments();

  const agent = useMemo(() => (agents ?? []).find((a) => a.id === agentId) ?? null, [agents, agentId]);
  const agentNameMap = useMemo(() => new Map((agents ?? []).map((a) => [a.id, a.name])), [agents]);

  const show = useCallback((id: string) => setAgentId(id), []);
  const close = useCallback(() => setAgentId(null), []);

  const element = (
    <AgentFlowDrawer
      agent={agent}
      assignments={assignments ?? []}
      activeAgentIds={activeAgentIds}
      agentNameMap={agentNameMap}
      open={!!agent}
      onClose={close}
      onOpenAgent={onOpenAgent}
      onOpenRun={onOpenRun}
    />
  );

  return { show, close, selectedId: agent ? agent.id : null, element };
}
