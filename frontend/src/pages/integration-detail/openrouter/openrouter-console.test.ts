/**
 * Unit tests for the OpenRouter console helpers - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  catalogAuthors,
  expiryFromDateInput,
  formatPerMillion,
  exactTokens,
  formatTokens,
  formatUsd,
  exactRequests,
  exactUsd,
  formatRequests,
  limitShare,
  orderedSelection,
  rollUpActivity,
  selectionChanged,
} from "./openrouter-console.ts";
import { exactCurrency } from "../../../lib/number-format.ts";
import type { OpenRouterActivityRow, OpenRouterModel } from "@/types";

const row = (overrides: Partial<OpenRouterActivityRow>): OpenRouterActivityRow => ({
  date: "2026-09-30",
  model: "openai/gpt-4o",
  provider_name: "OpenAI",
  usage: 0,
  byok_usage_inference: 0,
  requests: 0,
  prompt_tokens: 0,
  completion_tokens: 0,
  reasoning_tokens: 0,
  cached_tokens: 0,
  ...overrides,
});

const model = (id: string, overrides: Partial<OpenRouterModel> = {}): OpenRouterModel => ({
  id,
  name: id,
  author: id.split("/")[0],
  created: null,
  description: null,
  context_length: null,
  max_completion_tokens: null,
  is_moderated: false,
  input_modalities: ["text"],
  output_modalities: ["text"],
  pricing: {
    prompt: 1,
    completion: 2,
    input_cache_read: null,
    input_cache_write: null,
    internal_reasoning: null,
    request: null,
    image: null,
    web_search: null,
  },
  variable_pricing: false,
  is_free: false,
  supports_tools: false,
  supports_reasoning: false,
  supports_structured_outputs: false,
  supported_parameters: [],
  knowledge_cutoff: null,
  expiration_date: null,
  ...overrides,
});

test("amounts keep the precision a small spend needs", () => {
  assert.deepEqual(
    [formatUsd(0), formatUsd(0.0042), formatUsd(12.5), formatUsd(1234.4), formatUsd(null), formatUsd(Number.NaN)],
    ["$0.00", "$0.0042", "$12.50", "$1.2K", "—", "—"],
  );
  // Compact from $1,000 on the platform rule (#10476), the exact amount on hover.
  assert.deepEqual([formatUsd(12345), formatUsd(921), formatUsd(-40522)], ["$12.3K", "$921", "-$40.5K"]);
  assert.deepEqual([exactUsd(12345), exactUsd(921), exactUsd(null)], ["$12,345.00", undefined, undefined]);
  // Zero follows the locale like every other figure (UX #10529 r4 B1): never the en "$0.00" in fr.
  assert.equal(formatUsd(0, "fr"), exactCurrency("fr", "USD", 0));
  assert.match(formatUsd(0, "fr"), /^0,00\s\$US$/u);
  assert.deepEqual([formatRequests(25_900), formatRequests(42), exactRequests(25_900), exactRequests(42)], ["25.9K", "42", "25,900", undefined]);
  assert.deepEqual(
    [formatPerMillion(3), formatPerMillion(0.002), formatPerMillion(0), formatPerMillion(null)],
    ["$3.00", "$0.0020", "$0", "—"],
  );
});

test("token counts are compact, 3 significant digits at most, exact on hover (#10476)", () => {
  assert.deepEqual(
    [formatTokens(128_000), formatTokens(200_000), formatTokens(1_000_000), formatTokens(1_048_576), formatTokens(41_573_210), formatTokens(121_640), formatTokens(512), formatTokens(0), formatTokens(null)],
    ["128K", "200K", "1M", "1M", "41.6M", "122K", "512", "—", "—"],
  );
  assert.equal(formatTokens(20_230_000, "fr").replace(/[\u00a0\u202f]/g, " "), "20,2 M");
  assert.equal(exactTokens(41_573_210), "41,573,210");
  assert.equal(exactTokens(512), undefined);
  assert.equal(exactTokens(null), undefined);
});

test("the share of a limit spent is bounded and absent without a limit", () => {
  assert.equal(limitShare(50, 12.5), 75);
  assert.equal(limitShare(50, 60), 0);
  assert.equal(limitShare(50, -5), 100);
  assert.equal(limitShare(null, 10), null);
  assert.equal(limitShare(0, 0), null);
});

test("activity rolls up per day and model, most spent first", () => {
  const rollup = rollUpActivity([
    row({ date: "2026-09-29", model: "openai/gpt-4o", provider_name: "OpenAI", usage: 1, requests: 2, prompt_tokens: 10, completion_tokens: 5 }),
    row({ date: "2026-09-29", model: "openai/gpt-4o", provider_name: "Azure", usage: 0.5, requests: 1, prompt_tokens: 4 }),
    row({ date: "2026-09-30", model: "anthropic/claude-sonnet-5", provider_name: "Anthropic", usage: 3, requests: 1, reasoning_tokens: 7 }),
  ]);
  assert.deepEqual(rollup.series, [
    { date: "2026-09-29", model: "openai/gpt-4o", cost: 1.5 },
    { date: "2026-09-30", model: "anthropic/claude-sonnet-5", cost: 3 },
  ]);
  assert.deepEqual(
    rollup.byModel.map((m) => [m.model, m.usage, m.requests, m.providers]),
    [
      ["anthropic/claude-sonnet-5", 3, 1, ["Anthropic"]],
      ["openai/gpt-4o", 1.5, 3, ["Azure", "OpenAI"]],
    ],
  );
  assert.deepEqual(rollup.totals, { usage: 4.5, requests: 4, tokens: 26, days: 2 });
  assert.deepEqual(rollUpActivity([]).totals, { usage: 0, requests: 0, tokens: 0, days: 0 });
});

const CATALOG = [
  model("openai/gpt-4o", { name: "OpenAI: GPT-4o", supports_tools: true, context_length: 128_000, created: "2024-05-13T00:00:00+00:00" }),
  model("meta-llama/llama-3.3-70b-instruct:free", { name: "Llama 3.3 70B (free)", is_free: true, pricing: { ...model("x/y").pricing, prompt: 0, completion: 0 } }),
  model("anthropic/claude-sonnet-5", { name: "Anthropic: Claude Sonnet 5", supports_tools: true, context_length: 200_000, created: "2026-01-01T00:00:00+00:00", pricing: { ...model("x/y").pricing, prompt: 3, completion: 15 } }),
  model("openrouter/auto", { name: "Auto Router", variable_pricing: true, pricing: { ...model("x/y").pricing, prompt: null, completion: null } }),
];

const ids = (models: OpenRouterModel[]) => models.map((m) => m.id);

test("the catalog authors are listed once, sorted, for the author filter", () => {
  assert.deepEqual(catalogAuthors(CATALOG), ["anthropic", "meta-llama", "openai", "openrouter"]);
});

test("a selection is saved with the offered models first, new ones in catalog order", () => {
  const current = new Set(["z/new", "openai/gpt-4o", "anthropic/claude-sonnet-5", "retired/old"]);
  assert.deepEqual(
    orderedSelection(["retired/old", "openai/gpt-4o", "gone/removed"], current, ids(CATALOG)),
    ["retired/old", "openai/gpt-4o", "anthropic/claude-sonnet-5", "z/new"],
  );
  assert.equal(selectionChanged(["a/b", "c/d"], new Set(["c/d", "a/b"])), false);
  assert.equal(selectionChanged(["a/b"], new Set(["a/b", "c/d"])), true);
  assert.equal(selectionChanged(["a/b", "c/d"], new Set(["a/b", "e/f"])), true);
});

test("an expiry date is the end of that day in UTC", () => {
  assert.equal(expiryFromDateInput("2026-12-31"), "2026-12-31T23:59:59Z");
  assert.equal(expiryFromDateInput(""), null);
  assert.equal(expiryFromDateInput("31/12/2026"), null);
});
