/**
 * Unit tests for what every platform source editor shares - `yarn test:unit`:
 * a filter group read as it is stored, and two stored configurations compared.
 * The editing itself is `platform-form.test.ts`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { asGroup, canonicalNative, emptyGroup, filterKey, isValueless, sameNative } from "./platform-native.ts";

test("a stored filter group is read as it is, a missing one as the empty group", () => {
  const stored = { mode: "or", filters: [{ key: ["objectLabel"], values: ["l1"], operator: "eq", mode: "or", id: "f1" }], extra: 1 };
  assert.equal(asGroup(stored, true), stored);
  assert.deepEqual(asGroup(undefined, true), { mode: "and", filters: [], filterGroups: [] });
  assert.deepEqual(asGroup({ mode: "or" }, false), { mode: "and", filters: [] });
  assert.deepEqual(emptyGroup(false), { mode: "and", filters: [] });
  assert.equal(filterKey({ key: ["entity_type", "parent_types"], values: [], operator: "eq", mode: "or" }), "entity_type");
  assert.equal(filterKey({ key: "base_entity", values: [], operator: "eq", mode: "and" }), "base_entity");
  assert.equal(isValueless("not_empty"), true);
  assert.equal(isValueless("eq"), false);
});

test("two configurations are the same widget whatever defaults one leaves out", () => {
  assert.equal(sameNative({ a: 1, b: false, c: [], d: { mode: "and", filters: [] } }, { a: 1 }), true);
  assert.equal(sameNative({ isTo: false }, { isTo: true }), false);
  assert.deepEqual(canonicalNative({ b: 2, a: { z: "", y: [1] } }), { a: { y: [1] }, b: 2 });
});
