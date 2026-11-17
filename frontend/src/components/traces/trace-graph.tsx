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

import { useMemo } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Scan } from "lucide-react";
import { buildSpanTree, flattenSpanTree } from "@/components/traces/tree-utils";
import { useColorMode } from "@/hooks/useColorMode";
import type { LLMSpan } from "@/types";

interface TraceGraphProps {
  spans: LLMSpan[];
  selectedSpanId?: string | null;
  onSelectSpan?: (spanId: string) => void;
}

interface PositionedNode {
  id: string;
  name: string;
  depth: number;
  x: number;
  y: number;
}

interface GraphEdge {
  sourceId: string;
  targetId: string;
}

const NODE_W = 170;
const NODE_H = 42;
const PAD_X = 40;
const PAD_Y = 40;
const X_STEP = 220;
const Y_STEP = 70;

// SVG presentation attributes (fill/stroke) can't reference Tailwind tokens, so
// the graph resolves theme-aware colors via `useColorMode` (mirrors agent-flow).
function resolveTraceGraphColors(isLight: boolean) {
  return {
    edge: isLight ? "var(--color-accent)" : "var(--color-accent)",
    nodeFill: isLight ? "hsl(0,0%,100%)" : "hsla(0,0%,100%,0.02)",
    nodeFillSelected: isLight ? "hsla(197,100%,38%,0.12)" : "hsla(197,100%,53%,0.2)",
    nodeStroke: isLight ? "hsl(220,16%,80%)" : "hsla(0,0%,100%,0.15)",
    nodeStrokeSelected: isLight ? "var(--color-accent)" : "var(--color-accent)",
    label: isLight ? "hsl(220,10%,25%)" : "hsl(0,0%,92%)",
  };
}

export function TraceGraph({ spans, selectedSpanId, onSelectSpan }: TraceGraphProps) {
  const colorMode = useColorMode();
  const colors = resolveTraceGraphColors(colorMode === "light");
  const roots = useMemo(() => buildSpanTree(spans), [spans]);

  const graph = useMemo(() => {
    const ordered = flattenSpanTree(roots);
    const depthById = new Map<string, number>();
    const siblingIndex = new Map<number, number>();
    const nodes: PositionedNode[] = [];
    const edges: GraphEdge[] = [];

    for (const node of ordered) {
      const parentDepth = node.parent_span_id ? (depthById.get(node.parent_span_id) ?? -1) : -1;
      const depth = parentDepth + 1;
      depthById.set(node.id, depth);
      const levelIndex = siblingIndex.get(depth) ?? 0;
      siblingIndex.set(depth, levelIndex + 1);

      nodes.push({
        id: node.id,
        name: node.name,
        depth,
        x: PAD_X + depth * X_STEP,
        y: PAD_Y + levelIndex * Y_STEP,
      });

      if (node.parent_span_id) {
        edges.push({ sourceId: node.parent_span_id, targetId: node.id });
      }
    }

    return { nodes, edges };
  }, [roots]);

  const nodeById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node])),
    [graph.nodes],
  );

  const svgWidth = useMemo(() => {
    if (graph.nodes.length === 0) return 400;
    return Math.max(...graph.nodes.map((n) => n.x + NODE_W)) + PAD_X;
  }, [graph.nodes]);

  const svgHeight = useMemo(() => {
    if (graph.nodes.length === 0) return 200;
    return Math.max(...graph.nodes.map((n) => n.y + NODE_H)) + PAD_Y;
  }, [graph.nodes]);

  if (graph.nodes.length === 0) {
    return (
      <EmptyState icon={Scan} title="No graph available" description="This trace has no spans to render." />
    );
  }

  return (
    <Card>
      <CardContent className="p-3">
        <div className="overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-hover">
          <svg
            width={svgWidth}
            height={svgHeight}
            viewBox={`0 0 ${svgWidth} ${svgHeight}`}
            role="img"
            aria-label="Trace graph"
            style={{ display: "block", maxWidth: "100%" }}
          >
            {graph.edges.map((edge) => {
              const source = nodeById.get(edge.sourceId);
              const target = nodeById.get(edge.targetId);
              if (!source || !target) return null;
              const x1 = source.x + NODE_W;
              const y1 = source.y + NODE_H / 2;
              const x2 = target.x;
              const y2 = target.y + NODE_H / 2;
              return (
                <path
                  key={`${edge.sourceId}->${edge.targetId}`}
                  d={`M ${x1} ${y1} C ${(x1 + x2) / 2} ${y1}, ${(x1 + x2) / 2} ${y2}, ${x2} ${y2}`}
                  fill="none"
                  stroke={colors.edge}
                  strokeOpacity="0.55"
                  strokeWidth="1.5"
                />
              );
            })}

            {graph.nodes.map((node) => (
              <g
                key={node.id}
                role="button"
                tabIndex={0}
                onClick={() => onSelectSpan?.(node.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") onSelectSpan?.(node.id);
                }}
                style={{ cursor: "pointer" }}
              >
                <rect
                  x={node.x}
                  y={node.y}
                  rx={8}
                  width={NODE_W}
                  height={NODE_H}
                  fill={selectedSpanId === node.id ? colors.nodeFillSelected : colors.nodeFill}
                  stroke={selectedSpanId === node.id ? colors.nodeStrokeSelected : colors.nodeStroke}
                />
                <text
                  x={node.x + 10}
                  y={node.y + 25}
                  fill={colors.label}
                  fontSize={11}
                >
                  {node.name.length > 24 ? `${node.name.slice(0, 24)}…` : node.name}
                </text>
              </g>
            ))}
          </svg>
        </div>
      </CardContent>
    </Card>
  );
}

