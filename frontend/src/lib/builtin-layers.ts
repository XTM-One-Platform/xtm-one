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
 * The two layers of a built-in agent (#914, slice 4), as the agent page shows
 * them. The contract is `backend/app/services/builtin_customer_layer.py`:
 *
 * - "Locked by the product": the seeded mechanics (persona core, members,
 *   handovers, gates, assignments, built-in tools), rewritten by each release.
 * - "Yours": the customer layer (integrations, MCP servers, knowledge bases,
 *   skills, custom tools, organization notes, model, approvals), never
 *   touched by the seed.
 *
 * And the agent's learned skill (`backend/app/services/builtin_learned_skill.py`):
 * the company-managed skill tagged `learned-for-agent:<agent id>`.
 */
import type { AgentSkill } from "@/types";

/** Tag keying the learned skill of an agent (`AGENT_TAG_PREFIX`). */
const LEARNED_AGENT_TAG_PREFIX = "learned-for-agent:";
/** Tag an administrator sets to stop the agent writing (`FROZEN_TAG`). */
export const LEARNED_FROZEN_TAG = "learned-frozen";
/** Upper bound of the organization notes (`CUSTOMER_NOTES_MAX_CHARS`). */
export const CUSTOMER_NOTES_MAX_CHARS = 4000;

/** What the release owns on a built-in, in the order the card lists it. @i18n-keys */
export const LOCKED_ITEMS = [
  "Persona",
  "Role and handovers",
  "Gates and their defaults",
  "Assignments and their prompts",
  "Built-in tools",
] as const;

/** What the organization owns on a built-in, in the order the card lists it. @i18n-keys */
export const YOURS_ITEMS = [
  "Integrations and their tools",
  "MCP servers",
  "Knowledge bases",
  "Skills and custom tools",
  "Organization notes",
  "Model",
  "Tool approvals",
] as const;

/** The customer-layer fields of `YOURS_ITEMS`, to say which one a person already set. */
const YOURS_FIELDS: Record<(typeof YOURS_ITEMS)[number], readonly string[]> = {
  "Integrations and their tools": [
    "integration_bindings",
    "integration_template_bindings",
    "integration_allowed_tools",
    "integration_element_bindings",
    "integration_read_only",
  ],
  "MCP servers": ["mcp_server_ids", "mcp_allowed_tools"],
  "Knowledge bases": ["knowledge_base_ids", "knowledge_base_permissions"],
  "Skills and custom tools": ["skill_ids", "skill_load_modes", "custom_tool_ids"],
  "Organization notes": ["customer_notes"],
  Model: ["llm_provider", "llm_provider_id", "llm_model", "llm_model_source"],
  "Tool approvals": ["approve_tool_calls"],
};

/**
 * Whether the organization set *item* on this built-in. The API records only
 * the fields the seed would otherwise rewrite; integrations and notes are read
 * off the agent itself.
 */
export function yoursItemSet(
  item: (typeof YOURS_ITEMS)[number],
  agent: { customer_fields?: string[] | null; customer_notes?: string | null; integration_bindings?: Record<string, string[]> | null },
): boolean {
  if (item === "Organization notes") return !!agent.customer_notes?.trim();
  if (item === "Integrations and their tools" && Object.values(agent.integration_bindings ?? {}).some((ids) => ids.length > 0)) {
    return true;
  }
  const recorded = new Set(agent.customer_fields ?? []);
  return YOURS_FIELDS[item].some((field) => recorded.has(field));
}

/** The learned skill of *agentId* among *skills*, or undefined before its first lesson. */
export function learnedSkillOf(skills: readonly AgentSkill[] | undefined, agentId: string): AgentSkill | undefined {
  const tag = `${LEARNED_AGENT_TAG_PREFIX}${agentId}`;
  return skills?.find((skill) => skill.tags?.includes(tag));
}

/** Whether an administrator froze the learned skill. */
export function learnedSkillFrozen(skill: Pick<AgentSkill, "tags"> | undefined): boolean {
  return !!skill?.tags?.includes(LEARNED_FROZEN_TAG);
}

/** The lessons of a learned skill: its `## ` section titles, in order. */
export function learnedLessonTitles(content: string | null | undefined): string[] {
  return (content ?? "")
    .split("\n")
    .filter((line) => line.startsWith("## "))
    .map((line) => line.slice(3).trim())
    .filter(Boolean);
}

/** The run a learned-skill version names in its commit message (`... (run <id>)`), or null. */
export function versionRunId(commitMessage: string | null | undefined): string | null {
  const match = /\(run ([0-9a-f-]{8,})\)\s*$/i.exec(commitMessage ?? "");
  return match ? match[1] : null;
}
