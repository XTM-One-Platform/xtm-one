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
 * Pure helpers of the fleet page edition: who
 * may edit, the members panel's hierarchy, the parents a member may take
 * without a cycle, and the agents each picker offers.
 *
 * Membership is never an agent edit: these helpers only shape what the panel
 * sends to `/api/v1/fleets`; the API owns every rule and refuses the rest.
 */
import type { Fleet, FleetMember } from "@/hooks/api/useFleetApi";

/** Whether the caller edits the fleet, and why not when it cannot. */
interface FleetEditRights {
  canEdit: boolean;
  /** Untranslated reason shown on a read-only panel; empty when `canEdit`. */
  reason: string;
}

export const READ_ONLY_REASON =
  "Only the fleet's owner and people granted configuration on it can edit it.";
/** The lock of the built-in Platform Health fleet: no write for anyone, administrators included (#101 S1b). */
export const HEALTH_READ_ONLY_REASON = "Platform Health is built in and read-only.";
export const ARCHIVED_REASON = "This fleet is archived: restore it to edit its members.";
export const BUILTIN_REASON = "Built-in fleets ship with the platform and are read-only.";
/**
 * The Members view of a built-in fleet, where its capability slots stay the
 * customer's (#914): what is locked and what stays yours, never "read-only".
 */
export const BUILTIN_MEMBERS_REASON =
  "Built-in fleets ship with the platform: their members, handovers and gates are locked; the integrations bound to their capability slots stay yours.";
export const BUILTIN_ADMIN_REASON =
  "Built-in fleets ship with the platform and are read-only. An administrator can still hide their activity from non-admins.";

/**
 * Whether the caller sets "Hide activity from non-admins" on a fleet whose
 * other settings are read-only: an administrator, on a built-in fleet that is
 * company-managed. The API takes that field alone on a built-in fleet (#11624).
 */
export function builtinActivityEditable(
  fleet: (Pick<Fleet, "can_manage" | "is_company_managed"> & Partial<Pick<Fleet, "is_builtin">>) | null | undefined,
  isAdmin: boolean,
): boolean {
  return isAdmin && !!fleet?.can_manage && !!fleet?.is_builtin && !!fleet?.is_company_managed;
}

/**
 * The untranslated confirmation and toast texts of a removal, the same
 * from the members panel and from the graph's node menu. Only a home member
 * goes back to the playground: a connected agent or fleet only loses its
 * connection.
 */
export function removeMemberCopy(member: Pick<FleetMember, "name" | "role">): {
  title: string;
  titleValues: { name: string };
  description: string | undefined;
} {
  return {
    title: "Remove {name} from the fleet?",
    titleValues: { name: member.name },
    description: member.role === "member" ? "The agent is back in the playground, unchanged." : undefined,
  };
}

/** The colour the settings picker shows for a fleet that has none (a native colour input needs one). */
export const DEFAULT_FLEET_COLOR = "#00bcd4";

/**
 * `can_manage` is computed by the API for the caller (owner, `config` grantee
 * or admin). An archived fleet keeps its settings editable (to restore it) but
 * not its members.
 */
export function fleetEditRights(
  fleet: (Pick<Fleet, "can_manage" | "is_archived"> & Partial<Pick<Fleet, "is_builtin" | "read_only_reason">>) | null | undefined,
): {
  settings: FleetEditRights;
  members: FleetEditRights;
} {
  // A locked surface states the real reason of its lock (UX r1 B4 on #464).
  if (fleet?.read_only_reason) {
    const locked = { canEdit: false, reason: HEALTH_READ_ONLY_REASON };
    return { settings: locked, members: locked };
  }
  if (!fleet?.can_manage) {
    const ro = { canEdit: false, reason: READ_ONLY_REASON };
    return { settings: ro, members: ro };
  }
  if (fleet.is_builtin) {
    // The seed rewrites a built-in fleet on every boot (backend `fleet_seed`).
    const builtin = { canEdit: false, reason: BUILTIN_REASON };
    return { settings: builtin, members: builtin };
  }
  return {
    settings: { canEdit: true, reason: "" },
    members: fleet.is_archived ? { canEdit: false, reason: ARCHIVED_REASON } : { canEdit: true, reason: "" },
  };
}

