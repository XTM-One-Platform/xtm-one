/**
 * Unit tests for the inputs reader and the token manager - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { choicesOf, readInput, readInputs, tokenTable } from "./inputs.ts";

const DEFINITION = {
  inputs: {
    sev: { type: "input.dropdown", token: "sev", defaultValue: "high", options: { items: [{ label: "High", value: "high" }, "low"] } },
    team: { type: "input.multiselect", token: "team", dataSources: { primary: "teams" }, options: { valueField: "id", labelField: "name" } },
    go: { type: "input.button", options: { label: "Low", actions: [{ type: "setToken", token: "sev", value: "low" }, { type: "link", url: "javascript:x" }, { type: "evil" }] } },
    broken: { type: "input.dropdown" },
    odd: { type: "input.slider", token: "x" },
  },
  layout: { globalInputs: ["sev", "go", "ghost"], options: { submitButton: true, submitOnDashboardLoad: false } },
  defaults: { tokens: { default: { sev: { value: "medium" }, limit: { value: "10" }, "bad name": { value: "x" } } } },
  expressions: { tokens: { label: '"Severity " + $sev$' } },
  dataSources: { picked: { type: "derived", options: { steps: [{ op: "where", value: "$sev$" }] } } },
  visualizations: { v: { title: "$label$ for $env:user$ ($picked:result.value$)" } },
};

test("inputs are read typed; what cannot be drawn is left out", () => {
  const read = readInputs(DEFINITION);
  assert.deepEqual(read.inputs.map((input) => [input.id, input.kind]), [["sev", "dropdown"], ["team", "multiselect"], ["go", "button"]]);
  assert.deepEqual(read.inputs[0].items, [{ label: "High", value: "high" }, { label: "low", value: "low" }]);
  assert.deepEqual(read.inputs[2].actions, [{ type: "setToken", token: "sev", value: "low" }, { type: "link", url: "javascript:x", newTab: false }]);
  assert.deepEqual(read.globalInputs, ["sev", "go"]);
  // An input's default wins over the definition's.
  assert.deepEqual(read.defaults, { sev: "high", limit: "10" });
  assert.deepEqual(read.expressions, { label: '"Severity " + $sev$' });
  assert.deepEqual([read.submitButton, read.submitOnLoad], [true, false]);
});

test("a dropdown's choices: its items, then its source's rows or distribution", () => {
  const [, team] = readInputs(DEFINITION).inputs;
  const rows = { data: { columns: [{ key: "id" }, { key: "name" }], rows: [{ id: "t1", name: "Blue" }, { id: "t1", name: "dup" }, { id: "", name: "x" }] } };
  assert.deepEqual(choicesOf(team, rows), [{ label: "Blue", value: "t1" }]);
  const [sev] = readInputs(DEFINITION).inputs;
  assert.deepEqual(choicesOf(sev, { data: { items: [{ label: "critical", value: 3 }, { label: "high", value: 1 }] } }), [
    { label: "High", value: "high" },
    { label: "low", value: "low" },
    { label: "critical", value: "critical" },
  ]);
});

test("the token manager lists every token with its value, default, users and setters", () => {
  const rows = tokenTable(DEFINITION, { sev: "high", label: "Severity high", "env:user": "marcus" });
  const sev = rows.find((row) => row.name === "sev");
  assert.deepEqual(sev, {
    name: "sev",
    value: "high",
    defaultValue: "high",
    usedBy: ["dataSources.picked", "expressions.tokens.label"],
    setBy: ["inputs.sev", "inputs.go"],
  });
  assert.deepEqual(rows.find((row) => row.name === "env:user")?.setBy, ["environment"]);
  assert.deepEqual(rows.find((row) => row.name === "picked:result.value")?.setBy, ["dataSources.picked"]);
  assert.deepEqual(rows.find((row) => row.name === "label")?.usedBy, ["visualizations.v"]);
  assert.deepEqual(rows.find((row) => row.name === "limit")?.usedBy, []);
});

test("a dropdown reads a typed cell as its text", () => {
  const [, team] = readInputs(DEFINITION).inputs;
  const rows = {
    data: {
      columns: [{ key: "id", kind: "number" }, { key: "name", kind: "chip" }],
      rows: [{ id: 7, name: { label: "Blue", color: "info" } }, { id: 8, name: null }, { id: null, name: { label: "x" } }],
    },
  };
  assert.deepEqual(choicesOf(team, rows), [
    { label: "Blue", value: "7" },
    { label: "8", value: "8" },
  ]);
});

test("an entity input reads its platform, integration and types, the bad ones left out", () => {
  const input = readInput("country", {
    type: "input.entity",
    token: "entity",
    title: "Country",
    defaultValue: "c1",
    options: { source: "opencti", integration_id: "22222222-2222-4222-8222-222222222222", entityTypes: ["Country", "<b>", 3] },
  });
  assert.equal(input?.kind, "entity");
  assert.deepEqual([input?.entitySource, input?.integrationId, input?.entityTypes, input?.defaultValue], [
    "opencti",
    "22222222-2222-4222-8222-222222222222",
    ["Country"],
    "c1",
  ]);
  // Another kind carries none of them.
  assert.equal(readInput("q", { type: "input.text", token: "q", options: { source: "opencti" } })?.entitySource, undefined);
});
