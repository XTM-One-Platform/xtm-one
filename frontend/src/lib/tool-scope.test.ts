/**
 * Unit tests for the integration tool allow-list keys.
 *
 * Run with `yarn test:unit` (Node's built-in runner, types stripped natively).
 * The property under test: scoping one OpenAPI integration never changes what
 * another OpenAPI integration on the same agent may call.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { effectiveAllowedTools, isToolScopeKeyActive, toolScopeKey } from "./tool-scope.ts";

const petstore = { id: "a1", provider: "openapi" };
const erp = { id: "b2", provider: "openapi" };
const github = { id: "c3", provider: "github" };

test("OpenAPI integrations are scoped per integration, other providers per provider", () => {
  assert.equal(toolScopeKey(petstore), "openapi@a1");
  assert.equal(toolScopeKey(erp), "openapi@b2");
  assert.equal(toolScopeKey(github), "github");
});

test("a per-integration list restricts only its own integration", () => {
  const allowed = { [toolScopeKey(petstore)]: ["pets_list"] };
  assert.deepEqual(effectiveAllowedTools(allowed, petstore), ["pets_list"]);
  assert.equal(effectiveAllowedTools(allowed, erp), null);
});

test("provider and per-integration lists intersect", () => {
  const allowed = { openapi: ["x", "y"], [toolScopeKey(petstore)]: ["y", "z"] };
  assert.deepEqual(effectiveAllowedTools(allowed, petstore), ["y"]);
  assert.deepEqual(effectiveAllowedTools(allowed, erp), ["x", "y"]);
  assert.equal(effectiveAllowedTools(null, github), null);
  assert.equal(effectiveAllowedTools({ github: null }, github), null);
});

test("an entry applies while its provider, and its integration for a per-integration key, is bound", () => {
  const providers = new Set(["openapi", "github"]);
  const bound = new Set(["a1", "c3"]);
  assert.equal(isToolScopeKeyActive("github", providers, bound), true);
  assert.equal(isToolScopeKeyActive("slack", providers, bound), false);
  assert.equal(isToolScopeKeyActive("openapi@a1", providers, bound), true);
  assert.equal(isToolScopeKeyActive("openapi@b2", providers, bound), false);
  assert.equal(isToolScopeKeyActive("openapi@a1", new Set(["github"]), bound), false);
});
