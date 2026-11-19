// SPDX-License-Identifier: Apache-2.0

/**
 * The catalog keys of the provisioning screens (#122): the deactivation
 * cascade's preview lines, the refusals, who deactivated an account and what
 * the cascade did to a reviewed object. Values are catalog keys.
 */

/** @i18n-keys The cascade steps the preview counts, in the order they run. */
export const PREVIEW_LINES = {
  api_keys_revoked: "{count, plural, one {# personal API key revoked} other {# personal API keys revoked}}",
  runs_cancelled: "{count, plural, one {# running or queued run stopped} other {# running or queued runs stopped}}",
  timers_cancelled: "{count, plural, one {# armed timer cancelled} other {# armed timers cancelled}}",
  scheduled_actions_cancelled: "{count, plural, one {# scheduled action cancelled} other {# scheduled actions cancelled}}",
  private_agents_disabled: "{count, plural, one {# private agent disabled with its assignments} other {# private agents disabled with their assignments}}",
  agents_transferred: "{count, plural, one {# shared agent moved to {successor}} other {# shared agents moved to {successor}}}",
  assignments_paused: "{count, plural, one {# assignment that runs as this account paused} other {# assignments that run as this account paused}}",
  fleets_transferred: "{count, plural, one {# fleet moved to {successor}} other {# fleets moved to {successor}}}",
  workspaces_transferred: "{count, plural, one {# workspace moved to {successor}} other {# workspaces moved to {successor}}}",
  integrations_transferred: "{count, plural, one {# shared integration moved to {successor}} other {# shared integrations moved to {successor}}}",
  integrations_disconnected: "{count, plural, one {# private integration disconnected} other {# private integrations disconnected}}",
  personal_credentials_disconnected: "{count, plural, one {# personal sign-in to a shared integration removed} other {# personal sign-ins to shared integrations removed}}",
} as const;

/** @i18n-keys The refusals the preview answers (``preview_deactivation`` and the route). */
export const REFUSAL_LABELS = {
  "The platform admin account cannot be disabled": "The platform admin account cannot be disabled",
  "The account is already deactivated": "The account is already deactivated",
  "Cannot deactivate yourself": "Cannot deactivate yourself",
} as const;

/** @i18n-keys Who deactivated an account (``UserDeactivation.source``). */
export const DEACTIVATION_SOURCE_LABELS = {
  admin: "Administrator",
  scim: "Identity provider (SCIM)",
  sso: "Single sign-on",
  system: "Platform",
} as const;

/** @i18n-keys What the cascade did to a reviewed object. */
export const REVIEW_ACTION_LABELS = {
  disabled: "Disabled",
  transferred: "Moved",
  paused: "Paused",
  disconnected: "Disconnected",
  kept_no_successor: "Kept, no administrator",
  bound_by_other_agent: "Used by another agent",
} as const;

/**
 * @i18n-keys The line under a reviewed object, by what the cascade did to it;
 * the values are the item's `params` (`_review_item` in
 * backend/app/services/user_deactivation.py). An action without a line here
 * shows its badge alone.
 */
export const REVIEW_DETAIL_LABELS = {
  transferred: "Now owned by {successor}{paused, plural, =0 {.} one {; # assignment that ran as this account was paused.} other {; # assignments that ran as this account were paused.}}",
  paused: "Assignment of the agent {agent}; it ran as this account.",
  disconnected: "{provider} account of this user; its token was cleared.",
  kept_no_successor: "No other active administrator could take it over.",
  bound_by_other_agent: "{count, plural, one {Bound to # integration of this user, now disconnected.} other {Bound to # integrations of this user, now disconnected.}}",
} as const;

/**
 * @i18n-keys One line of the latest SCIM changes, by `<entity_type>:<action>` of
 * the audit row (`list_scim_activity` in backend/app/api/v1/scim.py); `{name}`
 * is the row's `entity_name`. A row this table does not name, or one without a
 * name, shows the backend's own summary.
 */
export const SCIM_ACTIVITY_LABELS = {
  "user:create": "Account {name} provisioned",
  "user:update": "Account {name} changed",
  "user:delete": "Account {name} deprovisioned",
  "group:create": "Group {name} provisioned",
  "group:update": "Group {name} changed",
  "group:delete": "Group {name} deleted",
  "scim_token:create": "SCIM token {name} generated",
  "scim_token:delete": "SCIM token {name} revoked",
} as const;

/** The catalog key of `key` in a label table, else `fallback` (a value the backend added later). */
export function labelOf(table: Readonly<Record<string, string>>, key: string, fallback: string): string {
  return table[key] ?? fallback;
}
