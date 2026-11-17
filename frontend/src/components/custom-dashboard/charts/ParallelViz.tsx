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
 * Parallel coordinates (`viz/parallel.ts`): an axis per column, its name on
 * top, a numeric axis' ends or a categorical axis' values beside it; a line
 * per row, coloured by its value on the first categorical axis.
 */
import { useMemo, useState } from "react";

import { useFormatter } from "@/i18n";
import { formatCompactNumber } from "@/lib/utils";
import type { WidgetRowsData } from "@/types";

import { parallelLayout } from "../viz/parallel";
import { cellText } from "../viz/shapes";
import { vizColor, VIZ_LINE, VIZ_MUTED, VIZ_TEXT } from "./viz-colors";

const TOP = 26;
const BOTTOM = 14;
const SIDE = 44;
const MAX_TICK_LABELS = 10;

export function ParallelViz({ data, fields, width, height }: { data: WidgetRowsData; fields: unknown; width: number; height: number }) {
  const { t_i18n } = useFormatter();
  const plotW = Math.max(40, width - 2 * SIDE);
  const plotH = Math.max(40, height - TOP - BOTTOM);
  const layout = useMemo(() => parallelLayout(data, plotW, plotH, fields), [data, plotW, plotH, fields]);
  const [hover, setHover] = useState<number | null>(null);
  const colourAxis = layout.axes.find((axis) => axis.kind === "category");
  const toneOf = (row: number) => {
    if (!colourAxis || colourAxis.kind !== "category") return 0;
    const value = cellText(data.rows[row]?.[colourAxis.key]).trim();
    return Math.max(0, colourAxis.values.indexOf(value));
  };
  if (layout.axes.length === 0) return null;
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={t_i18n("Parallel coordinates of {count, plural, one {# row} other {# rows}}", { count: data.rows.length })} data-viz="parallel">
      <g transform={`translate(${SIDE},${TOP})`}>
        <g fill="none" onPointerLeave={() => setHover(null)}>
          {layout.lines.map((line) => (
            <path
              key={line.row}
              d={line.path}
              onPointerEnter={() => setHover(line.row)}
              style={{
                stroke: vizColor(toneOf(line.row)),
                strokeOpacity: hover === null ? 0.55 : hover === line.row ? 1 : 0.12,
                strokeWidth: hover === line.row ? 2.5 : 1.5,
              }}
            >
              <title>{layout.axes.map((axis) => `${axis.label}: ${data.rows[line.row]?.[axis.key] || "-"}`).join(" · ")}</title>
            </path>
          ))}
        </g>
        {layout.axes.map((axis) => (
          <g key={axis.key} data-axis={axis.key}>
            <line x1={axis.x} x2={axis.x} y1={0} y2={plotH} style={{ stroke: VIZ_LINE, strokeWidth: 1.5 }} />
            <text x={axis.x} y={-12} textAnchor="middle" style={{ ...VIZ_TEXT, fontWeight: 600 }}>
              {axis.label}
            </text>
            {axis.kind === "number" ? (
              <>
                <text x={axis.x + 4} y={4} style={VIZ_MUTED}>
                  {formatCompactNumber(axis.max)}
                </text>
                <text x={axis.x + 4} y={plotH} style={VIZ_MUTED}>
                  {formatCompactNumber(axis.min)}
                </text>
              </>
            ) : (
              axis.values
                .filter((_v, i, all) => all.length <= MAX_TICK_LABELS || i % Math.ceil(all.length / MAX_TICK_LABELS) === 0)
                .map((value) => {
                  const index = axis.values.indexOf(value);
                  const y = axis.values.length === 1 ? plotH / 2 : (index / (axis.values.length - 1)) * plotH;
                  return (
                    <text key={value} x={axis.x + 4} y={y} dominantBaseline="central" style={VIZ_MUTED}>
                      {value.length > 14 ? `${value.slice(0, 13)}…` : value}
                    </text>
                  );
                })
            )}
          </g>
        ))}
      </g>
    </svg>
  );
}