/**
 * Whether the fleet's graph edits handovers (add an edge, edit or delete one,
 * the node menu's "Add a handover"). An archived fleet is read as it was, and
 * a built-in fleet's handovers are rewritten by the seed on every boot: the
 * graph of either is read-only, like its members. The per-agent rights stay
 * with `useHandoverEdgeEditing`.
 */
export function graphHandoversEditable(
  fleet: (Pick<Fleet, "is_archived"> & Partial<Pick<Fleet, "is_builtin">>) | null | undefined,
): boolean {
  return !fleet?.is_archived && !fleet?.is_builtin;
}

/** A home member in the panel: its row and its depth in the hierarchy. */
interface MemberTreeRow {
  member: FleetMember;
  depth: number;
}

/**
 * The home members as an indented list: the orchestrator first, then roots
 * by name, each followed by its children by name. A parent the caller cannot
 * see, or a cycle in stored data, never hides a member: it is drawn as a root.
 */
export function memberTree(members: readonly FleetMember[]): MemberTreeRow[] {
  const home = members.filter((m) => m.role === "member");
  const byId = new Map(home.map((m) => [m.id, m]));
  const children = new Map<string, FleetMember[]>();
  const roots: FleetMember[] = [];
  for (const m of home) {
    const parent = m.parent_member_id && m.parent_member_id !== m.id ? byId.get(m.parent_member_id) : undefined;
    if (parent) children.set(parent.id, [...(children.get(parent.id) ?? []), m]);
    else roots.push(m);
  }
  const order = (a: FleetMember, b: FleetMember) =>
    Number(b.is_orchestrator) - Number(a.is_orchestrator) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id);
  const rows: MemberTreeRow[] = [];
  const seen = new Set<string>();
  const walk = (m: FleetMember, depth: number) => {
    if (seen.has(m.id)) return;
    seen.add(m.id);
    rows.push({ member: m, depth });
    for (const child of [...(children.get(m.id) ?? [])].sort(order)) walk(child, depth + 1);
  };
  for (const root of [...roots].sort(order)) walk(root, 0);
  // A cycle among stored rows leaves members unreached: list them as roots.
  for (const m of [...home].sort(order)) walk(m, 0);
  return rows;
}

/** The member and every member under it in the hierarchy. */
export function subtreeIds(members: readonly FleetMember[], memberId: string): Set<string> {
  const ids = new Set<string>([memberId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const m of members) {
      if (m.role === "member" && m.parent_member_id && ids.has(m.parent_member_id) && !ids.has(m.id)) {
        ids.add(m.id);
        grew = true;
      }
    }
  }
  return ids;
}

