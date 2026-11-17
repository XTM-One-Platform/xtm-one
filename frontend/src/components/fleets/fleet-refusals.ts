/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * What a refused fleet write shows: the members panel, the
 * remove action, the re-parent, the settings panel and the New fleet dialog
 * all go through it.
 *
 * The routes answer a `FleetActionRefusedError` (`backend/app/services/fleets.py`)
 * in English. `FLEET_REFUSAL_MATCHERS` is the one table that recognizes those
 * wordings: a new server wording is added here once. Each surface then maps
 * the refusal to its own catalog sentence (`MEMBER_REFUSAL_SENTENCES` for an
 * existing fleet, `CREATE_REFUSAL_SENTENCES` in `new-fleet-form.ts` for a
 * creation), so a fr, es or ja user reads it in their language; a 4xx refusal
 * the table does not know keeps the server's words, never nothing; a 5xx or a
 * network error reads "Try again in a moment.", never the server's raw text.
 */
import { BackendUnavailableError } from "@/lib/api";

export type FleetRefusal =
  | "archived_fleet"
  | "archived_workspace"
  | "company_workspace"
  | "company_agents"
  | "home_elsewhere"
  | "already_member"
  | "connected_here"
  | "already_connected"
  | "self_connection"
  | "same_fleet_move"
  | "move_connected"
  | "self_parent"
  | "parent_connected"
  | "hierarchy_loop"
  | "orchestrator_not_member"
  | "exposed_not_member"
  | "not_exposed";

