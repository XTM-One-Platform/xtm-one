/**
 * Per-case output sockets of a Switch / Classify (CLM) node - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { withCaseSockets } from "./case-sockets.ts";
import type { FlowPort } from "../../types/index.ts";

const port = (id: string, types: FlowPort["types"] = ["branch"]): FlowPort => ({
  id,
  label: id,
  types,
  multiple: true,
});

// The Classify (CLM) node's outputs, as the catalog serves them.
const CLASSIFY = [port("case"), port("default"), port("error", ["error"])];

test("each label becomes a socket, before the node's other outputs", () => {
  const outputs = withCaseSockets(CLASSIFY, { investigate: "...", noise: "..." }, new Set());
  assert.deepEqual(
    outputs.map((p) => [p.id, p.label]),
    [
      ["case:investigate", "investigate"],
      ["case:noise", "noise"],
      ["default", "default"],
      ["error", "error"],
    ],
  );
  assert.deepEqual(outputs[0].types, ["branch"]);
});

test("the legacy case handle stays while an edge still uses it", () => {
  const outputs = withCaseSockets(CLASSIFY, { yes: "" }, new Set(["case"]));
  assert.deepEqual(
    outputs.map((p) => p.id),
    ["case", "case:yes", "default", "error"],
  );
});

test("no cases yet keeps the case handle as the drag affordance", () => {
  for (const cases of [undefined, null, {}, [], "yes", { "": "empty" }]) {
    assert.deepEqual(withCaseSockets(CLASSIFY, cases, new Set()), CLASSIFY);
  }
});

test("a node without a case output is left alone", () => {
  const outputs = [port("out", ["flow"]), port("error", ["error"])];
  assert.equal(withCaseSockets(outputs, { yes: "" }, new Set()), outputs);
});
