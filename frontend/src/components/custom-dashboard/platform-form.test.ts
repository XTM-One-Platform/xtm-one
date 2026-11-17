/**
 * Unit tests for the one platform source form - `yarn test:unit`.
 *
 * The engine runs on the descriptions the server serves (the fixture
 * `backend/tests/fixtures/dashboards/platform_forms.json`): new widgets are
 * written as OpenCTI, OpenAEV and OpenCRQ write them, and every edit changes
 * only what it is about and keeps everything else of the stored configuration.
 * The new widget of every type is pinned in
 * `backend/tests/fixtures/dashboards/editor_widgets.json`, which
 * `backend/tests/test_dashboard_platform_schema.py` runs through `check_native`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { platformForm } from "../../testing/platform-forms.ts";
import {
  addItem,
  addable,
  childScope,
  choicesOf,
  edit,
  evaluate,
  formCarries,
  listBounds,
  locate,
  newWidget,
  operatorsFor,
  readField,
  rootScope,
  writeField,
} from "./platform-form.ts";
import type { PlatformFilterGroup } from "./platform-native.ts";

type Raw = Record<string, unknown>;
type Selection = Raw & { filters: PlatformFilterGroup; columns?: unknown[] };

const cti = platformForm("opencti");
const aev = platformForm("openaev");
const crq = platformForm("opencrq");

const EMPTY = { mode: "and", filters: [], filterGroups: [] };
// Ids are random: compared as "an id" (OpenAEV filters).
const withoutIds = (value: unknown): unknown => JSON.parse(JSON.stringify(value, (key, v) => (key === "id" ? "ID" : v)));
const selections = (widget: Raw) => widget.dataSelection as Selection[];
const config = (widget: Raw) => widget.widget_config as Raw;
const identity = (key: string) => key;

/** The value a field shows. */
function read(form: typeof cti, native: Raw, name: string, items: number[] = []) {
  const found = locate(form, native, name, items);
  assert.ok(found, name);
  return readField(found.field, found.scope);
}

/** A list's button, pressed as a person would: only while it adds an item. */
function press(form: typeof cti, native: Raw, list: string, label: string): Raw | null {
  const found = locate(form, native, list);
  assert.ok(found, list);
  const index = (found.field.add ?? []).findIndex((add) => add.label === label);
  return addable(found.field, found.scope, index) ? addItem(found.field, found.scope, index) : null;
}

test("a new OpenCTI widget is written as OpenCTI writes it once its perspective is chosen", () => {
  assert.deepEqual(newWidget(cti, "donut", { perspective: "relationships" }), {
    type: "donut",
    perspective: "relationships",
    parameters: {},
    dataSelection: [
      {
        label: "",
        number: 10,
        sort_by: "created_at",
        sort_mode: "desc",
        attribute: "entity_type",
        date_attribute: "created_at",
        isTo: true,
        perspective: "relationships",
        filters: EMPTY,
        dynamicFrom: EMPTY,
        dynamicTo: EMPTY,
        columns: [
          { attribute: "entity_type", label: "Type" },
          { attribute: "from_entity_type", label: "Source type" },
          { attribute: "from_relationship_type", label: "Source name" },
          { attribute: "to_entity_type", label: "Target type" },
          { attribute: "to_relationship_type", label: "Target name" },
          { attribute: "created", label: "Original creation date" },
          { attribute: "created_at", label: "Platform creation date" },
          { attribute: "createdBy" },
          { attribute: "objectMarking" },
        ],
      },
    ],
  });
  // A perspective the type does not take is replaced by its first one.
  assert.equal(newWidget(cti, "map", { perspective: "entities" }).perspective, "relationships");
  assert.deepEqual(selections(newWidget(cti, "number", { perspective: "audits" }))[0].columns, []);
  // A new source starts as the description's default.
  assert.deepEqual(newWidget(cti), newWidget(cti, "number", { perspective: "entities" }));
});

