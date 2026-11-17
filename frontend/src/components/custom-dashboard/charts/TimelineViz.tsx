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
 * A timeline (`viz/timeline.ts`): the time axis on top, one band per lane
 * (its name in the gutter), an event with an end as a bar and one without as
 * a dot, its label beside it. More rows than the cell holds scroll inside it.
 */
import { useMemo } from "react";

import { useFormatter } from "@/i18n";
import type { WidgetEventsData } from "@/types";

import { labelRoom, timelineLayout, type TickUnit } from "../viz/timeline";
import { vizColor, VIZ_LINE, VIZ_MUTED, VIZ_TEXT } from "./viz-colors";

const GUTTER = 110;
const AXIS = 22;
const ROW = 22;
const MAX_ROW = 36;
const PAD = 8;

const TICK_FORMAT: Record<TickUnit, Intl.DateTimeFormatOptions> = {
  hour: { hour: "2-digit", minute: "2-digit", timeZone: "UTC" },
  day: { day: "numeric", month: "short", timeZone: "UTC" },
  week: { day: "numeric", month: "short", timeZone: "UTC" },
  month: { month: "short", year: "2-digit", timeZone: "UTC" },
  quarter: { month: "short", year: "numeric", timeZone: "UTC" },
  year: { year: "numeric", timeZone: "UTC" },
};

export function TimelineViz({ data, width, height }: { data: WidgetEventsData; width: number; height: number }) {
  const { t_i18n, locale } = useFormatter();
  const gutter = Math.min(GUTTER, Math.max(60, width * 0.22));
  const plot = Math.max(40, width - gutter - PAD);
  const layout = useMemo(() => timelineLayout(data, plot), [data, plot]);
  // Rows share the cell's height, from ROW up to MAX_ROW px each; more rows scroll.
  const row = Math.max(ROW, Math.min(MAX_ROW, Math.floor((height - AXIS - PAD) / Math.max(1, layout.rows))));
  const fullHeight = AXIS + layout.rows * row + PAD;
  const format = (at: number, unit: TickUnit) => new Intl.DateTimeFormat(locale, TICK_FORMAT[unit]).format(at);
  const when = (start: number, end: number | null) => {
    const f = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" });
    return end === null ? f.format(start) : `${f.format(start)} – ${f.format(end)}`;
  };
  return (
    <div className="h-full w-full overflow-y-auto overflow-x-hidden" style={{ maxHeight: height }} data-viz="timeline">
      <svg width={width} height={fullHeight} viewBox={`0 0 ${width} ${fullHeight}`} role="img" aria-label={t_i18n("Timeline of {count, plural, one {# event} other {# events}}", { count: layout.items.length })}>
        {layout.lanes.map((lane, index) => (
          <g key={`${lane.name}-${index}`}>
            {index % 2 === 1 ? (
              <rect x={0} y={AXIS + lane.row * row} width={width} height={lane.rows * row} style={{ fill: "var(--bg-elevation-hover)", fillOpacity: 0.5 }} />
            ) : null}
            <text x={4} y={AXIS + lane.row * row + row / 2} dominantBaseline="central" style={VIZ_MUTED}>
              {(lane.name || "-").length * 6 > gutter - 8 ? `${lane.name.slice(0, Math.max(1, Math.floor((gutter - 14) / 6)))}…` : lane.name || "-"}
              <title>{lane.name || "-"}</title>
            </text>
          </g>
        ))}
        <g transform={`translate(${gutter},0)`}>
          {layout.ticks.map((tick) => (
            <g key={tick.at}>
              <line x1={tick.x} x2={tick.x} y1={AXIS - 4} y2={fullHeight - PAD} style={{ stroke: VIZ_LINE, strokeDasharray: "2 3" }} />
              <text x={tick.x} y={AXIS - 8} textAnchor="middle" style={VIZ_MUTED}>
                {format(tick.at, tick.unit)}
              </text>
            </g>
          ))}
          {layout.items.map((item) => {
            const y = AXIS + item.row * row + row / 2;
            const colour = vizColor(item.lane);
            // Its room was kept on its row (`labelRoom`), on the side the layout chose.
            const left = item.labelSide === "left";
            const labelX = left ? item.x - 8 : item.w > 0 ? item.x + item.w + 4 : item.x + 8;
            const room = labelRoom(item.label);
            return (
              <g key={item.index} data-event={item.index}>
                {item.w > 0 ? (
                  <rect x={item.x} y={y - 6} width={item.w} height={12} rx={3} style={{ fill: colour, fillOpacity: 0.8 }} />
                ) : (
                  <circle cx={item.x} cy={y} r={5} style={{ fill: colour }} />
                )}
                <text x={labelX} y={y} textAnchor={left ? "end" : "start"} dominantBaseline="central" style={VIZ_TEXT}>
                  {item.label.length * 6 > room - 6 ? `${item.label.slice(0, Math.max(1, Math.floor((room - 12) / 6)))}…` : item.label}
                </text>
                <title>{`${item.label} · ${when(item.start, item.end)}`}</title>
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}
