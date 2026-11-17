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
 * A trellis (`viz/trellis.ts`): the visualization's chart drawn once per
 * panel, in columns, every value axis on one scale unless the visualization
 * says otherwise; panels beyond the cell's height scroll inside it, and those
 * beyond `MAX_PANELS` are counted.
 */
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { useFormatter } from "@/i18n";
import { formatCompactNumber } from "@/lib/utils";

import type { PlatformView } from "../spec";
import { trellisColumns, type Trellis, type TrellisPanel } from "../viz/trellis";
import { vizColor } from "./viz-colors";

const AXIS = { fill: "var(--text-default-secondary)", fontSize: 10 } as const;
const GAP = 12;
const TITLE = 18;
const PANEL_MIN_HEIGHT = 110;

function PanelChart({ panel, view, domain, height }: { panel: TrellisPanel; view: PlatformView; domain: [number, number] | null; height: number }) {
  const yDomain = domain ?? ["auto", "auto"];
  const grid = <CartesianGrid strokeDasharray="3 3" stroke="var(--border-elevation-subtle)" opacity={0.4} />;
  const tooltip = <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(v) => formatCompactNumber(Number(v))} />;
  const valueAxis = (props: { type?: "number" }) => (
    <YAxis {...props} domain={yDomain} tickFormatter={(v) => formatCompactNumber(Number(v))} tick={AXIS} stroke="var(--border-elevation-subtle)" width={34} />
  );
  if (panel.data.shape === "series") {
    const rows = panel.data.data.series[0]?.points ?? [];
    const colour = vizColor(0);
    return (
      <ResponsiveContainer width="100%" height={height}>
        {view === "bar" ? (
          <BarChart data={rows}>
            {grid}
            <XAxis dataKey="x" tick={AXIS} stroke="var(--border-elevation-subtle)" minTickGap={12} />
            {valueAxis({})}
            {tooltip}
            <Bar dataKey="y" fill={colour} />
          </BarChart>
        ) : view === "area" ? (
          <AreaChart data={rows}>
            {grid}
            <XAxis dataKey="x" tick={AXIS} stroke="var(--border-elevation-subtle)" minTickGap={12} />
            {valueAxis({})}
            {tooltip}
            <Area type="monotone" dataKey="y" stroke={colour} fill={colour} fillOpacity={0.25} />
          </AreaChart>
        ) : (
          <LineChart data={rows}>
            {grid}
            <XAxis dataKey="x" tick={AXIS} stroke="var(--border-elevation-subtle)" minTickGap={12} />
            {valueAxis({})}
            {tooltip}
            <Line type="monotone" dataKey="y" stroke={colour} dot={false} strokeWidth={2} />
          </LineChart>
        )}
      </ResponsiveContainer>
    );
  }
  const items = panel.data.data.items;
  if (view === "donut") {
    return (
      <ResponsiveContainer width="100%" height={height}>
        <PieChart>
          <Pie data={items} dataKey="value" nameKey="label" innerRadius="50%" outerRadius="85%" paddingAngle={1}>
            {items.map((_item, i) => (
              <Cell key={i} fill={vizColor(i)} stroke="var(--bg-elevation-default)" />
            ))}
          </Pie>
          {tooltip}
        </PieChart>
      </ResponsiveContainer>
    );
  }
  const horizontal = view !== "bar";
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={items} layout={horizontal ? "vertical" : "horizontal"}>
        {grid}
        {horizontal ? (
          <>
            <XAxis type="number" domain={yDomain} tickFormatter={(v) => formatCompactNumber(Number(v))} tick={AXIS} stroke="var(--border-elevation-subtle)" />
            <YAxis type="category" dataKey="label" tick={AXIS} width={80} stroke="var(--border-elevation-subtle)" />
          </>
        ) : (
          <>
            <XAxis dataKey="label" tick={AXIS} stroke="var(--border-elevation-subtle)" minTickGap={8} />
            {valueAxis({})}
          </>
        )}
        {tooltip}
        <Bar dataKey="value" fill={vizColor(0)} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function TrellisViz({
  trellis,
  columns: asked,
  view,
  height,
}: {
  trellis: Trellis;
  columns: number | "auto";
  view: PlatformView;
  height: number;
}) {
  const { t_i18n } = useFormatter();
  // The width is read by CSS: `auto` asks for as many columns as 220 px panels fit.
  const fixed = asked === "auto" ? null : trellisColumns(trellis.panels.length, Number.POSITIVE_INFINITY, asked);
  const rows = Math.ceil(trellis.panels.length / (fixed ?? Math.min(trellis.panels.length, 3)));
  const panelHeight = Math.max(PANEL_MIN_HEIGHT, Math.floor((height - (rows - 1) * GAP) / Math.max(1, rows)) - TITLE);
  return (
    <div className="flex h-full flex-col gap-1 overflow-y-auto" style={{ maxHeight: height }} data-viz="trellis">
      <div
        className="grid"
        style={{
          gap: GAP,
          gridTemplateColumns: fixed ? `repeat(${fixed}, minmax(0, 1fr))` : "repeat(auto-fill, minmax(220px, 1fr))",
        }}
      >
        {trellis.panels.map((panel) => (
          <section key={panel.key} className="flex min-w-0 flex-col" aria-label={panel.key} data-panel={panel.key}>
            <h4 className="truncate content-caption font-medium text-default-primary" style={{ height: TITLE }} title={panel.key}>
              {panel.key}
            </h4>
            <PanelChart panel={panel} view={view} domain={trellis.domain} height={panelHeight} />
          </section>
        ))}
      </div>
      {trellis.hidden > 0 ? (
        <span className="content-caption text-default-secondary">
          {t_i18n("{count, plural, one {# more panel not shown} other {# more panels not shown}}", { count: trellis.hidden })}
        </span>
      ) : null}
    </div>
  );
}
