/**
 * Unit tests for where a widget's data is drawn (`yarn test:unit`): the maps,
 * graphs and flows of a definition reach the widget view they draw, and only
 * their views and a trellis leave `PlatformChart`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { normalizeDashboard } from "../spec.ts";
import { drawnAsChart } from "./route.ts";

const ok = (shape: string, data: unknown) => ({ status: "ok", fetched_at: "", open_url: null, shape, data }) as never;

test("a chart's data with a chart view stays a chart; a map, a graph or a trellis does not", () => {
  const series = ok("series", { series: [] });
  assert.equal(drawnAsChart(series, "line", {}), true);
  assert.equal(drawnAsChart(series, "line", { trellis: { splitBy: "series" } }), false);
  assert.equal(drawnAsChart(series, "line", { trellis: { splitBy: "series", enabled: false } }), true);
  assert.equal(drawnAsChart(ok("rows", { columns: [], rows: [], total: null }), "timeline", {}), false);
  assert.equal(drawnAsChart(ok("rows", { columns: [], rows: [], total: null }), "table", { trellis: { splitBy: "a" } }), true);
  assert.equal(drawnAsChart(ok("geo", { points: [], regions: [] }), undefined, {}), false);
});

test("each new visualization type is a data widget drawing its view", () => {
  const types = {
    "xtm.map": "map",
    "xtm.choropleth.svg": "choroplethsvg",
    "xtm.linkgraph": "linkgraph",
    "xtm.networkgraph": "networkgraph",
    "xtm.sankey": "sankey",
    "xtm.parallelcoordinates": "parallel",
    "xtm.timeline": "timeline",
    "xtm.treemap": "treemap",
  };
  const ids = Object.keys(types).map((_t, i) => `v${i}`);
  const dashboard = normalizeDashboard({
    version: 2,
    title: "Maps",
    dataSources: { src: { type: "static", options: { shape: "geo", data: { points: [], regions: [] } } } },
    visualizations: Object.fromEntries(Object.keys(types).map((type, i) => [ids[i], { type, dataSources: { primary: "src" }, options: { region: "europe" } }])),
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }] },
      layoutDefinitions: { main: { structure: ids.map((item, i) => ({ item, position: { x: 0, y: i * 12, w: 12, h: 12 } })) } },
    },
  });
  const widgets = dashboard.tabs[0].widgets;
  assert.deepEqual(
    widgets.map((w) => [w.kind, w.kind === "data" ? w.view : null]),
    Object.values(types).map((view) => ["data", view]),
  );
  assert.deepEqual(widgets[0].kind === "data" && widgets[0].options, { region: "europe" });
});
