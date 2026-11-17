/**
 * Unit tests for the [DEPRECATED] built-in id reader - `yarn test:unit`.
 *
 * Twelve former ids are aliases of five groups; an agent, an
 * assignment or the General Assistant may still store one. A picker shows the
 * group's card on for such a row, a toggle writes the group id and drops the
 * former ids, and a read view shows the group once.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { aliasIndex, canonicalSuiteId, canonicalSuiteIds, carriesSuite, withoutSuite } from "./builtin-tool-aliases.ts";

const index = aliasIndex([
  { id: "web", aliases: ["web_fetch", "browser"] },
  { id: "coordination", aliases: ["work_items", "shared_state", "workset"] },
  { id: "code_interpreter", aliases: [] },
  { id: "agent_builder" },
]);

test("a former id reads as its group, any other id as itself", () => {
  assert.equal(canonicalSuiteId("browser", index), "web");
  assert.equal(canonicalSuiteId("web", index), "web");
  assert.equal(canonicalSuiteId("code_interpreter", index), "code_interpreter");
  assert.equal(canonicalSuiteId("not_in_this_build", index), "not_in_this_build");
});

test("two former ids of one group collapse into the group, once, in order", () => {
  assert.deepEqual(canonicalSuiteIds(["code_interpreter", "web_fetch", "browser", "workset"], index), [
    "code_interpreter",
    "web",
    "coordination",
  ]);
});

test("an entity carries a group through any of its former ids", () => {
  assert.equal(carriesSuite(["browser"], "web", index), true);
  assert.equal(carriesSuite(["web"], "web", index), true);
  assert.equal(carriesSuite(["work_items"], "web", index), false);
  assert.equal(carriesSuite(null, "web", index), false);
  assert.equal(carriesSuite(undefined, "coordination", index), false);
});

test("removing a group drops its former ids and nothing else", () => {
  assert.deepEqual(withoutSuite(["web_fetch", "code_interpreter", "browser", "web"], "web", index), [
    "code_interpreter",
  ]);
});
