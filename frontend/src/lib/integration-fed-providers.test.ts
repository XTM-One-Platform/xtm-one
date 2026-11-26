/**
 * Unit tests for the integrations that feed a platform LLM provider - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { addableByHand, feedingPagePath, integrationsToFeed } from "./integration-fed-providers.ts";
import type { Integration } from "@/types";

const integration = (overrides: Partial<Integration>): Integration =>
  ({ id: "i-1", name: "OpenRouter", provider: "openrouter", is_company_managed: true, has_api_key: true, ...overrides }) as Integration;

test("a company-managed OpenRouter integration feeding nothing is offered", () => {
  const offered = integrationsToFeed("openrouter", [integration({})], []);
  assert.deepEqual(offered.map((i) => i.id), ["i-1"]);
});

test("an integration already feeding a config is not offered again", () => {
  const offered = integrationsToFeed(
    "openrouter",
    [integration({ id: "i-1" }), integration({ id: "i-2" })],
    [{ source_integration_id: "i-1" }, { source_integration_id: null }],
  );
  assert.deepEqual(offered.map((i) => i.id), ["i-2"]);
});

test("a personal integration is not offered: the server refuses it", () => {
  assert.deepEqual(integrationsToFeed("openrouter", [integration({ is_company_managed: false })], []), []);
});

test("an integration without a stored API key is not offered: the server refuses it", () => {
  assert.deepEqual(integrationsToFeed("openrouter", [integration({ has_api_key: false })], []), []);
});

test("an integration of another provider is not offered", () => {
  assert.deepEqual(integrationsToFeed("openrouter", [integration({ provider: "anthropic" })], []), []);
});

test("an LLM provider no integration feeds offers none", () => {
  assert.deepEqual(integrationsToFeed("anthropic", [integration({ provider: "anthropic" })], []), []);
});

test("the feeding page is the integration's Models tab", () => {
  assert.equal(feedingPagePath("i-1", "openrouter"), "/integrations/i-1/models");
  assert.equal(feedingPagePath("i-1", "anthropic"), null);
});

test("a company-managed LM Studio integration with a server URL is offered, routed or not, its token optional", () => {
  const lmstudio = (overrides: Partial<Integration>) =>
    integration({
      provider: "lmstudio", has_api_key: false, relay_group_id: "g-1",
      extra_config: { base_url: "http://gpu-host:1234" }, ...overrides,
    });
  assert.deepEqual(integrationsToFeed("lmstudio", [lmstudio({})], []).map((i) => i.id), ["i-1"]);
  assert.deepEqual(integrationsToFeed("lmstudio", [lmstudio({ relay_group_id: null })], []).map((i) => i.id), ["i-1"]);
  assert.deepEqual(integrationsToFeed("lmstudio", [lmstudio({ extra_config: null })], []), []);
  assert.deepEqual(integrationsToFeed("lmstudio", [lmstudio({ extra_config: { base_url: " " } })], []), []);
  assert.deepEqual(integrationsToFeed("lmstudio", [lmstudio({ is_company_managed: false })], []), []);
  assert.equal(feedingPagePath("i-1", "lmstudio"), "/integrations/i-1/models");
});

test("only an integration creates an LM Studio provider", () => {
  assert.equal(addableByHand("lmstudio"), false);
  assert.equal(addableByHand("openrouter"), true);
  assert.equal(addableByHand("anthropic"), true);
});