/** The home members a member may report to: never itself nor one of its descendants. */
export function parentCandidates(members: readonly FleetMember[], memberId: string): FleetMember[] {
  const excluded = subtreeIds(members, memberId);
  return members
    .filter((m) => m.role === "member" && !excluded.has(m.id))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Whether dropping `draggedId` on `targetId` re-parents it: two distinct home
 * members, the target outside the dragged member's subtree, and not already
 * its parent.
 */
export function canReparent(members: readonly FleetMember[], draggedId: string, targetId: string | null): boolean {
  const dragged = members.find((m) => m.id === draggedId && m.role === "member");
  if (!dragged) return false;
  if (targetId === null) return dragged.parent_member_id !== null;
  if (draggedId === targetId) return false;
  const target = members.find((m) => m.id === targetId && m.role === "member");
  if (!target) return false;
  if (dragged.parent_member_id === targetId) return false;
  return !subtreeIds(members, draggedId).has(targetId);
}

/**
 * The re-parent a drop on the fleet graph asks for: the graph speaks
 * agent ids, the hierarchy member ids. Dropping the node of home member A on
 * the node of home member B makes B the parent of A, under `canReparent`;
 * anything else (a ghost, the agent itself, a descendant, the current parent)
 * is null and writes nothing.
 */
export function graphReparent(
  members: readonly FleetMember[],
  draggedAgentId: string,
  targetAgentId: string,
): { member: FleetMember; parentId: string } | null {
  const home = (agentId: string) => members.find((m) => m.role === "member" && m.agent_id === agentId);
  const member = home(draggedAgentId);
  const parent = home(targetAgentId);
  if (!member || !parent || !canReparent(members, member.id, parent.id)) return null;
  return { member, parentId: parent.id };
}

/** The connected rows: agents then fleets, each by name. */
export function connectedRows(members: readonly FleetMember[]): FleetMember[] {
  return members
    .filter((m) => m.role === "connected")
    .sort((a, b) => Number(!!a.target_fleet_id) - Number(!!b.target_fleet_id) || a.name.localeCompare(b.name));
}

/** The agents a "Connect an agent" picker offers: neither a home member nor already connected. */
export function connectableAgentIds(agentIds: readonly string[], members: readonly FleetMember[]): string[] {
  const taken = new Set(members.filter((m) => m.agent_id).map((m) => m.agent_id as string));
  return agentIds.filter((id) => !taken.has(id));
}

/** The fleets a "Connect a fleet" picker offers: never this fleet nor one already connected. */
export function connectableFleetIds(fleetIds: readonly string[], fleetId: string, members: readonly FleetMember[]): string[] {
  const taken = new Set(members.filter((m) => m.target_fleet_id).map((m) => m.target_fleet_id as string));
  return fleetIds.filter((id) => id !== fleetId && !taken.has(id));
}

/** The fleets a member may move to: those the caller manages, never archived, never this one. */
export function moveTargets<F extends Pick<Fleet, "id" | "can_manage" | "is_archived" | "name">>(
  fleets: readonly F[],
  fleetId: string,
): F[] {
  return fleets
    .filter((f) => f.id !== fleetId && f.can_manage && !f.is_archived)
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * The exposure list after a toggle: the orchestrator is always exposed (the
 * API adds it), so it is never sent and never toggled off.
 */
export function toggledExposure(
  exposed: readonly string[],
  agentId: string,
  orchestratorId: string | null,
): string[] {
  if (agentId === orchestratorId) return exposed.filter((id) => id !== orchestratorId);
  const next = exposed.includes(agentId) ? exposed.filter((id) => id !== agentId) : [...exposed, agentId];
  return next.filter((id) => id !== orchestratorId);
}

/**
 * The home members exposed on purpose (their stored flag, `exposed_explicitly`).
 * The API also exposes the orchestrator implicitly, in `fleet.exposed_agent_ids`
 * and in a member's `is_exposed`, so neither is this list: seeding the settings
 * form from them would send the previous orchestrator back as an explicit
 * exposure after a change.
 */
export function explicitExposure(members: readonly FleetMember[]): string[] {
  return members
    .filter((m) => m.role === "member" && m.exposed_explicitly && m.agent_id)
    .map((m) => m.agent_id as string);
}

/**
 * The exposure list a settings save sends, or `null` when nothing changes.
 * Both sides drop the orchestrator the save leaves in place (the API exposes it
 * anyway), and a new orchestrator always resends the list so the previous one
 * keeps only the exposure it had on its own flag.
 */
export function exposureToSend(
  draft: readonly string[],
  stored: readonly string[],
  nextOrchestratorId: string | null,
  orchestratorChanged: boolean,
): string[] | null {
  const next = [...new Set(draft.filter((id) => id !== nextOrchestratorId))];
  const before = [...new Set(stored.filter((id) => id !== nextOrchestratorId))];
  const same = [...next].sort().join(",") === [...before].sort().join(",");
  return same && !orchestratorChanged ? null : next;
}

/** The settings fields that changed, so a save sends only them. */
export function changedSettings<T extends Record<string, unknown>>(before: T, after: T): Partial<T> {
  const out: Partial<T> = {};
  for (const key of Object.keys(after) as (keyof T)[]) {
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) out[key] = after[key];
  }
  return out;
}
