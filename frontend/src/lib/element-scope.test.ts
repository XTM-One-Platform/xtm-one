/**
 * Unit tests for the Scope(s) enforcement rules - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ENFORCEABLE_ELEMENT_SCOPES,
  hasScopeSelection,
  isElementScopeEnforceable,
  isScopeEnforced,
  normalizeScopeEnforcement,
  withScopeEnforcement,
} from "./element-scope.ts";

const REPO = "github:repository";
const SLACK = "slack:slack_channel";
const ORG = "github:organization";
const API = { id: "acme/api", name: "api", group: "acme", metadata: {} };
const SUPPORT = { id: "C0SUPPORT", name: "support", group: "", metadata: {} };
const ACME = { id: "acme", name: "acme", group: "", metadata: {} };

test("only the four types a provider holds to a selection can be enforced", () => {
  assert.deepEqual([...ENFORCEABLE_ELEMENT_SCOPES].sort(), ["github:repository", "hubspot:crm_object_type", "microsoft:teams_channel", "slack:slack_channel"]);
  assert.equal(isElementScopeEnforceable(REPO), true);
  assert.equal(isElementScopeEnforceable(ORG), false);
});

test("a selection is guidance unless its type is enforceable, selected and switched on", () => {
  const bindings = { [REPO]: [API], [SLACK]: [], [ORG]: [ACME] };
  assert.equal(hasScopeSelection(bindings, REPO), true);
  assert.equal(hasScopeSelection(bindings, SLACK), false);
  assert.equal(isScopeEnforced(REPO, bindings, null), false);
  assert.equal(isScopeEnforced(REPO, bindings, [REPO]), true);
  assert.equal(isScopeEnforced(SLACK, bindings, [SLACK]), false);
  assert.equal(isScopeEnforced(ORG, bindings, [ORG]), false);
  assert.equal(isScopeEnforced(REPO, undefined, [REPO]), false);
});

test("the saved choice keeps the flags of selected, enforceable types, sorted", () => {
  const bindings = { [REPO]: [API], [SLACK]: [SUPPORT] };
  assert.deepEqual(normalizeScopeEnforcement([SLACK, REPO, REPO, ORG], bindings), [REPO, SLACK]);
  assert.equal(normalizeScopeEnforcement([ORG], bindings), null);
  assert.equal(normalizeScopeEnforcement([REPO], { [REPO]: [] }), null);
  assert.equal(normalizeScopeEnforcement(null, bindings), null);
});

test("switching a type on or off lists it at most once", () => {
  assert.deepEqual(withScopeEnforcement(null, REPO, true), [REPO]);
  assert.deepEqual(withScopeEnforcement([REPO], REPO, true), [REPO]);
  assert.deepEqual(withScopeEnforcement([REPO, SLACK], REPO, false), [SLACK]);
  assert.deepEqual(withScopeEnforcement(undefined, SLACK, false), []);
});
