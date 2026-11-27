/**
 * Unit tests for the version 2 definition reader and the source editor's
 * helpers - `yarn test:unit`.
 *
 * A definition is drawn tab by tab: each placed visualization becomes the
 * widget it draws (content, a platform source, a static source), the defaults
 * under its own options; what is not understood stays a placeholder.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  addVisualizationOperations,
  freeId,
  parseSource,
  refreshSeconds,
  sourceEntries,
  sourceScopes,
  sourceText,
  presentedOptions,
  readDefinition,
} from "./definition.ts";
import { normalizeDashboard } from "./spec.ts";

const INTEGRATION = "11111111-1111-4111-8111-111111111111";

function definition(extra: Record<string, unknown> = {}) {
  return {
    version: 2,
    title: "Exposure",
    description: "Week 43",
    dataSources: {
      ds_kev: {
        type: "platform",
        options: { source: "opencrq", integration_id: INTEGRATION, native: { metric: "kev_findings" } },
      },
      answer: { type: "static", options: { shape: "number", data: { value: 42 } } },
    },
    visualizations: {
      note: { type: "xtm.text", title: "Note", options: { body: "Read me" } },
      kev: { type: "xtm.column", title: "KEV", dataSources: { primary: "ds_kev" } },
      big: { type: "xtm.singlevalue", title: "Answer", dataSources: { primary: "answer" } },
      later: { type: "xtm.text", options: { body: "On tab two" } },
      gauge: { type: "xtm.gauge9000", dataSources: { primary: "answer" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }, { layoutId: "two", label: "Two" }] },
      layoutDefinitions: {
        main: {
          type: "grid",
          structure: [
            { item: "note", position: { x: 0, y: 0, w: 4, h: 4 } },
            { item: "kev", position: { x: 4, y: 0, w: 8, h: 8 } },
            { item: "big", position: { x: 0, y: 4, w: 4, h: 6 } },
            { item: "gauge", position: { x: 0, y: 10, w: 12, h: 4 } },
          ],
        },
        two: { type: "absolute", structure: [{ item: "later", position: { x: 700, y: 40, w: 300, h: 200 } }] },
      },
    },
    ...extra,
  };
}

test("a definition is read tab by tab, each visualization as the widget it draws", () => {
  const dashboard = normalizeDashboard(definition());
  assert.equal(dashboard.title, "Exposure");
  assert.equal(dashboard.subtitle, "Week 43");
  assert.deepEqual(
    dashboard.tabs.map((tab) => [tab.id, tab.label, tab.type, tab.widgets.map((w) => `${w.id}:${w.kind}`)]),
    [
      ["main", "Main", "grid", ["note:text", "kev:platform", "big:data", "gauge:unsupported"]],
      ["two", "Two", "absolute", ["later:text"]],
    ],
  );
  assert.deepEqual(dashboard.widgets, dashboard.tabs[0].widgets);
  assert.equal(dashboard.showTabBar, true);
  const [note, kev, big] = dashboard.tabs[0].widgets;
  assert.deepEqual([note.layout, note.width], [{ x: 0, y: 0, w: 4, h: 4 }, "third"]);
  assert.ok(kev.kind === "platform");
  assert.deepEqual([kev.source, kev.integrationId, kev.view, kev.nativeType], ["opencrq", INTEGRATION, "bar", "kev_findings"]);
  assert.ok(big.kind === "data");
  assert.equal(big.view, "number");
});

test("an absolute layout's pixels are no grid cell: its widgets are placed in rows", () => {
  const [later] = normalizeDashboard(definition()).tabs[1].widgets;
  assert.deepEqual(later.layout, { x: 0, y: 0, w: 12, h: 2 });
});

test("the defaults go under each visualization's own options, global then by type", () => {
  const dashboard = normalizeDashboard(
    definition({
      defaults: {
        visualizations: {
          global: { options: { showLastUpdated: true, unit: "global" } },
          "xtm.column": { options: { unit: "findings", stack: "auto" } },
        },
      },
      visualizations: {
        ...definition().visualizations,
        kev: { type: "xtm.column", dataSources: { primary: "ds_kev" }, options: { stack: "percent" } },
      },
    }),
  );
  const kev = dashboard.tabs[0].widgets.find((w) => w.id === "kev");
  assert.ok(kev?.kind === "platform");
  assert.deepEqual(kev.options, { showLastUpdated: true, unit: "findings", stack: "percent" });
});

test("a static widget is read again when its data changes, not when another one does", () => {
  const before = normalizeDashboard(definition()).tabs[0].widgets.find((w) => w.id === "big");
  const other = definition({ title: "Renamed" });
  const changed = definition();
  changed.dataSources.answer.options.data = { value: 43 };
  const same = normalizeDashboard(other).tabs[0].widgets.find((w) => w.id === "big");
  const after = normalizeDashboard(changed).tabs[0].widgets.find((w) => w.id === "big");
  assert.ok(before?.kind === "data" && same?.kind === "data" && after?.kind === "data");
  assert.equal(same.fingerprint, before.fingerprint);
  assert.notEqual(after.fingerprint, before.fingerprint);
});

test("one tab shows no bar unless asked; the application properties are read as switches", () => {
  const single = definition({
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }] },
      layoutDefinitions: { main: { structure: [{ item: "note", position: { x: 0, y: 0, w: 4, h: 4 } }] } },
    },
    applicationProperties: { hideEdit: true, hideExport: "yes", hideOpenInPlatform: true },
  });
  const dashboard = normalizeDashboard(single);
  assert.equal(dashboard.showTabBar, false);
  assert.deepEqual(dashboard.appProperties, {
    hideEdit: true,
    hideExport: false,
    hideOpenInPlatform: true,
    collapseNavigation: false,
  });
  const hidden = definition();
  (hidden.layout.tabs as Record<string, unknown>).options = { showTabBar: false };
  assert.equal(normalizeDashboard(hidden).showTabBar, false);
});

test("a broken definition never throws: what is not understood is skipped or a placeholder", () => {
  for (const broken of [
    { version: 2 },
    { version: 2, layout: { tabs: { items: [{ layoutId: "ghost" }] } } },
    { version: 2, visualizations: [], layout: { tabs: "x", layoutDefinitions: null } },
    definition({ visualizations: { note: "text" } }),
  ]) {
    const dashboard = normalizeDashboard(broken);
    assert.ok(dashboard.tabs.length >= 1);
  }
  const dashboard = normalizeDashboard(definition({ visualizations: { note: "text" } }));
  assert.deepEqual(dashboard.tabs[0].widgets.map((w) => w.id), []);
});

test("a widget dashboard still reads as one tab without a bar", () => {
  const dashboard = normalizeDashboard({ title: "T", widgets: [{ kind: "text", id: "a", body: "x" }] });
  assert.deepEqual(
    dashboard.tabs.map((tab) => [tab.id, tab.widgets.length]),
    [["layout_1", 1]],
  );
  assert.equal(dashboard.showTabBar, false);
});

test("the source editor offers the dashboard, the picked widget's visualization and source, and the defaults", () => {
  const spec = definition();
  assert.deepEqual(
    sourceScopes(spec, "kev").map((scope) => [scope.key, scope.path]),
    [
      ["dashboard", null],
      ["visualization", "visualizations.kev"],
      ["dataSource", "dataSources.ds_kev"],
      ["defaults", "defaults"],
    ],
  );
  assert.deepEqual(
    sourceScopes(spec, "note").map((scope) => scope.key),
    ["dashboard", "visualization", "defaults"],
  );
  assert.deepEqual(
    sourceScopes(spec, null).map((scope) => scope.key),
    ["dashboard", "defaults"],
  );
});

test("the source editor shows an element by its path and reads JSON or says why not", () => {
  const spec = definition();
  assert.deepEqual(JSON.parse(sourceText(spec, "visualizations.kev")), spec.visualizations.kev);
  assert.deepEqual(JSON.parse(sourceText(spec, null)), spec);
  assert.deepEqual(JSON.parse(sourceText(spec, "defaults")), {});
  assert.deepEqual(parseSource('{"a": 1}'), { ok: true, value: { a: 1 } });
  const bad = parseSource("{a: 1}");
  assert.equal(bad.ok, false);
});

// ── Level 2: every source type, refresh, the Data sources panel ─────────────

function sourcesDefinition() {
  return {
    version: 2,
    title: "Sources",
    dataSources: {
      base: { type: "static", options: { shape: "rows", data: { columns: [{ key: "sev" }], rows: [] } } },
      top: { type: "derived", options: { extends: "base", steps: [{ op: "top", field: "sev" }], refresh: "5m" } },
      kev: { type: "assignment", name: "KEV", options: { assignment_id: INTEGRATION, shape: "number" } },
      q: { type: "ask", options: { agent_id: INTEGRATION, question: "Why?", shape: "text" } },
      unused: { type: "variable", options: { variable_id: INTEGRATION, refresh: "10s" } },
    },
    visualizations: {
      a: { type: "xtm.pie", dataSources: { primary: "top" } },
      b: { type: "xtm.singlevalue", dataSources: { primary: "kev" } },
      c: { type: "xtm.answer", dataSources: { primary: "q" } },
    },
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }] },
      layoutDefinitions: {
        main: {
          structure: [
            { item: "a", position: { x: 0, y: 0, w: 6, h: 8 } },
            { item: "b", position: { x: 6, y: 0, w: 6, h: 6 } },
            { item: "c", position: { x: 0, y: 8, w: 12, h: 8 } },
          ],
        },
      },
    },
  };
}

test("each source type is drawn as a data widget naming its source, type and refresh", () => {
  const widgets = normalizeDashboard(sourcesDefinition()).widgets;
  assert.deepEqual(
    widgets.map((w) => (w.kind === "data" ? [w.id, w.sourceId, w.sourceType, w.view, w.refresh] : [w.id, w.kind])),
    [
      ["a", "top", "derived", "donut", 300],
      ["b", "kev", "assignment", "number", undefined],
      ["c", "q", "ask", "text", undefined],
    ],
  );
});

test("an integration's read tool and a knowledge base table are drawn as data widgets", () => {
  const definition = sourcesDefinition();
  definition.dataSources = {
    ...definition.dataSources,
    tool: { type: "integration", options: { integration_id: INTEGRATION, tool: "list_issues", shape: "rows" } },
    table: { type: "knowledge", options: { knowledge_base_id: INTEGRATION, document_id: INTEGRATION, shape: "distribution" } },
  } as typeof definition.dataSources;
  definition.visualizations = {
    ...definition.visualizations,
    d: { type: "xtm.table", dataSources: { primary: "tool" } },
    e: { type: "xtm.bar", dataSources: { primary: "table" } },
  } as typeof definition.visualizations;
  definition.layout.layoutDefinitions.main.structure.push(
    { item: "d", position: { x: 0, y: 16, w: 6, h: 6 } },
    { item: "e", position: { x: 6, y: 16, w: 6, h: 6 } },
  );
  const widgets = normalizeDashboard(definition).widgets.filter((w) => w.id === "d" || w.id === "e");
  assert.deepEqual(
    widgets.map((w) => [w.id, w.kind, w.kind === "data" ? w.sourceType : null]),
    [
      ["d", "data", "integration"],
      ["e", "data", "knowledge"],
    ],
  );
});

test("a derived widget is read again when the source it derives from changes", () => {
  const before = normalizeDashboard(sourcesDefinition()).widgets[0];
  const changed = sourcesDefinition();
  changed.dataSources.base.options.data.columns = [{ key: "severity" }];
  const after = normalizeDashboard(changed).widgets[0];
  assert.ok(before.kind === "data" && after.kind === "data");
  assert.notEqual(after.fingerprint, before.fingerprint);
});

test("a refresh outside 30 s to a day is no refresh", () => {
  assert.equal(refreshSeconds("10s"), undefined);
  assert.equal(refreshSeconds("2d"), undefined);
  assert.equal(refreshSeconds("1h"), 3600);
  assert.equal(refreshSeconds("24h"), 86_400);
  assert.equal(refreshSeconds(30), undefined);
});

test("the panel lists every source with what uses it, an unused one included", () => {
  assert.deepEqual(
    sourceEntries(sourcesDefinition()).map((entry) => [entry.id, entry.type, entry.usedBy]),
    [
      ["base", "static", ["top"]],
      ["top", "derived", ["a"]],
      ["kev", "assignment", ["b"]],
      ["q", "ask", ["c"]],
      ["unused", "variable", []],
    ],
  );
});

test("a new id is never one taken by a source, a visualization or an input", () => {
  const spec = sourcesDefinition();
  assert.equal(freeId(spec, "fresh"), "fresh");
  assert.equal(freeId(spec, "top"), "top_2");
  assert.equal(freeId(spec, "a"), "a_2");
  assert.equal(freeId(spec, "../x y"), "x_y");
});

test("a new visualization of a source is placed below the others in the tab named", () => {
  const { id, operations } = addVisualizationOperations(sourcesDefinition(), "unused", "xtm.answer", " Target ", "main");
  assert.equal(id, "viz_unused");
  assert.deepEqual(operations[0], {
    op: "set",
    path: "visualizations.viz_unused",
    value: { type: "xtm.answer", dataSources: { primary: "unused" }, title: "Target" },
  });
  const layout = operations[1].value as { layoutDefinitions: { main: { structure: unknown[] } } };
  assert.deepEqual(layout.layoutDefinitions.main.structure[layout.layoutDefinitions.main.structure.length - 1], {
    item: "viz_unused",
    type: "block",
    position: { x: 0, y: 16, w: 6, h: 4 },
  });
  // An unknown layout falls back to the first tab's.
  assert.equal(addVisualizationOperations(sourcesDefinition(), "kev", "xtm.singlevalue", "", "ghost").operations[1].path, "layout");
});

test("the source editor opens on a data source the panel names", () => {
  assert.deepEqual(
    sourceScopes(sourcesDefinition(), "a", "kev").map((scope) => [scope.key, scope.path]),
    [
      ["dashboard", null],
      ["dataSource", "dataSources.kev"],
      ["defaults", "defaults"],
    ],
  );
});

test("a module's presentation lies under a visualization's options when it is drawn with the presentation's view", () => {
  const presentation = { view: "xtm.pie", options: { showDonutHole: true, labelDisplay: "values" } };
  // The visualization's own options win; the preset fills the rest.
  assert.deepEqual(presentedOptions({ labelDisplay: "off" }, "donut", presentation), { showDonutHole: true, labelDisplay: "off" });
  // Drawn another way, it keeps its own look.
  assert.deepEqual(presentedOptions({ labelDisplay: "off" }, "bar", presentation), { labelDisplay: "off" });
  for (const wrong of [null, undefined, "xtm.pie", { view: "xtm.nothing", options: { a: 1 } }, { view: 3, options: {} }]) {
    assert.deepEqual(presentedOptions({ a: 2 }, "donut", wrong), { a: 2 });
  }
  assert.deepEqual(presentedOptions({}, "donut", { view: "xtm.pie", options: "junk" }), {});
  assert.deepEqual(presentedOptions({}, undefined, presentation), {});
});

test("a validation view XTM One invented is read as its replacement, and takes its module's preset", () => {
  const legacy = ["xtm.validationgates", "xtm.exposuregauge", "xtm.resiliencerings", "xtm.outcomecards", "xtm.outcomegrid"];
  const read = readDefinition({
    version: 2,
    dataSources: { out: { type: "static", options: { shape: "outcomes", data: { items: [] } } } },
    visualizations: Object.fromEntries(legacy.map((type, i) => [`v${i}`, { type, dataSources: { primary: "out" }, options: { sampleDisplay: "full" } }])),
    layout: {
      tabs: { items: [{ layoutId: "main", label: "Main" }] },
      layoutDefinitions: { main: { type: "grid", structure: legacy.map((_, i) => ({ item: `v${i}`, position: { x: 0, y: i * 6, w: 12, h: 6 } })) } },
    },
  });
  assert.deepEqual(
    read.tabs[0].records.map((record) => [record.kind, record.view, record.options]),
    ["exposureconsole", "scoregauge", "ringgauge", "domaincards", "outcomematrix"].map((view) => ["data", view, { sampleDisplay: "full" }]),
  );
  // Drawn with its replacement, a module's preset for it applies.
  assert.deepEqual(presentedOptions({}, "scoregauge", { view: "xtm.scoregauge", options: { sampleDisplay: "full" } }), { sampleDisplay: "full" });
});
