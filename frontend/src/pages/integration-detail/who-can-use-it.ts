// SPDX-License-Identifier: Apache-2.0

import type { Agent, ResourceExposurePath } from "@/types";

/** The agent's bindings without this integration (the unbind write); the
 *  sharing confirmation's "Unbind them too" writes the same. */
export { bindingsWithout } from "@/hooks/useCredentialSharingConfirm";

/** One agent row: an agent path of the exposure answer (when the viewer
 *  manages the integration) or a bound agent alone (when they do not). */
interface AgentAccessRow {
  key: string;
  agent: Agent | null;
  path: ResourceExposurePath | null;
  name: string;
  ownerName: string | null;
  orphaned: boolean;
}

/**
 * Each agent ONCE: the exposure answer's agent paths first (they carry the
 * audience and the orphan flag), then the bound agents the answer does not
 * name. Paths of other kinds (assignments, flows, knowledge bases...) are not
 * agent rows.
 */
export function agentAccessRows(paths: ResourceExposurePath[], agents: Agent[]): AgentAccessRow[] {
  const byId = new Map(agents.map((a) => [a.id, a]));
  const rows: AgentAccessRow[] = [];
  const seen = new Set<string>();
  paths.forEach((path, index) => {
    if (path.kind !== "agent") return;
    const id = path.entity?.id ?? null;
    const key = id ?? `hidden-${index}`;
    if (seen.has(key)) return;
    seen.add(key);
    const agent = id ? byId.get(id) ?? null : null;
    rows.push({
      key,
      agent,
      path,
      name: path.entity?.visible && path.entity.name ? path.entity.name : agent?.name ?? "",
      ownerName: path.entity?.owner_name ?? agent?.owner_name ?? null,
      orphaned: !!path.orphaned,
    });
  });
  for (const agent of agents) {
    if (seen.has(agent.id)) continue;
    seen.add(agent.id);
    rows.push({ key: agent.id, agent, path: null, name: agent.name, ownerName: agent.owner_name, orphaned: false });
  }
  return rows;
}
