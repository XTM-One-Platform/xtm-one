import assert from "node:assert/strict";
import { test } from "node:test";

import {
  declaredOutputShapes,
  mergeShapes,
  nodeOutputShapes,
  observedOutputShapes,
  shapeOfSchema,
  shapeOfValue,
  shapePaths,
} from "./output-shapes.ts";
import { variableSuggestions } from "./upstream.ts";

// Only the fields the shape code reads; the canvas types carry many more.
type TestNode = Parameters<typeof variableSuggestions>[1][number];
type TestEdge = Parameters<typeof variableSuggestions>[2][number];

function node(id: string, kind: string, config: Record<string, unknown> = {}): TestNode {
  return { id, position: { x: 0, y: 0 }, data: { kind, config } } as TestNode;
}

function edge(source: string, target: string, targetHandle = "in"): TestEdge {
  return { id: `${source}-${target}`, source, target, targetHandle } as TestEdge;
}

test("a value's shape merges every list item, as n8n's schema view does", () => {
  const shape = shapeOfValue({
    hits: [{ title: "a" }, { title: "b", score: 2 }],
    note: null,
  });
  assert.deepEqual(shapePaths(shape).map((p) => [p.path, p.shape.type]), [
    ["hits", "list"],
    ["hits.0.title", "text"],
    ["hits.0.score", "number"],
    ["note", "null"],
  ]);
  assert.equal(shape.fields?.hits.items?.fields?.title.sample, "a");
});

test("two items that disagree on a key's type widen it to any", () => {
  const shape = shapeOfValue([{ id: 1 }, { id: "x" }]);
  assert.equal(shape.items?.fields?.id.type, "any");
});

test("a key the reference syntax cannot address is not offered", () => {
  const paths = shapePaths(shapeOfValue({ "a.b": 1, "with space": 2, ok: 3 }));
  assert.deepEqual(
    paths.map((p) => p.path),
    ["ok"],
  );
});

test("a JSON Schema becomes a declared shape, nullable and union types included", () => {
  const shape = shapeOfSchema({
    type: "object",
    properties: {
      severity: { type: "string", description: "How bad it is" },
      score: { type: ["integer", "null"] },
      tags: { type: "array", items: { type: "string" } },
      either: { anyOf: [{ type: "number" }, { type: "null" }] },
    },
  });
  assert.deepEqual(shapePaths(shape).map((p) => [p.path, p.shape.type]), [
    ["severity", "text"],
    ["score", "number"],
    ["tags", "list"],
    ["either", "number"],
  ]);
  assert.equal(shape.fields?.severity.description, "How bad it is");
  assert.equal(shape.fields?.severity.declared, true);
});

test("the declared structure wins a conflict and the last run adds its samples and extra keys", () => {
  const merged = mergeShapes(
    shapeOfSchema({ type: "object", properties: { count: { type: "number" }, missing: { type: "string" } } }),
    shapeOfValue({ count: "3", extra: true }),
    "first",
  );
  assert.equal(merged.fields?.count.type, "number", "declared type kept");
  assert.equal(merged.fields?.count.declared, true);
  assert.equal(merged.fields?.count.observed, true);
  assert.deepEqual(merged.fields?.missing, { type: "text", declared: true });
  assert.deepEqual(merged.fields?.extra, { type: "boolean", sample: true, observed: true });
});

test("a declared `any` takes the structure the last run produced", () => {
  // A `json` trigger parameter: the server declares it with no type.
  const shapes = nodeOutputShapes(
    declaredOutputShapes({ payload: { type: "object", properties: { alert: {} } } }),
    observedOutputShapes({ payload: { alert: { host: { ip: "10.0.0.4" } } } }),
  );
  assert.deepEqual(
    shapePaths(shapes.payload).map((p) => [p.path, p.shape.type, p.shape.declared ?? false]),
    [
      ["alert", "object", true],
      ["alert.host", "object", false],
      ["alert.host.ip", "text", false],
    ],
  );
});

test("the server's declared schemas become shapes, output by output", () => {
  const shapes = declaredOutputShapes({
    fields: { type: "object", properties: { verdict: { type: "boolean", description: "Go or no go" } } },
  });
  assert.deepEqual(Object.keys(shapes), ["fields"]);
  assert.deepEqual(shapes.fields.fields?.verdict, {
    type: "boolean",
    declared: true,
    description: "Go or no go",
  });
  assert.deepEqual(declaredOutputShapes(undefined), {});
});

test("walker bookkeeping keys never show as outputs", () => {
  assert.deepEqual(Object.keys(observedOutputShapes({ output: "x", _tainted: true })), ["output"]);
});

const KINDS = new Map([
  [
    "trigger.manual",
    { kind: "trigger.manual", label: "Manual", output_variables: [{ name: "payload", label: "Payload", description: "" }] },
  ],
  [
    "agent",
    {
      kind: "agent",
      label: "Agent",
      output_variables: [
        { name: "output", label: "Output", description: "", type: "text" },
        { name: "fields", label: "Fields", description: "", type: "object" },
      ],
    },
  ],
  ["condition.if", { kind: "condition.if", label: "If", output_variables: [] }],
]) as unknown as Parameters<typeof variableSuggestions>[3];

test("the autocomplete offers each path below an output, typed, never a path into text", () => {
  const nodes = [node("t1", "trigger.manual"), node("a1", "agent"), node("c1", "condition.if")];
  const edges = [edge("t1", "a1"), edge("a1", "c1")];
  const shapes = new Map([
    [
      "a1",
      nodeOutputShapes(
        { fields: shapeOfSchema({ type: "object", properties: { severity: { type: "string" } } }) },
        // The last run's text answer reads as JSON: still no path into `output`.
        observedOutputShapes({ output: '{"severity": "low"}', fields: { severity: "low" } }),
      ),
    ],
  ]);
  const suggestions = variableSuggestions("c1", nodes, edges, KINDS, shapes);
  assert.deepEqual(
    suggestions.map((s) => [s.ref, s.type ?? null]),
    [
      ["t1.payload", null],
      ["a1.output", "text"],
      ["a1.fields", "object"],
      ["a1.fields.severity", "text"],
    ],
  );
  const severity = suggestions.find((s) => s.ref === "a1.fields.severity");
  assert.deepEqual(
    [severity?.declared, severity?.observed, severity?.sample],
    [true, true, "low"],
  );
});

test("without shapes the autocomplete keeps the catalog's outputs only", () => {
  const nodes = [node("t1", "trigger.manual"), node("a1", "agent"), node("c1", "condition.if")];
  const edges = [edge("t1", "a1"), edge("a1", "c1")];
  assert.deepEqual(
    variableSuggestions("c1", nodes, edges, KINDS).map((s) => s.ref),
    ["t1.payload", "a1.output", "a1.fields"],
  );
});
