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

import { Icon } from "@filigran/design-system";
/**
 * Agent-node extras in the config drawer.
 *
 * The agent node runs an existing agent and OVERRIDES some of its execution
 * settings for this node only — the stored agent is never changed, which is
 * what lets one agent live in several flows. This adds three things the
 * catalog-driven form cannot express on its own:
 *
 * 1. **Create an agent** — opens the agent wizard in a new tab, so an author
 *    can mint an agent without leaving the canvas.
 * 2. **Auto-map on select** — picking a different agent pre-fills the override
 *    fields (model / temperature / reasoning / iterations / persona) with that
 *    agent's own values, as an editable starting point. It fires ONLY on a
 *    change, never on mount, so it can't clobber overrides you already typed.
 * 3. **Capabilities recap + expand** — a read-only summary of what the selected
 *    agent already carries (tools, KBs, skills, handovers) and one button that
 *    materializes all of it onto the canvas as the matching capability nodes and
 *    handover edges, so the node's real shape becomes editable graph. Idempotent
 *    (an already-wired socket / existing handover is left alone), so it is safe
 *    to press after wiring some capabilities by hand.
 */

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAgent } from "@/hooks/api/useAgentApi";
import { useFormatter } from "@/i18n";
import type { Agent } from "@/types";
import type { CanvasNode } from "./graph-adapter";
import { withBasePath } from "@/lib/base-path";

interface AgentNodeExtrasProps {
  node: CanvasNode;
  disabled?: boolean;
  onApplyConfigPatch?: (
    nodeId: string,
    patch: Record<string, unknown>,
    label?: string,
  ) => void;
  onMaterializeAgentGraph?: (nodeId: string, agent: Agent) => void;
}