/** The stable wordings of the server's refusals, first match wins. */
const FLEET_REFUSAL_MATCHERS: { refusal: FleetRefusal; match: RegExp }[] = [
  { refusal: "archived_fleet", match: /^This fleet is archived: restore it to change its members/ },
  { refusal: "archived_workspace", match: /^That workspace is archived/ },
  { refusal: "company_workspace", match: /^A company-managed fleet needs a company-managed workspace/ },
  { refusal: "company_agents", match: /^A company-managed fleet contains only company-managed agents/ },
  { refusal: "home_elsewhere", match: /is already a member of the fleet '|was just added to another fleet/ },
  { refusal: "already_member", match: /is already a member of this fleet/ },
  { refusal: "connected_here", match: /is a connected agent of this fleet/ },
  { refusal: "already_connected", match: /^Already connected to this fleet/ },
  { refusal: "self_connection", match: /^A fleet cannot be connected to itself/ },
  { refusal: "same_fleet_move", match: /^The member is already in this fleet: pick another fleet/ },
  { refusal: "move_connected", match: /^Only a home member moves to another fleet/ },
  { refusal: "self_parent", match: /^A member cannot report to itself/ },
  { refusal: "parent_connected", match: /^A member reports to another member of the fleet, never to a connected one/ },
  { refusal: "hierarchy_loop", match: /^That parent reports to this member: the hierarchy would loop/ },
  { refusal: "orchestrator_not_member", match: /^The orchestrator must be a member of the fleet/ },
  { refusal: "exposed_not_member", match: /^Agent \S+ is not a member of this fleet/ },
  { refusal: "not_exposed", match: /is not exposed to other fleets/ },
];

/** Which known refusal an error carries, `undefined` for an unknown one. */
export function matchFleetRefusal(error: unknown): FleetRefusal | undefined {
  const raw = error instanceof Error ? error.message : "";
  return FLEET_REFUSAL_MATCHERS.find((entry) => entry.match.test(raw))?.refusal;
}

/** The catalog sentence of each refusal on an existing fleet. @i18n-keys */
export const MEMBER_REFUSAL_SENTENCES = {
  archived_fleet: "This fleet is archived: restore it to edit its members.",
  archived_workspace: "The workspace you picked is archived: pick another one.",
  company_workspace: "A company-managed fleet needs a company-managed workspace.",
  company_agents: "A company-managed fleet contains only company-managed agents.",
  home_elsewhere: "An agent you picked already has a home fleet: remove it there first, or connect it instead.",
  already_member: "The agent is already a member of this fleet.",
  connected_here: "An agent is either a home member or a connected agent, not both.",
  already_connected: "Already connected to this fleet.",
  self_connection: "A fleet cannot be connected to itself.",
  same_fleet_move: "The member is already in this fleet: pick another fleet.",
  move_connected: "Only a home member moves to another fleet: remove a connected agent instead.",
  self_parent: "A member cannot report to itself.",
  parent_connected: "A member reports to another member of the fleet, never to a connected agent.",
  hierarchy_loop: "That parent reports to this member: the hierarchy would loop.",
  orchestrator_not_member: "The orchestrator must be a member of the fleet.",
  exposed_not_member: "Only a home member of this fleet can be exposed.",
  not_exposed: "That agent belongs to another fleet that does not expose it: hand over to its orchestrator or an exposed agent.",
} as const satisfies Record<FleetRefusal, string>;

/**
 * Why `POST /fleets/bulk` skipped one fleet, as a catalog sentence (UX r1 B2 on
 * #9933): the per-fleet `detail` is the backend's English refusal
 * (`backend/app/api/v1/fleets.py` `bulk_fleet_action`), never shown raw.
 * First match wins; an unknown wording reads the generic sentence. @i18n-keys
 */
const BULK_SKIP_REASONS = {
  raise: "Only an administrator can raise the autonomy level.",
  builtin: "Built-in fleet: only an administrator switches its automations.",
  builtinReadOnly: "Built-in fleet: read-only.",
  notManager: "You do not manage this fleet.",
  notFound: "Fleet not found",
  stop: "An emergency stop is in effect: lift it before resuming a fleet.",
  archived: "This fleet is archived.",
  other: "This fleet cannot be changed.",
} as const;

const BULK_SKIP_MATCHERS: { reason: keyof typeof BULK_SKIP_REASONS; match: RegExp }[] = [
  { reason: "raise", match: /^Only an administrator can raise a fleet's autonomy level/ },
  { reason: "builtin", match: /^Only an administrator can .+ a built-in fleet\./ },
  { reason: "builtinReadOnly", match: /^Built-in fleets ship with the platform and are read-only/ },
  { reason: "notManager", match: /^Only (the creator or an admin|admins) can / },
  { reason: "notFound", match: /not found$/ },
  { reason: "stop", match: /^An emergency stop is in effect/ },
  { reason: "archived", match: /archived/ },
];

/** The catalog key of why a bulk call skipped a fleet (translate it with `t_i18n`). */
export function bulkSkipReason(detail: string): string {
  const reason = BULK_SKIP_MATCHERS.find((entry) => entry.match.test(detail))?.reason ?? "other";
  return BULK_SKIP_REASONS[reason];
}

/**
 * The words a browser's `fetch` rejects with when the request never reached
 * the server: Chromium "Failed to fetch", Firefox "NetworkError when attempting
 * to fetch resource.", Safari "Load failed", React Native style "Network request
 * failed". Anchored, so a dynamic import failure ("Failed to fetch dynamically
 * imported module") or a TypeError from our own code does not match.
 */
const FETCH_REJECTION = /^(Failed to fetch|NetworkError when attempting to fetch resource\.?|Load failed|Network request failed)$/;

/**
 * A write the server failed or never received: a 5xx, a gateway outage or a
 * network error. `lib/api.ts` `doFetch` already wraps a fetch rejection in a
 * `BackendUnavailableError`; a bare `TypeError` counts only when it carries a
 * fetch rejection's words, so a client-side bug keeps its own message instead
 * of hiding behind "Try again in a moment.". Their words ("Internal
 * Server Error", "Failed to fetch") are not a refusal to show.
 */
function isServerFailure(error: unknown): boolean {
  if (error instanceof BackendUnavailableError) return true;
  if (error instanceof TypeError) return FETCH_REJECTION.test(error.message);
  const status = (error as { status?: number } | null)?.status;
  return typeof status === "number" && status >= 500;
}

/**
 * The description of a refused write on an existing fleet: the catalog
 * sentence of a known refusal, a localized "not allowed" for a 403, a
 * localized "try again" for a 5xx or a network error, else the
 * server's own words (`undefined` when the error carries none).
 */
export function fleetRefusalMessage(error: unknown, t: (text: string) => string): string | undefined {
  const known = matchFleetRefusal(error);
  if (known) return t(MEMBER_REFUSAL_SENTENCES[known]);
  if ((error as { status?: number } | null)?.status === 403) return t("You are not allowed to change this fleet.");
  if (isServerFailure(error)) return t("Try again in a moment.");
  const raw = error instanceof Error ? error.message : "";
  return raw || undefined;
}
