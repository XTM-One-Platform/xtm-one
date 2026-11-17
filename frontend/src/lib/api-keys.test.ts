/**
 * Unit tests for the personal API key dialog - `yarn test:unit`.
 *
 * The server refuses a key past the administrator's maximum lifetime and
 * rewrites nothing a request does not send, so the dialog must never offer an
 * expiry it would refuse and must send only what the person changed: a save
 * that re-sent every scope would turn a key restricted to the surfaces of the
 * day into one that silently follows every surface added later.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { ApiKeyPolicy, UserApiKey } from "../types/index.ts";
import {
  DAY_MS,
  EXPIRY_CLOCK_MARGIN_MS,
  allowsNoExpiry,
  apiKeyFormFromKey,
  apiKeyFormProblem,
  apiKeyState,
  apiKeyUsableOn,
  buildCreateApiKeyRequest,
  buildUpdateApiKeyRequest,
  daysUntil,
  defaultExpiryDays,
  expiryFromDate,
  expiryFromDays,
  expiryPresets,
  newApiKeyForm,
  parseLimit,
} from "./api-keys.ts";

const NOW = Date.UTC(2026, 9, 2, 12, 0, 0);

function policy(overrides: Partial<ApiKeyPolicy> = {}): ApiKeyPolicy {
  return {
    surfaces: ["rest", "llm_proxy", "mcp"],
    max_lifetime_days: 0,
    quota_periods: ["daily", "monthly"],
    max_rate_limit_per_minute: 100_000,
    max_quota_limit: 1_000_000_000,
    max_agents: 200,
    ...overrides,
  };
}

function key(overrides: Partial<UserApiKey> = {}): UserApiKey {
  return {
    id: "k1",
    name: "CI",
    key_prefix: "fcp-" + "0".repeat(8),
    is_active: true,
    is_from_env: false,
    last_used_at: null,
    request_count: 0,
    created_at: new Date(NOW - DAY_MS).toISOString(),
    surfaces: ["rest", "llm_proxy", "mcp"],
    all_surfaces: true,
    read_only: false,
    agent_ids: null,
    agents: [],
    expires_at: null,
    expired: false,
    revoked_at: null,
    rate_limit_per_minute: null,
    quota_limit: null,
    quota_period: null,
    quota_used: null,
    full_scope: true,
    beyond_max_lifetime: false,
    ...overrides,
  };
}

test("a key's state reads revocation first, then expiry", () => {
  assert.equal(apiKeyState(key({ is_active: false, expired: true }), NOW), "revoked");
  assert.equal(apiKeyState(key({ expired: true }), NOW), "expired");
  assert.equal(apiKeyState(key({ expires_at: new Date(NOW - 1000).toISOString() }), NOW), "expired");
  assert.equal(apiKeyState(key({ expires_at: new Date(NOW + 3 * DAY_MS).toISOString() }), NOW), "expiring");
  assert.equal(apiKeyState(key({ expires_at: new Date(NOW + 30 * DAY_MS).toISOString() }), NOW), "active");
  assert.equal(apiKeyState(key(), NOW), "active");
  assert.equal(daysUntil(new Date(NOW + 2.5 * DAY_MS).toISOString(), NOW), 3);
  assert.equal(daysUntil(new Date(NOW - DAY_MS).toISOString(), NOW), 0);
});

test("a key is usable on a surface it holds while it is neither revoked nor expired", () => {
  assert.equal(apiKeyUsableOn(key(), "mcp", NOW), true);
  assert.equal(apiKeyUsableOn(key({ all_surfaces: false, surfaces: ["rest"] }), "mcp", NOW), false);
  assert.equal(apiKeyUsableOn(key({ all_surfaces: false, surfaces: ["mcp"], read_only: true }), "mcp", NOW), true);
  assert.equal(apiKeyUsableOn(key({ is_active: false }), "mcp", NOW), false);
  assert.equal(apiKeyUsableOn(key({ expires_at: new Date(NOW - 1000).toISOString() }), "mcp", NOW), false);
});

test("the expiry presets never go past the maximum lifetime, which is offered itself", () => {
  assert.deepEqual(expiryPresets(0), [7, 30, 90, 365]);
  assert.deepEqual(expiryPresets(90), [7, 30, 90]);
  assert.deepEqual(expiryPresets(45), [7, 30, 45]);
  assert.deepEqual(expiryPresets(3), [3]);
  assert.equal(allowsNoExpiry(0), true);
  assert.equal(allowsNoExpiry(30), false);
  assert.equal(defaultExpiryDays(0), 90);
  assert.equal(defaultExpiryDays(30), 30);
});

test("an expiry at the maximum lifetime stays inside it despite the browser clock", () => {
  const atMax = Date.parse(expiryFromDays(30, NOW, 30));
  assert.equal(atMax, NOW + 30 * DAY_MS - EXPIRY_CLOCK_MARGIN_MS);
  assert.equal(Date.parse(expiryFromDays(7, NOW, 0)), NOW + 7 * DAY_MS);
});

test("a custom date is the end of that day, refused when past, held to the maximum", () => {
  assert.equal(expiryFromDate("2026-10-01", NOW, 0), null);
  assert.equal(expiryFromDate("not a date", NOW, 0), null);
  const nextWeek = expiryFromDate("2026-10-09", NOW, 0);
  assert.ok(nextWeek);
  const parsed = new Date(nextWeek);
  assert.equal(parsed.getHours(), 23);
  assert.equal(parsed.getMinutes(), 59);
  const farAway = expiryFromDate("2027-10-09", NOW, 30);
  assert.equal(Date.parse(farAway ?? ""), NOW + 30 * DAY_MS - EXPIRY_CLOCK_MARGIN_MS);
});

test("a typed limit is a whole number within its bound, empty meaning none", () => {
  assert.equal(parseLimit("", 10), null);
  assert.equal(parseLimit("  ", 10), null);
  assert.equal(parseLimit("5", 10), 5);
  assert.equal(parseLimit("0", 10), undefined);
  assert.equal(parseLimit("11", 10), undefined);
  assert.equal(parseLimit("2.5", 10), undefined);
  assert.equal(parseLimit("-1", 10), undefined);
});

test("a new key is every surface, read-write, every agent, for the default lifetime", () => {
  const form = newApiKeyForm(policy({ max_lifetime_days: 30 }));
  form.name = "  Build server ";
  assert.equal(apiKeyFormProblem(form, policy({ max_lifetime_days: 30 }), NOW), null);
  const body = buildCreateApiKeyRequest(form, policy({ max_lifetime_days: 30 }), NOW);
  assert.equal(body.name, "Build server");
  assert.equal(body.surfaces, null);
  assert.equal(body.agent_ids, null);
  assert.equal(body.read_only, false);
  assert.equal(Date.parse(body.expires_at ?? ""), NOW + 30 * DAY_MS - EXPIRY_CLOCK_MARGIN_MS);
  assert.equal(body.quota_limit, null);
  assert.equal(body.quota_period, null);
});

test("a scoped key sends its surfaces, agents and limits", () => {
  const p = policy();
  const form = { ...newApiKeyForm(p), name: "MCP reader" };
  form.surfaces = ["mcp"];
  form.readOnly = true;
  form.restrictAgents = true;
  form.agentIds = ["a1", "a2"];
  form.expiry = "never";
  form.rateLimit = "60";
  form.quotaLimit = "1000";
  form.quotaPeriod = "daily";
  const body = buildCreateApiKeyRequest(form, p, NOW);
  assert.deepEqual(body.surfaces, ["mcp"]);
  assert.equal(body.read_only, true);
  assert.deepEqual(body.agent_ids, ["a1", "a2"]);
  assert.equal(body.expires_at, null);
  assert.equal(body.rate_limit_per_minute, 60);
  assert.equal(body.quota_limit, 1000);
  assert.equal(body.quota_period, "daily");
});

test("the form names the first thing the server would refuse", () => {
  const p = policy({ max_agents: 1 });
  const base = { ...newApiKeyForm(p), name: "k" };
  assert.equal(apiKeyFormProblem({ ...base, name: " " }, p, NOW), "name");
  assert.equal(apiKeyFormProblem({ ...base, surfaces: [] }, p, NOW), "surfaces");
  assert.equal(apiKeyFormProblem({ ...base, restrictAgents: true }, p, NOW), "agents");
  assert.equal(apiKeyFormProblem({ ...base, restrictAgents: true, agentIds: ["a", "b"] }, p, NOW), "too_many_agents");
  assert.equal(apiKeyFormProblem({ ...base, expiry: "custom", expiryDate: "2020-01-01" }, p, NOW), "expiry_date");
  assert.equal(apiKeyFormProblem({ ...base, rateLimit: "0" }, p, NOW), "rate_limit");
  assert.equal(apiKeyFormProblem({ ...base, quotaLimit: "x" }, p, NOW), "quota_limit");
});

test("a surface the deployment does not serve is neither offered nor counted", () => {
  const xtm = policy({ surfaces: ["rest", "mcp"] });
  const form = { ...newApiKeyForm(xtm), name: "k" };
  assert.deepEqual(form.surfaces, ["rest", "mcp"]);
  assert.equal(buildCreateApiKeyRequest(form, xtm, NOW).surfaces, null);
  assert.equal(apiKeyFormProblem({ ...form, surfaces: ["llm_proxy"] }, xtm, NOW), "surfaces");
});

test("saving an untouched key sends nothing", () => {
  const p = policy();
  const stored = key({
    all_surfaces: false,
    surfaces: ["rest"],
    read_only: true,
    agent_ids: ["a1"],
    expires_at: new Date(NOW + 10 * DAY_MS).toISOString(),
    rate_limit_per_minute: 30,
    quota_limit: 500,
    quota_period: "daily",
  });
  const initial = apiKeyFormFromKey(stored, p);
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial }, p, NOW), {});
});

test("a save sends only the fields the person changed, nulls clearing them", () => {
  const p = policy();
  const stored = key({
    all_surfaces: false,
    surfaces: ["rest"],
    agent_ids: ["a1"],
    rate_limit_per_minute: 30,
    quota_limit: 500,
    quota_period: "daily",
  });
  const initial = apiKeyFormFromKey(stored, p);
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, surfaces: ["rest", "llm_proxy", "mcp"] }, p, NOW), {
    surfaces: null,
  });
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, restrictAgents: false }, p, NOW), {
    agent_ids: null,
  });
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, agentIds: ["a2", "a1"] }, p, NOW), {
    agent_ids: ["a2", "a1"],
  });
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, rateLimit: "" }, p, NOW), {
    rate_limit_per_minute: null,
  });
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, quotaPeriod: "monthly" }, p, NOW), {
    quota_limit: 500,
    quota_period: "monthly",
  });
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, quotaLimit: "" }, p, NOW), {
    quota_limit: null,
    quota_period: null,
  });
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, expiry: "never" }, p, NOW), {
    expires_at: null,
  });
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, name: " Renamed " }, p, NOW), {
    name: "Renamed",
  });
});

test("an every-surface key keeps following every surface until one is switched off", () => {
  const p = policy({ surfaces: ["rest", "mcp"] });
  const initial = apiKeyFormFromKey(key(), p);
  assert.deepEqual(initial.surfaces, ["rest", "mcp"]);
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, readOnly: true }, p, NOW), { read_only: true });
  assert.deepEqual(buildUpdateApiKeyRequest(initial, { ...initial, surfaces: ["mcp"] }, p, NOW), {
    surfaces: ["mcp"],
  });
});
