/**
 * Unit tests for the wizard's selection labels - `yarn test:unit`.
 *
 * The canvas and the step pickers name every selected id through the flow node
 * catalog. An id a loaded catalog does not list is no longer available to the
 * user (deleted, unshared, or restored from an older draft): it is named as
 * such rather than shown as a raw id (its picker row is covered by
 * `flow-builder/unlisted-options.test.ts`) - creating the agent with it would
 * be refused.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { resolveOptionLabel } from "./option-labels.ts";

const catalog = [
  { value: "kb-1", label: "Runbooks" },
  { value: "kb-2", label: "" },
];

test("the catalog names every id it lists", () => {
  assert.equal(resolveOptionLabel(catalog, "kb-1", "Unavailable"), "Runbooks");
  assert.equal(resolveOptionLabel(catalog, "kb-1"), "Runbooks");
});

test("an id a loaded catalog does not list is named unavailable, never shown as a raw id", () => {
  assert.equal(resolveOptionLabel(catalog, "kb-gone", "Unavailable"), "Unavailable");
});

test("before the catalog has loaded nothing is claimed unavailable: the id is shown as is", () => {
  assert.equal(resolveOptionLabel(catalog, "kb-gone"), "kb-gone");
  assert.equal(resolveOptionLabel([], "kb-gone"), "kb-gone");
});

test("an empty catalog label falls back to the id", () => {
  assert.equal(resolveOptionLabel(catalog, "kb-2", "Unavailable"), "kb-2");
});
