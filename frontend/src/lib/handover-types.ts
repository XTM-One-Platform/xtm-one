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
 * The handover type map of the General Assistant and of an
 * agent. The backend half is `app/services/handover_types.py`; both are
 * checked against `tests_e2e/fixtures/handover-type-cases.json`.
 *
 * An EMPTY map is the legacy mode in which every handover tool (delegate,
 * consult and transfer) is registered for every target: `general_chat`,
 * `agent_chat` and the assignment engine read it so, and so does every
 * configuration saved before a type was picked for a target. Every screen
 * shows such a target as running every type (`ALL_HANDOVER_TYPES`), and the
 * map stays empty until a type is picked: filling it in on an unrelated save,
 * or when a target is added next to them, would narrow every target to
 * delegate. A configuration is in the legacy mode only while it has targets
 * (`runsEveryHandoverType`): a first target, like a target added to a map that
 * holds a type, is recorded as delegate.
 *
 * Settings > AI and models > General Assistant saves a map that holds a type written out for
 * every target, delegate for one without a type or with a type the General
 * Assistant does not run (a `mandatory_transfer` saved before the API refused
 * one): that is how those run. An agent keeps every type it stores.
 *
 * A type is set per target and none of them means "every type", so the first
 * type picked in the legacy mode makes every other target delegate:
 * `withHandoverType` and `withHandoverEdge` name those targets, and the screen
 * asks before it narrows them.
 */

type HandoverTypeMap = Readonly<Record<string, string>>;

/** The picker value of a target in the legacy mode, which runs every type. */
export const ALL_HANDOVER_TYPES = "__all__";

/** The stored entry of a target that runs every type in a map that holds a
 *  type (`app/services/handover_types.py` ALL_TYPES). */
const STORED_ALL_HANDOVER_TYPES = "all";

function holdsAType(types: HandoverTypeMap | null | undefined): types is HandoverTypeMap {
  return !!types && Object.keys(types).length > 0;
}

/** The legacy mode: no target has a type, so each one runs every type. */
export function isLegacyHandoverMap(types: HandoverTypeMap | null | undefined): boolean {
  return !holdsAType(types);
}

/** The type `agentId` runs, as the picker shows it. */
export function shownHandoverType(
  types: HandoverTypeMap | null | undefined,
  agentId: string,
  runnableTypes: readonly string[],
): string {
  if (!holdsAType(types)) return ALL_HANDOVER_TYPES;
  const type = types[agentId];
  // A merged map (a fleet node of the map) marks a target that runs every type,
  // and so does a stored "all" (update_agent's merge, #2207).
  if (type === ALL_HANDOVER_TYPES || type === STORED_ALL_HANDOVER_TYPES) return ALL_HANDOVER_TYPES;
  if (type !== undefined && runnableTypes.includes(type)) return type;
  // An entry of several types ("consult|delegate") is shown by the first one
  // it runs, in the backend's order (consult, delegate, transfer).
  const parts = (type ?? "").split("|");
  return ["consult", "delegate", "transfer"].find((t) => parts.includes(t) && runnableTypes.includes(t)) ?? "delegate";
}

export function runnableHandoverTypes(
  targetIds: readonly string[],
  stored: HandoverTypeMap | null | undefined,
  runnableTypes: readonly string[],
): Record<string, string> {
  if (!holdsAType(stored)) return {};
  const runnable: Record<string, string> = {};
  for (const id of targetIds) runnable[id] = shownHandoverType(stored, id, runnableTypes);
  return runnable;
}

/** The type map once `agentId` is added next to `targetIds`: it joins targets
 *  that run every type, and is recorded as delegate anywhere else. */
export function withAddedHandoverTarget(
  targetIds: readonly string[],
  types: HandoverTypeMap | null | undefined,
  agentId: string,
): Record<string, string> {
  if (runsEveryHandoverType(targetIds, types)) return {};
  return { ...(types ?? {}), [agentId]: "delegate" };
}

interface HandoverTarget {
  handover_agent_ids?: readonly string[] | null;
  handover_types?: HandoverTypeMap | null;
}

