// SPDX-License-Identifier: Apache-2.0

/**
 * Personal API keys: the dialog's form and what it sends, the expiry
 * choices the administrator's maximum lifetime leaves, and a key's state.
 *
 * Pure so `node --test` covers it (`api-keys.test.ts`). The server is the
 * authority on every bound (`api/v1/api_keys.py`); these helpers only keep the
 * dialog from offering a choice it would refuse.
 */

import type {
  ApiKeyPolicy,
  ApiKeyQuotaPeriod,
  ApiKeySurface,
  CreateApiKeyRequest,
  UpdateApiKeyRequest,
  UserApiKey,
} from "../types/index.ts";

export const DAY_MS = 86_400_000;

/** A key expiring within this many days reads as expiring soon. */
const EXPIRING_SOON_DAYS = 7;

/** Kept off the latest allowed expiry so a browser clock ahead of the server's is not refused. */
export const EXPIRY_CLOCK_MARGIN_MS = 5 * 60_000;

/** @i18n-keys */
export const API_KEY_SURFACE_LABELS = {
  rest: "Platform API",
  llm_proxy: "LLM proxy",
  mcp: "MCP endpoints",
} as const satisfies Record<ApiKeySurface, string>;

/** @i18n-keys */
export const API_KEY_SURFACE_DESCRIPTIONS = {
  rest: "The REST API under /api/v1.",
  llm_proxy: "The OpenAI-compatible API under /v1.",
  mcp: "Your agents as MCP tools and the platform MCP servers.",
} as const satisfies Record<ApiKeySurface, string>;

/** @i18n-keys */
export const API_KEY_QUOTA_PERIOD_LABELS = {
  daily: "Per day",
  monthly: "Per month",
} as const satisfies Record<ApiKeyQuotaPeriod, string>;

/** @i18n-keys */
export const API_KEY_QUOTA_USAGE_LABELS = {
  daily: "{used} / {limit} today",
  monthly: "{used} / {limit} this month",
} as const satisfies Record<ApiKeyQuotaPeriod, string>;

type ApiKeyState = "active" | "expiring" | "expired" | "revoked";

/** Whether a key still authenticates, and for how long. */
export function apiKeyState(
  key: Pick<UserApiKey, "is_active" | "expired" | "expires_at">,
  now: number,
): ApiKeyState {
  if (!key.is_active) return "revoked";
  if (key.expired) return "expired";
  if (!key.expires_at) return "active";
  const expiresAt = Date.parse(key.expires_at);
  if (Number.isNaN(expiresAt)) return "active";
  if (expiresAt <= now) return "expired";
  return expiresAt - now <= EXPIRING_SOON_DAYS * DAY_MS ? "expiring" : "active";
}

/** Whether the key still authenticates on *surface* (any agent, any method). */
export function apiKeyUsableOn(
  key: Pick<UserApiKey, "is_active" | "expired" | "expires_at" | "all_surfaces" | "surfaces">,
  surface: ApiKeySurface,
  now: number,
): boolean {
  const state = apiKeyState(key, now);
  if (state === "revoked" || state === "expired") return false;
  return key.all_surfaces || key.surfaces.includes(surface);
}

/** Whole days left before *iso*, at least 0. */
export function daysUntil(iso: string, now: number): number {
  return Math.max(0, Math.ceil((Date.parse(iso) - now) / DAY_MS));
}

// ── Expiry ──────────────────────────────────────────────────────────────────

/** The preset lifetimes the dialog offers, in days. */
const EXPIRY_PRESET_DAYS: readonly number[] = [7, 30, 90, 365];

/** `keep` leaves an existing key's expiry as it is; a number of days is a preset. */
export type ExpiryChoice = "keep" | "custom" | "never" | `${number}`;

/** The presets a maximum lifetime allows, the maximum itself added when it is not one. */
export function expiryPresets(maxLifetimeDays: number): number[] {
  if (maxLifetimeDays <= 0) return [...EXPIRY_PRESET_DAYS];
  const presets = EXPIRY_PRESET_DAYS.filter((days) => days < maxLifetimeDays);
  presets.push(maxLifetimeDays);
  return presets;
}

/** A key may go without an expiry only when no maximum lifetime is set. */
export function allowsNoExpiry(maxLifetimeDays: number): boolean {
  return maxLifetimeDays <= 0;
}

/** The lifetime a new key is offered first: 90 days, or the maximum when shorter. */
export function defaultExpiryDays(maxLifetimeDays: number): number {
  return maxLifetimeDays > 0 ? Math.min(90, maxLifetimeDays) : 90;
}

