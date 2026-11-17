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
 * The `treemap` kind (`viz/treemap.ts`): each top-level group in its own
 * colour, its children in lighter steps of it, a group's name on its header
 * strip; a cell names itself and its value where it has the room, on hover
 * anyway.
 *
 * With a look (`look.ts`: a palette, a lead colour or a separator), a flat
 * treemap (a distribution) is drawn as the modules draw theirs (OpenCTI's
 * tree): one palette colour per cell, or one colour in shades of the value,
 * cells parted by a line in the surface colour, each cell's name centred in
 * bold, as large as fits, its value in the tooltip.
 */
import { useMemo } from "react";

import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import { formatCompactNumber } from "@/lib/utils";
import type { WidgetTreeData } from "@/types";

import { HEADER, treemapLayout, type TreemapCell } from "../viz/treemap";
import { mixColor, paletteColor } from "./colors";
import { compact } from "./common";
import { lookColors, lookNumber, nthColor, readLook, type Look } from "./look";
import { vizColor, VIZ_SURFACE } from "./viz-colors";

const LABEL_MIN_W = 44;
const LABEL_MIN_H = 18;

/** A cell's label size: 12 px, smaller to fit its width (about 0.6 em a character), none under 7 px. */
function cellFontSize(name: string, width: number, height: number): number {
  const size = Math.min(12, (width - 8) / Math.max(1, name.length * 0.6), height * 0.6);
  return size >= 7 ? Math.floor(size) : 0;
}

/** The colour of the `index`-th cell of a flat look: its palette colour, or the lead in shades of its value. */
function flatCellColor(look: Look, colors: readonly string[] | undefined, index: number, value: number, max: number, mode: "dark" | "light"): string {
  if (look.colorByPoint) return nthColor(colors, index) ?? paletteColor(index);
  const color = nthColor(colors, 0) ?? paletteColor(0);
  if (!look.valueShades || !(max > 0)) return color;
  // Smaller values in a deeper shade (darker in the dark theme, lighter in the light one), half way at most.
  const shade = (1 - Math.max(0, value) / max) * 0.5;
  return mixColor(color, mode === "dark" ? "#000000" : "#ffffff", shade);
}

function FlatTreemap({ cells, look, width, height, label }: { cells: TreemapCell[]; look: Look; width: number; height: number; label: string }) {
  const { locale } = useFormatter();
  const mode = useColorMode();
  const colors = lookColors(look, mode);
  const max = Math.max(0, ...cells.map((c) => c.value));
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} data-viz="treemap">
      {cells.map((cell, index) => {
        const name = cell.path[cell.path.length - 1];
        const size = cellFontSize(name, cell.w, cell.h);
        return (
          <g key={cell.path.join("\u001f")} data-cell={name}>
            <rect
              x={cell.x}
              y={cell.y}
              width={cell.w}
              height={cell.h}
              style={{ fill: flatCellColor(look, colors, index, cell.value, max, mode), stroke: VIZ_SURFACE, strokeWidth: look.separatorWidth ?? 1 }}
            >
              <title>{`${name}: ${lookNumber(look, cell.value, compact, locale)}`}</title>
            </rect>
            {size > 0 ? (
              <text
                x={cell.x + cell.w / 2}
                y={cell.y + cell.h / 2}
                textAnchor="middle"
                dominantBaseline="central"
                pointerEvents="none"
                style={{ fill: "var(--text-default-primary)", fontSize: size, fontWeight: 600, fontFamily: '"IBM Plex Sans", sans-serif' }}
              >
                {name}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}

export function TreemapViz({ data, width, height, options }: { data: WidgetTreeData; width: number; height: number; options?: Record<string, unknown> }) {
  const { t_i18n } = useFormatter();
  const cells = useMemo(() => treemapLayout(data, width, height), [data, width, height]);
  const look = readLook(options);
  const label = t_i18n("Treemap of {count, plural, one {# value} other {# values}}", { count: data.items.length });
  if ((look.palette || look.paletteLead || look.separatorWidth !== undefined) && cells.every((cell) => cell.leaf && cell.depth === 0)) {
    return <FlatTreemap cells={cells} look={look} width={width} height={height} label={label} />;
  }
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} data-viz="treemap">
      {cells.map((cell) => {
        const name = cell.path[cell.path.length - 1];
        const title = `${cell.path.join(" / ")}: ${formatCompactNumber(cell.value)}`;
        const colour = vizColor(cell.group);
        if (!cell.leaf) {
          return (
            <g key={cell.path.join("\u001f")}>
              <rect x={cell.x} y={cell.y} width={cell.w} height={cell.h} rx={3} style={{ fill: colour, fillOpacity: 0.12, stroke: colour, strokeOpacity: 0.5 }}>
                <title>{title}</title>
              </rect>
              {cell.w >= LABEL_MIN_W && cell.h >= HEADER * 2.5 ? (
                <text x={cell.x + 6} y={cell.y + HEADER / 2 + 1} dominantBaseline="central" style={{ fill: "var(--text-default-primary)", fontSize: 11, fontWeight: 600 }}>
                  {name}
                </text>
              ) : null}
            </g>
          );
        }
        const fits = cell.w >= LABEL_MIN_W && cell.h >= LABEL_MIN_H;
        const two = fits && cell.h >= LABEL_MIN_H * 2;
        return (
          <g key={cell.path.join("\u001f")} data-cell={cell.path.join("/")}>
            <rect
              x={cell.x}
              y={cell.y}
              width={cell.w}
              height={cell.h}
              rx={2}
              style={{ fill: colour, fillOpacity: cell.depth === 0 ? 0.85 : 0.6, stroke: VIZ_SURFACE, strokeWidth: 1 }}
            >
              <title>{title}</title>
            </rect>
            {fits ? (
              <text x={cell.x + 6} y={cell.y + 13} style={{ fill: "var(--text-default-primary)", fontSize: 11 }}>
                <tspan>{name.length * 6.5 > cell.w - 10 ? `${name.slice(0, Math.max(1, Math.floor((cell.w - 16) / 6.5)))}…` : name}</tspan>
                {two ? (
                  <tspan x={cell.x + 6} dy={14} style={{ fill: "var(--text-default-primary)", fillOpacity: 0.8 }}>
                    {formatCompactNumber(cell.value)}
                  </tspan>
                ) : null}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}
