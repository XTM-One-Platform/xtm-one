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
 * `pathGraph`: outcomes laid out by group and linked on hover, as OpenAEV draws
 * its attack path (`attack_paths/AttackPath.tsx`, `AttackPatternNode.tsx`,
 * `DraggableEdge.tsx`, `AttackPathContextLayer.tsx`; reference, OpenAEV
 * section 11), with React Flow.
 *
 * A header per group at the top (`50 + 200x`, 0), each outcome of the group
 * under it (`50 + 200x`, `50 + 150y`) as a box coloured by its success rate
 * with its count; hovering one draws an arrow to every node of each outcome it
 * leads to (an arc above when both sit on one row, the source on the right).
 * The run's start under the first column, its end under the last, linked by
 * its duration. The success-rate legend sits on the right. Two of the module's
 * defects are drawn as intended (reference, "Defects drawn as intended"): its
 * nodes never get their counts and are all muted, and its legend sits left.
 * Without the run it reads, it asks for one.
 */
import "@xyflow/react/dist/style.css";
import {
  BaseEdge,
  ConnectionLineType,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  getBezierPath,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { useMemo, useState } from "react";

import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";

import type { WidgetOutcome, WidgetOutcomesData } from "@/types";

import { PATH, pathEdges, pathNodes, pathPosition, rowArc, type MatrixCell } from "../viz/outcomes";
import { useChartEvents } from "./common";
import { formatDuration, formatNamedDate } from "./formats";
import { OutcomeBox, RateLegend } from "./OutcomeMatrix";
import { dataHref } from "./links";

const GEOLOGICA = "var(--font-sans-geologica)";
const ERROR = "var(--color-feedback-error-primary)";

type OutcomeNodeData = { item: WidgetOutcome; rate: number | null; onHover: (id: string | null) => void };

/** The box an outcome node draws: its success rate and its resolved checks. */
function cellOf(item: WidgetOutcome, rate: number | null): MatrixCell {
  const total = item.success + item.failed;
  return {
    id: item.id ?? item.label,
    code: item.code ?? "",
    label: item.label,
    subIds: item.subIds ?? [],
    success: item.success,
    failure: item.failed,
    total,
    present: true,
    tested: total > 0 && rate !== null,
    rate,
    successCount: rate ? Math.round(rate * total) : 0,
  };
}

function OutcomeNode({ id, data }: NodeProps<Node<OutcomeNodeData>>) {
  return (
    <div onMouseEnter={() => data.onHover(id)} onMouseLeave={() => data.onHover(null)} data-node={id}>
      <OutcomeBox cell={cellOf(data.item, data.rate)} width={PATH.nodeWidth} />
      <Handle type="target" position={Position.Left} isConnectable style={HIDDEN} />
      <Handle type="source" position={Position.Right} isConnectable style={HIDDEN} />
    </div>
  );
}

/** A phase's header (no border, left-aligned) or a date: React Flow's default node, its handles hidden. */
function LabelNode({ data }: NodeProps<Node<{ label: string; header?: boolean }>>) {
  return (
    <div
      className="text-[12px]"
      style={{
        width: PATH.nodeWidth,
        padding: 10,
        borderRadius: 3,
        textAlign: data.header ? "left" : "center",
        border: data.header ? "none" : "var(--xy-node-border, var(--xy-node-border-default))",
        color: "var(--xy-node-color, var(--xy-node-color-default))",
      }}
      data-label-node={data.header ? "header" : "date"}
    >
      {data.label}
      <Handle type="target" position={Position.Left} style={HIDDEN} />
      <Handle type="source" position={Position.Right} style={HIDDEN} />
    </div>
  );
}

/** A link between two outcomes: an arc over them on one row, else a bezier. */
function RowEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style, markerEnd }: EdgeProps) {
  const arc = rowArc(sourceX, sourceY, targetX, targetY);
  const [bezier] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  return <BaseEdge id={id} path={arc ?? bezier} markerEnd={markerEnd} style={style} />;
}

