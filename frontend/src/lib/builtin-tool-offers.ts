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
 * Which built-in suites a picker offers when the answer depends on more than
 * the deployment switches `useBuiltinTools()` resolves.
 *
 * An agent chat's session picker: a session selection replaces the
 * agent's own list, and the server drops an agent-only suite the agent does
 * not carry from the session of anyone who may not configure the agent
 * (`agent_chat._bound_chat_session_builtin_tool_ids`), and wires the trace
 * suites only for someone who may read the agent's chats
 * (`agent_chat._turn_trace_tool_ids`): such a suite acts as the agent (its
 * traces, work queue, shared state). The picker never offers what the server
 * would drop: an admin and the owner of a personal agent pass both rules.
 *
 * An agent's configuration grid: a suite whose deployment switch is
 * off is listed while the agent carries it, so it can be removed, and is never
 * added.
 */

export interface ChatAgentTools {
  builtin_tool_ids?: readonly string[] | null;
  user_id: string | null;
  is_company_managed: boolean;
}

interface ChatViewer {
  userId: string | undefined;
  isAdmin: boolean;
}

/** Whether the viewer may configure the agent, as far as the client can tell:
 *  an admin, or the owner of an agent that is not company-managed. The
 *  server's rule (`can_manage`) also admits a `config` grantee; the client
 *  never claims more than the server grants. */
export function mayConfigureAgent(agent: ChatAgentTools, viewer: ChatViewer): boolean {
  if (viewer.isAdmin) return true;
  return !agent.is_company_managed && !!viewer.userId && agent.user_id === viewer.userId;
}

/** The agent suites an agent chat's picker offers: every one, except an
 *  agent-only suite the agent does not carry when the viewer may not
 *  configure the agent. An agent not loaded yet carries nothing. */
export function offeredInAgentChat<T extends { id: string; agentOnly: boolean }>(
  agentSuites: readonly T[],
  agent: ChatAgentTools | undefined,
  viewer: ChatViewer,
): T[] {
  const carried = new Set(agent?.builtin_tool_ids ?? []);
  const configures = !!agent && mayConfigureAgent(agent, viewer);
  return agentSuites.filter((suite) => !suite.agentOnly || configures || carried.has(suite.id));
}

/** The suites an agent's configuration grid lists, in registry order: every
 *  suite the deployment offers, plus a switched-off one the agent carries,
 *  saved or in the draft being edited. Such a suite stays inactive until an
 *  administrator turns its switch on; hiding it would leave it on the agent
 *  unseen, to come back to life that day. A switched-off suite the agent does
 *  not carry stays hidden, and so does an internal one (Platform Health's
 *  specialists' group, #1943): it is listed only on an agent that carries it. */
export function listedInAgentConfig<T extends { id: string; available: boolean; internal?: boolean }>(
  suites: readonly T[],
  saved: readonly string[],
  draft: readonly string[],
): T[] {
  const carried = new Set([...saved, ...draft]);
  return suites.filter((suite) => carried.has(suite.id) || (suite.available && !suite.internal));
}

/** The draft after the grid toggles `suite`. A suite in the draft is removed
 *  and an available one is added. A switched-off one, or one only an
 *  administrator adds (`bindLocked`) for a viewer who is not one, is only put
 *  back when the saved agent carries it, which undoes a removal made before
 *  the save: a save can drop such a suite, never gain one. A refused toggle
 *  returns the draft itself. */
export function toggledInAgentConfig(
  draft: string[],
  suite: { id: string; available: boolean; bindLocked?: boolean },
  saved: readonly string[],
): string[] {
  if (draft.includes(suite.id)) return draft.filter((id) => id !== suite.id);
  if ((suite.available && !suite.bindLocked) || saved.includes(suite.id)) return [...draft, suite.id];
  return draft;
}