test("an OpenCTI edit changes what it is about and keeps the rest", () => {
  const stored = {
    type: "vertical-bar",
    perspective: "entities",
    parameters: { title: "Reports", description: "kept" },
    extra: "kept",
    dataSelection: [
      {
        label: "",
        number: null,
        attribute: "entity_type",
        date_attribute: "published",
        perspective: "entities",
        filters: {
          mode: "or",
          filters: [{ key: ["objectLabel"], values: ["l1"], operator: "eq", mode: "or", id: "f1" }],
          filterGroups: [{ mode: "and", filters: [{ key: ["confidence"], values: ["50"], operator: "gt", mode: "or" }], filterGroups: [] }],
        },
        dynamicFrom: EMPTY,
        dynamicTo: EMPTY,
        unknown_key: 7,
      },
    ],
  };
  assert.equal(formCarries(cti, stored), true);
  // The types picker adds its filter first and leaves the others, nested groups included.
  const typed = edit(cti, stored, "types", ["Report", "Note"], [0]);
  const filters = selections(typed)[0].filters;
  assert.deepEqual(filters.filters[0], { key: ["entity_type"], values: ["Report", "Note"], operator: "eq", mode: "or" });
  assert.deepEqual(filters.filters.slice(1), stored.dataSelection[0].filters.filters);
  assert.deepEqual(filters.filterGroups, stored.dataSelection[0].filters.filterGroups);
  assert.equal(filters.mode, "or");
  assert.deepEqual(read(cti, typed, "types", [0]), ["Report", "Note"]);
  // Changed in place, then removed when no type is left.
  const one = edit(cti, typed, "types", ["Report"], [0]);
  assert.deepEqual(selections(one)[0].filters.filters[0].values, ["Report"]);
  const none = edit(cti, one, "types", [], [0]);
  assert.deepEqual(selections(none)[0].filters, stored.dataSelection[0].filters);
  // A parameter set or removed; the selection's other keys untouched.
  const stacked = edit(cti, none, "parameters.stacked", true);
  assert.deepEqual(stacked.parameters, { title: "Reports", description: "kept", stacked: true });
  assert.deepEqual(edit(cti, stacked, "parameters.title", "").parameters, { description: "kept", stacked: true });
  const dated = edit(cti, stacked, "date_attribute", "created_at", [0]);
  assert.deepEqual({ ...selections(dated)[0], date_attribute: "published" }, stored.dataSelection[0]);
  assert.equal(dated.extra, "kept");
  // An emptied top N is OpenCTI's null; a number of results it does not show is not offered.
  const donut = { ...stored, type: "donut", dataSelection: [{ ...stored.dataSelection[0], number: 8 }] };
  assert.equal(selections(edit(cti, donut, "number", null, [0]))[0].number, null);
  assert.equal(locate(cti, stored, "number", [0]), undefined);
});

test("selections follow OpenCTI: added with its keys, up to the type's limit, never the last removed", () => {
  const bars = newWidget(cti, "vertical-bar", { perspective: "relationships" });
  const two = press(cti, bars, "dataSelection", "Add entities");
  assert.ok(two);
  assert.deepEqual(selections(two)[1], {
    label: "",
    attribute: "entity_type",
    date_attribute: "created_at",
    perspective: "entities",
    filters: EMPTY,
    dynamicFrom: EMPTY,
    dynamicTo: EMPTY,
  });
  let full = bars;
  for (let i = 0; i < 9; i += 1) full = press(cti, full, "dataSelection", "Add relationships") ?? full;
  assert.equal(selections(full).length, 5);
  const list = locate(cti, bars, "dataSelection");
  assert.ok(list);
  assert.deepEqual(listBounds(list.field, list.scope), { count: 1, min: 1, max: 5 });
  // Activity and history cannot add a second breakdown.
  assert.equal(press(cti, newWidget(cti, "horizontal-bar", { perspective: "audits" }), "dataSelection", "Add a data selection"), null);
  // Another type: its limit and perspectives apply.
  assert.equal(selections(edit(cti, full, "type", "donut")).length, 1);
  const bookmark = edit(cti, full, "type", "bookmark");
  assert.equal(bookmark.perspective, "entities");
  assert.deepEqual(selections(bookmark)[0].filters, EMPTY);
  assert.equal(selections(edit(cti, full, "type", "heatmap")).length, 5);
  // Another perspective starts each selection's filters and columns over.
  const counted = { ...two, dataSelection: [{ ...selections(two)[0], attribute: "internal_id", date_attribute: "start_time" }, selections(two)[1]] };
  const audits = edit(cti, counted, "perspective", "audits");
  assert.deepEqual(
    selections(audits).map((s) => [s.perspective, s.columns, s.attribute, s.date_attribute]),
    [["audits", [], "entity_type", "created_at"], ["audits", [], "entity_type", "created_at"]],
  );
});