const HIDDEN = { visibility: "hidden" } as const;
const NODE_TYPES = { outcome: OutcomeNode, label: LabelNode };
const EDGE_TYPES = { row: RowEdge };

export default function PathGraph({ data, height }: { data: WidgetOutcomesData; height: number }) {
  const { t_i18n, locale } = useFormatter();
  const mode = useColorMode();
  const { onSelect } = useChartEvents();
  const [hovered, setHovered] = useState<string | null>(null);
  const laid = useMemo(() => pathNodes(data), [data]);
  const nodes = useMemo(() => {
    const placed: Node[] = laid.nodes.map((node) =>
      node.kind === "group"
        ? { id: node.id, type: "label", data: { label: node.label, header: true }, position: { x: node.x, y: node.y }, draggable: false, targetPosition: Position.Left, sourcePosition: Position.Right }
        : {
            id: node.id,
            type: "outcome",
            data: { item: node.item as WidgetOutcome, rate: node.rate ?? null, onHover: setHovered } satisfies OutcomeNodeData,
            position: { x: node.x, y: node.y },
            draggable: false,
            targetPosition: Position.Left,
            sourcePosition: Position.Right,
          },
    );
    const { start, end } = data.timeline ?? { start: null, end: null };
    const date = (id: string, value: string, x: number): Node => ({
      id, type: "label", data: { label: formatNamedDate(value, "longDateTime", locale) }, position: pathPosition(x, laid.maxY + 1),
      draggable: false, targetPosition: Position.Left, sourcePosition: Position.Right,
    }); // prettier-ignore
    if (start) placed.push(date("start-date-node", start, 0));
    if (end) placed.push(date("end-date-node", end, laid.maxX));
    return placed;
  }, [laid, data.timeline, locale]);
  const edges = useMemo(() => {
    const linked: Edge[] = pathEdges(data, hovered).map((edge) => ({
      ...edge,
      type: "row",
      markerEnd: { type: MarkerType.ArrowClosed, width: PATH.arrow, height: PATH.arrow, color: ERROR },
      style: { stroke: ERROR },
    }));
    const { start, end } = data.timeline ?? { start: null, end: null };
    if (start && end) {
      linked.push({
        id: "time-edge",
        source: "start-date-node",
        target: "end-date-node",
        label: formatDuration(new Date(end).getTime() - new Date(start).getTime()),
        labelShowBg: false,
        labelStyle: { fill: "var(--text-default-primary)", fontSize: 14 },
      });
    }
    return linked;
  }, [data, hovered]);

  if (data.needsScope) {
    return (
      <div className="flex flex-col items-center justify-center" style={{ height: height * 0.8 }}>
        <h5 className="text-center lowercase first-letter:uppercase" style={{ fontFamily: GEOLOGICA, fontWeight: 700, fontSize: 16 }}>
          {t_i18n("You must set a simulation")}
        </h5>
      </div>
    );
  }
  return (
    <div className="flex flex-col" style={{ height }}>
      <div className="flex justify-end pb-2">
        <RateLegend />
      </div>
      <div
        className="min-h-0 flex-1"
        // The canvas takes the widget's surface: React Flow's own (#141414 dark) would sit as a square in the card.
        style={{ ["--xy-node-background-color" as string]: "none", ["--xy-background-color" as string]: "transparent" }}
        data-testid="path-graph"
      >
        <ReactFlow
          colorMode={mode}
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          edgeTypes={EDGE_TYPES}
          onNodeClick={(event, node) => {
            event.stopPropagation();
            const item = (node.data as Partial<OutcomeNodeData>).item;
            if (node.type !== "outcome" || !item) return;
            if (onSelect) onSelect({ name: item.label, value: item.id ?? item.label });
            else if (dataHref(item.link)) window.open(dataHref(item.link), "_blank", "noopener,noreferrer");
          }}
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable
          elementsSelectable
          connectionLineType={ConnectionLineType.SmoothStep}
          panOnDrag
          panOnScroll
          zoomOnScroll={false}
          preventScrolling={false}
          proOptions={{ hideAttribution: true }}
        />
      </div>
    </div>
  );
}
