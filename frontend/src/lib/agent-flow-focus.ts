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
 * Focus on the agent handover map (Agentic Flow > Map).
 *
 * Focusing agents keeps them, every agent they hand over to or receive from,
 * and the handovers touching them. The map either dims the rest (`dim`) or
 * lays out the focused neighbourhood alone (`isolate`).
 */

export type AgentFlowFocusMode = "dim" | "isolate";

interface FocusEdge {
  sourceId: string;
  targetId: string;
}

/**
 * The focused ids the graph draws, or `null` when there is none. An id that is
 * no node (a stale `?focus=` link, a deleted agent) focuses nothing, so a focus
 * made only of such ids leaves the whole map shown, undimmed.
 */
export function focusOnGraph(
  focused: ReadonlySet<string> | null | undefined,
  graphIds: ReadonlySet<string>,
): Set<string> | null {
  const shown = new Set([...(focused ?? [])].filter((id) => graphIds.has(id)));
  return shown.size > 0 ? shown : null;
}

/** Whether a handover belongs to the focus: it starts or ends on a focused agent. */
export function edgeTouchesFocus(edge: FocusEdge, focused: ReadonlySet<string>): boolean {
  return focused.has(edge.sourceId) || focused.has(edge.targetId);
}

/** The focused agents plus every agent one handover away from them, either direction. */
export function focusNeighbourhood(
  edges: readonly FocusEdge[],
  focused: ReadonlySet<string>,
): Set<string> {
  const ids = new Set(focused);
  for (const edge of edges) {
    if (focused.has(edge.sourceId)) ids.add(edge.targetId);
    if (focused.has(edge.targetId)) ids.add(edge.sourceId);
  }
  return ids;
}

/**
 * What an isolated map shows: the focused neighbourhood and the handovers
 * touching a focused agent. A handover between two neighbours is left out, as
 * the dimmed map dims it.
 */
export function isolateFocus<E extends FocusEdge>(
  edges: readonly E[],
  focused: ReadonlySet<string>,
): { nodeIds: Set<string>; edges: E[] } {
  return {
    nodeIds: focusNeighbourhood(edges, focused),
    edges: edges.filter((edge) => edgeTouchesFocus(edge, focused)),
  };
}
