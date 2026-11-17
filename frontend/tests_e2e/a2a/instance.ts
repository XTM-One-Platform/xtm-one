/**
 * The one deployment this suite runs against, and the clock it is running.
 *
 * Self-federation is a supported target
 * (`backend/tests/test_a2a_client_self_federation.py`): this platform's
 * inbound half mints exactly the OAuth pair its outbound half presents, and
 * serves its card where its own client can read it. Two registrations on one
 * deployment — one facing in, one facing out — are a complete partner
 * relationship, with every byte of the protocol on the wire between them.
 *
 * The platform image serves the SPA and the API from one port, so pages and
 * protocol calls address the same origin.
 */

import { SCRIPTED_MODEL, SCRIPTED_PROVIDER } from '../scripted-llm/client';

/** Re-exported so a spec has one import for "what this deployment is". */
export { SCRIPTED_MODEL, SCRIPTED_PROVIDER };

/**
 * The A2A clocks, in seconds.
 *
 * Read from the same names the deployment was given, so a run that shortens
 * the grace period gets a suite that knows it. The fallbacks are the values
 * the PRODUCT ships: pointed at a deployment nobody configured, the suite
 * believes what that deployment is actually doing.
 *
 * Specs read these rather than repeating numbers, so a timing assertion stays
 * honest when the defaults move.
 */
export interface Clock {
  sweepIntervalSeconds: number;
  verdictStalenessSeconds: number;
  floorIntervalSeconds: number;
  delegationCeilingSeconds: number;
  graceSeconds: number;
  reapIntervalSeconds: number;
  breakerThreshold: number;
  breakerCooldownSeconds: number;
  dedupWindowSeconds: number;
}

function seconds(name: string, fallback: number): number {
  const raw = process.env[name];
  const parsed = raw === undefined ? NaN : Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export const CLOCK: Clock = {
  sweepIntervalSeconds: seconds('A2A_CLIENT_SWEEP_INTERVAL_SECONDS', 3600),
  verdictStalenessSeconds: seconds('A2A_CLIENT_VERDICT_STALENESS_SECONDS', 900),
  floorIntervalSeconds: seconds('A2A_CLIENT_FLOOR_INTERVAL_SECONDS', 900),
  delegationCeilingSeconds: seconds('A2A_CLIENT_DELEGATION_CEILING_SECONDS', 300),
  graceSeconds: seconds('A2A_CLIENT_GRACE_SECONDS', 15),
  reapIntervalSeconds: seconds('A2A_REAP_INTERVAL_SECONDS', 60),
  breakerThreshold: seconds('A2A_CLIENT_BREAKER_THRESHOLD', 3),
  breakerCooldownSeconds: seconds('A2A_CLIENT_BREAKER_COOLDOWN_SECONDS', 300),
  dedupWindowSeconds: seconds('A2A_DEDUP_WINDOW_SECONDS', 86400),
};

/**
 * One deployment, one origin.
 *
 * `E2E_BASE_URL` is the single knob, and it is the same one the ordinary e2e
 * suite takes: the platform image serves the SPA and the API on one port, so
 * naming it once names both.
 */
export const SELF = {
  name: 'the deployment',
  origin: process.env.E2E_BASE_URL ?? 'http://localhost:4000',
  authFile: 'tests_e2e/.auth/a2a.json',
} as const;

export type Instance = typeof SELF;

/**
 * The real model, when the deployment was given one.
 *
 * The scripted model proves every mechanism behind a decision and none of the
 * decision itself — whether a real model, shown the real tool descriptions in
 * `app/a2a/client/tool.py`, still chooses to delegate. Both providers can be
 * seeded and an agent picks one when it is created, so specs whose subject is
 * the CHOICE ask for this one and skip when it is absent.
 *
 * Absent is the normal state, CI included: the `x-e2e-environment` anchor omits
 * `LLM_PROVIDER_OLLAMA_*`, so set those there and here together. Tool calling
 * is the whole requirement.
 */
export const REAL_PROVIDER = 'ollama';
export const REAL_MODEL: string | null =
  process.env.E2E_REAL_MODEL ?? process.env.LLM_PROVIDER_OLLAMA_MODELS ?? null;
export const HAS_REAL_MODEL = typeof REAL_MODEL === 'string' && REAL_MODEL.length > 0;

/**
 * Is the re-verification sweep running on a short cycle?
 *
 * Parked by default, and deliberately: `due_targets` treats a NULL verdict as
 * due whatever the staleness threshold says, so a live sweep verifies every
 * registration a spec creates within seconds — which makes "nothing verifies a
 * registration on save" unobservable though true. Specs whose subject IS the
 * sweep skip unless `A2A_CLIENT_SWEEP_INTERVAL_SECONDS` is short; the anchor
 * omits that name, so set it there and here together.
 */
export const SWEEP_IS_FAST = CLOCK.sweepIntervalSeconds <= 60;

/**
 * Whether a worker is running — a `delegate` handover needs one.
 *
 * Unset means yes: the documented `docker compose … up` starts the `worker`
 * service alongside the platform. A deployment without one leaves every
 * delegated branch at `pending`, so those specs say so and skip.
 */
export const WORKER_UP = process.env.E2E_A2A_WORKER !== '0';

export const ADMIN_EMAIL = process.env.E2E_USER_EMAIL;
export const ADMIN_PASSWORD = process.env.E2E_USER_PASSWORD;

/**
 * What every request on the A2A wire must carry.
 *
 * Nothing, deliberately. The partner-facing surface refuses plain http it
 * cannot confirm encrypted (`core/utils.py::is_secure_transport`). The suite
 * runs on the host, so its requests reach a containerised platform from the
 * bridge gateway, and the e2e job names that network in
 * TEST_ONLY_INSECURE_TRANSPORT_PEERS. That exemption reads the real socket
 * peer and is lost the moment a request carries `X-Forwarded-For`, so
 * this must never state an address. Nor `X-Forwarded-Proto`, which would make
 * `external_base` publish `https://` card URLs nothing is listening on. Kept
 * as the one place that says so, for every context the suite builds.
 */
export const WIRE_HEADERS: Record<string, string> = {};
