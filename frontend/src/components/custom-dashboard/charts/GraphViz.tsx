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
 * A link graph or a network graph, on React Flow (the flow builder's canvas),
 * placed by `viz/graph.ts`: ranks left to right for a link graph, forces for
 * a network. It pans and zooms (the wheel zooms with Ctrl only, so the page
 * still scrolls), and fits itself to its cell. Nodes are coloured by group,
 * a network's sized by degree; an edge is as thick as its value.
 *
 * `colorMode` follows the app's theme: React Flow stamps it on its container,
 * and the design system keys its surfaces on that class (white nodes on a
 * dark page otherwise).
 */
import "@xyflow/react/dist/style.css";

import { Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { useMemo } from "react";

import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import { formatCompactNumber } from "@/lib/utils";
import type { WidgetGraphData } from "@/types";

import { linkGraphLayout, networkLayout, type PlacedNode } from "../viz/graph";
import { vizColor, VIZ_LINE } from "./viz-colors";

type VizNodeData = { node: PlacedNode };

const HIDDEN_HANDLE = { opacity: 0, width: 1, height: 1, minWidth: 0, minHeight: 0, border: 0 } as const;

function BoxNode({ data }: NodeProps<Node<VizNodeData>>) {
  const { node } = data;
  return (
    <div
      className="flex h-full w-full items-center gap-2 rounded-md border border-elevation-subtle bg-elevation-default px-2 content-caption text-default-primary"
      style={{ borderLeft: `3px solid ${vizColor(node.tone)}` }}
      title={node.group ? `${node.label} · ${node.group}` : node.label}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} style={HIDDEN_HANDLE} />
      <span className="min-w-0 truncate">{node.label}</span>
      <Handle type="source" position={Position.Right} isConnectable={false} style={HIDDEN_HANDLE} />
    </div>
  );
}

function DotNode({ data }: NodeProps<Node<VizNodeData>>) {
  const { node } = data;
  const centre = { ...HIDDEN_HANDLE, top: "50%", left: "50%" } as const;
  return (
    <div className="relative h-full w-full" title={node.group ? `${node.label} · ${node.group}` : node.label}>
      <div className="h-full w-full rounded-full" style={{ background: vizColor(node.tone), boxShadow: "0 0 0 2px var(--bg-elevation-default)" }} />
      <Handle type="target" position={Position.Top} isConnectable={false} style={centre} />
      <Handle type="source" position={Position.Bottom} isConnectable={false} style={centre} />
      <span className="pointer-events-none absolute left-1/2 top-full mt-1 max-w-36 -translate-x-1/2 truncate whitespace-nowrap content-caption text-default-primary">
        {node.label}
      </span>
    </div>
  );
}

const NODE_TYPES = { box: BoxNode, dot: DotNode };

export default function GraphViz({
  graph,
  kind,
  width,
  height,
}: {
  graph: WidgetGraphData;
  kind: "link" | "network";
  width: number;
  height: number;
}) {
  const { t_i18n } = useFormatter();
  const colorMode = useColorMode();
  const placed = useMemo(
    () => (kind === "link" ? linkGraphLayout(graph) : networkLayout(graph, width, height)),
    // A network is placed for its first size: a resize fits the view, it does not move the nodes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [graph, kind],
  );
  const nodes = useMemo<Node<VizNodeData>[]>(
    () =>
      placed.nodes.map((node) => ({
        id: node.id,
        type: kind === "link" ? "box" : "dot",
        position: { x: node.x, y: node.y },
        data: { node },
        width: node.w,
        height: node.h,
        draggable: false,
        selectable: false,
        connectable: false,
      })),
    [placed, kind],
  );
  const most = Math.max(1, ...placed.edges.map((edge) => edge.value ?? 1));
  const edges = useMemo<Edge[]>(
    () =>
      placed.edges.map((edge, index) => ({
        id: `e${index}`,
        source: edge.source,
        target: edge.target,
        type: kind === "link" ? "default" : "straight",
        label: edge.label ?? (kind === "link" && edge.value !== null && edge.value > 1 ? formatCompactNumber(edge.value) : undefined),
        style: { stroke: VIZ_LINE, strokeWidth: 1 + (3 * (edge.value ?? 1)) / most, strokeOpacity: 0.9 },
        labelStyle: { fill: "var(--text-default-secondary)", fontSize: 10 },
        labelBgStyle: { fill: "var(--bg-elevation-default)" },
        selectable: false,
        focusable: false,
      })),
    [placed, kind, most],
  );
  return (
    <div
      className="h-full w-full overflow-hidden rounded-md"
      data-viz={kind === "link" ? "linkgraph" : "networkgraph"}
      role="img"
      aria-label={t_i18n("Graph of {nodes, plural, one {# node} other {# nodes}} and {edges, plural, one {# link} other {# links}}", { nodes: placed.nodes.length, edges: placed.edges.length })}
    >
      <ReactFlow
        colorMode={colorMode}
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        // Room for the labels under a network's nodes.
        fitViewOptions={{ padding: kind === "network" ? 0.22 : 0.12 }}
        minZoom={0.2}
        maxZoom={2.5}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        zoomOnScroll={false}
        zoomActivationKeyCode="Control"
        preventScrolling={false}
        proOptions={{ hideAttribution: true }}
        style={{ background: "transparent" }}
      />
      {placed.groups.length > 0 ? (
        <ul className="pointer-events-none absolute bottom-1 left-2 flex flex-wrap gap-x-3 gap-y-0.5" aria-hidden="true">
          {placed.groups.map((group, index) => (
            <li key={group} className="flex items-center gap-1 content-caption text-default-secondary">
              <span className="inline-block h-2 w-2 rounded-full" style={{ background: vizColor(index) }} />
              {group}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
