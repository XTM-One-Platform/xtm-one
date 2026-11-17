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
 * The tools a chat session starts with: an agent's configured set, or the
 * General Assistant's (the user's profile overrides first, then the admin
 * config). Read by `useSessionTools`.
 */
import { getAgentRequiredTemplateProviders, resolveTemplateBindings } from "@/lib/utils";
import type { Agent, GeneralAssistantConfig, Integration, User } from "@/types";

/** Extract integration IDs from user/GA bindings (provider->id mapping -> id list). */
function extractIntegrationIds(
  userBindings?: Record<string, string[]> | null,
  gaBindings?: Record<string, string[]> | null,
): string[] {
  return Object.values(userBindings ?? gaBindings ?? {}).flat();
}

/** The five categories of "default" tools a chat session starts with. */
export interface DefaultToolSets {
  builtin: string[];
  custom: string[];
  integration: string[];
  mcp: string[];
  kb: string[];
}

/**
 * An agent's *default* tool set — what a brand-new conversation with the agent
 * starts with. Integration ids dedupe across static bindings and
 * template-resolved providers (an integration can appear in both).
 */
export function agentDefaultToolSets(
  agent: Agent | undefined,
  connectedIntegrations: Integration[],
  currentUserId: string | undefined,
): DefaultToolSets {
  if (!agent) return { builtin: [], custom: [], integration: [], mcp: [], kb: [] };
  const staticIds = agent.integration_bindings
    ? Object.values(agent.integration_bindings).flat()
    : [];
  const templateIds = resolveTemplateBindings(
    getAgentRequiredTemplateProviders(agent),
    connectedIntegrations,
    agent.integration_bindings,
    currentUserId,
  );
  return {
    builtin: agent.builtin_tool_ids || [],
    custom: agent.custom_tool_ids || [],
    integration: [...new Set([...staticIds, ...templateIds])],
    mcp: agent.mcp_server_ids || [],
    kb: agent.knowledge_base_ids || [],
  };
}

/**
 * The General Assistant's *default* tool set — user profile overrides
 * (``ga_*``) take precedence over the admin GA config.
 */
export function gaDefaultToolSets(
  user: User | null | undefined,
  ga: GeneralAssistantConfig | null | undefined,
  connectedIntegrations: Integration[],
): DefaultToolSets {
  const staticIds = extractIntegrationIds(
    user?.ga_integration_bindings,
    ga?.integration_bindings,
  );
  const templateIds = resolveTemplateBindings(
    getAgentRequiredTemplateProviders(ga ? (ga as Partial<Agent>) : null),
    connectedIntegrations,
    user?.ga_integration_bindings ?? ga?.integration_bindings,
    user?.id,
  );
  return {
    builtin: user?.ga_builtin_tool_ids ?? ga?.builtin_tool_ids ?? [],
    custom: user?.ga_custom_tool_ids ?? ga?.custom_tool_ids ?? [],
    integration: [...staticIds, ...templateIds],
    mcp: user?.ga_mcp_server_ids ?? ga?.mcp_server_ids ?? [],
    kb: user?.ga_knowledge_base_ids ?? ga?.knowledge_base_ids ?? [],
  };
}

/** Flat union of all default tool ids — the baseline sent with each message.
 *  De-duplicated so an id that repeats (e.g. the same integration id across GA
 *  bindings) appears once, keeping the persisted baseline a true union. */
export function flattenDefaultToolSets(sets: DefaultToolSets): string[] {
  return [
    ...new Set([...sets.builtin, ...sets.custom, ...sets.integration, ...sets.mcp, ...sets.kb]),
  ];
}
