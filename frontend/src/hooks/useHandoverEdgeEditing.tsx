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
 * useHandoverEdgeEditing - the handover edits of a graph (draw, retype,
 * delete an edge), one implementation for the agent-to-agent map and the
 * fleet page graph.
 *
 * Returns the props `AgentFlowGraph` takes for editing (`editableAgentIds`,
 * `onAddEdge`, `onEditEdge`) and the confirmation dialog element to render.
 * Every write is an agent update (`PUT /agents/{id}`); a refusal shows the
 * server's reason (`handoverWriteRefusal`), such as the fleet boundary's
 * "is not exposed to other fleets". `onSaved` runs after a successful write,
 * for a page whose own reads draw the handovers (the fleet graph).
 */

import { useCallback, useMemo, type ReactNode } from "react";
import { useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useToast } from "@/components/ui/toast";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useFormatter } from "@/i18n";
import {
  canEditHandoversOf,
  handoverWriteRefusal,
  planHandoverEdgeAdd,
  planHandoverEdgeEdit,
  type HandoverEdgePlan,
} from "@/lib/handover-edge-edit";
import type { Agent } from "@/types";

export interface HandoverEdgeEditing {
  editableAgentIds: Set<string>;
  onAddEdge: (sourceId: string, targetId: string, handoverType: string) => Promise<void>;
  onEditEdge: (sourceId: string, targetId: string, newTypeOrDelete: string) => Promise<void>;
  /** The type-narrowing confirmation: render it once in the page. */
  confirmElement: ReactNode;
}

export function useHandoverEdgeEditing(
  agents: readonly Agent[] | null | undefined,
  options: { onSaved?: () => void } = {},
): HandoverEdgeEditing {
  const { onSaved } = options;
  const { user, isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();
  const { toast } = useToast();
  const updateAgent = useUpdateAgent();
  const aiOff = useAiActionDisabled();
  const aiDisabledReason = useAiDisabledReason();
  const { t_i18n, intl } = useFormatter();
  const [confirmElement, confirm] = useConfirm();

  const editableAgentIds = useMemo(() => {
    const set = new Set<string>();
    for (const a of agents ?? []) {
      if (canEditHandoversOf(a, user?.id, isAdmin, isXtmOne)) set.add(a.id);
    }
    return set;
  }, [agents, user?.id, isAdmin, isXtmOne]);

  const nameOf = useCallback(
    (id: string) => (agents ?? []).find((a) => a.id === id)?.name ?? t_i18n("Unknown agent"),
    [agents, t_i18n],
  );

  // A type is set per target, so the first type an agent whose targets each
  // run every type is given makes the others delegate: say so first.
  const confirmNarrowing = useCallback(
    async (source: Agent, narrowed: string[]) => {
      if (narrowed.length === 0) return true;
      const names = narrowed.map(nameOf);
      return confirm({
        title: t_i18n("Give each agent a handover type?"),
        description: t_i18n(
          "Without handover types, {agent} can delegate to, consult or transfer to each of its handover agents. Types are set per agent, so giving this one a type sets them all: {count, plural, one {{names} becomes Delegate} other {{names} become Delegate}}.",
          { agent: source.name, count: narrowed.length, names: intl.formatList(names, { type: "conjunction" }) },
        ),
        confirmLabel: t_i18n("Set types"),
        variant: "warning",
      });
    },
    [nameOf, confirm, intl, t_i18n],
  );

  const save = useCallback(
    (sourceId: string, plan: HandoverEdgePlan, done: string, failed: string) => {
      updateAgent.mutate(
        { id: sourceId, handover_agent_ids: plan.ids, handover_types: plan.types },
        {
          onSuccess: () => {
            toast({ title: done, variant: "success" });
            onSaved?.();
          },
          onError: (error) =>
            toast({ title: failed, description: handoverWriteRefusal(error), variant: "error" }),
        },
      );
    },
    [updateAgent, toast, onSaved],
  );

  const onAddEdge = useCallback(
    async (sourceId: string, targetId: string, handoverType: string) => {
      if (aiOff) {
        toast({ title: aiDisabledReason ?? "", variant: "error" });
        return;
      }
      const source = (agents ?? []).find((a) => a.id === sourceId);
      if (!source) return;
      const plan = planHandoverEdgeAdd(source, targetId, handoverType);
      if (plan === "duplicate") {
        toast({
          title: t_i18n("Edge already exists"),
          description: t_i18n("This handover relationship already exists."),
          variant: "error",
        });
        return;
      }
      if (!(await confirmNarrowing(source, plan.narrowed))) return;
      save(sourceId, plan, t_i18n("Handover added"), t_i18n("Failed to add handover"));
    },
    [agents, aiOff, aiDisabledReason, toast, t_i18n, confirmNarrowing, save],
  );

  const onEditEdge = useCallback(
    async (sourceId: string, targetId: string, newTypeOrDelete: string) => {
      if (aiOff) {
        toast({ title: aiDisabledReason ?? "", variant: "error" });
        return;
      }
      const source = (agents ?? []).find((a) => a.id === sourceId);
      if (!source) return;
      const plan = planHandoverEdgeEdit(source, targetId, newTypeOrDelete);
      if (!(await confirmNarrowing(source, plan.narrowed))) return;
      save(
        sourceId,
        plan,
        newTypeOrDelete === "delete" ? t_i18n("Handover removed") : t_i18n("Handover updated"),
        t_i18n("Failed to update handover"),
      );
    },
    [agents, aiOff, aiDisabledReason, toast, t_i18n, confirmNarrowing, save],
  );

  return { editableAgentIds, onAddEdge, onEditEdge, confirmElement };
}