test("OpenCTI's choices: operators by key type, dates by perspective, columns by entity type", () => {
  assert.deepEqual(operatorsFor(cti, { label: "d", type: "date" }), ["gt", "gte", "lt", "lte", "nil", "not_nil", "within"]);
  assert.deepEqual(operatorsFor(cti, { label: "i", type: "id", multiple: true }), ["eq", "not_eq", "only_eq_to", "not_only_eq_to", "nil", "not_nil"]);
  assert.deepEqual(operatorsFor(cti, { label: "i", type: "id" }), ["eq", "not_eq", "nil", "not_nil"]);
  const scope = rootScope(cti, {});
  const dates = (perspective: string, types: string[]) => evaluate({ $template: "dates", with: { perspective, types } }, scope);
  assert.deepEqual(dates("relationships", []), ["created_at", "updated_at", "created", "modified", "start_time", "stop_time"]);
  assert.deepEqual(dates("relationships", ["stix-sighting-relationship"]), ["created_at", "updated_at", "created", "modified", "first_seen", "last_seen"]);
  assert.deepEqual(dates("audits", []), ["created_at", "updated_at", "created", "modified"]);
  // The date picker of a relationships selection counting sightings offers theirs.
  const sightings = edit(cti, newWidget(cti, "line", { perspective: "relationships" }), "types", ["stix-sighting-relationship"], [0]);
  const picker = locate(cti, sightings, "date_attribute", [0]);
  assert.ok(picker);
  assert.deepEqual(choicesOf(picker.field.options, picker.scope, identity).map((c) => c.value).slice(-2), ["first_seen", "last_seen"]);
  const columns = (perspective: string, types: string[]) => evaluate({ $template: "availableColumns", with: { perspective, types } }, scope) as unknown[];
  assert.deepEqual(columns("entities", ["Malware"]).slice(-1), [{ attribute: "malware_types", label: "Malware type" }]);
  assert.equal(columns("entities", ["Malware", "Report"]).length, 10);
  assert.deepEqual(columns("relationships", []).slice(-2), [{ attribute: "start_time" }, { attribute: "stop_time" }]);
});

test("a new OpenAEV widget is written as OpenAEV writes it", () => {
  assert.deepEqual(withoutIds(newWidget(aev, "donut", { entity: "expectation-inject" })), {
    widget_type: "donut",
    widget_config: {
      widget_configuration_type: "structural-histogram",
      title: "",
      date_attribute: "base_created_at",
      time_range: "DEFAULT",
      series: [{ name: "", filter: { mode: "and", filters: [{ id: "ID", key: "base_entity", mode: "and", values: ["expectation-inject"], operator: "eq" }] } }],
      mode: "structural",
      field: "",
      limit: 10,
    },
  });
  const list = config(newWidget(aev, "list", { entity: "finding" }));
  assert.deepEqual(withoutIds(list.perspective), {
    name: "",
    filter: { mode: "and", filters: [{ id: "ID", key: "base_entity", mode: "and", values: ["finding"], operator: "eq" }] },
  });
  assert.equal(list.series, undefined);
  const domains = config(newWidget(aev, "average")).series as { filter: PlatformFilterGroup }[];
  assert.equal(domains[0].filter.filters[0].mode, "or");
  const coverage = newWidget(aev, "security-coverage");
  assert.equal(config(coverage).field, "base_attack_patterns_side");
  assert.equal(config(coverage).limit, undefined);
  const composed = locate(aev, coverage, "widget_config.series");
  assert.ok(composed);
  assert.deepEqual(childScope(composed.scope, composed.field).params, { expectation: "DETECTION", simulation: null });
  assert.deepEqual(
    (config(coverage).series as { name: string; filter: PlatformFilterGroup }[]).map((s) => [s.name, s.filter.filters.map((f) => [f.key, f.values])]),
    [
      ["SUCCESS", [["base_entity", ["expectation-inject"]], ["inject_expectation_status", ["SUCCESS"]], ["inject_expectation_type", ["DETECTION"]]]],
      ["FAILED", [["base_entity", ["expectation-inject"]], ["inject_expectation_status", ["FAILED"]], ["inject_expectation_type", ["DETECTION"]]]],
    ],
  );
  // A new source is a number of findings.
  assert.deepEqual(withoutIds(newWidget(aev)), withoutIds(newWidget(aev, "number", { entity: "finding" })));
});

