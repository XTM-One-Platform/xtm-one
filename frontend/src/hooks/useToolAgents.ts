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
 * Which assistants can do a job that one built-in tool provides.
 *
 * A deck is written by an agent carrying `generate_presentation`, a video
 * made by one carrying `video_studio`, or by the General Assistant when its
 * effective tools carry it. Every "with your assistant" hand-off of such a
 * module opens the chat on the answer: a person handed to an agent that
 * cannot do the job sees it refuse after the fact. One hook, so the pickers
 * and the hand-offs of every module agree on who is offered and who is first.
 */
import { useMemo } from "react";

import { useAgents } from "@/hooks/api/useAgentApi";
import { useMe } from "@/hooks/api/useAuthApi";
import { useBuiltinToolGroups, useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { aliasIndex } from "@/lib/builtin-tool-aliases";
import { generalAssistantStandIn } from "@/lib/general-assistant-stand-in";
import { agentsCarrying, carriesJob, firstToolAgent, type ToolJob } from "@/lib/tool-agents";
import type { Agent } from "@/types";

export interface ToolAgents {
  /** Enabled chat agents that carry the tool, by name. */
  agents: Agent[];
  /**
   * The General Assistant is offered on the same terms: not linked to an agent
   * (a linked one stands in `agents` on its own merits) and carrying the tool
   * in its effective tools — the user's own override, else the platform's
   * (the precedence `general_chat` applies).
   */
  generalAssistantOffered: boolean;
  /** The id of the agent the General Assistant is linked to, if any. */
  linkedAgentId: string | null;
  /** Nobody can do the job on this install: no agent and no General Assistant. */
  none: boolean;
  /** The inputs (agents, General Assistant config) are still loading. */
  resolving: boolean;
  /**
   * The agent to offer first — `null` when the answer is the General Assistant
   * (or nobody: read `none`). The user's own default chat agent when it carries
   * the tool (their chat opens on it anyway); else the General Assistant when
   * offered — it carries the tool out of the box and its persona is general,
   * whereas the built-in agents that carry it are specialists; else a
   * company-managed agent that carries it; else any.
   */
  defaultAgentId: string | null;
}

/**
 * *job* is a suite id, or a suite and the function the job needs when the job
 * is one sub-area of a merged group (`{ suite: "platform", fn: "create_fleet" }`).
 * An entity storing a [DEPRECATED] former id of the suite carries it
 * (the registry's `aliases`).
 */
export function useToolAgents(job: string | ToolJob): ToolAgents {
  const { data: agents, isLoading: agentsLoading } = useAgents();
  const { data: user } = useMe();
  const { data: gaConfig, isLoading: gaLoading } = useGeneralAssistantConfig();
  const { data: groups } = useBuiltinToolGroups();
  const aliases = useMemo(() => aliasIndex(groups ?? []), [groups]);
  const suite = typeof job === "string" ? job : job.suite;
  const fn = typeof job === "string" ? undefined : job.fn;

  const toolAgents = useMemo(
    () => agentsCarrying(agents ?? [], { suite, fn }, aliases),
    [agents, suite, fn, aliases],
  );
  const linkedAgentId = generalAssistantStandIn(gaConfig?.linked_agent_id, agents)?.id ?? null;
  // The General Assistant's ids carry no allow list: a group it carries is whole.
  const generalAssistantOffered =
    !linkedAgentId && carriesJob(user?.ga_builtin_tool_ids ?? gaConfig?.builtin_tool_ids ?? [], null, suite, aliases);
  const explicitDefaultId = user?.default_agent_id ?? null;
  const defaultAgentId = useMemo(
    () => firstToolAgent(toolAgents, explicitDefaultId, generalAssistantOffered),
    [toolAgents, explicitDefaultId, generalAssistantOffered],
  );

  return {
    agents: toolAgents,
    generalAssistantOffered,
    linkedAgentId,
    none: toolAgents.length === 0 && !generalAssistantOffered,
    // The queries' own loading state, not "no data yet": a General Assistant
    // config request that failed leaves `gaConfig` undefined for good, and
    // "still resolving" would then lock a dialog forever. Without the config
    // the General Assistant is simply not offered.
    resolving: agentsLoading || gaLoading,
    defaultAgentId,
  };
}
