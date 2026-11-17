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
 * The agent that answers in the General Assistant's place, the mirror
 * of `backend/app/services/general_assistant_link.py`.
 *
 * Settings > AI and models > General Assistant > "Link to Agent" stores `linked_agent_id`. The
 * agent it names stands in for the General Assistant when every user can chat
 * with it: enabled, open to chat, company-managed and not access-restricted.
 * The backend refuses to link any other agent, binds a conversation created
 * without an agent to it and answers the General Assistant's channel messages
 * with it; the chat page opens it by default and the sidebar shows it instead
 * of the General Assistant entry.
 */

interface StandInCandidate {
  id: string;
  enabled: boolean;
  disable_chat?: boolean;
  is_company_managed: boolean;
  access_restricted?: boolean;
}

/** Why an agent cannot stand in, in the order `stand_in_refusal()` checks. */
type StandInRefusal = "not_company_managed" | "access_restricted" | "disabled" | "chat_disabled";

/** Why `agent` may not answer in the General Assistant's place, `null` when it may. */
export function standInRefusal(agent: StandInCandidate): StandInRefusal | null {
  if (!agent.is_company_managed) return "not_company_managed";
  if (agent.access_restricted) return "access_restricted";
  if (!agent.enabled) return "disabled";
  if (agent.disable_chat) return "chat_disabled";
  return null;
}

/** Whether `agent` may answer in the General Assistant's place. */
export function canStandInForGeneralAssistant(agent: StandInCandidate): boolean {
  return standInRefusal(agent) === null;
}

/**
 * The agent `linkedAgentId` names when it may stand in, `null` when the General
 * Assistant answers itself (no link, an agent the viewer's list does not hold,
 * or one that stopped qualifying).
 */
export function generalAssistantStandIn<T extends StandInCandidate>(
  linkedAgentId: string | null | undefined,
  agents: readonly T[] | undefined,
): T | null {
  if (!linkedAgentId || !agents) return null;
  return agents.find((a) => a.id === linkedAgentId && canStandInForGeneralAssistant(a)) ?? null;
}

/**
 * The agent that answers `conv`'s turns: its own, else the one the server
 * names in the General Assistant's place (`answering_agent_id`, an unbound
 * Slack or Teams conversation continued on the web). `null`: the
 * General Assistant answers. The server decides it (`turn_stand_in()`), for
 * the conversation's owner: never re-derived here from the link.
 */
export function conversationAgentId(
  conv: { agent_id?: string | null; answering_agent_id?: string | null } | null | undefined,
): string | null {
  return conv?.agent_id || conv?.answering_agent_id || null;
}