export function AgentNodeExtras({
  node,
  disabled,
  onApplyConfigPatch,
  onMaterializeAgentGraph,
}: AgentNodeExtrasProps) {
  const { t_i18n } = useFormatter();
  const agentId =
    typeof node.data.config?.agent_id === "string"
      ? node.data.config.agent_id
      : "";
  const { data: agent } = useAgent(agentId);

  // The agent this node carried when the drawer opened (this component is keyed
  // by node id, so the ref is per node). A later change to a *different* agent
  // is an active pick — the cue to offer deploying it onto the canvas.
  const mountAgentIdRef = useRef(agentId);
  const promptedRef = useRef<Set<string>>(new Set());
  const [deployAgent, setDeployAgent] = useState<Agent | null>(null);

  // Auto-map the agent's values into the override fields as defaults.
  //  - First time we see an agent for this node (mount included): fill only
  //    the EMPTY fields, so a node's already-saved overrides are preserved
  //    but a freshly-picked agent still populates the drawer.
  //  - A later switch to a *different* agent re-maps every field, since the
  //    values now come from that new agent.
  const mappedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!agent || !agentId || agent.id !== agentId || !onApplyConfigPatch) return;
    if (mappedRef.current === agentId) return;
    const firstMap = mappedRef.current === null;
    mappedRef.current = agentId;

    const config = node.data.config ?? {};
    const isBlank = (v: unknown) => v === undefined || v === null || v === "";
    const candidates: Record<string, unknown> = {
      persona_override: agent.persona ?? "",
      model: agent.llm_model ?? "",
      temperature: agent.temperature ?? null,
      reasoning_effort: agent.reasoning_effort ?? "",
      max_iterations: agent.max_iterations ?? null,
    };
    // First map (mount / freshly-picked agent) fills only the EMPTY fields so
    // saved overrides survive; an agent switch overwrites every field. All
    // keys go in ONE patch — sequential single-key writes clobber each other.
    const patch: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(candidates)) {
      if (firstMap && !isBlank(config[key])) continue;
      patch[key] = value;
    }

    // Adopt the agent's name so the node stops reading "Agent". Same rule as
    // the fields: on first map keep a label the user already typed; on an
    // agent switch, take the new agent's name.
    const currentLabel =
      typeof node.data.label === "string" ? node.data.label : "";
    const label =
      firstMap && currentLabel.trim() !== "" ? undefined : agent.name;

    if (Object.keys(patch).length > 0 || label !== undefined) {
      onApplyConfigPatch(node.id, patch, label);
    }

    // A pick of a *different* agent than the drawer opened with is the moment
    // to offer deploying it — rather than making the author hunt for the
    // "Expand onto the canvas" button afterwards. Only when the agent actually
    // carries something to deploy, and once per agent so it doesn't nag.
    if (
      agentId !== mountAgentIdRef.current &&
      !promptedRef.current.has(agentId) &&
      onMaterializeAgentGraph &&
      hasExpandable(agent)
    ) {
      promptedRef.current.add(agentId);
      setDeployAgent(agent);
    }
  }, [
    agent,
    agentId,
    node.id,
    node.data.config,
    node.data.label,
    onApplyConfigPatch,
    onMaterializeAgentGraph,
  ]);

  return (
    <div className="space-y-3">
      {/* The "or create" half of the chooser — only while no agent is picked;
          once one is selected it would be noise. */}
      {!agentId && (
        <Button
          variant="outline"
          size="sm"
          className="w-full gap-1.5"
          disabled={disabled}
          // A new tab keeps the unsaved flow intact; the author picks the new
          // agent from the dropdown on return (the catalog refetches on focus).
          onClick={() => window.open(withBasePath("/agents/new"), "_blank", "noopener")}
        >
          <Icon name="plus" size={16} />
          {t_i18n("Create an agent")}
        </Button>
      )}

      {agent && (
        <div className="space-y-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2.5">
          <div className="flex items-center gap-1.5">
            <Icon name="bot" size={16} className="text-accent" />
            <span className="truncate content-compact font-medium">{agent.name}</span>
          </div>
          <p className="content-caption text-default-secondary">
            {t_i18n("These overrides apply to this node only.")}
          </p>
          <div className="flex flex-wrap gap-1">
            <CapabilityBadge
              label={t_i18n("{count, plural, one {# tool} other {# tools}}", {
                count:
                  (agent.custom_tool_ids?.length ?? 0) +
                  (agent.builtin_tool_ids?.length ?? 0),
              })}
            />
            <CapabilityBadge
              label={t_i18n("{count, plural, one {# knowledge base} other {# knowledge bases}}", {
                count: agent.knowledge_base_ids?.length ?? 0,
              })}
            />
            <CapabilityBadge
              label={t_i18n("{count, plural, one {# skill} other {# skills}}", {
                count: agent.skill_ids?.length ?? 0,
              })}
            />
            <CapabilityBadge
              label={t_i18n("{count, plural, one {# handover} other {# handovers}}", {
                count: agent.handover_agent_ids?.length ?? 0,
              })}
            />
          </div>

          {hasExpandable(agent) && onMaterializeAgentGraph && (
            <Button
              variant="outline"
              size="sm"
              className="w-full gap-1.5"
              disabled={disabled}
              // Idempotent: skips already-wired capability sockets and existing
              // handover edges, so a second press adds only what is missing.
              onClick={() => onMaterializeAgentGraph(node.id, agent)}
            >
              <Icon name="network" size={16} />
              {t_i18n("Expand the agent onto the canvas")}
            </Button>
          )}
        </div>
      )}

      {/* Offered right when an agent is picked, so deploying its capabilities +
          handovers is one click here rather than a hunt for the button above. */}
      <Dialog
        open={deployAgent !== null}
        onOpenChange={(open) => {
          if (!open) setDeployAgent(null);
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {t_i18n("Deploy {name} onto the canvas?", {
                name: deployAgent?.name ?? "",
              })}
            </DialogTitle>
            <DialogDescription>
              {t_i18n(
                "Add this agent's tools, integrations, memory, guardrails and handovers as connected nodes, using its own values. You can also do this later from the panel.",
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setDeployAgent(null)}
            >
              {t_i18n("Not now")}
            </Button>
            <Button
              size="sm"
              className="gap-1.5"
              onClick={() => {
                if (deployAgent && onMaterializeAgentGraph) {
                  onMaterializeAgentGraph(node.id, deployAgent);
                }
                setDeployAgent(null);
              }}
            >
              <Icon name="network" size={16} />
              {t_i18n("Deploy")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** True when the agent carries anything the canvas can materialize as nodes. */
function hasExpandable(agent: Agent): boolean {
  return (
    (agent.builtin_tool_ids?.length ?? 0) > 0 ||
    (agent.custom_tool_ids?.length ?? 0) > 0 ||
    Object.values(agent.integration_bindings ?? {}).some(
      (ids) => (ids?.length ?? 0) > 0,
    ) ||
    (agent.skill_ids?.length ?? 0) > 0 ||
    (agent.knowledge_base_ids?.length ?? 0) > 0 ||
    agent.memory_short_term_enabled ||
    agent.memory_medium_term_enabled ||
    agent.memory_persistent_enabled ||
    agent.memory_user_enabled ||
    !!agent.guardrails?.trim() ||
    (agent.output_format === "json" && !!agent.output_schema) ||
    (agent.handover_agent_ids?.length ?? 0) > 0
  );
}

function CapabilityBadge({ label }: { label: string }) {
  return (
    <Badge variant="outline" className="text-[9px] text-default-secondary">
      {label}
    </Badge>
  );
}
