/**
 * Unit tests for the entity list scope helpers - `yarn test:unit`.
 *
 * The library pages and pickers read the library set; the detail pages read
 * `scope=all`, every row for an admin. A picker keeps its library options plus
 * the rows the entity is already bound to, so an admin editing a colleague's
 * agent sees and keeps every binding.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  effectiveListScope,
  entityListKey,
  entityListPath,
  withBoundEntities,
} from "./entity-list-scope.ts";

test("only an admin asking for it sends scope=all", () => {
  assert.equal(effectiveListScope("all", true), "all");
  assert.equal(effectiveListScope("all", false), undefined);
  assert.equal(effectiveListScope("visible", true), undefined);
  assert.equal(effectiveListScope(undefined, true), undefined);
});

test("the library key and path are unchanged, the admin scope is appended", () => {
  assert.deepEqual(entityListKey(["agents"], undefined), ["agents"]);
  assert.deepEqual(entityListKey(["agents"], "all"), ["agents", { scope: "all" }]);
  assert.equal(entityListPath("/agents", undefined), "/agents");
  assert.equal(entityListPath("/agents", "all"), "/agents?scope=all");
  assert.equal(
    entityListPath("/template-containers?kind=presentation", "all"),
    "/template-containers?kind=presentation&scope=all",
  );
});

const row = (id: string) => ({ id, name: id });

test("a picker keeps its options and adds the bound rows only the admin scope holds", () => {
  const visible = [row("mine"), row("company")];
  const all = [row("mine"), row("company"), row("colleague-bound"), row("colleague-other")];

  const options = withBoundEntities(visible, all, ["company", "colleague-bound"]);
  assert.deepEqual(options?.map((item) => item.id), ["mine", "company", "colleague-bound"]);
});

test("nothing to add returns the library set itself", () => {
  const visible = [row("mine")];
  assert.equal(withBoundEntities(visible, [row("mine"), row("other")], ["mine"]), visible);
  assert.equal(withBoundEntities(visible, [row("other")], null), visible);
  // A bound row no list holds (deleted meanwhile) adds nothing.
  assert.equal(withBoundEntities(visible, [row("mine")], ["gone"]), visible);
});

test("the options wait for the library set", () => {
  assert.equal(withBoundEntities(undefined, [row("other")], ["other"]), undefined);
});

test("a bound row the library set lacks waits for the admin scope, so no clamp drops it", () => {
  const visible = [row("mine")];
  assert.equal(withBoundEntities(visible, undefined, ["mine", "colleague-bound"]), undefined);
  const options = withBoundEntities(visible, [row("mine"), row("colleague-bound")], ["mine", "colleague-bound"]);
  assert.deepEqual(options?.map((item) => item.id), ["mine", "colleague-bound"]);
});

test("bindings the library set already holds never wait", () => {
  const visible = [row("mine"), row("company")];
  assert.equal(withBoundEntities(visible, undefined, ["mine", "company"]), visible);
  assert.equal(withBoundEntities(visible, undefined, ["", "mine"]), visible);
});
