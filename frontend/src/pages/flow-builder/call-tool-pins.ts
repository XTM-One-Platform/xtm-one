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
 * The tool a Call-tool node picked. The node stores a tool NAME and,
 * for a custom tool, the id of the one picked for it: `tool_id`, and in LLM
 * mode `candidate_tool_ids` (candidate name -> id). A run executes that tool
 * while it is still an enabled tool of that name the flow can use, and
 * resolves the name otherwise. Several custom tools may share a name, so the
 * drawer offers a custom tool by its id and a built-in by its name.
 *
 * No React and no `@/` import, so `call-tool-pins.test.ts` runs under
 * `node --test`.
 */

export interface CallableTool {
  name: string;
  source: string;
  id?: string | null;
  designated?: boolean;
}

interface CandidatePick {
  name: string;
  pin: string | null;
}

/** What a picker item stands for: a custom tool's id, a built-in's name. */
export function toolKey(tool: CallableTool): string {
  return tool.source === "builtin" || !tool.id ? tool.name : tool.id;
}

/** The pin a picked tool stores next to its name: a custom tool's id. */
export function pinOf(tool: CallableTool): string | null {
  return tool.source === "builtin" ? null : (tool.id ?? null);
}

/**
 * The listed tool a node storing `name` and `pin` runs, read the way a run
 * reads it: a built-in wins its name, then the pinned tool while it still
 * carries the name, and a node without a pin runs the tool its name
 * designates. A pin the list does not hold shows nothing: the drawer flags
 * the node rather than lend it another tool's schema.
 */
export function shownTool<T extends CallableTool>(
  tools: readonly T[],
  name: string,
  pin: string | null | undefined,
): T | undefined {
  if (!name) return undefined;
  const builtin = tools.find((t) => t.source === "builtin" && t.name === name);
  if (builtin) return builtin;
  if (pin) {
    return tools.find((t) => t.source !== "builtin" && t.id === pin && t.name === name);
  }
  return tools.find((t) => t.source !== "builtin" && t.name === name && t.designated);
}

/**
 * What tells custom tools sharing a name apart in the picker, by picker key:
 * `describe(tool)` (whose it is), followed by the start of its id where that
 * still repeats within the name - two company-managed tools, two owners of
 * one name. A name only one tool carries needs no hint.
 */
export function toolHints<T extends CallableTool>(
  tools: readonly T[],
  describe: (tool: T) => string,
): Map<string, string> {
  const byName = new Map<string, T[]>();
  for (const tool of tools) {
    if (tool.source === "builtin" || !tool.id) continue;
    byName.set(tool.name, [...(byName.get(tool.name) ?? []), tool]);
  }
  const hints = new Map<string, string>();
  for (const group of byName.values()) {
    if (group.length < 2) continue;
    const described = group.map((tool) => ({ tool, hint: describe(tool) }));
    for (const { tool, hint } of described) {
      const repeated = described.filter((other) => other.hint === hint).length > 1;
      hints.set(toolKey(tool), repeated ? `${hint}, ${(tool.id ?? "").slice(0, 8)}` : hint);
    }
  }
  return hints;
}

/** A node's candidates as stored, each with the pin stored for it. */
export function readCandidates(names: readonly string[], pins: unknown): CandidatePick[] {
  const byName =
    pins && typeof pins === "object" && !Array.isArray(pins)
      ? (pins as Record<string, unknown>)
      : {};
  return names.map((name) => {
    const pin = byName[name];
    return { name, pin: typeof pin === "string" && pin ? pin : null };
  });
}

/** The picker key of a stored candidate: its tool's, else its name (an
 *  unlisted candidate the picker keeps but cannot show). */
export function candidateKey(tools: readonly CallableTool[], pick: CandidatePick): string {
  const tool = shownTool(tools, pick.name, pick.pin);
  return tool ? toolKey(tool) : pick.name;
}

/**
 * What a new candidate selection stores; `keys` are picker keys, in order.
 * A candidate already stored keeps its name and pin as stored, so a node
 * written before pins keeps resolving its names; a newly picked tool stores
 * its name and pin. One tool per name: a later pick replaces an earlier one
 * of the same name.
 */
export function pickCandidates(
  tools: readonly CallableTool[],
  current: readonly CandidatePick[],
  keys: readonly string[],
): { candidate_tools: string[]; candidate_tool_ids: Record<string, string> | null } {
  const picks: CandidatePick[] = [];
  for (const key of keys) {
    const stored = current.find((c) => candidateKey(tools, c) === key);
    const tool = stored ? undefined : tools.find((t) => toolKey(t) === key);
    const pick = stored ?? (tool ? { name: tool.name, pin: pinOf(tool) } : undefined);
    if (!pick) continue;
    const sameName = picks.findIndex((p) => p.name === pick.name);
    if (sameName >= 0) picks.splice(sameName, 1);
    picks.push(pick);
  }
  const pins = Object.fromEntries(
    picks.flatMap((p) => (p.pin ? [[p.name, p.pin] as const] : [])),
  );
  return {
    candidate_tools: picks.map((p) => p.name),
    candidate_tool_ids: Object.keys(pins).length > 0 ? pins : null,
  };
}
