/**
 * Unit tests for the delegation tree of a run - `yarn test:unit`.
 *
 * The descendants came back flat and in creation order, so an agent that a
 * delegated agent delegated to read as the root's own delegation.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { orderRunTree, type RunTreeRun } from "./run-tree.ts";

const ROOT = "csirt";

function run(id: string, parent: string | null, minute: number): RunTreeRun {
  return {
    id,
    parent_run_id: parent,
    created_at: `2026-09-29T06:${String(minute).padStart(2, "0")}:00Z`,
  };
}

function shape(entries: { run: RunTreeRun; depth: number }[]): string[] {
  return entries.map(({ run: r, depth }) => `${depth}:${r.id}`);
}

test("a delegated agent's delegations sit under it, not under the root", () => {
  const runs = [
    run("triage", ROOT, 32),
    run("response", "triage", 33),
    run("intel", "triage", 34),
  ];

  assert.deepEqual(shape(orderRunTree(ROOT, runs)), ["1:triage", "2:response", "2:intel"]);
});

test("each run follows the run that delegated it, depth first", () => {
  // Created interleaved: the root's second delegation lands between the
  // first one's children.
  const runs = [
    run("triage", ROOT, 32),
    run("response", "triage", 33),
    run("posture", ROOT, 34),
    run("intel", "response", 35),
    run("hunt", "triage", 36),
  ];

  assert.deepEqual(shape(orderRunTree(ROOT, runs)), [
    "1:triage",
    "2:response",
    "3:intel",
    "2:hunt",
    "1:posture",
  ]);
});

test("siblings read in creation order whatever order the API sent them in", () => {
  const runs = [run("b", ROOT, 40), run("a", ROOT, 35), run("c", ROOT, 45)];

  assert.deepEqual(shape(orderRunTree(ROOT, runs)), ["1:a", "1:b", "1:c"]);
});

test("a run whose parent is not in the list hangs under the root", () => {
  const runs = [run("orphan", "gone", 30), run("triage", ROOT, 32)];

  assert.deepEqual(shape(orderRunTree(ROOT, runs)), ["1:orphan", "1:triage"]);
});

test("a cycle among the runs loses none of them", () => {
  const runs = [run("a", "b", 30), run("b", "a", 31), run("c", ROOT, 32)];

  const ordered = orderRunTree(ROOT, runs);

  assert.deepEqual(shape(ordered), ["1:c", "1:a", "1:b"]);
});

test("a run naming itself as its parent hangs under the root", () => {
  assert.deepEqual(shape(orderRunTree(ROOT, [run("self", "self", 30)])), ["1:self"]);
});
