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
 * A MITRE Attack Flow: the steps of an attack from left to right, as its
 * builder draws them - an action's card names its tactic and its technique,
 * a condition branches on true or false, an operator joins (AND) or offers a
 * choice (OR), an asset is what an action touches, the start is the flow.
 * The `graph` shape, laid out by `viz/attack-flow`; a plain graph's node is
 * drawn as an action. It opens readable, whole when it fits; it pans, and
 * zooms with Ctrl and the wheel.
 */
import "@xyflow/react/dist/style.css";
import { Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { useMemo } from "react";

import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WidgetGraphData } from "@/types";

import { FLOW, layoutFlow, openingViewport, type FlowKind, type FlowNode } from "../viz/attack-flow";
import { useChartEvents } from "./common";

const HIDDEN = { visibility: "hidden" } as const;

/**
 * What a card's header names.
 * @i18n-keys
 */
const KIND_TITLES = {
  start: "Attack flow",
  action: "Action",
  condition: "Condition",
  asset: "Asset",
} as const;

/** A card's accent by kind: an action in the accent, a condition a warning, an asset and the start quieter. */
const KIND_CLASSES: Record<Exclude<FlowKind, "operator">, { frame: string; header: string }> = {
  start: { frame: "border-accent", header: "bg-accent text-negative-primary" },
  action: { frame: "border-accent/40", header: "bg-accent/15 text-accent" },
  condition: { frame: "border-feedback-warning-primary/50", header: "bg-feedback-warning-primary/15 text-feedback-warning-primary" },
  asset: { frame: "border-elevation-subtle", header: "bg-elevation-hover text-default-secondary" },
};

type CardData = { node: FlowNode; kind: Exclude<FlowKind, "operator"> };

function Handles() {
  return (
    <>
      <Handle type="target" position={Position.Left} style={HIDDEN} />
      <Handle type="source" position={Position.Right} style={HIDDEN} />
    </>
  );
}

function FlowCard({ data }: NodeProps<Node<CardData>>) {
  const { t_i18n } = useFormatter();
  const { node, kind } = data;
  const look = KIND_CLASSES[kind];
  // An action's header names its tactic; its body its technique and name.
  const header = kind === "action" && node.tactic ? node.tactic : t_i18n(KIND_TITLES[kind]);
  // A platform names a technique "[T1566] Phishing": its id is already on the line above.
  const prefix = node.technique ? `[${node.technique}]` : null;
  const name = prefix && node.label.startsWith(prefix) ? node.label.slice(prefix.length).trim() : node.label;
  return (
    <div
      className={cn("flex flex-col overflow-hidden rounded-sm border bg-elevation-default text-[12px] shadow-sm", look.frame)}
      style={{ width: FLOW.width, height: FLOW.height }}
      title={node.description ?? undefined}
      data-flow-kind={kind}
    >
      <div className={cn("truncate px-2 py-1 text-[11px] font-semibold uppercase tracking-wide", look.header)}>{header}</div>
      <div className="flex min-h-0 flex-1 flex-col justify-center gap-0.5 px-2">
        {kind === "action" && node.technique && <span className="font-mono text-[11px] text-default-secondary">{node.technique}</span>}
        <span className="line-clamp-2 font-medium text-default-primary">{name}</span>
      </div>
      <Handles />
    </div>
  );
}

function OperatorNode({ data }: NodeProps<Node<{ node: FlowNode }>>) {
  return (
    <div
      className="flex items-center justify-center rounded-full border border-accent bg-elevation-default text-[12px] font-bold text-accent"
      style={{ width: FLOW.operator, height: FLOW.operator }}
      data-flow-kind="operator"
    >
      {data.node.operator ?? data.node.label}
      <Handles />
    </div>
  );
}

const NODE_TYPES = { card: FlowCard, operator: OperatorNode };

export default function AttackFlow({ graph, width, height }: { graph: WidgetGraphData; width: number; height: number }) {
  const { t_i18n } = useFormatter();
  const mode = useColorMode();
  const { onSelect } = useChartEvents();
  const laid = useMemo(() => layoutFlow(graph), [graph]);
  const nodes = useMemo<Node[]>(
    () =>
      laid.placed.map(({ node, kind, x, y }) => ({
        id: node.id,
        type: kind === "operator" ? "operator" : "card",
        data: kind === "operator" ? { node } : ({ node, kind } satisfies CardData),
        position: { x, y },
        draggable: false,
        targetPosition: Position.Left,
        sourcePosition: Position.Right,
      })),
    [laid],
  );
  const edges = useMemo<Edge[]>(
    () =>
      graph.edges.map((edge, index) => ({
        id: `e${index}`,
        source: edge.source,
        target: edge.target,
        type: "smoothstep",
        // A condition's branches: true and false, in the reader's language.
        label: edge.label === "true" ? t_i18n("True") : edge.label === "false" ? t_i18n("False") : (edge.label ?? undefined),
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      })),
    [graph, t_i18n],
  );
  return (
    <div
      style={{ width, height, ["--xy-node-background-color" as string]: "none", ["--xy-background-color" as string]: "transparent" }}
      data-testid="attack-flow"
    >
      <ReactFlow
        colorMode={mode}
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodeClick={(event, node) => {
          event.stopPropagation();
          const flowNode = (node.data as { node?: FlowNode }).node;
          if (onSelect && flowNode) onSelect({ name: flowNode.label, value: flowNode.technique ?? flowNode.id });
        }}
        // Opened readable: a flow too long for the tile starts at its beginning, and is panned along.
        key={`${width}x${height}`}
        defaultViewport={openingViewport(laid, { width, height })}
        minZoom={0.2}
        zoomActivationKeyCode="Control"
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag
        panOnScroll
        zoomOnScroll={false}
        preventScrolling={false}
        proOptions={{ hideAttribution: true }}
      />
    </div>
  );
}
