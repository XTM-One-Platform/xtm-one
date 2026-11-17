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
 * Node id → output name → shape, for every node of the flow.
 *
 * The declared structures come from the server (`POST /flows/output-schemas`,
 * the run's own rules and the caller's visibility); the observed ones from the
 * flow's last run. `output-shapes.ts` merges the two per node. The last run is
 * the most recent one: a node it did not reach has a declared shape only.
 */

import { useMemo } from "react";
import {
  useFlowOutputSchemas,
  useFlowRunDetail,
  useFlowRuns,
} from "@/hooks/api/useFlowApi";
import { useDebounce } from "@/hooks/useDebounce";
import type { CanvasEdge, CanvasNode } from "./graph-adapter";
import {
  declaredOutputShapes,
  nodeOutputShapes,
  observedOutputShapes,
  type Shape,
} from "./output-shapes";

export function useOutputShapes(
  flowId: string | undefined,
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  /** Off while no drawer reads the shapes: nothing is fetched. */
  enabled = true,
): Map<string, Record<string, Shape>> {
  // The graph without positions: moving a node changes nothing it declares.
  const graphKey = useMemo(
    () =>
      JSON.stringify({
        nodes: nodes.map((n) => ({ id: n.id, kind: n.data.kind, config: n.data.config ?? {} })),
        edges: edges.map((e) => ({
          id: e.id,
          source: e.source,
          source_handle: e.sourceHandle ?? "out",
          target: e.target,
          target_handle: e.targetHandle ?? "in",
          kind: e.data?.kind,
        })),
      }),
    [nodes, edges],
  );
  // Typing in a prompt re-asks once the typing pauses, not per keystroke.
  const debouncedKey = useDebounce(graphKey, 400);
  const { data: declared } = useFlowOutputSchemas(debouncedKey, enabled);
  const { data: runs } = useFlowRuns(flowId ?? "", enabled && !!flowId);
  const { data: lastRun } = useFlowRunDetail(enabled ? (runs?.runs[0]?.id ?? null) : null);

  // Walked once per run, not on every canvas change (a drag moves `nodes`
  // on each frame): node id → the kind that ran and its outputs' shapes.
  const observedShapes = useMemo(() => {
    const outputs = new Map<string, { kind: string; output: Record<string, unknown> }>();
    // Journal order: a node's LAST completed step wins (loop passes overwrite).
    for (const step of lastRun?.steps ?? []) {
      if (step.status === "completed" && step.output) {
        outputs.set(step.node_id, { kind: step.node_kind, output: step.output });
      }
    }
    const shapes = new Map<string, { kind: string; shapes: Record<string, Shape> }>();
    for (const [nodeId, { kind, output }] of outputs) {
      shapes.set(nodeId, { kind, shapes: observedOutputShapes(output) });
    }
    return shapes;
  }, [lastRun]);

  // The shapes read only each node's id and kind: keyed on those as a value,
  // a drag (a new `nodes` on each frame) recomputes nothing below.
  const nodeKindsKey = useMemo(
    () => JSON.stringify(nodes.map((n) => [n.id, n.data.kind])),
    [nodes],
  );

  return useMemo(() => {
    const shapes = new Map<string, Record<string, Shape>>();
    if (!enabled) return shapes;
    for (const [nodeId, kind] of JSON.parse(nodeKindsKey) as [string, string][]) {
      // An id the last run gave to a node of another kind says nothing here.
      const observed = observedShapes.get(nodeId);
      shapes.set(
        nodeId,
        nodeOutputShapes(
          declaredOutputShapes(declared?.nodes[nodeId]),
          observed?.kind === kind ? observed.shapes : {},
        ),
      );
    }
    return shapes;
  }, [enabled, nodeKindsKey, declared, observedShapes]);
}
