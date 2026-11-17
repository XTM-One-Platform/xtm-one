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
 * The canvas edge.
 *
 * Edges are typed — `data`, `handover`, `condition_branch`, `error` — and the
 * type is legible at a glance rather than only in a menu: colour plus a dash
 * pattern, so the distinction survives greyscale and colour-blindness.
 *
 * The stroke comes from a CSS variable (see `flow-builder.css`): an SVG
 * `stroke` attribute cannot reference a Tailwind class, and the variable is what
 * both the stylesheet and this path can read.
 */

import { memo } from "react";
import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  getSmoothStepPath,
  getStraightPath,
  type EdgeProps,
} from "@xyflow/react";
import { HANDOVER_TYPE_CONFIG } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { CanvasEdge } from "./graph-adapter";
import { EDGE_DASH, EDGE_STROKE } from "./node-visuals";

function TypedEdgeComponent({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  label,
  data,
  selected,
  markerEnd,
}: EdgeProps<CanvasEdge>) {
  const { t_i18n } = useFormatter();
  // The edge's shape is a per-edge choice (right panel): bezier by default,
  // straight, or the two orthogonal styles (step = square corners).
  const pathParams = {
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  };
  const pathStyle = data?.pathStyle;
  const [path, labelX, labelY] =
    pathStyle === "straight"
      ? getStraightPath(pathParams)
      : pathStyle === "step"
        ? getSmoothStepPath({ ...pathParams, borderRadius: 0 })
        : pathStyle === "smoothstep"
          ? getSmoothStepPath(pathParams)
          : getBezierPath(pathParams);

  const kind = data?.kind ?? "data";

  // A handover carries its type in the label. Colour the edge by that type
  // (matching the agent-flow legend) and show the type's proper label rather
  // than the raw stored value.
  const handover =
    kind === "handover" && typeof label === "string"
      ? HANDOVER_TYPE_CONFIG[label]
      : undefined;
  const stroke = data?.objectTyped
    ? "var(--flow-object-edge)"
    : (handover?.edgeColor ?? EDGE_STROKE[kind] ?? EDGE_STROKE.data);
  const displayLabel = handover ? t_i18n(handover.label) : label;

  return (
    <>
      {/* Loop-body under-stroke: the link version of the nodes' double
          outline — a wider orange line beneath the edge's own colour, so the
          branch that re-executes every pass reads as one system. */}
      {data?.inLoopBranch && (
        <path
          d={path}
          fill="none"
          stroke="var(--flow-loop-outline)"
          strokeWidth={selected ? 7 : 6}
          strokeLinecap="round"
        />
      )}
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        className={data?.live ? "flow-edge-live" : undefined}
        style={{
          stroke,
          strokeDasharray: data?.live ? "8 6" : EDGE_DASH[kind],
          strokeWidth: selected ? 3 : 2,
        }}
      />
      {displayLabel ? (
        <EdgeLabelRenderer>
          <div
            // `nodrag`/`nopan` keep a click on the label from panning the canvas.
            className="nodrag nopan pointer-events-auto absolute rounded-sm border border-elevation-subtle layer-1 bg-elevation-default px-1.5 py-0.5 content-caption text-default-secondary"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
          >
            {displayLabel}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

export const TypedEdge = memo(TypedEdgeComponent);