/**
 * Whether a handover of `type` into `target` is a synchronous consult into an
 * agent that itself delegates in the background, so the consult answer may
 * come back before the target's delegated tasks finish. The handover editor
 * shows it as a hint on the type chip of that target; nothing else reads it.
 */
export function consultsBackgroundDelegator(type: string, target: HandoverTarget | null | undefined): boolean {
  if (type !== "consult" || !target) return false;
  const ids = target.handover_agent_ids ?? [];
  const types = target.handover_types ?? {};
  return ids.some((id) => (types[id] || "delegate") === "delegate");
}

/** The type map once `agentId` is no longer a handover target. */
export function withRemovedHandoverTarget(types: HandoverTypeMap, agentId: string): Record<string, string> {
  const { [agentId]: _removed, ...rest } = types;
  return rest;
}

interface HandoverTypeChange {
  types: Record<string, string>;
  /** The other targets that go from every handover type to delegate. */
  narrowed: string[];
}

/** The type map once `type` is picked for `agentId`, one of `targetIds`. */
export function withHandoverType(
  targetIds: readonly string[],
  types: HandoverTypeMap,
  agentId: string,
  type: string,
): HandoverTypeChange {
  if (holdsAType(types)) return { types: { ...types, [agentId]: type }, narrowed: [] };
  const typed: Record<string, string> = {};
  for (const id of targetIds) typed[id] = id === agentId ? type : "delegate";
  typed[agentId] = type;
  return { types: typed, narrowed: Object.keys(typed).filter((id) => id !== agentId) };
}

/** Whether the targets each run every handover type: there are targets and no
 *  type. A first target is typed (`withAddedHandoverTarget`). */
export function runsEveryHandoverType(
  targetIds: readonly string[],
  types: HandoverTypeMap | null | undefined,
): boolean {
  return targetIds.length > 0 && !holdsAType(types);
}

/** An agent's explicit map written out for `targetIds`: each target keeps the
 *  type it stores, delegate for one without, which is how it runs. */
export function explicitHandoverTypes(targetIds: readonly string[], types: HandoverTypeMap): Record<string, string> {
  const written: Record<string, string> = {};
  for (const id of targetIds) written[id] = types[id] || "delegate";
  return written;
}

interface HandoverEdgeChange {
  ids: string[];
  types: Record<string, string>;
  /** The other targets that go from every handover type to delegate. */
  narrowed: string[];
}

/**
 * An agent's handover once `agentId` runs `type`, added as a target when it is
 * not one yet: what the flow map saves for an edge it draws or edits. In the
 * legacy mode `ALL_HANDOVER_TYPES` keeps the map empty and a type types every
 * target, naming the ones it narrows; otherwise only that target changes.
 */
export function withHandoverEdge(
  targetIds: readonly string[],
  types: HandoverTypeMap | null | undefined,
  agentId: string,
  type: string,
): HandoverEdgeChange {
  const ids = targetIds.includes(agentId) ? [...targetIds] : [...targetIds, agentId];
  if (runsEveryHandoverType(targetIds, types)) {
    if (type === ALL_HANDOVER_TYPES) return { ids, types: {}, narrowed: [] };
    const change = withHandoverType(ids, {}, agentId, type);
    return { ids, types: change.types, narrowed: change.narrowed };
  }
  const explicit = type === ALL_HANDOVER_TYPES ? "delegate" : type;
  return { ids, types: { ...(types ?? {}), [agentId]: explicit }, narrowed: [] };
}

/**
 * An agent's handover once `agentId` is no longer a target: its entry is
 * dropped. When that empties a map that held a type, the others are written
 * out as delegate instead, so removing its only typed target never turns them
 * into the legacy mode that runs every type.
 */
export function withoutHandoverEdge(
  targetIds: readonly string[],
  types: HandoverTypeMap | null | undefined,
  agentId: string,
): { ids: string[]; types: Record<string, string> } {
  const ids = targetIds.filter((id) => id !== agentId);
  if (!holdsAType(types)) return { ids, types: {} };
  const rest = withRemovedHandoverTarget(types, agentId);
  return { ids, types: holdsAType(rest) || ids.length === 0 ? rest : explicitHandoverTypes(ids, types) };
}