test("OpenAEV's coverage series follow detection or prevention and the simulation", () => {
  // Written through the compose: both series are rebuilt from the two choices.
  const pick = (native: Raw, index: number, value: unknown) => {
    const composed = locate(aev, native, "widget_config.series");
    assert.ok(composed);
    const field = composed.field.fields?.[0].fields?.[index];
    assert.ok(field);
    return writeField(field, childScope(composed.scope, composed.field), value);
  };
  const coverage = newWidget(aev, "attack-path");
  const chosen = pick(pick(coverage, 0, "PREVENTION"), 1, "sim-1");
  const series = config(chosen).series as { name: string; filter: PlatformFilterGroup }[];
  assert.deepEqual(
    series.map((s) => [s.name, ...s.filter.filters.map((f) => [f.key, f.values[0]]).slice(2)]),
    [
      ["SUCCESS", ["inject_expectation_type", "PREVENTION"], ["base_simulation_side", "sim-1"]],
      ["FAILED", ["inject_expectation_type", "PREVENTION"], ["base_simulation_side", "sim-1"]],
    ],
  );
  // No simulation: its filter goes, the expectation stays.
  const cleared = config(pick(chosen, 1, null)).series as { filter: PlatformFilterGroup }[];
  assert.deepEqual(cleared[0].filter.filters.map((f) => f.key), ["base_entity", "inject_expectation_status", "inject_expectation_type"]);
  assert.equal(cleared[0].filter.filters[2].values[0], "PREVENTION");
});

test("an OpenAEV type or mode change keeps what both read the same way", () => {
  const donut = newWidget(aev, "donut", { entity: "finding" });
  (donut.widget_config as Raw).field = "finding_type";
  (donut.widget_config as Raw).title = "Findings";
  const line = edit(aev, donut, "type", "line");
  assert.deepEqual(
    [config(line).widget_configuration_type, config(line).mode, config(line).interval, config(line).field, config(line).limit],
    ["temporal-histogram", "temporal", "week", undefined, undefined],
  );
  assert.deepEqual(config(line).series, config(donut).series);
  const number = edit(aev, line, "type", "number");
  assert.deepEqual(Object.keys(config(number)).sort(), ["date_attribute", "series", "time_range", "title", "widget_configuration_type"]);
  const bars = edit(aev, edit(aev, donut, "type", "vertical-barchart"), "mode", "temporal-histogram");
  assert.equal(config(bars).widget_configuration_type, "temporal-histogram");
  // To a list: its entity is kept, its filters start over.
  const list = edit(aev, donut, "type", "list");
  assert.equal(((config(list).perspective as Raw).filter as PlatformFilterGroup).filters[0].values[0], "finding");
  assert.equal(config(list).title, "Findings");
  // A series' entity changes in place: the filter keeps its id, the other filters go.
  const group = (config(donut).series as { filter: PlatformFilterGroup }[])[0].filter;
  const filtered = { ...donut, widget_config: { ...config(donut), series: [{ name: "", filter: { ...group, filters: [...group.filters, { id: "f2", key: "finding_type", mode: "and", values: ["cve"], operator: "eq" }] } }] } };
  const moved = (config(edit(aev, filtered, "entity", "asset", [0])).series as { filter: PlatformFilterGroup }[])[0].filter;
  assert.equal(moved.filters.length, 1);
  assert.equal(moved.filters[0].id, group.filters[0].id);
  assert.deepEqual(moved.filters[0].values, ["asset"]);
  // A time range other than a custom one drops its dates.
  const custom = edit(aev, { ...donut, widget_config: { ...config(donut), time_range: "CUSTOM", start: "2026-01-01T00:00:00Z" } }, "widget_config.time_range", "LAST_DAY");
  assert.equal(config(custom).start, undefined);
});