/** The latest expiry the server accepts from *now*, `null` without a maximum lifetime. */
export function latestAllowedExpiry(now: number, maxLifetimeDays: number): number | null {
  return maxLifetimeDays > 0 ? now + maxLifetimeDays * DAY_MS - EXPIRY_CLOCK_MARGIN_MS : null;
}

function clampExpiry(moment: number, now: number, maxLifetimeDays: number): number {
  const latest = latestAllowedExpiry(now, maxLifetimeDays);
  return latest === null ? moment : Math.min(moment, latest);
}

/** The expiry *days* from *now*, held to the maximum lifetime. */
export function expiryFromDays(days: number, now: number, maxLifetimeDays: number): string {
  return new Date(clampExpiry(now + days * DAY_MS, now, maxLifetimeDays)).toISOString();
}

/** A `YYYY-MM-DD` date input value for *moment*, in local time. */
export function dateInputValue(moment: number): string {
  const date = new Date(moment);
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

/** The end of the local day *value* names, held to the maximum lifetime; `null` when it is not in the future. */
export function expiryFromDate(value: string, now: number, maxLifetimeDays: number): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const endOfDay = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 23, 59, 59).getTime();
  if (Number.isNaN(endOfDay) || endOfDay <= now) return null;
  return new Date(clampExpiry(endOfDay, now, maxLifetimeDays)).toISOString();
}

// ── Form ────────────────────────────────────────────────────────────────────

export interface ApiKeyForm {
  name: string;
  /** The surfaces switched on; all of them = every surface. */
  surfaces: ApiKeySurface[];
  readOnly: boolean;
  restrictAgents: boolean;
  agentIds: string[];
  expiry: ExpiryChoice;
  /** `YYYY-MM-DD`, read when `expiry` is `custom`. */
  expiryDate: string;
  /** Typed text: empty = no limit. */
  rateLimit: string;
  quotaLimit: string;
  quotaPeriod: ApiKeyQuotaPeriod;
}

export type ApiKeyFormProblem =
  | "name"
  | "surfaces"
  | "agents"
  | "too_many_agents"
  | "expiry_date"
  | "rate_limit"
  | "quota_limit";

/** @i18n-keys */
export const API_KEY_FORM_PROBLEMS = {
  name: "Give the key a name.",
  surfaces: "Turn on at least one surface.",
  agents: "Pick at least one agent, or turn the restriction off.",
  too_many_agents: "A key can name at most {max, plural, one {# agent} other {# agents}}.",
  expiry_date: "Pick an expiry date in the future.",
  rate_limit: "The rate limit is a whole number between 1 and {max}, or empty for none.",
  quota_limit: "The quota is a whole number between 1 and {max}, or empty for none.",
} as const satisfies Record<ApiKeyFormProblem, string>;

/** What the dialog starts from for a new key: every surface, read-write, the default lifetime. */
export function newApiKeyForm(policy: ApiKeyPolicy): ApiKeyForm {
  return {
    name: "",
    surfaces: [...policy.surfaces],
    readOnly: false,
    restrictAgents: false,
    agentIds: [],
    expiry: `${defaultExpiryDays(policy.max_lifetime_days)}` as const,
    expiryDate: "",
    rateLimit: "",
    quotaLimit: "",
    quotaPeriod: policy.quota_periods.includes("monthly") ? "monthly" : (policy.quota_periods[0] ?? "monthly"),
  };
}

/** What the dialog starts from for an existing key: its scopes as stored, its expiry kept. */
export function apiKeyFormFromKey(key: UserApiKey, policy: ApiKeyPolicy): ApiKeyForm {
  return {
    name: key.name,
    surfaces: key.all_surfaces
      ? [...policy.surfaces]
      : policy.surfaces.filter((surface) => key.surfaces.includes(surface)),
    readOnly: key.read_only,
    restrictAgents: key.agent_ids !== null,
    agentIds: key.agent_ids ?? [],
    expiry: "keep",
    expiryDate: key.expires_at ? dateInputValue(Date.parse(key.expires_at)) : "",
    rateLimit: key.rate_limit_per_minute ? String(key.rate_limit_per_minute) : "",
    quotaLimit: key.quota_limit ? String(key.quota_limit) : "",
    quotaPeriod: key.quota_period ?? newApiKeyForm(policy).quotaPeriod,
  };
}

/** A typed limit: `null` when empty, `undefined` when it is not a whole number between 1 and *max*. */
export function parseLimit(text: string, max: number): number | null | undefined {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (!/^\d+$/.test(trimmed)) return undefined;
  const value = Number(trimmed);
  return value >= 1 && value <= max ? value : undefined;
}

