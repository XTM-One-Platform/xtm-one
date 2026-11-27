/**
 * Unit tests for the studio's configuration of a visualization - `yarn test:unit`.
 *
 * A type switches to those drawing the same shape and keeps the options the
 * new type takes; a colour is the theme's, one colour, ranges or another
 * expression; the server's `set` and `delete` operations apply as it applies
 * them; a chart added draws sample data of its own; an input added sets a
 * token of its own.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  addChart,
  addInput,
  placementOf,
  setPlacement,
  applyOperations,
  colorFields,
  displayFields,
  readColorRule,
  setColorRule,
  setOptions,
  setType,
  setVisualization,
  switchType,
  typesForShape,
  typesLike,
  ADDABLE_TYPES,
  STARTER_RANGES,
} from "./viz-config.ts";

const base = () => ({
  dataSources: { ds_a: { type: "static", options: { shape: "series", data: { series: [] } } } },
  visualizations: {
    viz_a: { type: "xtm.line", title: "Trend", dataSources: { primary: "ds_a" }, options: { stackMode: "stacked", legendDisplay: "off", showBrush: true } },
  },
  inputs: {},
  layout: {
    tabs: { items: [{ layoutId: "layout_1", label: "Overview" }] },
    layoutDefinitions: { layout_1: { type: "grid", structure: [{ item: "viz_a", type: "block", position: { x: 0, y: 0, w: 6, h: 9 } }] } },
    globalInputs: [],
  },
});

test("a type switches to those drawing a shape it draws", () => {
  assert.deepEqual(typesLike("xtm.line"), ["xtm.line", "xtm.area", "xtm.column", "xtm.radar", "xtm.heatmap", "xtm.scatter"]);
  assert.deepEqual(typesForShape("outcomes"), ["xtm.outcomematrix", "xtm.pathgraph", "xtm.exposureconsole", "xtm.scoregauge", "xtm.ringgauge", "xtm.domaincards"]);
  assert.deepEqual(typesLike("xtm.singlevalue"), ["xtm.singlevalue", "xtm.singlevalueicon", "xtm.singlevalueradial", "xtm.fillergauge", "xtm.markergauge"]);
  assert.deepEqual(typesLike("xtm.answer"), ["xtm.answer"]);
  assert.deepEqual(typesLike("xtm.platformlink"), []);
  assert.deepEqual(typesForShape("tree"), ["xtm.treemap"]);
});

test("a switch keeps the options the new type takes", () => {
  const options = { stackMode: "stacked", legendDisplay: "off", seriesColors: ["#112233"], link: { url: "/x" } };
  assert.deepEqual(switchType(options, "xtm.area"), options);
  assert.deepEqual(switchType(options, "xtm.pie"), { legendDisplay: "off", seriesColors: ["#112233"], link: { url: "/x" } });
  assert.deepEqual(switchType(options, "xtm.singlevalue"), { link: { url: "/x" } });
  // A map takes any option (`StudioVizOptions`).
  assert.deepEqual(switchType(options, "xtm.map"), options);
  const switched = setType(base(), "viz_a", "xtm.column") as { visualizations: { viz_a: { type: string; options: unknown } } };
  assert.equal(switched.visualizations.viz_a.type, "xtm.column");
  assert.deepEqual(switched.visualizations.viz_a.options, { stackMode: "stacked", legendDisplay: "off", showBrush: true });
});

test("each type has its fields", () => {
  assert.deepEqual(
    displayFields("xtm.pie").map((field) => field.key),
    ["showDonutHole", "labelDisplay", "legendDisplay", "collapseThreshold", "collapseLabel"],
  );
  assert.deepEqual(
    colorFields("xtm.fillergauge").map((field) => `${field.kind}:${field.key}`),
    ["colorRule:gaugeColor", "ranges:gaugeRanges"],
  );
  assert.deepEqual(displayFields("xtm.map"), []);
});

test("an option emptied is removed, a visualization key set to undefined too", () => {
  const cleared = setOptions(base(), "viz_a", { stackMode: undefined, legendDisplay: "", lineWidth: 2 }) as {
    visualizations: { viz_a: { options: unknown } };
  };
  assert.deepEqual(cleared.visualizations.viz_a.options, { showBrush: true, lineWidth: 2 });
  const untitled = setVisualization(base(), "viz_a", { title: undefined, description: "Daily" }) as { visualizations: { viz_a: Record<string, unknown> } };
  assert.equal("title" in untitled.visualizations.viz_a, false);
  assert.equal(untitled.visualizations.viz_a.description, "Daily");
  // An unknown visualization: nothing changes.
  const same = base();
  assert.equal(setVisualization(same, "nope", { title: "x" }), same);
});

test("a colour is the theme's, one colour, ranges or another expression", () => {
  assert.deepEqual(readColorRule({}, {}, "majorColor"), { mode: "theme" });
  assert.deepEqual(readColorRule({ majorColor: "#ff0000" }, {}, "majorColor"), { mode: "static", color: "#ff0000" });
  assert.deepEqual(
    readColorRule({ majorColor: "> majorValue | rangeValue(majorColorEditorConfig)" }, { majorColorEditorConfig: [{ to: 5, value: "#00ff00" }] }, "majorColor"),
    { mode: "ranges", ranges: [{ to: 5, value: "#00ff00" }] },
  );
  // Ranges named but missing from the context, or a richer expression: shown as written.
  assert.deepEqual(readColorRule({ majorColor: "> majorValue | rangeValue(missing)" }, {}, "majorColor"), {
    mode: "custom",
    expression: "> majorValue | rangeValue(missing)",
  });
  assert.deepEqual(readColorRule({ majorColor: "> primary | lastPoint() | gradient(g)" }, {}, "majorColor").mode, "custom");
});

test("ranges are written as the option and its context entry, and removed with it", () => {
  const definition = setType(base(), "viz_a", "xtm.singlevalue");
  const ranged = setColorRule(definition, "viz_a", "majorColor", "majorValue", { mode: "ranges", ranges: STARTER_RANGES }) as {
    visualizations: { viz_a: { options: Record<string, unknown>; context: Record<string, unknown> } };
  };
  assert.equal(ranged.visualizations.viz_a.options.majorColor, "> majorValue | rangeValue(majorColorEditorConfig)");
  assert.deepEqual(ranged.visualizations.viz_a.context.majorColorEditorConfig, STARTER_RANGES);
  const flat = setColorRule(ranged, "viz_a", "majorColor", "majorValue", { mode: "static", color: "#123456" }) as typeof ranged;
  assert.equal(flat.visualizations.viz_a.options.majorColor, "#123456");
  assert.deepEqual(flat.visualizations.viz_a.context, {});
  const themed = setColorRule(flat, "viz_a", "majorColor", "majorValue", { mode: "theme" }) as typeof ranged;
  assert.equal("majorColor" in themed.visualizations.viz_a.options, false);
});

test("operations apply as the server applies them", () => {
  const definition = base();
  const applied = applyOperations(definition, [
    { op: "set", path: "visualizations.viz.b-1", value: { type: "xtm.table" } },
    { op: "set", path: "theme", value: "light" },
    { op: "delete", path: "dataSources.ds_a" },
  ]) as Record<string, Record<string, unknown>>;
  assert.deepEqual(Object.keys(applied.visualizations), ["viz_a", "viz.b-1"]);
  assert.equal(applied.theme, "light");
  assert.deepEqual(applied.dataSources, {});
  // A path the server refuses, or a group element that is no object: nothing applied.
  assert.equal(applyOperations(definition, [{ op: "set", path: "visualizations", value: {} }]), definition);
  assert.equal(applyOperations(definition, [{ op: "set", path: "inputs.x", value: "text" }]), definition);
  assert.equal((definition as Record<string, unknown>).theme, undefined);
});

test("a chart added draws sample data of its own, below the others", () => {
  const added = addChart(base(), "xtm.pie", "layout_1", "Sample data");
  assert.ok(added);
  const out = added.definition as ReturnType<typeof base> & { dataSources: Record<string, { type: string; name: string; options: { shape: string } }> };
  assert.equal(added.id, "viz_ds_sample");
  assert.deepEqual(out.visualizations[added.id as "viz_a"], { type: "xtm.pie", dataSources: { primary: "ds_sample" } });
  assert.equal(out.dataSources.ds_sample.type, "static");
  assert.equal(out.dataSources.ds_sample.name, "Sample data");
  assert.equal(out.dataSources.ds_sample.options.shape, "distribution");
  assert.deepEqual(out.layout.layoutDefinitions.layout_1.structure[1], { item: "viz_ds_sample", type: "block", position: { x: 0, y: 9, w: 6, h: 4 } });
  // A second one takes the next free ids.
  const again = addChart(added.definition, "xtm.singlevalue", "layout_1", "Sample data");
  assert.equal(again?.id, "viz_ds_sample_2");
  // No sample draws a map: it is not offered.
  assert.equal(addChart(base(), "xtm.map", "layout_1", "Sample data"), null);
  assert.equal(ADDABLE_TYPES.includes("xtm.map"), false);
  assert.equal(ADDABLE_TYPES.includes("xtm.table"), true);
  assert.deepEqual(["xtm.radar", "xtm.polararea", "xtm.wordcloud", "xtm.heatmap", "xtm.outcomematrix"].map((t) => ADDABLE_TYPES.includes(t as never)), [true, true, true, true, true]);
});

test("an input added sets a token of its own, above the tabs", () => {
  const added = addInput(base(), "dropdown", "Severity");
  const out = added.definition as ReturnType<typeof base> & { inputs: Record<string, unknown> };
  assert.equal(added.id, "input_dropdown");
  assert.deepEqual(out.inputs.input_dropdown, { type: "input.dropdown", title: "Severity", token: "input_dropdown", options: { items: [] } });
  assert.deepEqual(out.layout.globalInputs, ["input_dropdown"]);
  const button = addInput(added.definition, "button", "Reset");
  assert.deepEqual((button.definition as typeof out).inputs.input_button, {
    type: "input.button",
    title: "Reset",
    options: { label: "Reset", actions: [{ type: "setToken", token: "input_button", value: "clicked" }] },
  });
  const range = addInput(base(), "timerange", "Period");
  assert.equal(((range.definition as typeof out).inputs.input_timerange as { defaultValue: string }).defaultValue, "30d");
});

test("a visualization's place is read and set, kept on the grid's columns", () => {
  const placement = placementOf(base(), "viz_a");
  assert.deepEqual(placement, { layoutId: "layout_1", type: "grid", x: 0, y: 0, w: 6, h: 9 });
  const moved = setPlacement(base(), "viz_a", { layoutId: "layout_1", type: "grid", x: 10, y: -2, w: 4, h: 99 });
  assert.deepEqual(placementOf(moved, "viz_a"), { layoutId: "layout_1", type: "grid", x: 8, y: 0, w: 4, h: 40 });
  assert.equal(placementOf(base(), "nope"), null);
});
