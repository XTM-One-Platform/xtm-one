/**
 * Unit tests for the layouts of the maps, graphs and flows (`yarn test:unit`):
 * the treemap's areas, the sankey's columns and bands, the timeline's lanes
 * and ticks, the parallel axes, the map's classes and bubbles, the graphs'
 * placements and the trellis' panels.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { bubbleRadius, choroplethClasses, mapLayers, MAX_BUBBLE, MIN_BUBBLE, regionsByCode } from "./geo.ts";
import { LINK_NODE, linkGraphLayout, MAX_RADIUS, MIN_RADIUS, networkLayout, nodeRadius } from "./graph.ts";
import { axisPosition, MAX_CATEGORIES, OTHER, parallelLayout } from "./parallel.ts";
import { NODE_WIDTH, sankeyLayout } from "./sankey.ts";
import { labelRoom, timelineLayout, timeTicks } from "./timeline.ts";
import { GAP, HEADER, squarify, treemapLayout } from "./treemap.ts";
import { MAX_PANELS, trellisColumns, trellisOptions, trellisPanels } from "./trellis.ts";

const area = (r: { w: number; h: number }) => r.w * r.h;
const close = (a: number, b: number, eps = 1e-6) => Math.abs(a - b) <= eps;

// ── Treemap ──────────────────────────────────────────────────────────────────

test("squarified cells fill the box, each the area of its value, none overlapping", () => {
  const rects = squarify([6, 6, 4, 3, 2, 2, 1], { x: 0, y: 0, w: 600, h: 400 });
  const total = 24;
  rects.forEach((rect, i) => assert.ok(close(area(rect), ([6, 6, 4, 3, 2, 2, 1][i] / total) * 600 * 400, 1e-3)));
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) {
      const a = rects[i];
      const b = rects[j];
      const overlap = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
      assert.ok(overlap < 1e-6, `cells ${i} and ${j} overlap`);
    }
  }
  // Squarified: no cell thinner than one to four.
  for (const rect of rects) assert.ok(Math.max(rect.w / rect.h, rect.h / rect.w) < 4);
});

test("a treemap nests each group's children under its header, the largest first", () => {
  const cells = treemapLayout(
    { items: [{ path: ["Malware", "Ransomware"], value: 30 }, { path: ["Malware", "Trojan"], value: 10 }, { path: ["Tool"], value: 20 }] },
    600,
    300,
  );
  assert.deepEqual(
    cells.map((c) => [c.path.join("/"), c.value, c.depth, c.leaf, c.group]),
    [["Malware", 40, 0, false, 0], ["Malware/Ransomware", 30, 1, true, 0], ["Malware/Trojan", 10, 1, true, 0], ["Tool", 20, 0, true, 1]],
  );
  const [malware, ransomware, , tool] = cells;
  assert.ok(ransomware.y >= malware.y + HEADER, "children start below the header");
  assert.ok(close(area(malware) + area(tool), 600 * 300, 600 * 300 * 0.02), "the top level fills the box, less the gaps");
  assert.ok(close(area(malware) / area(tool), 2, 0.1), "each the area of its value");
  assert.equal(malware.x, GAP / 2);
});

test("a treemap leaves out values that are not positive", () => {
  assert.deepEqual(treemapLayout({ items: [{ path: ["a"], value: 0 }, { path: [], value: 3 }] }, 100, 100), []);
});

// ── Sankey ───────────────────────────────────────────────────────────────────

const graph = (edges: [string, string, number | null][], extra: string[] = []) => {
  const ids = [...new Set([...edges.flatMap(([s, t]) => [s, t]), ...extra])];
  return {
    nodes: ids.map((id) => ({ id, label: id.toUpperCase(), group: null as string | null, value: null })),
    edges: edges.map(([source, target, value]) => ({ source, target, label: null, value })),
  };
};

test("a sankey puts sources left, sinks right, and each node as tall as its larger side", () => {
  const layout = sankeyLayout(graph([["a", "b", 4], ["a", "c", 2], ["b", "d", 4], ["c", "d", 1]]), 400, 200);
  const at = new Map(layout.nodes.map((n) => [n.id, n]));
  assert.deepEqual([...at.values()].map((n) => [n.id, n.column]), [["a", 0], ["b", 1], ["c", 1], ["d", 2]]);
  assert.equal(at.get("a")!.x, 0);
  assert.equal(at.get("d")!.x, 400 - NODE_WIDTH);
  // a holds 6 (out), d holds 5 (in): heights in proportion.
  assert.ok(close(at.get("a")!.h / at.get("d")!.h, 6 / 5, 1e-6));
  for (const node of layout.nodes) assert.ok(node.y >= 0 && node.y + node.h <= 200 + 1e-6, `${node.id} inside`);
  const band = layout.links.find((l) => l.source === "a" && l.target === "b")!;
  assert.ok(close(band.width / layout.links.find((l) => l.source === "a" && l.target === "c")!.width, 2, 0.01));
  assert.match(band.path, /^M[\d.]+,[\d.]+C/);
  assert.equal(layout.dropped, 0);
});

test("a sankey leaves out loops, empty flows and the flow closing a cycle, and says how many", () => {
  const layout = sankeyLayout(graph([["a", "b", 2], ["b", "a", 1], ["b", "b", 1], ["b", "c", 0], ["c", "d", null]]), 300, 100);
  assert.deepEqual(layout.links.map((l) => `${l.source}>${l.target}:${l.value}`), ["a>b:2", "c>d:1"]);
  assert.equal(layout.dropped, 3);
});

test("a sankey with no flow draws nothing", () => {
  assert.deepEqual(sankeyLayout(graph([], ["lonely"]), 300, 100), { nodes: [], links: [], dropped: 0 });
});

// ── Timeline ─────────────────────────────────────────────────────────────────

test("a timeline lays its lanes in order and stacks the events of a lane that overlap", () => {
  const layout = timelineLayout(
    {
      events: [
        { label: "Campaign", start: "2026-01-01", end: "2026-03-01", lane: "APT28", value: null },
        { label: "Phish", start: "2026-02-01", end: null, lane: "APT28", value: null },
        { label: "Report", start: "2026-04-01", end: null, lane: "Reports", value: null },
        { label: "Broken", start: "soon", end: null, lane: "Reports", value: null },
      ],
    },
    1000,
  );
  assert.deepEqual(layout.lanes, [{ name: "APT28", row: 0, rows: 2 }, { name: "Reports", row: 2, rows: 1 }]);
  assert.deepEqual(layout.items.map((i) => [i.label, i.row, i.w > 0]), [["Campaign", 0, true], ["Phish", 1, false], ["Report", 2, false]]);
  assert.equal(layout.rows, 3);
  const [campaign] = layout.items;
  assert.ok(campaign.x > 0 && campaign.x + campaign.w < 1000, "a margin keeps the ends inside");
});

test("an event's label keeps its room on its row, on the left near the axis' end", () => {
  const events = [
    { label: "First event with a long name", start: "2026-01-01", end: null, lane: null, value: null },
    { label: "Close", start: "2026-01-03", end: null, lane: null, value: null },
    { label: "Last", start: "2026-03-01", end: null, lane: null, value: null },
  ];
  const layout = timelineLayout({ events }, 600);
  // "Close" starts inside the first label's room: it takes a row of its own.
  assert.deepEqual(layout.items.map((i) => [i.label, i.row, i.labelSide]), [["First event with a long name", 0, "right"], ["Close", 1, "right"], ["Last", 0, "left"]]);
  assert.ok(layout.items[0].x + labelRoom(events[0].label) <= layout.items[2].x - labelRoom("Last"));
});

test("time ticks pick the finest unit that leaves room for their labels", () => {
  const day = 86_400_000;
  const from = Date.UTC(2026, 0, 1);
  assert.equal(timeTicks(from, from + 5 * day, 800)[0].unit, "day");
  assert.equal(timeTicks(from, Date.UTC(2026, 11, 31), 800)[0].unit, "quarter");
  const months = timeTicks(from, Date.UTC(2026, 11, 31), 1200);
  assert.equal(months[0].unit, "month");
  assert.deepEqual(months.slice(0, 3).map((t) => new Date(t.at).toISOString().slice(0, 10)), ["2026-01-01", "2026-02-01", "2026-03-01"]);
  const years = timeTicks(Date.UTC(2000, 5, 1), Date.UTC(2026, 0, 1), 400);
  assert.equal(years[0].unit, "year");
  assert.ok(years.length <= 5);
});

test("a single moment is drawn in the middle of a day's span; no event is no layout", () => {
  const one = timelineLayout({ events: [{ label: "x", start: "2026-01-01T12:00:00Z", end: null, lane: null, value: null }] }, 200);
  assert.ok(close(one.items[0].x, 100));
  assert.deepEqual(timelineLayout({ events: [] }, 200).items, []);
});

// ── Parallel coordinates ─────────────────────────────────────────────────────

const rowsOf = (keys: string[], rows: Record<string, string>[]) => ({ columns: keys.map((key) => ({ key, label: key })), rows, total: null });

test("parallel axes are numeric where every cell is a figure, categorical elsewhere", () => {
  const layout = parallelLayout(
    rowsOf(["actor", "cvss", "targets"], [
      { actor: "APT28", cvss: "9.8", targets: "12" },
      { actor: "APT29", cvss: "5", targets: "" },
      { actor: "APT28", cvss: "7.5", targets: "3" },
    ]),
    300,
    100,
  );
  assert.deepEqual(layout.axes.map((a) => [a.key, a.kind, a.x]), [["actor", "category", 0], ["cvss", "number", 150], ["targets", "number", 300]]);
  assert.deepEqual(layout.axes[1].kind === "number" && [layout.axes[1].min, layout.axes[1].max], [5, 9.8]);
  // APT28 is the first category: the top. 9.8 is the cvss maximum: the top too.
  assert.deepEqual(layout.lines[0].points.slice(0, 2), [{ x: 0, y: 0 }, { x: 150, y: 0 }]);
  // An empty cell breaks the line: two points, one segment, no move to nowhere.
  assert.equal(layout.lines[1].points[2], null);
  assert.equal(layout.lines[1].path, "M0,100L150,100");
});

test("a categorical axis keeps its first values and puts the rest on one step", () => {
  const rows = Array.from({ length: MAX_CATEGORIES + 5 }, (_, i) => ({ a: `v${i}`, b: String(i) }));
  const layout = parallelLayout(rowsOf(["a", "b"], rows), 100, 100);
  const axis = layout.axes[0];
  assert.ok(axis.kind === "category");
  assert.equal(axis.values.length, MAX_CATEGORIES);
  assert.equal(axis.values[axis.values.length - 1], OTHER);
  assert.equal(axisPosition(axis, `v${MAX_CATEGORIES + 2}`), 0);
  assert.deepEqual(parallelLayout(rowsOf(["a"], rows), 100, 100), { axes: [], lines: [] });
});

// ── Map ──────────────────────────────────────────────────────────────────────

test("a map draws the layers it asks for, else a choropleth for regions and bubbles for points", () => {
  const data = { points: [{ label: "Lyon", lat: 45, lon: 4, value: 1, group: null }], regions: [{ code: "FR", label: "France", value: 2 }] };
  assert.deepEqual(mapLayers({}, data), ["choropleth", "bubble"]);
  assert.deepEqual(mapLayers({ layers: [{ type: "marker" }, "choropleth", "lasers"] }, data), ["choropleth", "marker"]);
  assert.deepEqual(mapLayers({}, { points: [], regions: [] }), []);
});

test("choropleth classes are equal steps from the least to the largest value", () => {
  const classes = choroplethClasses([0, 10, 50, 100]);
  assert.deepEqual([0, 10, 19.9, 20, 50, 99, 100].map(classes.classOf), [0, 0, 0, 1, 2, 4, 4]);
  assert.deepEqual(classes.breaks, [0, 20, 40, 60, 80, 100]);
  assert.equal(choroplethClasses([7, 7]).classOf(7), 4);
});

test("a bubble's area follows its value; no value is the least bubble", () => {
  assert.equal(bubbleRadius(100, 100), MAX_BUBBLE);
  assert.ok(close(bubbleRadius(25, 100) ** 2 / bubbleRadius(100, 100) ** 2, 0.25));
  assert.deepEqual([bubbleRadius(null, 100), bubbleRadius(0, 100), bubbleRadius(5, 0)], [MIN_BUBBLE, MIN_BUBBLE, MIN_BUBBLE]);
});

test("regions are matched without case on the atlas, as written on an SVG", () => {
  const data = { points: [], regions: [{ code: "fr", label: "France", value: 1 }] };
  assert.equal(regionsByCode(data, true).get("FR")?.label, "France");
  assert.equal(regionsByCode(data, false).get("FR"), undefined);
});

// ── Graphs ───────────────────────────────────────────────────────────────────

test("a link graph runs left to right, each edge to a later rank", () => {
  const placed = linkGraphLayout(graph([["a", "b", 1], ["b", "c", 1], ["a", "c", 1]]));
  const x = new Map(placed.nodes.map((n) => [n.id, n.x]));
  assert.ok(x.get("a")! < x.get("b")! && x.get("b")! < x.get("c")!);
  assert.deepEqual(placed.nodes.map((n) => [n.w, n.h, n.degree]), [[LINK_NODE.width, LINK_NODE.height, 2], [LINK_NODE.width, LINK_NODE.height, 2], [LINK_NODE.width, LINK_NODE.height, 2]]);
});

test("a network graph sizes its nodes by degree and colours them by group", () => {
  const g = graph([["hub", "a", 1], ["hub", "b", 1], ["hub", "c", 1]]);
  g.nodes[0].group = "Intrusion set";
  g.nodes[1].group = "Malware";
  g.nodes[2].group = "Intrusion set";
  const placed = networkLayout(g, 400, 300);
  assert.deepEqual(placed.groups, ["Intrusion set", "Malware"]);
  assert.deepEqual(placed.nodes.map((n) => n.tone), [0, 1, 0, null]);
  assert.equal(placed.nodes[0].w, 2 * MAX_RADIUS);
  assert.equal(nodeRadius(0, 3), MIN_RADIUS);
  for (const node of placed.nodes) assert.ok(Number.isFinite(node.x) && Number.isFinite(node.y));
});

// ── Trellis ──────────────────────────────────────────────────────────────────

test("a trellis option asks for a split, columns, a shared scale and an order", () => {
  assert.equal(trellisOptions({}), null);
  assert.equal(trellisOptions({ trellis: { enabled: false, splitBy: "series" } }), null);
  assert.deepEqual(trellisOptions({ trellis: { splitBy: "series", columns: 9, sort: "-total", sharedScale: false } }), {
    splitBy: "series",
    columns: 6,
    sharedScale: false,
    sort: "-total",
    labelField: undefined,
    valueField: undefined,
  });
  assert.equal(trellisOptions({ trellis: { enabled: true } })?.columns, "auto");
});

test("a series trellis is one panel per series, sorted, on one shared scale", () => {
  const data = {
    shape: "series" as const,
    data: {
      series: [
        { name: "Reports", points: [{ x: "2026-01", y: 4 }, { x: "2026-02", y: 6 }] },
        { name: "Indicators", points: [{ x: "2026-01", y: 30 }, { x: "2026-02", y: -2 }] },
      ],
    },
  };
  const trellis = trellisPanels(data, trellisOptions({ trellis: { splitBy: "series", sort: "-total" } })!)!;
  assert.deepEqual(trellis.panels.map((p) => [p.key, p.total]), [["Indicators", 28], ["Reports", 10]]);
  assert.deepEqual(trellis.domain, [-2, 30]);
  assert.equal(trellisPanels(data, trellisOptions({ trellis: { splitBy: "series", sharedScale: false } })!)!.domain, null);
});

test("a tree trellis is one distribution per top-level name; a table one per value of its split column", () => {
  const tree = trellisPanels(
    { shape: "tree", data: { items: [{ path: ["EU", "FR"], value: 3 }, { path: ["EU", "DE"], value: 1 }, { path: ["US"], value: 5 }] } },
    trellisOptions({ trellis: { splitBy: "group" } })!,
  )!;
  assert.deepEqual(tree.panels.map((p) => [p.key, p.data.shape === "distribution" && p.data.data.items]), [
    ["EU", [{ label: "FR", value: 3 }, { label: "DE", value: 1 }]],
    ["US", [{ label: "US", value: 5 }]],
  ]);
  const table = trellisPanels(
    {
      shape: "rows",
      data: rowsOf(["region", "sector", "count"], [
        { region: "EU", sector: "Energy", count: "4" },
        { region: "EU", sector: "Energy", count: "1" },
        { region: "US", sector: "Finance", count: "2" },
      ]),
    },
    trellisOptions({ trellis: { splitBy: "region" } })!,
  )!;
  assert.deepEqual(table.panels.map((p) => [p.key, p.data.shape === "distribution" && p.data.data.items]), [
    ["EU", [{ label: "Energy", value: 5 }]],
    ["US", [{ label: "Finance", value: 2 }]],
  ]);
  assert.equal(trellisPanels({ shape: "rows", data: rowsOf(["a"], []) }, trellisOptions({ trellis: { splitBy: "ghost" } })!), null);
  assert.equal(trellisPanels({ shape: "number", data: { value: 1, change: null } }, trellisOptions({ trellis: { enabled: true } })!), null);
});

test("a trellis keeps at most its panels and says how many it left", () => {
  const series = Array.from({ length: MAX_PANELS + 3 }, (_, i) => ({ name: `s${String(i).padStart(2, "0")}`, points: [] }));
  const trellis = trellisPanels({ shape: "series", data: { series } }, trellisOptions({ trellis: { splitBy: "series" } })!)!;
  assert.equal(trellis.panels.length, MAX_PANELS);
  assert.equal(trellis.hidden, 3);
});

test("a trellis takes the asked columns, else as many as the room holds", () => {
  assert.deepEqual([trellisColumns(4, 1000, 3), trellisColumns(2, 1000, 5), trellisColumns(10, 1000, "auto"), trellisColumns(10, 150, "auto")], [3, 2, 4, 1]);
});
