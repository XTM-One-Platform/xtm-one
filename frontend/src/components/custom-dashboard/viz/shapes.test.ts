/**
 * Unit tests for the readings of the closed shapes (`yarn test:unit`): a
 * table read as a graph, as stages, as events; figures and moments in cells.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { cellNumber, cellText, distributionToTree, parseMoment, rowsToEvents, rowsToGraph, rowsToStages, textRow, withoutLinks } from "./shapes.ts";

const table = (keys: string[], rows: Record<string, string>[]) => ({
  columns: keys.map((key) => ({ key, label: key.toUpperCase() })),
  rows,
  total: null,
});

test("a cell's figure is read with its thousands separators, anything else is no figure", () => {
  assert.deepEqual(
    ["1,500,000", " 42 ", "-3.5", "1e3", "12 %", "", "abc", "1.2.3"].map(cellNumber),
    [1500000, 42, -3.5, 1000, null, null, null, null],
  );
});

test("a moment is an ISO date at UTC midnight or an ISO date-time; an impossible date is none", () => {
  assert.equal(parseMoment("2026-01-31"), Date.UTC(2026, 0, 31));
  assert.equal(parseMoment("2026-01-31T10:00:00Z"), Date.UTC(2026, 0, 31, 10));
  assert.deepEqual(["2026-02-30", "31/01/2026", "", "2026"].map(parseMoment), [null, null, null, null]);
  assert.equal(parseMoment(42), null);
});

test("a table is a graph from its source and target columns, a repeated pair summed", () => {
  const graph = rowsToGraph(
    table(["name", "source", "target", "value"], [
      { name: "r1", source: "APT28", target: "X-Agent", value: "2" },
      { name: "r2", source: "APT28", target: "X-Agent", value: "3" },
      { name: "r3", source: "APT28", target: "", value: "9" },
      { name: "r4", source: "X-Agent", target: "France", value: "x" },
    ]),
  );
  assert.deepEqual(graph, {
    nodes: [
      { id: "APT28", label: "APT28", group: null, value: null },
      { id: "X-Agent", label: "X-Agent", group: null, value: null },
      { id: "France", label: "France", group: null, value: null },
    ],
    edges: [
      { source: "APT28", target: "X-Agent", label: null, value: 5 },
      { source: "X-Agent", target: "France", label: null, value: 0 },
    ],
  });
});

test("without source and target columns, the first two are; named fields win, unknown ones draw nothing", () => {
  const rows = table(["a", "b", "c"], [{ a: "1", b: "2", c: "3" }]);
  assert.deepEqual(rowsToGraph(rows).edges, [{ source: "1", target: "2", label: null, value: 1 }]);
  assert.deepEqual(rowsToGraph(rows, { sourceField: "b", targetField: "c" }).edges, [{ source: "2", target: "3", label: null, value: 1 }]);
  assert.deepEqual(rowsToGraph(rows, { sourceField: "ghost" }), { nodes: [], edges: [] });
});

test("a table's columns are the stages of a link graph, each value a node of its stage", () => {
  const graph = rowsToStages(
    table(["actor", "malware", "country"], [
      { actor: "APT28", malware: "X-Agent", country: "France" },
      { actor: "APT28", malware: "X-Agent", country: "Germany" },
      { actor: "APT29", malware: "", country: "France" },
    ]),
  );
  assert.deepEqual(
    graph.nodes.map((n) => [n.label, n.group]),
    [["APT28", "ACTOR"], ["X-Agent", "MALWARE"], ["France", "COUNTRY"], ["Germany", "COUNTRY"], ["APT29", "ACTOR"]],
  );
  // The empty cell breaks APT29's chain: no link jumps a stage.
  assert.deepEqual(
    graph.edges.map((e) => [e.source.split("\u001f")[1], e.target.split("\u001f")[1], e.value]),
    [["APT28", "X-Agent", 2], ["X-Agent", "France", 1], ["X-Agent", "Germany", 1]],
  );
  assert.equal(rowsToStages(table(["actor", "malware", "country"], []), { fields: ["country", "actor"] }).nodes.length, 0);
});

test("a table's rows are events from their date column, lane from their type", () => {
  const events = rowsToEvents(
    table(["name", "type", "date"], [
      { name: "Q3 report", type: "Report", date: "2026-10-01" },
      { name: "No date", type: "Report", date: "" },
      { name: "Campaign", type: "Campaign", date: "2026-09-12" },
    ]),
  );
  assert.deepEqual(events.events, [
    { label: "Q3 report", start: "2026-10-01", end: null, lane: "Report", value: null },
    { label: "Campaign", start: "2026-09-12", end: null, lane: "Campaign", value: null },
  ]);
  assert.deepEqual(rowsToEvents(table(["name"], [{ name: "x" }])).events, []);
});

test("a distribution is a one-level tree of its positive values", () => {
  assert.deepEqual(distributionToTree({ items: [{ label: "a", value: 2 }, { label: "b", value: 0 }] }), {
    items: [{ path: ["a"], value: 2 }],
  });
});

test("a cell reads as text whatever its kind", () => {
  assert.equal(cellText("x"), "x");
  assert.equal(cellText(4.5), "4.5");
  assert.equal(cellText(Number.NaN), "");
  assert.equal(cellText({ label: "High", color: "danger" }), "High");
  assert.equal(cellText([{ label: "a" }, { nope: 1 }, { label: "b" }]), "a, b");
  for (const empty of [null, undefined, true, { label: 3 }]) assert.equal(cellText(empty), "");
  assert.deepEqual(textRow({ a: "x", n: 2, c: [{ label: "t" }], z: null, _url: "/agents" }), { a: "x", n: "2", c: "t", _url: "/agents" });
});

test("data loses every link into a platform, its open_url kept", () => {
  const data = {
    status: "ok",
    open_url: "https://cti.example/dashboard",
    data: {
      items: [{ label: "APT29", link: "https://cti.example/id/1", entityType: "Intrusion-Set" }],
      rows: [{ name: "a", _url: "https://cti.example/id/2" }],
      actions: [{ label: "Open", link: "https://aev.example/x" }],
      links: ["t2"],
    },
  };
  assert.deepEqual(withoutLinks(data), {
    status: "ok",
    open_url: "https://cti.example/dashboard",
    data: { items: [{ label: "APT29", entityType: "Intrusion-Set" }], rows: [{ name: "a" }], actions: [{ label: "Open" }], links: ["t2"] },
  });
});