/** The first thing the server would refuse in *form*, `null` when it can be sent. */
export function apiKeyFormProblem(form: ApiKeyForm, policy: ApiKeyPolicy, now: number): ApiKeyFormProblem | null {
  if (!form.name.trim()) return "name";
  if (!form.surfaces.some((surface) => policy.surfaces.includes(surface))) return "surfaces";
  if (form.restrictAgents && form.agentIds.length === 0) return "agents";
  if (form.restrictAgents && form.agentIds.length > policy.max_agents) return "too_many_agents";
  if (form.expiry === "custom" && expiryFromDate(form.expiryDate, now, policy.max_lifetime_days) === null) {
    return "expiry_date";
  }
  if (parseLimit(form.rateLimit, policy.max_rate_limit_per_minute) === undefined) return "rate_limit";
  if (parseLimit(form.quotaLimit, policy.max_quota_limit) === undefined) return "quota_limit";
  return null;
}

function surfacesField(form: ApiKeyForm, policy: ApiKeyPolicy): ApiKeySurface[] | null {
  const chosen = policy.surfaces.filter((surface) => form.surfaces.includes(surface));
  return chosen.length === policy.surfaces.length ? null : chosen;
}

/** The expiry *form* asks for: a timestamp, `null` for none, `undefined` to keep the stored one. */
function expiryField(form: ApiKeyForm, policy: ApiKeyPolicy, now: number): string | null | undefined {
  if (form.expiry === "keep") return undefined;
  if (form.expiry === "never") return null;
  if (form.expiry === "custom") return expiryFromDate(form.expiryDate, now, policy.max_lifetime_days);
  return expiryFromDays(Number(form.expiry), now, policy.max_lifetime_days);
}

function sameMembers(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((entry) => b.includes(entry));
}

/** The body that creates the key *form* describes. Check `apiKeyFormProblem` first. */
export function buildCreateApiKeyRequest(form: ApiKeyForm, policy: ApiKeyPolicy, now: number): CreateApiKeyRequest {
  const quotaLimit = parseLimit(form.quotaLimit, policy.max_quota_limit) ?? null;
  return {
    name: form.name.trim(),
    surfaces: surfacesField(form, policy),
    read_only: form.readOnly,
    agent_ids: form.restrictAgents ? [...form.agentIds] : null,
    expires_at: expiryField(form, policy, now) ?? null,
    rate_limit_per_minute: parseLimit(form.rateLimit, policy.max_rate_limit_per_minute) ?? null,
    quota_limit: quotaLimit,
    quota_period: quotaLimit === null ? null : form.quotaPeriod,
  };
}

/**
 * The fields of *form* that differ from *initial* (what the dialog opened with),
 * so a save never rewrites a scope nobody touched. Empty when nothing changed.
 */
export function buildUpdateApiKeyRequest(
  initial: ApiKeyForm,
  form: ApiKeyForm,
  policy: ApiKeyPolicy,
  now: number,
): UpdateApiKeyRequest {
  const body: UpdateApiKeyRequest = {};
  if (form.name.trim() !== initial.name.trim()) body.name = form.name.trim();
  if (!sameMembers(surfacesField(form, policy) ?? policy.surfaces, surfacesField(initial, policy) ?? policy.surfaces)) {
    body.surfaces = surfacesField(form, policy);
  }
  if (form.readOnly !== initial.readOnly) body.read_only = form.readOnly;
  const agents = form.restrictAgents ? form.agentIds : null;
  const initialAgents = initial.restrictAgents ? initial.agentIds : null;
  if (agents === null ? initialAgents !== null : initialAgents === null || !sameMembers(agents, initialAgents)) {
    body.agent_ids = agents === null ? null : [...agents];
  }
  const expiry = expiryField(form, policy, now);
  if (expiry !== undefined) body.expires_at = expiry;
  const rate = parseLimit(form.rateLimit, policy.max_rate_limit_per_minute) ?? null;
  if (rate !== (parseLimit(initial.rateLimit, policy.max_rate_limit_per_minute) ?? null)) {
    body.rate_limit_per_minute = rate;
  }
  const quota = parseLimit(form.quotaLimit, policy.max_quota_limit) ?? null;
  const initialQuota = parseLimit(initial.quotaLimit, policy.max_quota_limit) ?? null;
  if (quota !== initialQuota || (quota !== null && form.quotaPeriod !== initial.quotaPeriod)) {
    body.quota_limit = quota;
    body.quota_period = quota === null ? null : form.quotaPeriod;
  }
  return body;
}
