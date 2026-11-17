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
 * A sankey (`viz/sankey.ts`): bands as wide as their flow, in the colour of
 * the node they leave; each node names itself and its value beside it (left
 * of the last column, right of the others). Flows the layout left out
 * (loops, cycles, no value) are counted under the figure.
 */
import { useMemo } from "react";

import { useFormatter } from "@/i18n";
import { formatCompactNumber } from "@/lib/utils";
import type { WidgetGraphData } from "@/types";

import { sankeyLayout } from "../viz/sankey";
import { vizColor, VIZ_MUTED, VIZ_TEXT } from "./viz-colors";

const NOTE_HEIGHT = 18;
const LABEL_GAP = 6;

export function SankeyViz({ graph, width, height }: { graph: WidgetGraphData; width: number; height: number }) {
  const { t_i18n } = useFormatter();
  const preview = useMemo(() => sankeyLayout(graph, width, height), [graph, width, height]);
  const drawHeight = preview.dropped > 0 ? Math.max(40, height - NOTE_HEIGHT) : height;
  const layout = useMemo(() => (drawHeight === height ? preview : sankeyLayout(graph, width, drawHeight)), [preview, graph, width, height, drawHeight]);
  const tone = new Map(layout.nodes.map((node, index) => [node.id, index]));
  const lastColumn = Math.max(0, ...layout.nodes.map((node) => node.column));
  const nameOf = new Map(layout.nodes.map((node) => [node.id, node.label]));
  return (
    <div className="flex h-full flex-col" data-viz="sankey">
      <svg width={width} height={drawHeight} viewBox={`0 0 ${width} ${drawHeight}`} role="img" aria-label={t_i18n("Flows between {count, plural, one {# node} other {# nodes}}", { count: layout.nodes.length })}>
        <g fill="none">
          {layout.links.map((link, index) => (
            <path key={index} d={link.path} style={{ stroke: vizColor(tone.get(link.source) ?? 0), strokeOpacity: 0.35, strokeWidth: link.width }}>
              <title>{`${nameOf.get(link.source)} → ${nameOf.get(link.target)}: ${formatCompactNumber(link.value)}`}</title>
            </path>
          ))}
        </g>
        {layout.nodes.map((node, index) => {
          const right = node.column === lastColumn && lastColumn > 0;
          return (
            <g key={node.id} data-node={node.id}>
              <rect x={node.x} y={node.y} width={node.w} height={node.h} rx={2} style={{ fill: vizColor(index) }}>
                <title>{`${node.label}: ${formatCompactNumber(node.value)}`}</title>
              </rect>
              <text
                x={right ? node.x - LABEL_GAP : node.x + node.w + LABEL_GAP}
                y={node.y + node.h / 2}
                textAnchor={right ? "end" : "start"}
                dominantBaseline="central"
                style={VIZ_TEXT}
              >
                {node.label}
                <tspan style={VIZ_MUTED}>{` ${formatCompactNumber(node.value)}`}</tspan>
              </text>
            </g>
          );
        })}
      </svg>
      {layout.dropped > 0 ? (
        <span className="content-caption text-default-secondary" style={{ height: NOTE_HEIGHT }}>
          {t_i18n("{count, plural, one {# flow left out} other {# flows left out}} (a loop, a cycle or no value)", { count: layout.dropped })}
        </span>
      ) : null}
    </div>
  );
}
