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
 * Per-function scoping of built-in tool groups (`builtin_allowed_tools`).
 *
 * Shape, on an agent and on an assignment: built-in group id -> the function
 * names it keeps. A missing group, or a null value, keeps every function of
 * the group; the whole field null keeps every function of every group. An
 * assignment's scope is intersected with its agent's on the server, so the
 * assignment form only offers the functions the agent keeps.
 */

export type BuiltinAllowedTools = Record<string, string[] | null>;

/**
 * The draft as the API takes it: only the groups still enabled, each with an
 * explicit list (a null entry means "all" and is dropped), `{}` sent as null.
 */
export function builtinAllowedPayload(
  draft: BuiltinAllowedTools | null | undefined,
  enabledGroupIds: readonly string[],
): BuiltinAllowedTools | null {
  const enabled = new Set(enabledGroupIds);
  const kept: BuiltinAllowedTools = {};
  for (const [groupId, functions] of Object.entries(draft ?? {})) {
    if (functions === null || !enabled.has(groupId)) continue;
    kept[groupId] = [...new Set(functions)].sort();
  }
  return Object.keys(kept).length > 0 ? kept : null;
}

/** The functions of a group the scope keeps; `null` when it keeps them all. */
export function scopedFunctions(
  scope: BuiltinAllowedTools | null | undefined,
  groupId: string,
): string[] | null {
  const functions = scope?.[groupId];
  return functions === undefined || functions === null ? null : functions;
}

/**
 * The functions an assignment can pick for a group: the group's own, narrowed
 * to the ones its agent keeps.
 */
export function functionsOfferedToAssignment(
  groupFunctions: readonly string[],
  agentScope: BuiltinAllowedTools | null | undefined,
  groupId: string,
): string[] {
  const agentKept = scopedFunctions(agentScope, groupId);
  if (agentKept === null) return [...groupFunctions];
  const kept = new Set(agentKept);
  return groupFunctions.filter((name) => kept.has(name));
}

/**
 * The scope after a pick in the dialog: `null` (all) removes the group's
 * entry, a list replaces it.
 */
export function withGroupScope(
  scope: BuiltinAllowedTools | null | undefined,
  groupId: string,
  functions: string[] | null,
): BuiltinAllowedTools {
  const next: BuiltinAllowedTools = { ...(scope ?? {}) };
  if (functions === null) delete next[groupId];
  else next[groupId] = functions;
  return next;
}

/**
 * Whether an agent may read other agents' traces: the `observability`
 * group with `inspect_agent_traces` in its scope, or the [DEPRECATED]
 * `inspect_agent_traces` id an agent saved before the merge still carries.
 */
export function readsOtherAgentsTraces(
  builtinToolIds: readonly string[],
  scope: BuiltinAllowedTools | null | undefined,
): boolean {
  if (builtinToolIds.includes("inspect_agent_traces")) return true;
  if (!builtinToolIds.includes("observability")) return false;
  const kept = scopedFunctions(scope, "observability");
  return kept === null || kept.includes("inspect_agent_traces");
}

/**
 * Catalog labels of built-in functions shown in a scope dialog, keyed
 * by function id. A function missing here gets no label, so its row keeps the
 * language-neutral id as its only primary text; add it here (and to en, fr,
 * es, ja) to give it a translated label.
 *
 * @i18n-keys
 */
export const BUILTIN_FUNCTION_LABELS = {
  inspect_my_traces: "Inspect my traces",
  inspect_agent_traces: "Inspect agent traces",
  get_trace_payload: "Get trace payload",
} as const satisfies Record<string, string>;

/**
 * The rows of a built-in group's scope dialog: the function id as
 * secondary text under a translated label ("inspect_my_traces" reads
 * "Inspecter mes traces" in fr); a function with no catalog label has no
 * `label` and shows its id alone, never an untranslated English label.
 */
export function builtinFunctionRows(
  functions: readonly string[],
  t_i18n: (id: string) => string,
): { name: string; label?: string; description: string }[] {
  const labels: Record<string, string> = BUILTIN_FUNCTION_LABELS;
  return functions.map((name) => {
    const known = Object.prototype.hasOwnProperty.call(labels, name) ? labels[name] : undefined;
    return known ? { name, label: t_i18n(known), description: "" } : { name, description: "" };
  });
}

/** The query parameter a release notice opens the Tooling section with (#12192). */
export const ADDED_TOOLS_PARAM = "added";

/**
 * The functions named in a release notice's `added` parameter ("group.fn,...")
 * that the agent still leaves out: its group is scoped, the function exists in
 * the group on this deployment, and the scope does not keep it. A name that
 * no longer applies (added since, group unscoped or removed) is dropped, and
 * so is one the owner dismissed (#12245).
 */
export function pendingAddedFunctions(
  added: string | null | undefined,
  scope: BuiltinAllowedTools | null | undefined,
  groupFunctions: (groupId: string) => readonly string[],
  dismissed: readonly string[] = [],
): { groupId: string; name: string }[] {
  const seen = new Set<string>(dismissed);
  const pending: { groupId: string; name: string }[] = [];
  for (const raw of (added ?? "").split(",")) {
    const dot = raw.indexOf(".");
    if (dot <= 0 || seen.has(raw)) continue;
    seen.add(raw);
    const groupId = raw.slice(0, dot);
    const name = raw.slice(dot + 1);
    const kept = scopedFunctions(scope, groupId);
    if (kept === null || kept.includes(name) || !groupFunctions(groupId).includes(name)) continue;
    pending.push({ groupId, name });
  }
  return pending;
}

/** The agent `config` key of the `group.function` names dismissed from the release notice (#12245). */
const DISMISSED_BUILTIN_TOOLS_KEY = "builtin_tools_dismissed";

/** The `group.function` names the owner dismissed from the release notice, read from the agent's `config`. */
export function dismissedBuiltinFunctions(config: Record<string, unknown> | null | undefined): string[] {
  const raw = config?.[DISMISSED_BUILTIN_TOOLS_KEY];
  return Array.isArray(raw) ? raw.filter((name): name is string => typeof name === "string") : [];
}

/** The scope with *functions* added to their groups' lists (each group already scoped). */
export function withFunctionsAdded(
  scope: BuiltinAllowedTools | null | undefined,
  functions: readonly { groupId: string; name: string }[],
): BuiltinAllowedTools {
  const next: BuiltinAllowedTools = { ...(scope ?? {}) };
  for (const { groupId, name } of functions) {
    const kept = next[groupId];
    if (kept === undefined || kept === null || kept.includes(name)) continue;
    next[groupId] = [...kept, name].sort();
  }
  return next;
}

/** Order-insensitive equality of two normalised scopes (null == {}). */
export function sameBuiltinAllowed(
  a: BuiltinAllowedTools | null | undefined,
  b: BuiltinAllowedTools | null | undefined,
): boolean {
  const left = builtinAllowedPayload(a, Object.keys(a ?? {}));
  const right = builtinAllowedPayload(b, Object.keys(b ?? {}));
  return JSON.stringify(left) === JSON.stringify(right);
}