test("OpenAEV's operators follow the property", () => {
  assert.deepEqual(operatorsFor(aev, { label: "d", type: "instant" }), ["gt", "gte", "lt", "lte", "empty", "not_empty"]);
  assert.deepEqual(operatorsFor(aev, { label: "n", type: "double" }), ["gte", "lte", "gt", "lt", "eq", "not_eq", "empty", "not_empty"]);
  assert.deepEqual(operatorsFor(aev, { label: "e", type: "string", values: ["a"] }), ["eq", "not_eq", "empty", "not_empty"]);
  assert.deepEqual(operatorsFor(aev, { label: "a", type: "string", multiple: true }), ["contains", "not_contains", "empty", "not_empty"]);
  assert.deepEqual(operatorsFor(aev, { label: "s", type: "string" }), ["eq", "not_eq", "contains", "not_contains", "starts_with", "not_starts_with"]);
  assert.deepEqual(operatorsFor(aev, { label: "o", type: "string", operators: ["empty", "not_empty"] }), ["empty", "not_empty"]);
});

test("an OpenCRQ metric keeps its organization, and its window only when the new one takes one", () => {
  const trend = { metric: "exposure_trend_ale", organization_id: "o1", window_days: 30 };
  assert.deepEqual(read(crq, trend, "window_days"), 30);
  assert.deepEqual(edit(crq, trend, "type", "scenario_ale"), { metric: "scenario_ale", organization_id: "o1", window_days: 30 });
  assert.deepEqual(edit(crq, trend, "type", "active_findings"), { metric: "active_findings", organization_id: "o1" });
  assert.equal(locate(crq, { metric: "active_findings" }, "window_days"), undefined);
  assert.deepEqual(newWidget(crq), { metric: "exposure_trend_ale" });
});

test("the form shows every widget of a known type, and nothing else", () => {
  assert.equal(formCarries(cti, { ...newWidget(cti, "radar"), parameters: { description: "x" } }), true);
  assert.equal(formCarries(cti, { type: "text", dataSelection: [{}] }), false);
  assert.equal(formCarries(cti, { type: "donut", dataSelection: {} }), false);
  assert.equal(formCarries(aev, newWidget(aev, "attack-path")), true);
  assert.equal(formCarries(aev, { widget_type: "donut", widget_config: { widget_configuration_type: "list" } }), false);
  assert.equal(formCarries(aev, newWidget(aev, "list")), true);
  assert.equal(formCarries(crq, { metric: "active_findings", organization_id: "o1" }), true);
  assert.equal(formCarries(crq, { metric: "risk_score" }), false);
  assert.equal(formCarries(crq, {}), false);
});

/** One new widget of each type (and each OpenCTI perspective): what the form writes before anything is chosen. */
function editorWidgets() {
  const opencti = cti.types.flatMap((type) =>
    ((type.attrs?.perspectives as string[]) ?? []).map((perspective) => newWidget(cti, type.value, { perspective })),
  );
  // A breakdown names its field before it is saved (OpenAEV requires one).
  const openaev = aev.types.map((type) => {
    const widget = newWidget(aev, type.value, { entity: "finding" });
    if (config(widget).field === "") config(widget).field = "finding_type";
    return withoutIds(widget);
  });
  return { opencti, openaev };
}

test("the new widget of every type is the one the server is shown to accept", () => {
  const fixture = new URL("../../../../backend/tests/fixtures/dashboards/editor_widgets.json", import.meta.url);
  const widgets = editorWidgets();
  assert.deepEqual([widgets.opencti.length, widgets.openaev.length], [43, 13]);
  assert.deepEqual(JSON.parse(readFileSync(fixture, "utf8")), widgets);
});
