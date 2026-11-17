/**
 * Unit tests for the rows a picker adds for selections it does not list -
 * `yarn test:unit`.
 *
 * The flow builder's and the agent wizard's shared picker lists its options
 * from the flow node catalog. A selected id the catalog does not list (deleted,
 * unshared, restored from an older draft, stored by someone else) is offered as
 * a row of its own, first, so it can be removed - never dropped by itself.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { withUnlistedSelections } from "./unlisted-options.ts";

type Option = { value: string; label: string; description?: string | null };

const catalog: Option[] = [
  { value: "kb-1", label: "Runbooks" },
  { value: "kb-2", label: "" },
];

const unavailable = (value: string): Option => ({
  value,
  label: "Unavailable",
  description: "No longer available to you.",
});

test("a selection the catalog does not list is offered first in the picker, so it can be removed", () => {
  assert.deepEqual(
    withUnlistedSelections(catalog, ["kb-1", "kb-gone"], unavailable),
    [unavailable("kb-gone"), ...catalog],
  );
});

test("the picker keeps the catalog's options when every selection is listed", () => {
  assert.deepEqual(withUnlistedSelections(catalog, ["kb-1"], unavailable), catalog);
  assert.deepEqual(withUnlistedSelections(catalog, [], unavailable), catalog);
});

test("an unlisted value selected twice is offered once", () => {
  assert.deepEqual(withUnlistedSelections([], ["kb-gone", "kb-gone"], unavailable), [unavailable("kb-gone")]);
});
