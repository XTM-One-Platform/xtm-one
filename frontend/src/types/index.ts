// SPDX-License-Identifier: Apache-2.0

/** Shared TypeScript types for XTM One frontend. */

import type { ChatChartBlock } from "@/components/chat/chat-chart";
import type { ExtraFieldInputSpec } from "@/lib/extra-fields";

// Auth

/** When the caller's web session ends: `GET /auth/session`, `POST /auth/session/activity`. */
export interface SessionStatus {
  /** `other` for a credential that is not a web session (an API key, a platform token). */
  kind: "session" | "other";
  /** The server's clock, to time the warning and the check on it. */
  server_time: string;
  /** When the absolute lifetime ends. */
  expires_at: string | null;
  /** The idle timeout in force, in seconds; 0 when there is none. */
  idle_timeout_seconds: number;
  /** When the session ends unless its user does something. */
  idle_expires_at: string | null;
  /** False while the server cannot read Redis: the idle timeout is not enforced then. */
  idle_enforced: boolean;
}

/** Platform-wide Terms of Use acceptance state, shipped inline on `/auth/me`. */
export interface TermsOfUseState {
  /** Whether the gate applies to this deployment (config on + Agentic AI licensed). */
  required: boolean;
  /** Currently published document identifier (`TERMS_OF_USE_VERSION`). */
  version: string;
  /** Where the document lives (`TERMS_OF_USE_URL`). */
  url: string;
  /** True when the PLATFORM accepted THIS version — always true when `required` is false. */
  accepted: boolean;
  accepted_at: string | null;
  /** The version the platform last accepted; may be older than `version` after a bump. */
  accepted_version: string | null;
  /** Whether the current user may accept on behalf of the whole platform (admins only).
   *  Optional for backward-compatibility with payloads served by backend builds that
   *  predate the field (rolling deploys, cached `["me"]` responses); the UI falls back
   *  to the authenticated role when it is missing. */
  can_accept?: boolean;
}

/** Per-user TOTP two-factor state, shipped inline on `/auth/me`. */
export interface MfaState {
  /** Whether the second factor is active and enforced at login. */
  enabled: boolean;
  /** Whether this account may enroll. True for every account, SSO included —
   *  the factor is XTM One's own, layered on top of whichever way the first
   *  factor was proven. */
  available: boolean;
  /** Whether disabling will demand the account password. False for SSO-only
   *  accounts, which have none — the disable form hides the field. */
  requires_password_to_disable?: boolean;
  confirmed_at: string | null;
  /** Unused recovery codes left. The codes themselves are unrecoverable once
   *  enrollment is complete, so this count is all the UI can show. */
  recovery_codes_remaining: number;
}

export interface User {
  id: string;
  email: string;
  full_name: string;
  title: string | null;
  timezone: string | null;
  theme: "dark" | "light" | null;
  // UI language override: "auto" (inherit platform language) or a supported
  // locale code (e.g. "fr-fr").
  language: string;
  /** True when a group of theirs switches AI off, and that group's name
   *  (`services/group_ai_access.py`). The platform-wide switch is `ai_enabled`
   *  on the platform config: read both through `useAiEnabled()`. */
  ai_disabled_by_group?: boolean;
  ai_disabled_group?: string | null;
  // CTEM command center (XTM One home) per-user preferences.
  ctem_vertical: CtemVertical;
  ctem_splash_seen: boolean;
  /** What the person saw when they closed the command center's fleets invitation (#9566); null = never closed. */
  ctem_fleet_invite_dismissed?: { fleets: string[]; modules: string[] } | null;
  /** What the person closed on the home dashboard's context notices (#11586): the missing fields seen, and `generic:<id>`; null = never. */
  organization_context_notice_dismissed?: string[] | null;
  /** The fleets this person's command center shows, in order (#11464); null = the built-in CTEM fleets. */
  ctem_dashboard_fleet_ids?: string[] | null;
  /** The fleets this person's Copilot home shows, in order (#12063); null = every fleet, attention first. */
  copilot_dashboard_fleet_ids?: string[] | null;
  /** The command center this person built, per vertical; a vertical it does
   *  not name (or null) is the default layout. May name a widget this build no
   *  longer has, or skip a new one: read it through `resolveLayout()`. */
  ctem_dashboard_layouts?: Record<string, { items?: unknown; hidden?: unknown; context?: unknown }> | null;
  /** The Copilot dashboard this person built; null = the default layout. Read
   *  it through `resolveLayout()`. */
  copilot_dashboard_layout?: { items?: unknown; hidden?: unknown } | null;
  /** The chat sidebar's workspace groups this person opened; null = never
   *  chosen, every group collapsed (#9179). */
  chat_sidebar_expanded_workspaces?: string[] | null;
  /** The bell shows the work items waiting for this person (#9437); on by default. */
  notify_waiting_work_items?: boolean;
  /** The composer's reply suggestion after a chat turn; on by default. */
  reply_suggestions_enabled?: boolean;
  role: "admin" | "user";
  sso_provider: string | null;
  default_knowledge_base_ids: string[] | null;
  default_mcp_server_ids: string[] | null;
  default_agent_id: string | null;
  /** The custom dashboard shown on /dashboard instead of the platform's; null = the platform's. */
  home_dashboard_id?: string | null;
  // Per-user General Assistant overrides (null = use platform defaults)
  ga_builtin_tool_ids: string[] | null;
  ga_custom_tool_ids: string[] | null;
  ga_integration_bindings: Record<string, string[]> | null;
  ga_mcp_server_ids: string[] | null;
  ga_knowledge_base_ids: string[] | null;
  /** Browser profiles the General Assistant may open with its browser tool; null = none. */
  ga_browser_profile_ids?: string[] | null;
  // Per-user channel agent overrides for DM conversations
  channel_agent_config: Record<string, string> | null;
  // MCP endpoint – user-selected agents to expose on /mcp/user
  mcp_exposed_agent_ids: string[] | null;
  // Agents starred on /agents — pinned to the top of the list, which orders the
  // pinned block itself by the active sort rather than by this list's order.
  // null and [] are equivalent ("no favorites").
  favorite_agent_ids: string[] | null;
  // Own integration ids whose "personal OAuth token is shared" dashboard
  // reminder this user closed. Server-side so the choice
  // follows the user across browsers; reset per integration when its sharing
  // is turned off. null and [] are equivalent ("nothing closed").
  oauth_share_dismissed_integration_ids: string[] | null;
  // Group memberships (IDs) — lets the UI compute group-shared manage rights
  // with the same intersection semantics as the backend.
  group_ids: string[] | null;
  /** The groups this user manages: whose members' AI usage they read. */
  managed_groups?: ManagedGroupRef[];
  // Impersonation (populated when admin is impersonating a user)
  impersonating?: boolean;
  impersonated_by_id?: string | null;
  impersonated_by_name?: string | null;
  // Terms of Use acceptance — drives the blocking consent gate that wraps the
  // whole authenticated route tree (`TermsGate` in App.tsx).
  terms_of_use: TermsOfUseState;
  /** TOTP two-factor state. Optional so the type stays
   *  backward-compatible with cached payloads served by older backend builds;
   *  treat a missing value as "off and unavailable". */
  mfa?: MfaState;
}

export interface ImpersonationResponse {
  access_token: string;
  token_type: string;
  target_user_id: string;
  target_user_name: string;
  target_user_email: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

/** `POST /auth/register-with-invitation`: `redirect_url` is where a registered platform's invitation continues, else null. */
export interface RegisterWithInvitationResponse extends TokenResponse {
  redirect_url?: string | null;
}

/** Result of `POST /auth/login`.
 *
 *  Two shapes behind one type: when `mfa_required` is false the login is
 *  complete and `access_token` is set (the pre-2FA behaviour, unchanged); when
 *  it is true no session exists yet and `mfa_token` carries a short-lived
 *  challenge that only `POST /auth/mfa/verify` accepts. */
export interface LoginResponse {
  access_token?: string | null;
  token_type: string;
  mfa_required?: boolean;
  mfa_token?: string | null;
}

/** Payload of `POST /auth/mfa/setup` — everything needed to enroll a device.
 *
 *  `secret` is readable exactly once, here: apps that cannot scan a QR
 *  (KeePassXC on a desktop, a VM, a locked-down phone) need the manual entry
 *  key, and there is no second chance to read it. */
export interface MfaSetup {
  secret: string;
  otpauth_uri: string;
  /** Server-rendered SVG QR as a `data:` URL — drop it straight into an
   *  `<img src=...>`. Rendered backend-side so enrollment works air-gapped. */
  qr_code: string;
  digits: number;
  period: number;
  algorithm: string;
}

export interface MfaRecoveryCodes {
  recovery_codes: string[];
}

export interface AuthConfig {
  base_url: string;
  local_enabled: boolean;
  registration_enabled: boolean;
  allowed_domains: string[];
  oidc_enabled: boolean;
  oidc_display_name: string;
  saml_enabled: boolean;
  saml_display_name: string;
  /** Server-enforced minimum password length (from ``MIN_PASSWORD_LENGTH``).
   *  Optional so the type stays backward-compatible with cached payloads
   *  served by older backend builds; callers should default to
   *  ``MIN_PASSWORD_LENGTH`` from ``lib/utils`` when this is undefined. */
  min_password_length?: number;
  /** Resolved feature-flag ids enabled for this platform instance (from
   *  the backend's ENABLED_FEATURES env var — see
   *  backend/app/core/feature_flags.py). Optional for the same
   *  backward-compatibility reason as ``min_password_length``. Use
   *  ``isFeatureEnabled()`` from ``lib/utils`` rather than checking this
   *  array directly. */
  enabled_features?: string[];
  /** Whether financial cost values are available in this deployment mode.
   *  Optional for backward-compatibility with older backend payloads.
   *  When omitted, the UI defaults to visible costs. */
  cost_data_available?: boolean;
  /** Whether the native Runner subsystem is enabled on this deployment.
   *  Gates the XTM One Desktop page, its sidebar-footer and user-menu entries
   *  and the `app.desktop.open` palette command. Optional for
   *  backward-compatibility; when omitted the UI treats runners as enabled
   *  (the backend default). */
  runners_enabled?: boolean;
  /** Whether on-premises relays are on (`RELAYS_ENABLED`, true by
   *  default). Gates the Relays page and every "Route through" picker. Read
   *  only through `relaysEnabledFrom` (`lib/relays.ts`): unknown while the
   *  config loads, on when omitted (the backend default). */
  relays_enabled?: boolean;
  /** Whether the presentation layout check runs on this deployment
   *  (`PRESENTATION_LAYOUT_CHECK_ENABLED`). Gates the viewer's `Layout check`
   *  button; when omitted the UI treats the check as enabled (the backend
   *  default). */
  presentation_layout_check_enabled?: boolean;
  /** Whether the waiting arcade is on (`ARCADE_ENABLED`). Gates the
   *  waiting panel's take-control affordance, the leaderboard and the
   *  `arcade.*` palette commands; when omitted the UI treats it as enabled
   *  (the backend default). */
  arcade_enabled?: boolean;
  /** The frontend build the server serves now (the `<meta name="build-id">`
   *  of its `index.html`; empty without a built frontend). A page that booted
   *  another build offers a reload (`useStaleBuildNotice`). Optional:
   *  an older backend does not send it, and then nothing is compared. */
  build_id?: string;
}

// Users (admin)
export interface UserListItem {
  id: string;
  email: string;
  full_name: string;
  role: "admin" | "user";
  is_active: boolean;
  is_platform_admin: boolean;
  sso_provider: string | null;
  /** Who manages the account: an identity provider over SCIM, SSO, or local. */
  provisioning_source?: ProvisioningSource;
  /** Set when the identity provider removed the account (a SCIM DELETE). */
  deprovisioned_at?: string | null;
  created_at: string;
  integration_count: number;
  mcp_server_count: number;
  agent_count: number;
  knowledge_base_count: number;
  group_ids: string[];
}

type ProvisioningSource = "scim" | "sso" | "local";

/** A person as returned by the non-admin `/users/directory` search.
 *  Name and email only — deliberately far less than {@link UserListItem}. */
export interface UserDirectoryItem {
  id: string;
  full_name: string;
  email: string;
  /** Only ever false in `ids` resolve mode — search results are active-only. */
  is_active?: boolean;
}

export interface CreateUserRequest {
  email: string;
  full_name: string;
  password?: string;
  role: "admin" | "user";
  account_type: "local" | "sso";
}

export interface CreateUserResponse {
  id: string;
  email: string;
  full_name: string;
  role: string;
  sso_provider: string | null;
}

// Invitations (admin-generated, email-bound registration links)
export interface CreateInvitationRequest {
  email: string;
  role: "admin" | "user";
  expires_in_hours?: number;
  send_email?: boolean; // default true; false returns token/link (seed/automation)
}

export interface CreateInvitationResponse {
  id: string;
  email: string;
  role: "admin" | "user";
  expires_at: string;
  email_sent: boolean;
  link?: string | null; // registration link (token embedded), only when send_email=false
}

export interface InvitationListItem {
  id: string;
  email: string;
  role: "admin" | "user";
  status: "pending";
  created_at: string | null;
  expires_at: string | null;
}

export interface ValidateInvitationResponse {
  valid: boolean;
  email?: string;
  role?: "admin" | "user";
  expires_at?: string;
}

// Password reset (admin-initiated — emailed to the user, link not returned)
export interface PasswordResetResponse {
  email_sent: boolean;
  expires_at: string;
  link?: string | null; // only when send_email=false
}

export interface ValidatePasswordResetResponse {
  valid: boolean;
  email?: string;
  sso_only?: boolean;
}

// Vault references
/** The last failed read of a secret stored as a vault reference: which field, which provider, why. */
interface VaultSecretFailure {
  field: string;
  provider: string;
  /** Reason code: not_found, access_denied, unreachable, key_required, ... */
  reason: string;
  description: string;
  at: string;
}

/** What an entity's response says about its secrets stored as `vault://<provider>/<path>#<key>` references. */
export interface VaultSecretStatus {
  /** `{field: providers}` for each secret field holding a reference (an MCP document may name several). */
  references: Record<string, string[]>;
  /** The secret fields holding a value that starts with `vault://` but is not a valid reference: every use fails. */
  invalid_references: string[];
  failures: VaultSecretFailure[];
  /** `{field: locations}`: where each reference reads, for a viewer who may edit the entity. */
  locations?: Record<string, VaultSecretLocation[]>;
  /** `{field: variable ids}`: the credentials reading a secret variable `{{var_id.<id>}}`, ids only. */
  variables?: Record<string, string[]>;
}

/** Where a stored reference reads its secret; `path` / `key` only when the viewer may use that path. */
export interface VaultSecretLocation {
  provider: string;
  provider_display_name: string;
  path: string | null;
  key: string | null;
  /** The header or variable of an MCP document holding it. */
  entry: string | null;
}

// Secret vault providers
export type SecretVaultType = "hashicorp" | "aws" | "gcp" | "azure" | "onepassword";

type SecretVaultStatus =
  | "connected"
  | "failing"
  | "disabled"
  | "not_checked"
  | "misconfigured"
  | "environment"
  | "shadowed";

export interface SecretVaultGrant {
  principal: "everyone" | "group" | "user";
  id: string | null;
  paths: string[];
  /** The group's or user's name (responses only). */
  label?: string | null;
}

/** A provider as Settings > Access and security > Secret vaults shows it: never a credential. */
export interface SecretVaultProvider {
  id: string | null;
  name: string;
  display_name: string;
  type: SecretVaultType;
  source: "database" | "environment";
  enabled: boolean;
  status: SecretVaultStatus;
  status_reason: string | null;
  status_description: string | null;
  status_detail: string | null;
  last_check_at: string | null;
  settings: Record<string, string>;
  /** Which credentials are stored (`true`), by name. Never the value. */
  credentials_set: Record<string, boolean>;
  grants: SecretVaultGrant[];
  browsable: boolean;
  cache_seconds: number | null;
  writers: string | null;
  paths: string[];
  usage_count: number;
  created_at: string | null;
  updated_at: string | null;
  updated_by_name: string | null;
}

export interface SecretVaultList {
  providers: SecretVaultProvider[];
  default_cache_seconds: number;
}

/** A create, or an edit of a provider: credentials are write-only (`null` clears one, a missing key keeps it). */
export interface SecretVaultProviderInput {
  name: string;
  display_name: string;
  type: SecretVaultType;
  settings: Record<string, string | null>;
  credentials: Record<string, string | null>;
  enabled: boolean;
  grants: SecretVaultGrant[];
  browsable: boolean;
  cache_seconds: number | null;
}

interface SecretVaultCheckStep {
  step: "configuration" | "login" | "list" | "read";
  /** `null`: not run. */
  ok: boolean | null;
  reason: string | null;
  description: string | null;
  detail: string | null;
}

export interface SecretVaultConnectionReport {
  ok: boolean;
  steps: SecretVaultCheckStep[];
  /** HashiCorp: the KV mounts the platform's token sees. */
  mounts: { path: string; version: string }[] | null;
  checked_at: string;
}

/** A secret field holding a reference to a provider: the entity, never the path. */
export interface SecretVaultUse {
  kind: string;
  id: string;
  name: string;
  field: string;
}

/** A provider the current user may pick a secret from. */
export interface PickerVaultProvider {
  name: string;
  display_name: string;
  type: SecretVaultType;
  source: "database" | "environment";
  browsable: boolean;
  /** The path prefixes the user may use; `null`: every path. */
  paths: string[] | null;
}

export interface PickerVaultEntry {
  name: string;
  path: string;
  folder: boolean;
}

interface PickerUnavailable {
  reason: string;
  description: string;
}

export interface PickerVaultListing {
  entries: PickerVaultEntry[];
  next_page_token: string | null;
  unavailable: PickerUnavailable | null;
}

export interface PickerVaultKeys {
  keys: string[];
  /** How a key that is an id is shown (a 1Password field's label), by key. */
  labels?: Record<string, string>;
  single_value: boolean;
  unavailable: PickerUnavailable | null;
}

export interface VaultReferenceCheck {
  ok: boolean;
  reason: string | null;
  description: string | null;
}

// LLM
/** Where a scanned context window was found: the provider's own model listing,
 *  the bundled model catalog, or the platform's estimate for the model family. */
export type ContextWindowSource = "provider" | "catalog" | "family";

/** `POST /settings/llm-providers/{id}/context-windows/scan`: read-only lookup. */
export interface ContextWindowScan {
  provider: string;
  /** `failed`: the provider's listing could not be read, the catalog answered alone. */
  live_lookup: "ok" | "unsupported" | "failed";
  results: { model: string; context_window: number | null; source: ContextWindowSource | null }[];
}

/** `GET /settings/llm-providers/{id}/models`: what a configured provider lists now. */
export interface ProviderModelsResponse {
  provider: string;
  /** Chat models. */
  models: string[];
  /** Filigran AI Gateway only: every id it offers (chat, embedding and image). */
  offered_models?: string[] | null;
}

export interface LLMProvider {
  id: string;
  provider: string;
  display_name: string | null;
  /**
   * What its provider type is known to serve: `chat` or its one capability
   * (`embedding`, `speech`), and `decision`. Chat models are offered only for
   * a provider that chats.
   */
  capabilities?: string[];
  /** Whether its decision listing names every decision model it serves: OpenRouter's does not. */
  lists_decision_models?: boolean;
  /** The API key read from a vault, and its recent failures. */
  vault_secrets?: VaultSecretStatus | null;
  default_model: string | null;
  available_models: string[] | null;
  /** Per-model context window overrides (model → tokens). Null = use provider defaults. */
  context_windows: Record<string, number> | null;
  has_key: boolean;
  /** Filigran AI Gateway only, admins only: its URL (never the token). */
  gateway_url?: string | null;
  /** True when this provider was seeded from environment variables: its API key is env-managed and it cannot be deleted. */
  is_from_env: boolean;
  /**
   * True when the model catalog is owned by the environment (env-seeded
   * provider without `LLM_PROVIDER_MODELS_EDITABLE=true`): the Models editor
   * is hidden and the backend refuses `available_models` / `default_model`
   * changes. False for UI-created providers and for env-seeded providers in
   * editable mode, where admins curate the list on top of the env baseline.
   */
  models_env_locked: boolean;
  /** Env-seeded with no `LLM_PROVIDER_{P}_MODELS`: its catalog is its own listing,
   *  read again by `POST .../refresh-models`. Admins only, on the listing. */
  models_listed?: boolean;
  /** Editable mode, same provider, catalog an admin edited: `refresh-models` with
   *  `discard_edits` makes it follow the listing again. Admins only, on the listing. */
  models_followable?: boolean;
  /** Model ids this config shares with another, and which configs share them.
   *  Overlap is legitimate, but it is what makes a bare model id ambiguous. */
  model_overlaps?: { model: string; provider_ids: string[] }[];
  /** Only on the answer to a create / update that changed the chat models: the saved changes the admin is offered to email. */
  model_catalog_change?: ModelCatalogChange | null;
  /** Usable only by the groups and users below, not by everyone. */
  access_restricted?: boolean;
  /** Who may use a restricted provider; served to admins only (empty for anyone else). */
  shared_group_ids?: string[];
  shared_user_ids?: string[];
  /** Who it is never used for, whatever it grants; served to admins only. */
  excluded_group_ids?: string[];
  excluded_user_ids?: string[];
  /** The company-managed integration this provider is fed from (OpenRouter):
   *  its API key follows the integration's. Null for a provider made here. */
  source_integration_id?: string | null;
}

/** Who may use an LLM provider config, as a create or update writes it. */
export interface LLMProviderAccess {
  access_restricted: boolean;
  shared_group_ids: string[];
  shared_user_ids: string[];
  /** Never used for these groups and people, whatever the grants. */
  excluded_group_ids: string[];
  excluded_user_ids: string[];
}

export interface AvailableModel {
  model: string;
  provider: string;
  provider_id?: string;
  /** Its provider is restricted to some groups and users. */
  access_restricted?: boolean;
}

/** Why a provider that lists its decision models offered what it did. */
export type DecisionListingStatus = "listed" | "empty" | "incomplete" | "failed" | "unavailable";

export interface AvailableDecisionModels {
  models: AvailableModel[];
  listings: { provider_id: string; status: DecisionListingStatus }[];
}

/** A voice of a speech model. */
export interface SpeechVoice {
  id: string;
  name: string;
  /** premade, cloned, generated, professional, prebuilt...; "" for a fixed set. */
  category: string;
  description: string;
}

export interface SpeechVoicesResponse {
  provider: string | null;
  provider_id: string | null;
  model: string | null;
  voices: SpeechVoice[];
  /** Why the list is empty, when it is. */
  message: string | null;
}

/** A model on a provider config, as a route stores it. */
export interface ProviderRouteTarget {
  provider: string | null;
  provider_id: string | null;
  display_name: string | null;
  model: string | null;
}

/**
 * What one route that used a provider config becomes when it is deleted:
 * `reseeded` moves it to the replacement, pinned by id; `cleared`
 * empties it so it falls back to the next tier; `unchanged` means it did not
 * use the deleted config.
 */
export interface ProviderDeletionRoute {
  affected: boolean;
  action: "unchanged" | "reseeded" | "cleared";
  before: ProviderRouteTarget | null;
  after: ProviderRouteTarget | null;
  /** Owned by a `DEFAULT_*` environment variable, re-applied at the next restart. */
  env_managed: boolean;
  /**
   * For a `reseeded` route: true when it keeps its model on the replacement
   * (which offers it), false when it moves to the replacement's default model.
   */
  model_kept?: boolean | null;
}

/** The embedding default: cleared when affected, never switched to another model. */
interface ProviderDeletionEmbeddingRoute extends ProviderDeletionRoute {
  /** What embeddings fall back to while no embedding default is set. */
  fallback: ProviderRouteTarget | null;
  /** The fallback embeds with the model the stored vectors were built with. */
  fallback_same_model: boolean;
  /** Knowledge bases holding indexed chunks, which a new embedding default re-embeds. */
  knowledge_base_count: number;
  /**
   * No embedding default is stored and embeddings fall back to the deleted
   * provider today: they move to `fallback`; `previous_fallback` is today's.
   */
  fallback_moves?: boolean;
  previous_fallback?: ProviderRouteTarget | null;
}

/** Agents or users whose own model choice points at the deleted config. */
interface ProviderDeletionReferences {
  count: number;
  names: string[];
}

/** `GET /settings/llm-providers/{id}/deletion-impact` - computed by the same function the delete applies. */
export interface LLMProviderDeletionImpact {
  provider_id: string;
  provider: string;
  display_name: string | null;
  /** Where the chat defaults move: the requested config, else the rule's pick. */
  replacement: ProviderRouteTarget | null;
  replacement_explicit: boolean;
  /** The rule's pick: the first surviving chat-capable config, oldest first. */
  default_replacement_id: string | null;
  /** Surviving configs that can take over chat, each with its default chat model. */
  candidates: ProviderRouteTarget[];
  /** False when no surviving config can serve chat. */
  chat_available: boolean;
  /** Chat-capable survivors not offered as the replacement: they list no model and a moving default runs on one. */
  unlisted?: ProviderRouteTarget[];
  /** Where a chat call no stored default decides lands after the deletion: set when `chat_available` is false or `implicit_chat_moves` is true. */
  implicit_chat_fallback?: ProviderRouteTarget | null;
  /** No chat default is stored and chat implicitly goes to the deleted provider today (`previous_implicit_chat`). */
  implicit_chat_moves?: boolean;
  previous_implicit_chat?: ProviderRouteTarget | null;
  general_assistant: ProviderDeletionRoute;
  agent_default: ProviderDeletionRoute;
  image_default: ProviderDeletionRoute;
  embedding_default: ProviderDeletionEmbeddingRoute;
  routing_model: ProviderDeletionRoute;
  fallback_model: ProviderDeletionRoute;
  decision_model: ProviderDeletionRoute;
  agents: ProviderDeletionReferences;
  users: ProviderDeletionReferences;
  /** Sent back with the delete (`expected_impact`): it applies this impact or answers 409. */
  impact_fingerprint: string;
}

export interface EmbeddingModelTestResult {
  provider: string;
  model: string;
  capable: boolean;
  returned_dimensions: number | null;
  requested_dimensions: number | null;
  suggested_dimensions: number[];
  expected_dimensions: number | null;
  message: string;
}

export interface ProviderValidationResult {
  provider: string;
  capability: "reasoning" | "embedding" | "image" | "speech" | "video";
  connection_ok: boolean;
  models_supported: boolean;
  models: string[];
  /** Models grouped by upstream provider (set by the Filigran AI Gateway). */
  model_groups?: Record<string, string[]> | null;
  message: string;
}

export interface GeneralAssistantConfig {
  linked_agent_id: string | null;
  llm_provider: string | null;
  /** Which provider config serves `llm_model`. A model id alone is ambiguous. */
  llm_provider_id: string | null;
  llm_model: string | null;
  agent_default_provider: string | null;
  agent_default_provider_id: string | null;
  agent_default_model: string | null;
  default_reasoning_effort: string | null;
  embedding_provider: string | null;
  embedding_provider_id: string | null;
  embedding_model: string | null;
  embedding_dimensions: number | null;
  image_provider: string | null;
  image_provider_id: string | null;
  image_model: string | null;
  /** Video generation: the provider and model AI clips are made with. */
  video_provider?: string | null;
  video_provider_id?: string | null;
  video_model?: string | null;
  /** Speech generation: what the read-aloud and the Video Studio voice-over speak with. */
  speech_provider?: string | null;
  speech_provider_id?: string | null;
  speech_model?: string | null;
  /** A voice id or name of that model's provider; unset, its first voice. */
  speech_voice?: string | null;
  /** Music generation: what the Video Studio's AI tracks are composed with. */
  music_provider?: string | null;
  music_provider_id?: string | null;
  music_model?: string | null;
  /** Lightweight LLM for evaluating mandatory transfer routing conditions. */
  routing_model: string | null;
  routing_provider_id: string | null;
  /** Explicit model tried first when the primary model fails (rate limit / overloaded). */
  fallback_model: string | null;
  fallback_provider_id: string | null;
  /** Decision model (CLM) `act_on_page` chooses each action with; unset = the tool is off. */
  decision_model?: string | null;
  decision_provider_id?: string | null;
  /** Platform-wide default for max agentic loop iterations (chat + agent fallback). */
  max_iterations: number;
  system_prompt: string;
  knowledge_base_ids: string[];
  mcp_server_ids: string[];
  custom_tool_ids: string[];
  builtin_tool_ids: string[];
  /** The admin's skill choice; `null` = the built-in skills, those tagged `slug:<one of default_skill_slugs>`. */
  skill_ids?: string[] | null;
  default_skill_slugs?: string[];
  integration_bindings: Record<string, string[]>;
  integration_template_bindings: string[];
  is_builtin: boolean;
  handover_agent_ids: string[];
  handover_types: Record<string, string>;
  available_channel_types: string[] | null;
  /** Unified channel/bot configuration — same structure as Agent.channel_config. */
  channel_config: ChannelConfig | null;
  /** Agent UUIDs exposed via the global MCP endpoint. */
  mcp_exposed_agent_ids: string[];
  /** Display prefix of the global MCP API key (e.g. "mcp-abcdef12..."). */
  mcp_global_api_key_prefix: string | null;
  /** Global toggle for the platform MCP servers (/mcp/opencti, /mcp/openaev). */
  platform_mcp_enabled: boolean;
  /** Global toggle for XTM One's own MCP server (/mcp/xtm). Default off. */
  xtm_mcp_enabled: boolean;
  /**
   * The composer's reply suggestion after a chat turn, generated on the
   * routing model: nothing is generated while `routing_model` is unset.
   */
  reply_suggestions_enabled?: boolean;
  /**
   * Names of default-model fields unconditionally re-seeded from environment
   * variables on every startup and therefore read-only in the UI (e.g.
   * "agent_default_model", "default_reasoning_effort", "routing_model",
   * "image_model"). Mirrors the env-seeded LLM provider "Env" lock. Embedding
   * defaults are excluded — their seeding is conditional (fresh config only).
   */
  env_managed_fields: string[];
  /** Only on the answer to a save sent with `announce_default_model`: whether an email to the users was queued. */
  default_model_announced?: boolean | null;
}

/** A capability an announcement says arrived (`previous_model` null) or changed model. */
interface CapabilityNews {
  capability: "image" | "video" | "speech" | "music" | "decision";
  model: string;
  provider: string;
  previous_model: string | null;
}

/** The latest model announcement emailed to the users and how far it went. */
export interface ModelAnnouncementSummary {
  id: string;
  /** `env`: the env seed changed the catalog at a boot; `admin`: Settings > AI and models > Models. */
  source: "env" | "admin";
  status: "pending" | "sending" | "sent" | "skipped" | "expired" | "failed";
  skip_reason: string | null;
  default_model: string | null;
  previous_default_model: string | null;
  added_models: string[];
  removed_models: string[];
  capabilities: CapabilityNews[];
  created_at: string;
  sent_at: string | null;
  delivered: number;
  refused: number;
  unknown: number;
  /** Pending / sending / failed: the users it has not reached yet, those being emailed right now included. */
  remaining: number;
  /** Pending / sending: the open announcements recorded before it, sent first. */
  queued_behind: number;
  /** Pending after a failed batch: when the next batch may run. */
  retry_at: string | null;
  /** Pending / sending / failed: the failed batches in a row and the last SMTP error. */
  failed_attempts: number;
  last_error: string | null;
  /** Pending env change waiting for the deployed models to stop changing: when it may go out. */
  held_until: string | null;
  /** Pending / sending behind an older announcement: when that one's next attempt runs, if it waits for one. */
  blocked_until: string | null;
  /**
   * Pending / sending: the addresses the email server refused for now (counted in `remaining`), retried once after
   * everyone else; when that retry runs, once nobody else is left, or whether it is under way; the server's last reply
   * to one of them. Never a failed batch.
   */
  deferred: number;
  deferred_retry_at: string | null;
  deferred_retrying: boolean;
  last_refusal: string | null;
}

export interface ModelAnnouncementStatus {
  email_configured: boolean;
  /** `MODEL_ANNOUNCEMENT_EMAILS_DISABLED`: no announcement may be emailed on this platform, whatever SMTP. */
  emails_disabled: boolean;
  /** Active users an announcement goes to (the bootstrap admin excluded). */
  recipient_count: number;
  /** How many users the scheduler emails per batch, one batch a minute. */
  emails_per_minute: number;
  default_model: string | null;
  default_provider: string | null;
  /** The default the current one replaced, as "Email users" would say it. */
  previous_default_model: string | null;
  latest: ModelAnnouncementSummary | null;
  /** The model changes saved and not emailed yet, combined over `changes_window_hours`. */
  unannounced_change: ModelCatalogChange | null;
  /** `MODEL_CATALOG_CHANGES_WINDOW_HOURS`: how far back the saves are combined. */
  changes_window_hours: number;
  /** What an announcement may name as new: the chat models the users can pick now. */
  offered_models: CatalogModelRef[];
  /** Suggestions for a retired model: the models the platform recorded once and no longer offers. */
  formerly_offered_models: CatalogModelRef[];
}

/** A chat model and the provider offering it (`""` when the platform never recorded one). */
export interface CatalogModelRef {
  id: string;
  provider: string;
}

/** A model announced as retired and the provider the email names (`""`: the platform's guess). */
export type RetiredModelChoice = CatalogModelRef;

/** One configured provider's own model list: how many chat models it serves, or why it could not say. */
export interface ProviderCatalogSummary {
  provider_id: string;
  provider: string;
  label: string | null;
  model_count: number;
  error: string | null;
  /** It served more chat models than are listed: `model_count` is the cap, the first ones in id order. */
  truncated: boolean;
}

/** The chat models the configured providers serve that the users cannot pick, and how each provider answered. */
export interface ModelAnnouncementCatalogs {
  models: CatalogModelRef[];
  providers: ProviderCatalogSummary[];
}

/** An announcement email rendered and not sent: what the users would receive. */
export interface ModelAnnouncementPreview {
  subject: string;
  html: string;
  text: string;
}

/** The chat models the admin's provider saves added or retired for the users, not emailed yet. */
export interface ModelCatalogChange {
  /** The latest save it covers: emailing it consumes the saves up to it. */
  id: string;
  added_models: string[];
  removed_models: string[];
  added: CatalogModelRef[];
  removed: CatalogModelRef[];
  /** The latest save it covers and the first one. */
  created_at: string;
  first_saved_at: string;
  /** How many saves it combines. */
  saves: number;
}

// Integrations
/** Channel whitelist entry for bot integrations. */
export interface AllowedChannel {
  id: string;
  name: string;
  group: string;
}

/** Whose account the people an OAuth integration is shared with act with:
 * the share choice as stored - `shared` (its connected account) or
 *  `per_user` ("Don't share my connected account": each authorizes their own). */
type CredentialMode = "shared" | "per_user";

/** A person's own account on a `per_user` integration: its state, never its secret. */
interface UserCredentialStatus {
  /** `connected` / `error` / `reauthorize`. */
  status: string;
  has_access_token: boolean;
  token_expires_at: string | null;
  connected_at: string | null;
  updated_at: string | null;
}

/** One person who connected their own account, as the integration's managers see it. */
export interface IntegrationUserCredential extends UserCredentialStatus {
  user_id: string;
  user_name: string | null;
  user_email: string | null;
}

/** The Email signature setting of an integration whose tools send email. */
interface IntegrationSignatureSetting {
  enabled: boolean;
  /** The signature integration read; null = picked when the tools are built. */
  integration_id: string | null;
}

/** What an agent's binding says about its integration's Email signature setting. */
export type SignatureMode = "inherit" | "on" | "off";

export interface IntegrationSignatureOverride {
  signature_mode: SignatureMode;
  signature_integration_id?: string;
}

/** A signature integration the viewer may choose. */
interface SignatureIntegrationOption {
  id: string;
  name: string;
  provider: string;
}

/** What a sending integration's tools sign with, as resolved for the viewer. */
export interface ResolvedSignature {
  enabled: boolean;
  integration_id: string | null;
  integration_name: string | null;
  /** `binding` or `integration` when signatures are off. */
  off_by: "binding" | "integration" | null;
  /** Why no signature integration resolves, and how to set one. */
  missing: string | null;
}

/** `GET /integrations/{id}/email-signature`. */
export interface IntegrationEmailSignature {
  enabled: boolean;
  integration_id: string | null;
  integration_name: string | null;
  resolved: ResolvedSignature;
  signature_integrations: SignatureIntegrationOption[];
  can_manage: boolean;
}

/** One email-sending binding of an agent: its override, what it inherits, and the result. */
export interface AgentBindingSignature {
  integration_id: string;
  integration_name: string;
  provider: string;
  signature_mode: SignatureMode;
  signature_integration_id: string | null;
  inherited: ResolvedSignature;
  effective: ResolvedSignature;
}

/** `GET /agents/{id}/email-signatures`. */
export interface AgentEmailSignatures {
  bindings: AgentBindingSignature[];
  signature_integrations: SignatureIntegrationOption[];
}

/** The last token request an inbound `oauth_a2a` credential refused (#105).
 *  `reason` is a code the UI translates: secret_mismatch, unknown_client or
 *  no_reachable_agent; `what_to_do` is the backend's English fallback. */
export interface A2ARefusedTokenRequest {
  at: string;
  client_id: string | null;
  reason: string | null;
  what_to_do: string | null;
}

export interface Integration {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  provider: string;
  auth_type: string;
  status: string;
  /** The last connectivity test: `untested` (a typed credential nobody tested), `ok` or `error`; null before any (#11837). */
  connection_test_status?: string | null;
  name: string;
  description: string | null;
  is_company_managed: boolean;
  is_builtin: boolean;
  is_custom: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  /** Whether the requesting user may run the OAuth flow, computed server-side.
   * Read it through `canAuthorizeIntegration()`. */
  can_authorize?: boolean;
  /** The provider is registration-only on this platform (XTM One): created
   *  by registering the platform, never edited here. */
  is_registration_only?: boolean;
  /** A2A only: which way the partner relationship points, from its settings
   *  row; absent or null for every other provider. */
  a2a_direction?: "inbound" | "outbound" | null;
  /** A2A credential state, single-integration read only. Inbound, read by a
   *  manager: the last token request the credential refused (#105). */
  a2a_credential?: {
    usable?: boolean;
    last_token_minted_at?: string | null;
    last_error?: { code: string; detail: string | null; at: string | null } | null;
    last_refused_token_request?: A2ARefusedTokenRequest;
  } | null;
  tags: string[];
  scopes: string | null;
  /** Latest scopes from the provider catalog (for drift detection). */
  required_scopes: string | null;
  /** The scopes the stored token was granted (token endpoint `scope`); null when the provider did not say (#9776). */
  granted_scopes?: string[] | null;
  /** Requested scopes the stored token was not granted (a declined consent box); null when the grant is unknown (#9776). */
  missing_scopes?: string[] | null;
  /** OAuth endpoints (public URLs; OpenAPI integrations pre-fill them from the spec). */
  authorize_url?: string | null;
  token_url?: string | null;
  has_client_id: boolean;
  has_client_secret: boolean;
  has_access_token: boolean;
  has_api_key: boolean;
  has_extra_config: boolean;
  /** Whether this shared OAuth integration shares its owner's connected
   *  account with everyone it is shared with: recorded when it is shared,
   *  cleared when the owner opts out or it stops being shared. */
  oauth_share_acknowledged: boolean;
  /** Whose account the people it is shared with act with: `shared`
   *  (its connected account) or `per_user` (shared without it: each person
   *  authorizes their own). Optional for older backends, read as `shared`. */
  credential_mode?: CredentialMode;
  /** `per_user` only: the requesting user acts with their own account here
   *  (everyone but the holder of the integration's own). */
  uses_own_credential?: boolean;
  /** `per_user` only: the requesting user's own account on it, if any. */
  my_credential?: UserCredentialStatus | null;
  /** `per_user` only, for its managers: how many people connected their own account. */
  connected_user_count?: number | null;
  /** Whether this integration's tools pause for human approval.
   *  One answer for the whole connection — per-tool granularity is deferred. */
  requires_approval: boolean;
  /** True when a registered Suite platform decides the above, in which case the
   *  per-integration control is not editable here: the platform's own policy is
   *  what the gate enforces, and sending `requires_approval` returns 409. */
  approval_managed_by_platform: boolean;
  /** The Email signature setting, only on an integration whose tools send email. */
  signature?: IntegrationSignatureSetting | null;
  /** The stored OAuth token was connected by someone else than the owner. */
  oauth_connected_by_other?: boolean;
  oauth_token_owner_name?: string | null;
  /** The token's recorded owner when not the integration's owner (may consent to share it). */
  oauth_token_owner_id?: string | null;
  /** Whether it runs on the admin's OAuth app: the recorded value, or for an
   *  older integration its client id compared with the current OAuth default;
   *  null only when that cannot be read. Read it through
   *  `usesAdminOAuthApp()`, never directly. */
  uses_admin_oauth_credentials?: boolean | null;
  extra_config: Record<string, unknown> | null;
  /** The secret fields read from a vault, and their recent failures. */
  vault_secrets?: VaultSecretStatus | null;
  scope_config: { match_key: string; resolve_from_header?: string; resolve_from_platform?: string } | null;
  /** The relay group its requests are sent from; null = directly
   *  from the platform. Optional for older backends. */
  relay_group_id?: string | null;
  call_count: number;
  last_called_at: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Whether a provider is safe to bind in "resolved per user at runtime" mode.
 *
 * A per-user binding uses the acting user's OWN integration, falling back to a
 * genuine company-wide instance — never a colleague's personal account. With
 * neither, the provider silently contributes no tools, which the agent config
 * UI must say up front.
 */
interface PerUserProviderReadiness {
  provider: string;
  /** A connected company-managed / platform-managed instance exists. */
  has_company_wide_fallback: boolean;
  company_wide_instance_count: number;
  /** The current user has their own connected instance of this provider. */
  user_has_own_instance: boolean;
  /** Per-user resolution would find an integration for the current user. */
  resolvable_for_user: boolean;
  /** Owners of connected PERSONAL instances group-shared to the team while no
   *  company-wide instance exists — a colleague's account looking like the
   *  shared one. Never used by per-user resolution. */
  de_facto_shared_owners: string[];
}

export interface PerUserReadinessResponse {
  providers: PerUserProviderReadiness[];
}

// Who can act with an integration's stored credential
type CredentialKind = "none" | "oauth_account" | "api_key" | "bot_token" | "platform_managed";

type ExposurePathKind =
  | "company"
  | "group"
  | "people"
  | "agent"
  | "assignment"
  | "flow"
  | "knowledge_base"
  | "skill"
  | "general_assistant"
  | "personal_assistant";

interface ExposureAudience {
  everyone: boolean;
  people_count: number;
  people: { id: string; name: string }[];
  groups: { id: string; name: string; member_count: number }[];
  public_link: boolean;
  anonymous_channels: string[];
  mcp_key_holders: boolean;
}

// Every resource a person lends through something shared (/exposure/*)
export type LentResourceKind = "integration" | "mcp_server" | "custom_tool" | "channel_bot" | "variable";

/** What a lent resource holds that its audience acts with (never the value). */
type LentSecretKind =
  | CredentialKind
  | "mcp_oauth_token"
  | "mcp_headers"
  | "mcp_env"
  | "code"
  | "secret_value"
  | "plain_value";

export type ResourceExposurePathKind = ExposurePathKind | "prompt" | "custom_tool";

type ResourceExposureNote =
  | "chat_uses_own_integration"
  | "variable_resolves_for_viewers_and_runs"
  | "account_not_shared"
  | "share_consent_refused"
  | "owner_lost_access"
  | "per_user_resolves_to_acting_user";

type ResourceOptOut = "withhold_connected_account" | "per_user_binding" | "company_resource";

export interface ResourceAudience extends ExposureAudience {
  a2a_partners: boolean;
  webhook_callers: boolean;
}

interface LentSecret {
  kind: LentSecretKind;
  holder_id: string | null;
  holder_name: string | null;
  is_company: boolean;
  account_shared: boolean;
  scopes: string[];
  is_yours?: boolean | null;
}

interface LentResourceRef {
  kind: LentResourceKind;
  id: string | null;
  name: string | null;
  provider: string | null;
  auth_type: string | null;
  is_company_managed: boolean;
  owner_id: string | null;
  owner_name: string | null;
}

interface ResourceExposureEntity {
  type: ResourceExposurePathKind;
  id: string | null;
  name: string | null;
  visible: boolean;
  is_company_managed: boolean;
  owner_name: string | null;
}

export interface ResourceExposurePath {
  kind: ResourceExposurePathKind;
  reasons: string[];
  restricted: boolean;
  entity: ResourceExposureEntity | null;
  parent: ResourceExposureEntity | null;
  usable: boolean;
  /** An agent bound to an integration its owner can no longer see: its runs
   *  refuse the integration's tools until the access or the binding changes. */
  orphaned?: boolean;
  notes: ResourceExposureNote[];
  audience: ResourceAudience;
  /**
   * Who acts with what the resource stores through this path (carrier paths
   * only): `audience`, except an agent's chat audience where its chats use
   * their own integration.
   */
  lent?: ResourceAudience | null;
  /** The part of `audience` the resource's own shares do not already reach. */
  beyond_direct?: ResourceAudience | null;
}

/** `GET /exposure/{resources}/{id}`: every path through which a resource reaches people. */
export interface ResourceExposure {
  resource: LentResourceRef;
  secret: LentSecret;
  direct: ResourceAudience;
  paths: ResourceExposurePath[];
  reached: ResourceAudience;
  /** The agents bound to the integration whose owner can no longer see it. */
  orphaned_agents?: ResourceExposureEntity[];
  truncated: boolean;
}

export interface ResourceExposureSummary {
  id: string;
  kind: LentResourceKind;
  secret_kind: LentSecretKind;
  is_company: boolean;
  account_shared: boolean;
  ways: number;
  counts: Partial<Record<ResourceExposurePathKind, number>>;
  public_link: boolean;
}

type ResourcePreviewEntityType =
  | LentResourceKind
  | "agent"
  | "assignment"
  | "flow"
  | "knowledge_base"
  | "skill"
  | "prompt"
  | "general_assistant"
  | "group"
  | "platform_mcp";

/** `POST /exposure/preview`: a proposed change. `change` holds only the fields it
 *  sets, plus `new_owner_id` (owner transfer), `delete: true` (deletion) and,
 *  when creating a resource, `name` / `holds_secret`. */
export interface ResourcePreviewRequest {
  entity_type: ResourcePreviewEntityType;
  entity_id?: string | null;
  parent_id?: string | null;
  change: Record<string, unknown>;
}

/** One resource the change lends to new people (`gained`) or takes from people
 *  who could use it (`lost`). */
export interface ResourcePreviewItem {
  resource: LentResourceRef;
  secret: LentSecret;
  via: { type: ResourcePreviewEntityType; id: string | null; name: string | null } | null;
  gained: ResourceAudience;
  lost: ResourceAudience;
  notes: ResourceExposureNote[];
  opt_outs: ResourceOptOut[];
  /** Agents bound to the integration whose owner the change takes it from:
   *  their runs would refuse its tools. */
  orphaned_agents?: ResourceExposureEntity[];
}

export interface ResourcePreviewResponse {
  items: ResourcePreviewItem[];
  requires_confirmation: boolean;
  truncated: boolean;
}

// Channel Bots (separated from Integrations)
export interface ChannelBot {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  is_company_managed: boolean;
  allow_anonymous: boolean;
  allow_private: boolean;
  only_mention: boolean;
  /** Whether this bot's tools pause for approval. Per bot, not per
   *  provider: two Slack bots can disagree, and the switch is per bot. */
  requires_approval: boolean;
  provider: string;
  name: string;
  status: string;
  has_api_key: boolean;
  has_client_id: boolean;
  has_client_secret: boolean;
  has_extra_config: boolean;
  allowed_receive_channels: AllowedChannel[] | null;
  allowed_publish_channels: AllowedChannel[] | null;
  /** The token / app password read from a vault, and their recent failures. */
  vault_secrets?: VaultSecretStatus | null;
  created_at: string;
  updated_at: string;
}

export interface ChannelBotCatalogItem {
  provider: string;
  name: string;
  fields: string[];
  extra_fields: Record<string, string> | null;
  description: string | null;
  available_tools: ProviderTool[];
  webhook_path: string | null;
  setup_guide: SetupGuide | null;
}

export interface CreateChannelBotRequest {
  provider: string;
  name: string;
  is_company_managed?: boolean;
  api_key?: string;
  client_id?: string;
  client_secret?: string;
  extra_config?: Record<string, string>;
  allow_anonymous?: boolean;
  allow_private?: boolean;
  only_mention?: boolean;
  requires_approval?: boolean;
  allowed_receive_channels?: AllowedChannel[] | null;
  allowed_publish_channels?: AllowedChannel[] | null;
}

export interface UpdateChannelBotRequest {
  name?: string;
  is_company_managed?: boolean;
  api_key?: string;
  client_id?: string;
  client_secret?: string;
  extra_config?: Record<string, string>;
  allow_anonymous?: boolean;
  allow_private?: boolean;
  only_mention?: boolean;
  requires_approval?: boolean;
  allowed_receive_channels?: AllowedChannel[] | null;
  allowed_publish_channels?: AllowedChannel[] | null;
}

interface SetupGuidePermission {
  scope: string;
  description: string;
}

/** The open-source project an integration is built on, credited in its guide. */
interface SetupGuideOpenSource {
  /** The project's own name, which the guide links: not the integration's. */
  name: string;
  url: string;
  author: string | null;
  license: string | null;
}

export interface SetupGuide {
  title: string;
  url: string | null;
  steps: string[];
  permissions: SetupGuidePermission[];
  apis_to_enable: string[] | null;
  notes: string | null;
  manifest_example: string | null;
  open_source: SetupGuideOpenSource | null;
}

interface ProviderEvent {
  event: string;
  label: string;
  description: string;
  mechanism: "polling" | "webhook" | "hybrid";
  /** The `trigger_config.filter` keys the event takes, each with what it does. */
  filter_keys?: Record<string, string>;
}

export interface ProviderTool {
  name: string;
  description: string;
  tool_type: "read" | "write";
}

export interface IntegrationToolInfo {
  name: string;
  description: string;
  input_schema?: JsonSchema | null;
  /** Set for integrations whose tools come from their own imported spec. */
  tool_type?: "read" | "write";
}

export interface BuiltinSubTool {
  name: string;
  description: string;
  input_schema?: JsonSchema | null;
}

/** One built-in suite as `GET /settings/builtin-tools/groups` serves it
 *  (`backend/app/services/builtin_tool_groups.py`). `name`, `tagline`,
 *  `detail` and `category` are i18n keys; `detail` may name the platform as `{name}`. */
export interface BuiltinToolGroup {
  id: string;
  name: string;
  tagline: string;
  detail: string;
  category: string;
  /** A lucide-react icon name. */
  icon: string;
  gate: "code_interpreter" | "runners" | "dashboard_studio" | "health" | "flow_builder" | "deep_investigation" | "video_studio" | "mobile_companion" | null;
  default_on: boolean;
  admin_only: boolean;
  /** Only an administrator adds it to an agent: a picker shows it locked to anyone else. */
  bind_admin_only?: boolean;
  agent_keyed: boolean;
  /** A Platform Health specialist's suite, carried by the Platform Health fleet's own agents: no session picker offers it. */
  health_only: boolean;
  named_after_platform: boolean;
  aliases: string[];
  /** False while this deployment withholds the suite (a flag off): its functions are then empty. */
  offered: boolean;
  functions: string[];
  function_count: number;
  /** Characters the functions cost on the wire (name, description, input schema). */
  schema_chars: number;
}

export interface ProviderElementType {
  type: string; // e.g. "teams_channel", "slack_channel", "repository", "organization"
  name: string;
  description: string;
  multiple: boolean;
  /** When true, users can type a value manually in addition to selecting from the API list. */
  allow_manual_input?: boolean;
}

export interface IntegrationElement {
  id: string;
  name: string;
  group: string;
  metadata: Record<string, unknown>;
}

export interface ManagedWebhook {
  hook_id: number;
  repo: string;
  events: string[];
  target: "integration" | "assignment";
  assignment_id: string | null;
  webhook_url: string;
  created_at: string;
}

interface ExtraFieldOption {
  value: string;
  label: string;
}

/** The facets of a catalog entry that one selector value changes. */
export interface ProviderCatalogVariant {
  extra_fields?: Record<string, string> | null;
  setup_guide?: SetupGuide | null;
}

export interface ProviderCatalogItem {
  provider: string;
  name: string;
  category: "integration" | "channel";
  integration_category: string;
  auth_type: string;
  /** When present, the UI shows an auth mode selector (e.g. ["api_key", "platform_managed"]). */
  supported_auth_types?: string[];
  fields: string[];
  /** A2A only: which directions this mechanism can serve. Absent for every
   * other provider. */
  a2a_directions?: ("inbound" | "outbound")[];
  /** Reached only through a relay group (LM Studio): no "Direct" route, the server refuses a save without one. */
  requires_relay?: boolean;
  /** Created by an administrator as a company-managed integration only (Platform email): the switch is on and locked. */
  company_managed_only?: boolean;
  /** Which operator choice reshapes this entry (`a2a_direction` for A2A), and
   * the facets each of its values changes. The base entry is the default. */
  variant_selector?: string | null;
  variants?: Record<string, ProviderCatalogVariant> | null;
  extra_fields: Record<string, string> | null;
  extra_field_options: Record<string, ExtraFieldOption[]> | null;
  /** Which extra fields belong to which answer: {field: {control: [values]}}.
   *  A field whose condition is unmet is hidden and omitted when saving. */
  extra_field_visible_when?: Record<string, Record<string, string[]>> | null;
  /** The typed control of an extra field (a domain list as tags, a bounded number); absent, a text input. */
  extra_field_inputs?: Record<string, ExtraFieldInputSpec> | null;
  description: string | null;
  logo: string | null;
  integration_types: string[];
  available_events: ProviderEvent[];
  available_tools: ProviderTool[];
  /** A2A only. What an INBOUND binding gives the agents it exposes. */
  a2a_inbound_agent_tools?: ProviderTool[];
  /** A2A only. What an OUTBOUND binding exposes to a partner. */
  a2a_outbound_exposed?: ProviderTool[];
  element_types: ProviderElementType[];
  webhook_path: string | null;
  needs_shared_webhook: boolean;
  supports_assignment_webhooks: boolean;
  supports_webhook_management: boolean;
  setup_guide: SetupGuide | null;
  has_oauth_defaults: boolean;
  has_oauth_extra_config: boolean;
  is_custom?: boolean;
  /** Set when the tools come from a document imported per integration
   *  ("openapi"): the creation form asks for the spec, and tool listings read
   *  the integration's own tools instead of `available_tools`. */
  spec_import?: "openapi" | null;
  settings_schema?: {
    key: string;
    label: string;
    type: IntegrationBundleSettingType;
    required: boolean;
    placeholder?: string;
    options?: ExtraFieldOption[];
  }[];
  /** True for bot-type providers with ambient channel access (security-sensitive). */
  is_bot: boolean;
  /** True when admin has globally disabled this provider from the catalog. */
  is_disabled: boolean;
  /** True when XTM One qualifies the provider as Certified (the backend's one list); false for a custom provider. */
  is_certified?: boolean;
  /** True for providers that can only be added via platform registration (XTM One mode), from the backend's one list. */
  is_registration_only: boolean;
  /** Extra fields an admin still edits on a registration-only integration (e.g. the OpenAEV tenant); the registration keeps them. */
  registration_editable_extra_fields?: string[] | null;
}

// SSO Configuration (admin-managed)
export interface SSOConfig {
  oidc_enabled: boolean;
  oidc_display_name: string;
  oidc_has_client_id: boolean;
  oidc_has_client_secret: boolean;
  /** The OIDC client secret read from a vault. */
  vault_secrets?: VaultSecretStatus | null;
  oidc_discovery_url: string;
  oidc_authorization_url: string;
  oidc_token_url: string;
  oidc_userinfo_url: string;
  oidc_scopes: string;
  saml_enabled: boolean;
  saml_display_name: string;
  saml_idp_metadata_url: string;
  saml_sp_entity_id: string;
  saml_requested_authn_context: boolean;
  saml_groups_mapping_enabled: boolean;
  saml_groups_auto_create: boolean;
  oidc_auto_admin_new_users: boolean;
  saml_auto_admin_new_users: boolean;
  oidc_auto_create_users: boolean;
  saml_auto_create_users: boolean;
  saml_entity_id: string;
  saml_acs_url: string;
  saml_metadata_url: string;
  local_auth_enabled: boolean;
  registration_enabled: boolean;
  allowed_domains: string;
  /** `allowed_domains` as self-registration and SSO account creation read it: lower-cased, blanks dropped.
   *  Optional for backward-compatibility with older backend payloads; read a missing value as empty. */
  allowed_domain_list?: string[];
}

export interface SSOOIDCTestResult {
  ok: boolean;
  /** "failed" when the provider looks down, "misconfigured" when the URL is wrong. */
  status: "ok" | "failed" | "misconfigured";
  detail: string;
}

export interface UpdateSSOConfigRequest {
  oidc_enabled: boolean;
  oidc_display_name: string;
  oidc_client_id: string;
  oidc_client_secret: string;
  oidc_discovery_url: string;
  oidc_authorization_url: string;
  oidc_token_url: string;
  oidc_userinfo_url: string;
  oidc_scopes: string;
  saml_enabled: boolean;
  saml_display_name: string;
  saml_idp_metadata_url: string;
  saml_sp_entity_id: string;
  saml_requested_authn_context: boolean;
  saml_groups_mapping_enabled: boolean;
  saml_groups_auto_create: boolean;
  oidc_auto_admin_new_users: boolean;
  saml_auto_admin_new_users: boolean;
  oidc_auto_create_users: boolean;
  saml_auto_create_users: boolean;
  local_auth_enabled: boolean;
  registration_enabled: boolean;
  allowed_domains: string;
}

// OAuth Defaults (admin-managed)
export interface OAuthDefault {
  id: string;
  provider: string;
  has_client_id: boolean;
  has_client_secret: boolean;
  has_extra_config: boolean;
  /** Secret extra-config keys holding a stored value (names only). */
  stored_secret_keys?: string[];
  created_at: string;
  updated_at: string;
}

export interface CreateIntegrationRequest {
  id?: string;  // Pre-generated UUID (enables webhook URL preview before creation)
  provider: string;
  name: string;
  description?: string;
  /** A2A only: which way this partner relationship points. Exactly one —
   * inbound exposes the agents it is bound to, outbound gives them a
   * delegation tool. Required for an A2A provider; the backend defaults to
   * `inbound` when absent, so omitting it silently makes an inbound one. */
  a2a_direction?: "inbound" | "outbound";
  is_company_managed?: boolean;
  tags?: string[];
  auth_type?: string;
  client_id?: string;
  client_secret?: string;
  api_key?: string;
  extra_config?: Record<string, unknown>;
  /** Whether a shared OAuth integration shares its connected account.
   *  Omitted or true: it does, the default. false: the opt-out - the
   *  integration stays shared and its stored token is removed. */
  keep_oauth_credentials_on_share?: boolean;
  /** OAuth URL / scope overrides (OpenAPI integrations pre-fill them from the spec). */
  authorize_url?: string;
  token_url?: string;
  scopes?: string;
  /** OpenAPI integrations only (required there): the spec and the preview's choices. */
  openapi?: OpenAPIImportOptions;
  /** See `Integration.relay_group_id`. */
  relay_group_id?: string | null;
}

export interface UpdateIntegrationRequest {
  name?: string;
  description?: string;
  auth_type?: string;
  is_company_managed?: boolean;
  /** Owner transfer (admin only). */
  user_id?: string | null;
  tags?: string[];
  client_id?: string;
  client_secret?: string;
  api_key?: string;
  extra_config?: Record<string, unknown>;
  /** OAuth endpoint / scope edits (the backend accepts them for every provider). */
  authorize_url?: string;
  token_url?: string;
  scopes?: string;
  /** See ``CreateIntegrationRequest.keep_oauth_credentials_on_share``. */
  keep_oauth_credentials_on_share?: boolean;
  /** Whether this integration's tools pause for human approval.
   *  Refused with 409 for a provider a registered platform governs. */
  requires_approval?: boolean;
  /** The Email signature setting, on an integration whose tools send email.
   *  `signature_integration_id: null` picks the signature integration automatically. */
  signature_enabled?: boolean;
  signature_integration_id?: string | null;
}

// Integration bundles (admin import/export)
type IntegrationBundleSettingType = "string" | "secret" | "boolean" | "select";

export interface IntegrationBundleImportResponse {
  provider: string;
  version: string;
  upgraded: boolean;
  name: string;
  integration_created: boolean;
  integration_id: string | null;
  integration_name: string | null;
}

// OpenAPI / Swagger integrations (the spec is imported per integration)
export type OpenAPIAuthKind =
  | "none"
  | "api_key"
  | "bearer"
  | "basic"
  | "oauth2_authorization_code"
  | "oauth2_implicit"
  | "oauth2_device"
  | "oauth2_client_credentials"
  | "oauth2_password"
  | "mutual_tls"
  | "unsupported";

export interface OpenAPIAuthOption {
  id: string;
  kind: OpenAPIAuthKind;
  label: string;
  scheme_name?: string | null;
  /** api_key only: where the key is sent, and under which name. */
  location?: "header" | "query" | "cookie" | null;
  param_name?: string | null;
  authorize_url?: string | null;
  token_url?: string | null;
  /** Device authorization only: where the sign-in code is requested. */
  device_authorization_url?: string | null;
  scopes: Record<string, string>;
  /** Scopes to request by default when not every declared one should be (OpenID Connect). */
  default_scopes?: string[] | null;
  description?: string | null;
  supported: boolean;
  unsupported_reason?: string | null;
  /** The spec marks the scheme deprecated (OpenAPI 3.2). */
  deprecated?: boolean;
  /** The integration auth_type storing this option's credential. */
  integration_auth_type: string;
}

export interface OpenAPIOperation {
  /** "GET /pets/{petId}" — the stable identity of an operation. */
  key: string;
  method: string;
  path: string;
  operation_id?: string | null;
  /** The tool's name and description as agents see them. */
  tool_name?: string | null;
  tool_description?: string | null;
  /** What the conversion generated, before any user override. */
  default_tool_name?: string | null;
  default_tool_description?: string | null;
  summary: string;
  tags: string[];
  deprecated: boolean;
  tool_type: "read" | "write";
  /** False for `security: []` operations: they never carry the credential. */
  sends_credential: boolean;
  supported: boolean;
  skip_reason?: string | null;
  /** Whether a tool argument carries a file. */
  accepts_files?: boolean;
  warnings: string[];
  selected: boolean;
}

/** A tool name and description set by the user (absent = the generated one). */
export interface OpenAPIToolOverride {
  name?: string;
  description?: string;
}

/** Tool overrides by operation key. */
export type OpenAPIToolOverrides = Record<string, OpenAPIToolOverride>;

interface OpenAPIServer {
  url: string;
  description?: string | null;
  name?: string | null;
}

export interface OpenAPITag {
  name: string;
  description?: string | null;
  /** OpenAPI 3.2: a short display name, and the tag this one nests under. */
  summary?: string | null;
  parent?: string | null;
}

/**
 * Where a spec comes from: exactly one of `url`, `content` (a file's text)
 * or `archive` (a base64 ZIP of a spec split across files).
 */
export interface OpenAPISourceRequest {
  url?: string;
  content?: string;
  archive?: string;
  filename?: string;
  tool_prefix?: string;
}

/** The client certificate an OpenAPI integration presents (never its key). */
export interface ClientCertificateInfo {
  subject: string;
  issuer: string;
  not_before: string;
  not_after: string;
  fingerprint_sha256: string;
  expired: boolean;
  /** Expires within 30 days. */
  expires_soon: boolean;
  chain_length: number;
}

/** What the user needs to sign in with a code (OAuth2 device authorization). */
export interface DeviceAuthorization {
  flow_id: string;
  user_code: string;
  verification_uri: string;
  verification_uri_complete?: string | null;
  expires_in: number;
  interval: number;
}

export interface DevicePollResult {
  status: "pending" | "connected" | "denied" | "expired" | "failed";
  interval?: number | null;
  message?: string | null;
}

export interface OpenAPIImportOptions extends OpenAPISourceRequest {
  selected_operations?: string[];
  auth_option?: string;
  tool_overrides?: OpenAPIToolOverrides;
}

export interface OpenAPIPreview {
  spec_version: string;
  document_format: "json" | "yaml";
  title: string;
  api_version: string;
  description: string;
  source_url?: string | null;
  servers: OpenAPIServer[];
  default_server_url?: string | null;
  auth_options: OpenAPIAuthOption[];
  default_auth_option: string;
  tool_prefix: string;
  tags: OpenAPITag[];
  operations: OpenAPIOperation[];
  default_selection: string[];
  warnings: string[];
  operation_count: number;
  supported_count: number;
}

export interface OpenAPISpec {
  integration_id: string;
  source_type: "url" | "file";
  source_url?: string | null;
  source_filename?: string | null;
  spec_version: string;
  document_format: "json" | "yaml";
  title: string;
  api_version: string;
  description?: string | null;
  default_server_url?: string | null;
  tool_prefix: string;
  auth_option?: OpenAPIAuthOption | null;
  auth_options: OpenAPIAuthOption[];
  tags: OpenAPITag[];
  operations: OpenAPIOperation[];
  selected_count: number;
  supported_count: number;
  warnings: string[];
  /** The client certificate presented on every call (mutual TLS). */
  client_certificate?: ClientCertificateInfo | null;
  created_at?: string | null;
  updated_at?: string | null;
  refreshed_at?: string | null;
}

export interface OpenAPIRefreshResult {
  changed: boolean;
  /** Operations now in the spec that are not selected (new ones stay off). */
  added_operations: string[];
  /** Previously selected operations that are gone or no longer supported. */
  removed_operations: string[];
  spec: OpenAPISpec;
}

// Agent Templates (DB-backed, admin-managed)
/** One thing a template leaves for the user to configure — drives a mandatory
 *  step item of the creation wizard. */
export interface TemplateSetupRequirement {
  kind: "integration" | "knowledge_base" | "persona_field";
  /** What the entry points at (an integration type, a KB name, a field). */
  target: string;
  /** Short text shown in the wizard step. */
  instruction: string;
}

export interface AgentTemplate {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  icon: string;
  category: string;
  disable_chat: boolean;
  required_integrations: string[] | null;
  required_integration_types: string[] | null;
  recommended_mcp: string[] | null;
  must_be_defined: TemplateSetupRequirement[] | null;
  default_persona: string | null;
  /** Built-in skill slugs an agent created from the template starts with. */
  default_skills: string[] | null;
  /** Those skills' ids for the caller (the ones it may attach), in order: what the wizard preselects. */
  default_skill_ids: string[];
  assignment_count: number;
  is_builtin: boolean;
  enabled: boolean;
}

/** The agent wizard assistant's change to the draft (AGENT_WIZARD_ASSIST): only
 *  the fields it changes, each list whole, every id one the wizard offers. */
export interface AgentWizardPatch {
  /** Before the wizard starts only: the template to start from, null = blank. */
  template_slug?: string | null;
  name?: string;
  icon?: string;
  description?: string;
  persona?: string;
  tool_ids?: string[];
  integration_ids?: string[];
  knowledge_base_ids?: string[];
  /** `"<provider_id>::<model>"`, or "" for the platform default. */
  model?: string;
  memory?: { short?: boolean | null; medium?: boolean | null; persistent?: boolean | null; user?: boolean | null };
  guardrails?: string;
  skill_ids?: string[];
  handover_agent_ids?: string[];
  /** A limit set to null goes back to the platform's. */
  budget?: {
    max_cost_usd?: number | null;
    max_total_tokens?: number | null;
    max_tool_calls?: number | null;
    max_duration_seconds?: number | null;
  };
  focus_step?:
    | "identity" | "persona" | "tools" | "integrations" | "knowledge" | "review"
    | "model" | "memory" | "guardrails" | "skills" | "handovers" | "budget"
    | null;
}

export interface AgentWizardAssistResponse {
  reply: string;
  patch: AgentWizardPatch;
  /** What the assistant proposed that could not be kept. */
  notes: string[];
}

// Channel/Bot configuration
interface ChannelTarget {
  id: string;
  name: string;
  group?: string;
}

export interface ChannelEntry {
  channel_bot_id: string;
  receive_enabled: boolean;
  publish_enabled: boolean;
  receive_channels: ChannelTarget[];
  publish_channels: ChannelTarget[];
  allowed_tools: string[] | null;
  /** Allow anonymous (unauthenticated) users. null = inherit from channel bot. */
  allow_anonymous?: boolean | null;
  /** Allow private/DM conversations to bypass channel restrictions. null = inherit from channel bot. */
  allow_private?: boolean | null;
  /** Only respond when the bot is explicitly @mentioned. null = inherit from channel bot. */
  only_mention?: boolean | null;
  /** Admin-only: bypass bot-level channel whitelist. When true, the agent can receive/publish on all channels regardless of the bot's allowed_receive_channels/allowed_publish_channels. */
  bypass_whitelist?: boolean;
}

/** Maps channel type ("slack", "teams", "whatsapp", "telegram", "discord", "google_chat") to its config. */
export type ChannelConfig = Record<string, ChannelEntry>;

// Pipeline tool reference — identifies a tool by source and name
export interface ToolRef {
  source: "custom_tool" | "integration" | "mcp";
  tool_id?: string;
  server_id?: string;
  provider?: string;
  integration_id?: string;
  tool_name: string;
}

/**
 * A per-agent run budget — a PARTIAL override of the platform's own budget.
 *
 * Every field is optional and `null`/absent means "inherit": the platform
 * value applies, or a nearer layer does when the agent runs inside a flow.
 * That is why the Budget tab renders empty fields as "inherits <ceiling>"
 * rather than pre-filling them — a pre-filled copy would stop tracking the
 * platform value the moment an admin changed it.
 */
export interface AgentBudgetConfig {
  /** Hard ceiling on the priced model spend of one run, in USD. */
  max_cost_usd?: number | null;
  max_total_tokens?: number | null;
  max_tool_calls?: number | null;
  max_duration_seconds?: number | null;
  /** Share of the cost/token ceilings held back for the closing answer (0–0.5). */
  final_answer_reserve?: number | null;
  /** Continue on a cheaper model when the COST ceiling is reached, once per run. */
  fallback_enabled?: boolean | null;
  /** Which cheaper model. Empty = the platform fallback model. */
  fallback_model?: string | null;
}

/** Who reads or writes a company-managed row the agent governs. */
type CoordinationScope = "all" | "agents" | "self";

/**
 * Which agents read and write the company-managed work items an agent creates
 * and the company-managed shared state it owns. `null` is the default: everyone
 * reads them and every company-managed agent writes them. `agents` narrows to
 * the agent plus the listed company-managed agents, `self` to the agent alone;
 * writing implies reading (the server widens `read` to admit every writer).
 */
export interface CoordinationAccess {
  read: CoordinationScope;
  write: CoordinationScope;
  read_agent_ids: string[];
  write_agent_ids: string[];
}

/**
 * What the platform bounds and whether the caller may go above it —
 * `GET /agents/budget-policy`.
 *
 * `can_exceed_ceiling` is what lets the form tell a non-admin the rule up
 * front instead of letting the server refuse the save with a 403.
 * A `null` ceiling means the platform leaves that dimension unbounded.
 */
export interface BudgetPolicy {
  can_exceed_ceiling: boolean;
  ceilings: Partial<Record<BudgetLimitKey, number | null>>;
  final_answer_reserve: number | null;
  fallback_enabled: boolean;
  fallback_model: string | null;
}

/** The four dimensions a run budget bounds — the gated ones. */
export type BudgetLimitKey =
  | "max_cost_usd"
  | "max_total_tokens"
  | "max_tool_calls"
  | "max_duration_seconds";

// Agents
export interface Agent {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  owner_email: string | null;
  slug: string | null;
  template_slug: string | null;
  name: string;
  description: string | null;
  icon: string;
  persona: string | null;
  /** Output format preference: "markdown" | "html" | "json" | "plain_text". */
  output_format: string;
  /** Optional JSON Schema for structured output when output_format is "json". */
  output_schema: Record<string, unknown> | null;
  enabled: boolean;
  is_builtin: boolean;
  is_company_managed: boolean;
  /** A built-in's organization notes, appended after its persona (#914). */
  customer_notes?: string | null;
  /** The customer-layer fields a person set on a built-in, kept across seed runs (#914). */
  customer_fields?: string[] | null;
  is_public_catalog?: boolean;
  /** The catalog download carries the code of personal custom tools. */
  catalog_include_personal_tools?: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  /** Why nobody may edit it, administrators included (a Platform Health fleet
   *  agent, #101 S1b); null otherwise. Every write control is hidden then. */
  read_only_reason?: string | null;
  /** The requesting user's right to read its conversations - the rule the
   *  chats routes enforce (admin, owner, or a `config_with_chats` grant).
   *  Read it through `canViewAgentChats()`. */
  can_view_chats?: boolean;
  tags: string[];
  integration_bindings: Record<string, string[]> | null;
  /** The owner's personal integrations lent through this agent to the people
   *  it is shared with (their chats with it only); null lends nothing.
   *  Written with `lent_integration_ids` on PUT /agents/{id}, owner only. */
  integration_lending?: { integration_ids: string[]; set_by: string; set_at: string } | null;
  /** The owner's personal integrations this group-shared agent binds that
   *  none of its groups gets otherwise, sent by GET /agents/{id} to a viewer
   *  who may edit it (an editor cannot read the owner's rows). */
  shared_personal_integrations?: { id: string; name: string; provider: string }[] | null;
  /** The integrations it binds that its owner can no longer see: its runs
   *  refuse their tools. Sent by GET /agents/{id} to a viewer who may edit it. */
  orphaned_integrations?: { id: string; name: string; provider: string }[] | null;
  integration_template_bindings: string[] | null;
  mcp_server_ids: string[] | null;
  custom_tool_ids: string[] | null;
  builtin_tool_ids: string[];
  /** Runner (native OS-control) selector: which enrolled machines this agent's
   *  `computer_use` tools may drive. Empty/null = any visible online runner. */
  runner_ids: string[] | null;
  /** Runner label selector: narrows targetable runners to those whose labels
   *  overlap this list. Combined with `runner_ids` as an AND refinement. */
  runner_labels: string[] | null;
  /** Per-provider tool allow lists. Key = provider name, value = list of tool names or null (all). */
  integration_allowed_tools: Record<string, string[] | null> | null;
  /** Integration element bindings: scope agent to specific sub-elements (orgs, repos, channels, etc.). */
  integration_element_bindings: Record<string, IntegrationElement[]> | null;
  /** Element-binding keys whose selection is enforced as a boundary on the tools. null = none (guidance only). */
  integration_element_enforcement?: string[] | null;
  /** Per-MCP-server tool allow lists. Key = server UUID, value = list of tool names or null (all). */
  mcp_allowed_tools: Record<string, string[] | null> | null;
  /** Per-built-in-group function allow lists. Key = built-in group id, value = function names or null (all); null = every function. */
  builtin_allowed_tools?: Record<string, string[] | null> | null;
  /** Tool deny list: explicitly blocked tool names. Deny takes precedence over allow lists. */
  tool_deny_list: string[] | null;
  /** Bound integrations whose write tools are withheld and refused: provider -> integration ids. */
  integration_read_only?: Record<string, string[]> | null;
  /** Per-user (template) providers whose write tools are withheld and refused; each is in integration_template_bindings. */
  integration_template_read_only?: string[] | null;
  /** OpenCTI Live writes overrides per binding: integration id (or "opencti" for its per-user binding) -> "on" (Live) or "off" (Draft only); absent = the integration's setting. */
  integration_live_writes?: Record<string, "on" | "off"> | null;
  /** Email signature overrides per bound email-sending integration id. null = every binding inherits. */
  integration_signatures?: Record<string, IntegrationSignatureOverride> | null;
  /** Browser profiles the agent's browser tool may open, for a person who may use them. */
  browser_profile_ids?: string[] | null;
  /** The bound browser profiles opened read-only: no click, typing, key press or script. */
  browser_profile_read_only?: string[] | null;
  /** Prompt bindings: list of Prompt UUIDs whose content is injected into the system prompt. */
  prompt_ids: string[] | null;
  /** Skill bindings: list of AgentSkill UUIDs whose instructions are injected at runtime. */
  skill_ids: string[] | null;
  /**
   * Per-skill load mode. Key = AgentSkill UUID, value = "progressive" | "always".
   * "progressive" is the default: the skill is advertised in the prompt catalog
   * and its body is pulled in on demand by the `load_skill` tool. "always" pins
   * the body inline in the system prompt on every turn.
   */
  skill_load_modes: Record<string, "progressive" | "always"> | null;
  knowledge_base_ids: string[] | null;
  /**
   * Per-KB access permission. Key = KnowledgeBase UUID, value = "read" | "write".
   * Read is the default: a bound KB with no entry (or "read") is search-only.
   * "write" additionally grants the agent the store_knowledge tool for that KB.
   */
  knowledge_base_permissions: Record<string, "read" | "write"> | null;
  handover_agent_ids: string[] | null;
  handover_types: Record<string, string> | null;
  work_item_owner_agent_ids?: string[] | null;
  trace_supervision_target_agent_ids?: string[] | null;
  /** Who reads / writes the company-managed coordination rows it governs; null = default. */
  coordination_access?: CoordinationAccess | null;
  /** Per-target routing conditions for mandatory_transfer handovers. Maps agent UUID → condition string. */
  handover_conditions: Record<string, string> | null;
  /** Whether the agent may spawn background copies of itself to split a large workload. On by default. */
  allow_self_fanout: boolean;
  /** Whether a caller's per-call output schema (consult / background task) may replace the agent's format. Off by default. */
  accept_caller_output_format?: boolean;
  config: Record<string, unknown> | null;
  llm_provider: string | null;
  llm_provider_id: string | null;
  llm_model: string | null;
  /** "routing" = runs on the platform routing model, ignoring the fields above. */
  llm_model_source?: "routing" | null;
  /** LLM temperature override (0.0–2.0). null = provider default. */
  temperature: number | null;
  /** Reasoning effort for thinking/reasoning models. null = platform default. */
  reasoning_effort: string | null;
  /** Organization summary in the prompt (#11533): inherit the platform, always or off. */
  organization_context_mode?: OrganizationContextMode;
  /** Organization categories preloaded into the prompt (#11533); null inherits. */
  organization_context_categories?: string[] | null;
  /** Max agentic loop iterations for chat. null = inherit the platform default
   *  (admin-configurable; the factory default is 25). */
  max_iterations: number | null;
  /** Per-agent run budget. null = inherit the platform budget entirely. */
  budget_config: AgentBudgetConfig | null;
  /** Safety constraints always injected into system prompt, separate from persona. */
  guardrails: string | null;
  /** Pipeline: tool reference for pre-LLM processing. */
  prepare_tool_ref: ToolRef | null;
  /** Pipeline: tool reference for post-LLM processing. */
  finalize_tool_ref: ToolRef | null;
  /** Pipeline: skip the LLM loop entirely. */
  skip_llm: boolean;
  memory_short_term_enabled: boolean;
  memory_medium_term_enabled: boolean;
  memory_persistent_enabled: boolean;
  memory_user_enabled: boolean;
  /** Agent-level HITL default. Assignments inherit unless they override. */
  human_in_the_loop: boolean;
  human_in_the_loop_prompt: string | null;
  /** Per-tool approval gate. null follows the install default. */
  approve_tool_calls: boolean | null;
  available_channel_types: string[] | null;
  /** Unified channel/bot configuration. Maps channel type → config entry. */
  channel_config: ChannelConfig | null;
  disable_chat: boolean;
  /** When true (company-managed only), this agent's runs are hidden from other
   *  users - each run is private to the user it executed as. */
  hide_runs: boolean;
  /** Admin-only switch (#11624): runs, traces, work items and shared state of
   *  this company-managed agent are hidden from non-admins. */
  hide_activity_from_non_admins?: boolean;
  web_enabled: boolean;
  web_token: string | null;
  /** Suggested prompts displayed in the web widget greeting screen. */
  web_suggested_prompts: string[] | null;
  /** Whether a visitor of the public widget may attach files to a message. Off by default. */
  web_uploads_enabled: boolean;
  assignment_count: number;
  chat_query_count: number;
  /** Its home fleet, read-only; null = the playground. Membership is written on the fleet. */
  fleet?: AgentFleetRef | null;
  created_at: string;
  updated_at: string;
}

/** The home fleet of an agent (`AgentResponse.fleet`). */
interface AgentFleetRef {
  fleet_id: string;
  fleet_name: string;
  member_id: string;
  is_orchestrator: boolean;
  /** Whether other fleets may hand over to it (the orchestrator always is). */
  is_exposed: boolean;
}

/** A duplicate, plus the bindings of the original it left out because the
 *  copier may not use them (counts per kind, never names). */
export interface DuplicatedAgent extends Agent {
  warnings?: string[];
}

// Agent Skills (shared entities following agentskills.io spec)
/** Where a forked skill came from, as recorded at fork time (`agent_skills.forked_from`). */
interface SkillProvenance {
  skill_id: string;
  name: string;
  slug?: string | null;
  is_builtin: boolean;
  /** The source's latest version number when it was forked. */
  version_number?: number | null;
  forked_at: string;
}

export interface AgentSkill {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  /** URL of the SKILL.md source — null when created via direct content or file upload. */
  url: string | null;
  name: string;
  description: string | null;
  /** Environment requirements from YAML frontmatter (e.g. "Requires GitHub integration"). */
  compatibility: string | null;
  /** Licence notice (frontmatter `license`): metadata shown in the Catalog tab, never in the agent's text. */
  license: string | null;
  content: string | null;
  fetched_at: string | null;
  /**
   * Number of consecutive 4xx failures during auto-refresh.
   * Resets to 0 on a successful refresh or when the URL is changed.
   * When this equals the platform's quarantine threshold the skill is
   * excluded from further auto-refresh attempts until the URL is updated.
   */
  refresh_failure_count: number;
  /**
   * GitHub integration whose credential authenticates the URL fetch
   * — set when the SKILL.md lives in a private repository. A reference
   * only: the credential itself stays on the integration.
   */
  source_integration_id: string | null;
  /**
   * Branch / tag / commit tracked in that repository. Null means the
   * repository's default branch HEAD, i.e. always the latest merged version.
   */
  source_ref: string | null;
  /**
   * Number of consecutive authentication failures during auto-refresh.
   * Non-zero means the bound credential is being rejected (expired,
   * revoked, or lacking access to the repository): the content is stale, but
   * unlike `refresh_failure_count` this never quarantines the skill — refresh
   * keeps retrying daily and a rotated credential heals it.
   */
  auth_failure_count: number;
  tags: string[];
  enabled: boolean;
  /** When true, hidden from chat contexts but still available for agent config. */
  hide_from_chat: boolean;
  is_company_managed: boolean;
  is_public_catalog?: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  /** Whether the skill's frontmatter is valid for package (ZIP) export. */
  package_exportable: boolean;
  /** Non-blocking warnings about frontmatter issues that prevent package export. */
  frontmatter_warnings: string[];
  /** Provenance of a fork (`POST /skills/{id}/fork`, #9174); `null` = not a fork. */
  forked_from?: SkillProvenance | null;
  created_at: string;
  updated_at: string;
}

export interface CreateAgentSkillRequest {
  /** URL to a SKILL.md file — mutually sufficient with `content`. */
  url?: string;
  /** Direct markdown/text content — mutually sufficient with `url`. */
  content?: string;
  /**
   * Connected GitHub integration used to authenticate the URL fetch — required
   * when the URL points into a private repository. The backend reads the file
   * once before saving, so an unusable credential is rejected immediately.
   */
  source_integration_id?: string;
  /** Branch / tag / commit to track. Omit to follow the default branch HEAD. */
  source_ref?: string;
  name?: string;
  description?: string;
  compatibility?: string;
  tags?: string[];
  hide_from_chat?: boolean;
  is_company_managed?: boolean;
  shared_group_ids?: string[];
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

export interface UpdateAgentSkillRequest {
  url?: string;
  content?: string;
  /** Pass `null` to detach the credential and go back to anonymous fetches. */
  source_integration_id?: string | null;
  source_ref?: string | null;
  name?: string;
  description?: string;
  compatibility?: string;
  /** Licence notice; an empty string clears it. */
  license?: string;
  commit_message?: string;
  tags?: string[];
  enabled?: boolean;
  hide_from_chat?: boolean;
  is_company_managed?: boolean;
  /** Owner transfer (admin only). */
  user_id?: string | null;
  is_public_catalog?: boolean;
  shared_group_ids?: string[];
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

export interface SkillPackageValidationResponse {
  valid: boolean;
  errors: string[];
  warnings: string[];
  detected_name: string | null;
  detected_description: string | null;
  detected_compatibility: string | null;
  resource_count: number;
  version_count: number;
}

export interface SkillPackageImportResponse extends AgentSkill {
  warnings?: string[];
  mapped_resource_summary?: {
    builtins_count?: number;
    scripts_count?: number;
    references_count?: number;
    auto_linked_agent_count?: number;
  } | null;
}

/** Why a file inside a bulk archive was not turned into a skill. */
export type BulkSkillIgnoreReason =
  | "no_frontmatter"
  | "unsupported_file_type"
  | "not_a_skill_root"
  | "archive_metadata"
  | "too_large"
  | "invalid_encoding"
  | "ambiguous_owner";

export type BulkSkillCollisionPolicy = "skip" | "replace" | "as_new";

interface BulkSkillCandidate {
  path: string;
  name: string | null;
  description: string | null;
  compatibility: string | null;
  resource_count: number;
  version_count: number;
  importable: boolean;
  collision: boolean;
  warnings: string[];
  errors: string[];
}

interface IgnoredArchiveEntry {
  path: string;
  reason: BulkSkillIgnoreReason;
}

export interface SkillBulkPackageValidationResponse {
  valid: boolean;
  errors: string[];
  skills: BulkSkillCandidate[];
  ignored: IgnoredArchiveEntry[];
  skill_count: number;
  importable_count: number;
  ignored_count: number;
}

interface BulkSkillImportOutcome {
  path: string;
  name: string | null;
  status: "created" | "replaced" | "skipped" | "failed";
  skill_id: string | null;
  message: string | null;
}

export interface SkillBulkImportResponse {
  created_count: number;
  replaced_count: number;
  skipped_count: number;
  failed_count: number;
  results: BulkSkillImportOutcome[];
}

/** Entities a packaged skill carries onto every agent it is attached to.
 *
 * `scripts/` resources become Custom Tools and `references/` resources become
 * Knowledge Base documents when the package is imported. The backend binds and
 * unbinds them together with the skill itself, so the UI presents them as
 * skill-owned rather than hand-picked. Skills with no packaged resources are
 * absent from the listing.
 */
export interface SkillPackageBindings {
  skill_id: string;
  skill_name: string;
  custom_tool_ids: string[];
  knowledge_base_ids: string[];
}

/** `GET /skills/{id}/license`: the notice and the licence file bundled at the skill's root. */
export interface SkillLicense {
  license: string | null;
  file_path: string | null;
  text: string | null;
  truncated: boolean;
}

export interface SkillTier3ToolLink {
  id: string;
  name: string;
  description: string;
  resource_path: string;
}


export interface CreateAgentRequest {
  id?: string;  // Pre-generated UUID (enables webhook URL preview before creation)
  slug?: string;  // Custom slug (auto-generated from name if not provided)
  template_slug?: string;
  name: string;
  description?: string;
  icon?: string;
  persona?: string;
  output_format?: string;
  output_schema?: Record<string, unknown> | null;
  is_company_managed?: boolean;
  /** Organization context (#11533): omitted inherits the platform; "off" removes the summary and the tool. */
  organization_context_mode?: OrganizationContextMode;
  tags?: string[];
  integration_bindings?: Record<string, string[]>;
  mcp_server_ids?: string[];
  custom_tool_ids?: string[];
  builtin_tool_ids?: string[];
  runner_ids?: string[];
  runner_labels?: string[];
  integration_allowed_tools?: Record<string, string[] | null>;
  mcp_allowed_tools?: Record<string, string[] | null>;
  builtin_allowed_tools?: Record<string, string[] | null> | null;
  tool_deny_list?: string[] | null;
  integration_read_only?: Record<string, string[]> | null;
  integration_template_read_only?: string[] | null;
  integration_live_writes?: Record<string, "on" | "off"> | null;
  integration_signatures?: Record<string, IntegrationSignatureOverride> | null;
  browser_profile_ids?: string[] | null;
  browser_profile_read_only?: string[] | null;
  prompt_ids?: string[];
  skill_ids?: string[];
  skill_load_modes?: Record<string, "progressive" | "always"> | null;
  knowledge_base_ids?: string[];
  knowledge_base_permissions?: Record<string, "read" | "write"> | null;
  handover_agent_ids?: string[];
  handover_types?: Record<string, string>;
  work_item_owner_agent_ids?: string[];
  trace_supervision_target_agent_ids?: string[];
  coordination_access?: CoordinationAccess | null;
  config?: Record<string, unknown>;
  available_channel_types?: string[] | null;
  channel_config?: ChannelConfig | null;
  memory_short_term_enabled?: boolean;
  memory_medium_term_enabled?: boolean;
  memory_persistent_enabled?: boolean;
  memory_user_enabled?: boolean;
  disable_chat?: boolean;
  hide_runs?: boolean;
  prepare_tool_ref?: ToolRef | null;
  finalize_tool_ref?: ToolRef | null;
  skip_llm?: boolean;
  create_default_assignments?: boolean;
  llm_provider?: string;
  llm_provider_id?: string;
  llm_model?: string;
  guardrails?: string;
  /** Per-agent run budget; undefined fields inherit the platform limits. */
  budget_config?: {
    max_cost_usd?: number;
    max_total_tokens?: number;
    max_tool_calls?: number;
    max_duration_seconds?: number;
  } | null;
  commit_message?: string;
}

// Assignments
export type TriggerType =
  | "schedule"
  | "webhook"
  | "integration_event"
  | "manual"
  | "work_item"
  | "platform_event";

/** Typed trigger configuration shape (union of all trigger types). */
export interface TriggerConfig {
  cron?: string;
  path?: string;
  provider?: string;
  event?: string;
  /** The key the agent builder used to store the event under: read only once `event_matching` is "exact". */
  event_type?: string;
  /** Webhook triggers: event keys (or prefixes, on legacy matching) that may run the assignment. */
  events?: string[] | string;
  /** "exact" on a trigger created, given new events or switched to exact matching; absent on one saved before it (legacy prefix matching). */
  event_matching?: "exact";
  integration_id?: string;
  /** Shared secret used to sign webhook deliveries (auto-generated for new webhook assignments). */
  webhook_secret?: string;
  /** work_item triggers: which agent's queue this inbox drains (defaults to the assignment's own agent). */
  owner_agent_id?: string;
  /** work_item triggers: only auto-pick-up items whose correlation key starts with this prefix. */
  correlation_prefix?: string;
  /** work_item triggers: only auto-pick-up items at or above this priority. */
  priority_min?: WorkItemPriority;
  /** Runs this assignment's queue starts at once; `ASSIGNMENT_MAX_CONCURRENT_RUNS` when absent. */
  max_concurrent_runs?: number;
  /** work_item triggers: up to `slots` extra runs above max_concurrent_runs for the items whose correlation key starts with one of the prefixes. */
  express_lane?: { correlation_prefixes: string[]; slots: number };
  /** Dotted path into the trigger payload: queued runs sharing its value start one at a time, in arrival order. */
  ordering_key?: string;
  /** platform_event triggers: "own" (the default) takes the events on the agent's own objects, "all" every one (an administrator's assignment). */
  scope?: "own" | "all";
  /** platform_event triggers: a one-off watch, the first matching event starts one run and the subscription ends. */
  once?: boolean;
  /** platform_event one-off watch: the event that fired it, written by the server; saving without it re-arms the watch. */
  fired_event_id?: string;
  fired_at?: string;
  /** platform_event triggers: the subscription's deadline (ISO 8601 with its offset); an event after it starts nothing. */
  expires_at?: string;
  /** Evaluated before any run or gate; a dropped event is counted (`events_filtered_count`). */
  filter?: {
    channels?: string[];
    from_contains?: string;
    /** Generic element scope keyed by element type (e.g. teams_channel, slack_channel, jira_project). */
    scope?: Record<string, string[]>;
    /** Mail: sender addresses or domains; a deny entry wins. */
    from?: { allow?: string[]; deny?: string[] };
    to_me?: boolean;
    cc_me?: boolean;
    subject_contains?: string | string[];
    /** Mail: drop what no person wrote (auto-submitted, bulk, reports, calendar responses, no-reply senders). */
    exclude_automated?: boolean;
    folder?: string;
    chat_ids?: string[];
    exclude_authors?: string[];
    change_kinds?: ("created" | "updated" | "cancelled" | "completed" | "deleted")[];
    flag_status?: ("flagged" | "complete" | "notFlagged")[];
    /** Microsoft To Do: task lists kept or dropped (names or ids; "Tasks" is the default list). */
    lists?: string[];
    exclude_lists?: string[];
    /** Microsoft To Do: drop every change made through the platform's own To Do tools. */
    exclude_self?: boolean;
  };
  /** integration_event triggers on OpenCTI or OpenAEV: the trigger XTM One creates in that platform (fleet, purpose, filters). */
  provision?: Record<string, unknown>;
  /** Written by the server: what is provisioned in OpenCTI for this assignment. */
  opencti_provisioning?: TriggerProvisioningState;
  /** Written by the server: what is provisioned in OpenAEV for this assignment. */
  openaev_provisioning?: TriggerProvisioningState;
}

/** The provisioning state the server keeps under `trigger_config.<provider>_provisioning`. */
export interface TriggerProvisioningState {
  status?: "provisioned" | "error" | "orphaned";
  error?: string | null;
  /** "XTM One - fleet - purpose": the name of the objects in the source platform. */
  name?: string;
  notifier_id?: string;
  /** OpenCTI: the one live trigger. */
  trigger_id?: string;
  /** OpenAEV: one trigger per event. */
  trigger_ids?: Record<string, string>;
  checked_at?: string;
  integration_id?: string;
}

/** Condition of a tool-check gate: the result's shape, or a path into it. */
export type ActivationCheckCondition =
  | "non_empty"
  | "empty"
  | { path: string; in?: unknown[]; not_in?: unknown[]; contains?: string };

/** Tool-capable gate: one read-only tool call tested with no model call, before the gate prompt. */
export interface ActivationCheck {
  tool: string;
  arguments: Record<string, unknown>;
  condition: ActivationCheckCondition;
  integration_id?: string;
  /** Up to 3 more checks evaluated in order; the run is skipped at the first that fails. */
  also?: Omit<ActivationCheck, "also" | "or">[];
  /** Up to 3 alternatives: the run goes ahead when one check passes, skipped when all fail. */
  or?: Omit<ActivationCheck, "also" | "or">[];
}


/**
 * Trigger + next-run summary for one assignment, served by
 * `GET /assignments/schedule`. Powers the Agentic Flow "Schedule" tab.
 *
 * Read-only and computed on the fly: `next_run_at` comes from the cron
 * expression + `last_triggered_at` (shared scheduler logic), `pending_run_at`
 * is the earliest queued/delayed orchestration execution. Nothing is persisted.
 */
export interface AssignmentSchedule {
  assignment_id: string;
  assignment_name: string;
  agent_id: string;
  agent_name: string;
  agent_icon: string;
  trigger_type: TriggerType;
  enabled: boolean;
  /** schedule triggers */
  cron: string | null;
  /** The next fire, in UTC (ISO 8601). */
  next_run_at: string | null;
  /**
   * The zone the cron is read in (`trigger_config.timezone`): an IANA name,
   * "UTC" when none is configured or the configured one is unknown.
   */
  timezone?: string | null;
  /** `next_run_at` as a wall time in `timezone`, ISO 8601 with its offset. */
  next_run_local?: string | null;
  /** Set when the configured zone is not recognised (the cron is read in UTC). */
  timezone_warning?: string | null;
  /** all triggers */
  delay_seconds: number;
  last_triggered_at: string | null;
  /**
   * The outcome of the latest finished run (`completed`, `failed`, `skipped`,
   * `cancelled`), null without one: the list row's outcome dot.
   */
  last_run_status?: string | null;
  /** When that same run finished, the time shown beside its outcome dot. */
  last_run_finished_at?: string | null;
  has_active_run: boolean;
  pending_run_at: string | null;
  /**
   * Accepted webhook deliveries waiting behind the active run. Non-zero means
   * a backlog is forming — deliveries are arriving faster than the assignment
   * can run them.
   */
  queued_run_count: number;
  /** integration_event triggers */
  provider: string | null;
  event: string | null;
  poll_interval_seconds: number | null;
  /** webhook + integration_event triggers */
  webhook_url: string | null;
}

/** Parameter type for manual trigger input fields. */
export type ManualTriggerParameterType = "text" | "textarea" | "number" | "boolean" | "select";

/** Schema for a single manual trigger parameter. */
export interface ManualTriggerParameter {
  key: string;
  label: string;
  type: ManualTriggerParameterType;
  description?: string;
  required?: boolean;
  default?: string;
  options?: string[];
}

/** Cause of an assignment's current pause (`Assignment.paused_reason`). */
export type AssignmentPauseReason =
  | "user_toggle"
  | "agent_tool"
  | "auth_quarantine"
  | "stale_reference"
  | "invalid_schedule"
  | "created_disabled"
  | "system_seed"
  | "panic_stop"
  | "concierge_switch_over";

/** Whether a prompt starts with the organization summary (#11533): the most specific explicit value wins. */
export type OrganizationContextMode = "inherit" | "always" | "off";

export interface Assignment {
  id: string;
  agent_id: string;
  /** Owner (creator, or admin for seeded). Always set. */
  user_id: string | null;
  /** Visibility flag (shared with all + admin-only edit). Mirrors the agent. */
  is_company_managed: boolean;
  /** Optional "run as" user for company-managed agents. NULL = run as owner. */
  run_as_user_id: string | null;
  /** When true, a scheduled trigger fans out to one run per active user (each as
   *  themselves). Implies per-user-private runs. */
  run_as_all_users: boolean;
  /** A seeded built-in assignment an admin turns on and points at a run-as
   *  account: only `enabled` and `run_as_user_id` are writable. */
  builtin_admin_opt_in?: boolean;
  /** null = inherit the agent's hide_runs default; true/false overrides it. */
  hide_runs: boolean | null;
  name: string;
  description: string | null;
  trigger_type: TriggerType;
  trigger_config: TriggerConfig;
  /** Integration-event / webhook triggers: "legacy" = saved before exact event matching, still matching by prefix. */
  event_matching?: "exact" | "legacy" | null;
  activation_prompt: string | null;
  /** Tool-check gate evaluated before the gate prompt; null = none. */
  activation_check?: ActivationCheck | null;
  prompt: string;
  delay: string | null;
  tool_sources: string[] | null;
  /** NULL = inherit all from parent agent, {} = no integrations, {...} = custom subset. */
  integration_bindings: Record<string, string[]> | null;
  /** Per-provider tool allow lists for this assignment. NULL = no assignment-level restriction. */
  integration_allowed_tools: Record<string, string[] | null> | null;
  /** Per-built-in-group function allow lists, intersected with the agent's. NULL = no assignment-level restriction. */
  builtin_allowed_tools?: Record<string, string[] | null> | null;
  /** Integration element bindings: scope to specific sub-elements (channels, repos, etc.). */
  integration_element_bindings: Record<string, IntegrationElement[]> | null;
  /** Element-binding keys whose selection is enforced as a boundary on the tools. null = none; a type the agent enforces is a boundary too. */
  integration_element_enforcement?: string[] | null;
  /** Channel/bot config. NULL = inherit from agent, {} = no channels, {...} = custom. */
  channel_config: ChannelConfig | null;
  /** @deprecated superseded by read_knowledge_base_ids (null = inherit all agent KBs). */
  use_knowledge_bases: boolean;
  /** Read scope (RAG). null/[] = inherit ALL agent KBs (no "none" state); [ids] = subset. */
  read_knowledge_base_ids: string[] | null;
  /** Write scope (store_knowledge), refined from the agent's writable KBs. null = inherit all writable; [ids] = subset; [] = none. */
  write_knowledge_base_ids: string[] | null;
  /** Runner refinement (native OS-control). null = inherit the agent's runners; [ids] = subset; [] = none. */
  runner_ids: string[] | null;
  /** @deprecated superseded by the agent KB permission model + write_knowledge_base_ids. */
  write_knowledge_base_enabled: boolean;
  /** @deprecated superseded by write_knowledge_base_ids. */
  target_knowledge_base_id: string | null;
  ai_label: boolean;
  ai_label_prompt: string | null;
  notify_on_complete: boolean;
  show_in_digest: boolean;
  /** NULL = inherit from agent. true/false = explicit override. */
  human_in_the_loop: boolean | null;
  human_in_the_loop_prompt: string | null;
  /** Per-run cap on agentic loop turns. null = inherit (agent override -> platform default); a value overrides the agent. */
  max_iterations: number | null;
  /** Organization summary in the run's prompt (#11533): inherit the agent, always or off. */
  organization_context_mode?: OrganizationContextMode;
  /** Organization categories preloaded into the prompt (#11533); null inherits. */
  organization_context_categories?: string[] | null;
  /** Maximum wall-clock seconds for a single run. 0 = no limit. */
  timeout_seconds: number;
  /** When true, the assignment logs what it would do without taking real actions. */
  dry_run: boolean;
  /**
   * When true, integration events caused by the agent's own writes (e.g. a
   * GitHub issue edit/comment/label change this assignment made) are dropped
   * before they create a run — breaks the self-trigger feedback loop.
   */
  ignore_self_triggered_events: boolean;
  /** Automatically retry failed runs. */
  retry_on_failure: boolean;
  /** Max retry attempts (used when retry_on_failure is true). */
  max_retries: number;
  /** Input parameters for manual trigger (form schema). */
  manual_trigger_parameters: ManualTriggerParameter[] | null;
  enabled: boolean;
  /**
   * Why this assignment is currently paused. All null while enabled,
   * and all null for a pause that predates this tracking — the UI says
   * "reason unknown" rather than guessing.
   */
  paused_at: string | null;
  /** `user_toggle` | `agent_tool` | `auth_quarantine` | `stale_reference` |
   *  `invalid_schedule` | `created_disabled` | `system_seed`. Widened at runtime: the backend stores
   *  unrecognised codes rather than lose the pause record, so treat any lookup
   *  keyed on this as partial. */
  paused_reason: AssignmentPauseReason | null;
  /** The human behind the pause; null for autonomous system pauses. */
  paused_by_user_id: string | null;
  /** Denormalized actor label: a user's email, or the system component. */
  paused_by_label: string | null;
  /** Already-composed, human-readable explanation. */
  paused_details: string | null;
  /**
   * Not company-managed and its owner's account is deactivated:
   * nothing starts it, whatever `enabled` says. Derived by the backend at read time, so it clears
   * as soon as the account is reactivated; `enabled` and `paused_*` keep
   * describing the owner's own choices.
   */
  paused_by_owner_deactivation: boolean;
  execution_count: number;
  /** Integration events the trigger's filter dropped before a run was created. */
  events_filtered_count?: number;
  last_triggered_at: string | null;
  webhook_url: string | null;
  integration_webhook_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateAssignmentRequest {
  agent_id: string;
  name: string;
  description?: string;
  trigger_type: TriggerType;
  trigger_config?: TriggerConfig;
  /** Optional "should I run?" gate; `null` / blank = no gate. */
  activation_prompt?: string | null;
  /** Optional tool-check gate; `null` = none. */
  activation_check?: ActivationCheck | null;
  prompt: string;
  delay?: string;
  /** Optional "run as" user (company-managed agents only; admin-only). */
  run_as_user_id?: string | null;
  run_as_all_users?: boolean;
  hide_runs?: boolean | null;
  tool_sources?: string[];
  integration_bindings?: Record<string, string[]> | null;
  integration_allowed_tools?: Record<string, string[] | null> | null;
  builtin_allowed_tools?: Record<string, string[] | null> | null;
  integration_element_bindings?: Record<string, IntegrationElement[]> | null;
  integration_element_enforcement?: string[] | null;
  channel_config?: ChannelConfig | null;
  use_knowledge_bases?: boolean;
  read_knowledge_base_ids?: string[] | null;
  write_knowledge_base_ids?: string[] | null;
  runner_ids?: string[] | null;
  write_knowledge_base_enabled?: boolean;
  target_knowledge_base_id?: string | null;
  ai_label?: boolean;
  ai_label_prompt?: string | null;
  notify_on_complete?: boolean;
  show_in_digest?: boolean;
  human_in_the_loop?: boolean;
  human_in_the_loop_prompt?: string | null;
  /** null = inherit (agent override -> platform default); a value overrides the agent. */
  max_iterations?: number | null;
  organization_context_mode?: OrganizationContextMode;
  /** Organization categories preloaded into the prompt (#11533); null inherits. */
  organization_context_categories?: string[] | null;
  timeout_seconds?: number;
  dry_run?: boolean;
  ignore_self_triggered_events?: boolean;
  retry_on_failure?: boolean;
  max_retries?: number;
  manual_trigger_parameters?: ManualTriggerParameter[] | null;
}

/** A single tool call recorded in an assignment run's actions_taken. */
interface ActionTaken {
  tool: string;
  arguments?: Record<string, unknown>;
  /** Origin label: "custom:{id}", "mcp:{name}", "integration:{provider}", "builtin:{type}", etc. */
  source?: string;
}

/** How a HITL round was resolved. */
type HumanInputResolution = "answered" | "dismissed";

/**
 * One durable HITL resolution entry, appended by the respond/dismiss
 * endpoints. Survives the engine clearing the live question/response
 * fields on resume, so the run panel can keep showing the resolved state.
 */
/** An open request to act on a paused run, made from the card of its pasted link. */
export interface RunActionRequest {
  id: string;
  requester_name: string;
  source: string;
  created_at: string;
}

export interface HumanInputHistoryEntry {
  question: string | null;
  response: string | null;
  resolution: HumanInputResolution;
  responded_at: string;
  responded_by: string | null;
  responded_by_id: string | null;
}

/**
 * One tool call the agent wants to make, awaiting human approval.
 *
 * `input_schema` travels alongside `arguments` so the UI can label each field
 * with the tool's own description — a reviewer needs to know what
 * `cascade: true` *means*, not just that it is set.
 */
export interface ActionProposal {
  tool_call_id: string;
  tool_name: string;
  tool_description: string;
  arguments: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  source: string;
  /** The person's own consent to this one call: no standing
   *  "always" answers it, so the card offers no "Yes, always". */
  requires_consent?: boolean;
}

export interface PendingApprovals {
  run_id: string;
  status: string;
  /** `action_proposal` = render the approval form; `free_text` = legacy prompt. */
  kind: "action_proposal" | "free_text";
  question: string | null;
  proposals: ActionProposal[];
}

export type ApprovalVerdict = "approve" | "approve_always" | "reject";

/** One decision inside a batch (FR-031).
 *  `approve_always` is allowed since FR-032's revision: the caller must first
 *  show a disclosure ENUMERATING every tool it would permanently un-gate, which
 *  is what the original objection ("a group has no single tool") really asked
 *  for. */
export interface BatchApprovalDecision {
  run_id: string;
  tool_call_id: string;
  decision: "approve" | "approve_always" | "reject";
  rejection_reason?: string;
}

interface BatchApprovalResult {
  run_id: string;
  tool_call_id: string;
  status: "applied" | "refused";
  reason?: string | null;
  detail?: string | null;
}

interface PendingToolSummary {
  tool_name: string;
  /** Runs whose pending calls are ALL this tool, so a decision about it alone
   *  can answer them. A run cannot be half-answered. */
  run_count: number;
  call_count: number;
}

interface PendingMixedGroup {
  /** Sorted. The run is answerable only once EVERY one of these is selected,
   *  which is the rule the by-tool endpoint applies. */
  tool_names: string[];
  run_count: number;
  call_count: number;
}

export interface PendingByToolResponse {
  tools: PendingToolSummary[];
  /** The mixed runs themselves. A selection covering a whole combination
   *  answers its runs too, so a confirmation built from `tools` alone
   *  understates its own reach. */
  mixed: PendingMixedGroup[];
  /** `sum(mixed[].run_count)`, for surfaces that want only "how much is
   *  waiting" and should not have to add the list up. */
  mixed_run_count: number;
}

/** Partial success is a success — read `results`, never the status code alone. */
export interface BatchApprovalResponse {
  results: BatchApprovalResult[];
}

/**
 * A reviewer's decision about one proposed call. Keyed on `tool_call_id`, never
 * on tool name: one turn can call the same tool twice with different arguments.
 */
export interface ApprovalDecision {
  tool_call_id: string;
  decision: ApprovalVerdict;
  rejection_reason?: string | null;
}

export interface AssignmentRun {
  id: string;
  assignment_id: string | null;
  agent_id: string;
  status: string;
  is_company_managed: boolean;
  parent_run_id: string | null;
  child_run_count: number;
  trigger_payload: Record<string, unknown> | null;
  trigger_context: Record<string, unknown> | null;
  actions_taken: ActionTaken[] | null;
  result_summary: string | null;
  error: string | null;
  /**
   * Structured failure cause — `error` stays the free text a human
   * reads, this is what a query groups by. Null on a run that succeeded, and
   * on a failed one recorded before the classifier existed.
   */
  failure_cause: string | null;
  /**
   * How the cause was reached: `structural` (a type, an exception class, an
   * HTTP status code) or `heuristic` (a substring match on the error text).
   * Surfaced because the two carry very different confidence.
   */
  failure_determination: string | null;
  /**
   * Why a `completed` run stopped before its task was done:
   * `max_iterations`, `deadline`, `budget_exhausted`,
   * `degenerate_repetition`, `timeout` or `waiting_on_tasks`. Null when it
   * ended on its own terms.
   */
  stop_reason: string | null;
  human_input_question: string | null;
  human_input_response: string | null;
  human_input_history: HumanInputHistoryEntry[] | null;
  /** Populated when the run paused on the per-tool approval gate. */
  pending_approvals: ActionProposal[] | null;
  /** Decided already; waiting on the worker, not on the reviewer. */
  approval_answered?: boolean;
  langfuse_trace_id: string | null;
  llm_trace_id: string | null;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

// Knowledge Bases
export type KnowledgeBaseHtmlIngestion = "source" | "rendered";
/** Why a knowledge base's content is locked for every writer (`helpers.knowledge_base_content_lock`). */
type KnowledgeBaseContentLock = "package_installed" | "workspace" | "builtin" | "read_only";

/** Why a document failed to process, as served by the API (`DocumentErrorCode`
 *  in `app/services/kb_document_errors.py`). */
export type KnowledgeDocumentErrorCode =
  | "no_content"
  | "knowledge_base_not_found"
  | "no_extractable_text"
  | "embedding_not_configured"
  | "embedding_not_available"
  | "embedding_provider_error"
  | "dimension_mismatch"
  | "timeout"
  | "worker_timeout"
  | "internal_error";

export interface KnowledgeBase {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  description: string | null;
  embedding_model: string;
  is_system_managed: boolean;
  is_builtin: boolean;
  /** True once an admin has customized this built-in KB's import sources. */
  sync_sources_customized: boolean;
  /**
   * Content lock — the collection is ground truth. Still fully searchable and
   * readable; agent writes and ad-hoc document mutation (add / upload / crawl /
   * update / delete) are refused, as is deleting the KB. Configured import
   * sources and re-embedding keep working, so an externally-sourced KB stays
   * fresh. Unlike the company-managed read-only gate, this is about the KB's
   * content, not about who may administer the entity.
   */
  is_read_only: boolean;
  /**
   * How HTML documents are embedded: their stored markup (`source`,
   * the default) or their rendered page text (`rendered`). The stored source is
   * the same either way; the setting applies to documents processed after it
   * changes, and a re-embed applies it to the existing ones.
   */
  html_ingestion: KnowledgeBaseHtmlIngestion;
  /**
   * Whether the figures of an uploaded PDF (diagrams, charts, screenshots,
   * pages without a text layer) are described by a vision model at ingestion
   * and indexed next to their page's text. Applies to PDFs uploaded while it
   * is on; the stored document is the same either way.
   */
  describe_figures: boolean;
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to administer it, share tiers included,
   *  computed server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  /** Installed by a skill package: read-only on every write route whoever
   *  asks, as served by the API. */
  is_package_origin?: boolean;
  /** Why no one may write its content, as served by the API - the rule the
   *  agents' write scopes apply, so a write grant on it is never offered. */
  content_lock?: KnowledgeBaseContentLock | null;
  /** The workspace that owns this knowledge base: its visibility and
   *  its content are the workspace's, and it is never bound anywhere. */
  managed_by_workspace?: { id: string; name: string } | null;
  tags: string[];
  document_count: number;
  chunk_count: number;
  query_count: number;
  ready_count: number;
  pending_count: number;
  error_count: number;
  /** A fixed platform sentence (never raw exception text) and its code. */
  latest_error_message: string | null;
  latest_error_code?: KnowledgeDocumentErrorCode | null;
  /** Earliest scheduled automatic re-embed across this KB's error docs. */
  next_auto_retry_at: string | null;
  /** How many error docs have an auto-retry scheduled (vs. structural failures). */
  retryable_error_count: number;
  has_stale_embeddings?: boolean;
  stale_tuple_count?: number;
  stale_chunk_count?: number;
  created_at: string;
  updated_at: string;
}

/** The figure pass of a PDF in a knowledge base that describes figures. */
type KnowledgeDocumentFigureStatus = "pending" | "complete" | "partial" | "failed";

export interface KnowledgeDocument {
  id: string;
  filename: string;
  source_type: string;
  source_url: string | null;
  status: string;
  chunk_count: number;
  error_message: string | null;
  error_code?: KnowledgeDocumentErrorCode | null;
  /** `null` for any document that is not a PDF uploaded while the setting was on.
   *  A `ready` document whose figures are `partial` / `failed` is indexed (in
   *  part) as text only; `figure_note` says why, in fixed words. */
  figure_status?: KnowledgeDocumentFigureStatus | null;
  figure_note?: string | null;
  figure_pages?: number;
  created_at: string;
}

interface GlobalKnowledgeDocument {
  id: string;
  knowledge_base_id: string;
  knowledge_base_name: string;
  /** The document's KB was installed by a skill package: its document routes,
   *  reprocess included, refuse every write. */
  knowledge_base_is_package_origin?: boolean;
  filename: string;
  source_type: string;
  source_url: string | null;
  status: string;
  chunk_count: number;
  error_message: string | null;
  error_code?: KnowledgeDocumentErrorCode | null;
  created_at: string;
}

export interface GlobalKnowledgeDocumentsResponse {
  items: GlobalKnowledgeDocument[];
  total: number;
  limit: number;
  offset: number;
}

export interface KBSyncSource {
  id: string;
  source_type: string;
  config: Record<string, string>;
  sync_interval: string; // "manual" | "6h" | "12h" | "daily" | "weekly"
  enabled: boolean;
  integration_id: string | null;
  integration_name: string | null;
  last_synced_at: string | null;
  last_sync_status: string | null; // "success" | "error" | "syncing"
  last_sync_error: string | null;
  last_sync_documents: number;
  created_at: string;
  updated_at: string;
}

export interface KBExportPackage {
  id: string;
  export_id: string;
  part_number: number;
  total_parts: number;
  filename: string;
  file_size: number;
  expires_at: string;
  download_url: string;
}

export interface KBExportResponse {
  export_id: string;
  packages: KBExportPackage[];
}

// JSON Schema (subset used by tool input schemas)
export interface JsonSchema {
  type?: string;
  properties?: Record<string, { type?: string; description?: string; format?: string }>;
  required?: string[];
  [key: string]: unknown;
}

// Custom Tools (User-defined JavaScript tools)
export type CustomToolLanguage = "javascript" | "python";

export interface CustomTool {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  description: string | null;
  icon_url: string | null;
  input_schema: JsonSchema | null;
  js_function: string | null;
  language: CustomToolLanguage;
  tags: string[];
  /** Execution timeout in seconds (default 300 = 5 min). */
  timeout: number;
  enabled: boolean;
  /** When true, hidden from chat tool pickers but still available for agent config. */
  hide_from_chat: boolean;
  is_company_managed: boolean;
  is_public_catalog?: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  /** Installed by a skill package: read-only on every write route whoever
   *  asks, as served by the API. */
  is_package_origin?: boolean;
  /** Declared data cases (inputs, dry run, expectations). */
  test_cases?: CustomToolTestCase[] | null;
  /** Declared test code, in the tool's language. */
  test_code?: string | null;
  last_test_status?: CustomToolTestStatus | null;
  last_test_run_at?: string | null;
  last_test_results?: CustomToolTestReport | null;
  /** The repository source the tool is synced from: its content is read-only. */
  source_id?: string | null;
  source_name?: string | null;
  /** The tool's directory in the repository. */
  source_path?: string | null;
  source_commit_sha?: string | null;
  /** The directory's web page at the active commit. */
  source_url?: string | null;
  call_count: number;
  last_called_at: string | null;
  created_at: string;
  updated_at: string;
}

/** What a data case's output must look like; every given check must hold. */
interface CustomToolTestExpectation {
  equals?: string;
  contains?: string[];
  not_contains?: string[];
  regex?: string;
  json_subset?: unknown;
}

/** One declared data case of a custom tool. */
export interface CustomToolTestCase {
  name: string;
  inputs?: Record<string, unknown>;
  dry_run?: boolean;
  expect?: CustomToolTestExpectation;
  expect_error?: boolean;
  error_contains?: string;
}

/** A test run's overall outcome. */
export type CustomToolTestStatus = "passed" | "failed" | "error" | "no_tests";

/** One result of a test run: a data case or a test of the test code. */
export interface CustomToolTestResult {
  kind: "case" | "code";
  name: string;
  status: "passed" | "failed" | "error" | "skipped";
  dry_run?: boolean;
  message?: string;
  output?: string;
  duration_ms?: number;
}

/** `POST /custom-tools/{id}/run-tests`. */
export interface CustomToolTestReport {
  status: CustomToolTestStatus;
  ran_at: string;
  dry_run_only: boolean;
  duration_ms: number;
  summary: { passed: number; failed: number; error: number; skipped: number; total: number };
  results: CustomToolTestResult[];
}

export type CustomToolSourceProvider = "github" | "gitlab";
export type CustomToolSourceInterval = "manual" | "6h" | "12h" | "daily" | "weekly";

/** One tool's line in a sync report. */
export interface CustomToolSyncEntry {
  path: string;
  name?: string;
  tool_id?: string | null;
  /** created / updated: the test status the new version passed with. */
  tests?: CustomToolTestStatus | {
    status: CustomToolTestStatus;
    summary: CustomToolTestReport["summary"];
    failures: { kind: string; name: string; status: string; message?: string }[];
  };
  error?: string;
}

/** What the last sync of a source did, tool by tool. */
export interface CustomToolSyncReport {
  commit_sha: string | null;
  status: "ok" | "partial";
  created: CustomToolSyncEntry[];
  updated: CustomToolSyncEntry[];
  unchanged: CustomToolSyncEntry[];
  rejected: CustomToolSyncEntry[];
  disabled: CustomToolSyncEntry[];
  /** Tools the sync had disabled whose directory came back, enabled again
   *  (each also in `updated` or `unchanged`); absent from older reports. */
  restored?: CustomToolSyncEntry[];
  errors: CustomToolSyncEntry[];
  skipped_detached: string[];
}

/** A GitHub / GitLab repository synced into custom tools. */
export interface CustomToolSource {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  provider: CustomToolSourceProvider;
  repository: string;
  repository_url: string;
  ref: string | null;
  path: string;
  integration_id: string | null;
  integration_name: string | null;
  sync_interval: CustomToolSourceInterval;
  enabled: boolean;
  is_company_managed: boolean;
  detached_paths: string[];
  last_synced_at: string | null;
  last_commit_sha: string | null;
  last_sync_status: "ok" | "partial" | "error" | null;
  last_sync_error: string | null;
  last_sync_report: CustomToolSyncReport | null;
  tool_count: number;
  can_manage: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateCustomToolSourceRequest {
  name: string;
  provider: CustomToolSourceProvider;
  repository: string;
  ref?: string | null;
  path?: string;
  integration_id?: string | null;
  sync_interval?: CustomToolSourceInterval;
  enabled?: boolean;
  is_company_managed?: boolean;
}

export interface CreateCustomToolRequest {
  name: string;
  description?: string;
  icon_url?: string;
  input_schema?: JsonSchema;
  js_function?: string;
  language?: CustomToolLanguage;
  tags?: string[];
  timeout?: number;
  enabled?: boolean;
  hide_from_chat?: boolean;
  is_company_managed?: boolean;
  is_public_catalog?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  test_cases?: CustomToolTestCase[] | null;
  test_code?: string | null;
}

/** One input parameter of the tool whose code is being drafted. */
interface GenerateCustomToolCodeParameter {
  name: string;
  type?: string;
  description?: string;
  required?: boolean;
}

/** `POST /custom-tools/generate-code`: the brief plus the tool as it stands in the form. */
export interface GenerateCustomToolCodeRequest {
  description: string;
  language: CustomToolLanguage;
  tool_name?: string;
  tool_description?: string;
  parameters?: GenerateCustomToolCodeParameter[];
  /** The code in the editor, reworked rather than replaced when set. */
  current_code?: string;
}

export interface GenerateCustomToolCodeResponse {
  code: string;
  /** The model stopped at its output limit: the code may end early. */
  truncated: boolean;
  /** The syntax check of the returned code (one repair round already ran). */
  syntax_ok: boolean;
  syntax_error: string | null;
  syntax_error_line: number | null;
  repaired: boolean;
}

export interface CustomToolSkillLink {
  skill_id: string;
  skill_name: string;
  skill_description: string | null;
  resource_kind: string;
  resource_path: string;
  linked_at: string;
}

// Prompts (Reusable prompt templates)
export interface Prompt {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  description: string;
  content: string;
  tags: string[];
  /** When true, hidden from chat prompt picker but still available for agent config. */
  hide_from_chat: boolean;
  is_company_managed: boolean;
  is_public_catalog?: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreatePromptRequest {
  name: string;
  description?: string;
  content?: string;
  tags?: string[];
  hide_from_chat?: boolean;
  is_company_managed?: boolean;
  is_public_catalog?: boolean;
  commit_message?: string;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

interface VersionListItem {
  id: string;
  version_number: number;
  author_id: string | null;
  author_name: string | null;
  commit_message: string | null;
  is_active: boolean;
  is_locked: boolean;
  created_at: string;
}

export interface VersionListResponse {
  items: VersionListItem[];
  total: number;
}

interface VersionDiffLine {
  type: "context" | "add" | "remove";
  content: string;
  old_line: number | null;
  new_line: number | null;
}

export interface VersionDiffResponse {
  from_version: number;
  to_version: number;
  added: number;
  removed: number;
  lines: VersionDiffLine[];
  unified_diff: string;
}

/** A custom dashboard's published link, as its managers see it (`DashboardPublicLinkResponse`). */
export interface DashboardPublicLink {
  enabled_on_platform: boolean;
  url: string | null;
  path: string | null;
  created_at: string | null;
  publisher_name: string | null;
  refresh_minutes: number | null;
  snapshot_at: string | null;
  /** The highest markings the snapshot shows (OpenCTI's data); empty, no ceiling. */
  marking_ceiling: string[];
}

/** What a published link shows (`PublicDashboardResponse`): the definition reduced to what draws, and its snapshot. */
export interface PublicDashboard {
  title: string;
  definition: Record<string, unknown>;
  snapshot: { widgets: Record<string, WidgetData>; sources: Record<string, WidgetData> };
  snapshot_at: string | null;
}

/** Two versions of a custom dashboard compared, with which widgets changed (`DashboardVersionDiffResponse`). */
export interface DashboardVersionDiff extends VersionDiffResponse {
  changes: string | null;
}

export type AgentConfigVersionField =
  | "guardrails"
  | "temperature"
  | "reasoning_effort"
  | "max_iterations";

// MCP Servers (Tool Servers)
export interface MCPToolInfo {
  name: string;
  description: string;
  input_schema?: JsonSchema | null;
}

export type MCPCredentialMode = "shared" | "per_user";

export interface MCPServer {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  transport: "stdio" | "sse" | "streamable_http";
  command: string | null;
  args: string[] | null;
  has_env: boolean;
  url: string | null;
  has_headers: boolean;
  /** The headers / environment values read from a vault, and their recent failures. */
  vault_secrets?: VaultSecretStatus | null;
  auth_mode: "headers" | "oauth";
  has_client_id: boolean;
  /** Whether an OAuth client secret (a value or a vault reference) is stored. */
  has_client_secret?: boolean;
  oauth_status: "disconnected" | "connected" | "error";
  /** Whose OAuth account a call carries: `shared` (one token on the server,
   *  the default) or `per_user` (each person connects their own account).
   *  Optional so the type reads payloads from older backend builds. */
  credential_mode?: MCPCredentialMode;
  /** On a `per_user` server: the reader's own connection. Absent otherwise. */
  my_oauth_status?: "disconnected" | "connected" | "error" | null;
  scopes: string | null;
  authorize_url: string | null;
  token_url: string | null;
  /** RFC 7591 dynamic client registration endpoint (discovered or configured). */
  registration_url?: string | null;
  /** True when `has_client_id` came from dynamic registration, not from
   *  credentials the operator typed in. Optional so the type stays
   *  backward-compatible with payloads from older backend builds. */
  dcr_registered?: boolean;
  agent_ids: string[] | null;
  tags: string[];
  enabled: boolean;
  /** When true, hidden from chat tool pickers but still available for agent config. */
  hide_from_chat: boolean;
  is_company_managed: boolean;
  is_public_catalog?: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  discovered_tools: MCPToolInfo[] | null;
  /** Tool a Deep Investigation may call on this server. `null` = this
   *  server is not an investigation source, which is the default. Never
   *  inferred from `discovered_tools`: an MCP tool can have side effects, so
   *  the one an investigation may call is named by a person. */
  investigation_tool_name?: string | null;
  /** Argument the subject is passed under. `null` = read it off the tool's
   *  input schema when it is called. */
  investigation_argument?: string | null;
  /** The relay group a remote server is reached through; null =
   *  directly. Never set on a stdio server. */
  relay_group_id?: string | null;
  tool_count: number | null;
  tools_discovered_at: string | null;
  call_count: number;
  last_called_at: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * Importing an MCP configuration document — a client's `mcpServers` block or
 * a vendor's `server.json`. The backend parses both and hands back candidates
 * already expressed in this platform's own vocabulary, so nothing here has to
 * know which format the user picked.
 */
export interface MCPConfigCandidate {
  name: string;
  transport: "stdio" | "sse" | "streamable_http";
  command?: string | null;
  args: string[];
  env: Record<string, string>;
  url?: string | null;
  headers: Record<string, string>;
  /** Keys the document said were needed but supplied no usable value for. */
  missing_secrets: string[];
  /** Where in the document this entry came from. */
  source: string;
}

export interface MCPConfigPreviewResponse {
  format: string;
  candidates: MCPConfigCandidate[];
  /** Entries that could not be used, with the reason. Never fatal. */
  warnings: string[];
}

export interface MCPConfigImportResponse {
  created: MCPServer[];
  failed: { name: string; error: string }[];
}

export interface CreateMCPServerRequest {
  name: string;
  transport: "stdio" | "sse" | "streamable_http";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  auth_mode?: "headers" | "oauth";
  /** See `MCPServer.credential_mode`. */
  credential_mode?: MCPCredentialMode;
  client_id?: string;
  client_secret?: string;
  scopes?: string;
  authorize_url?: string;
  token_url?: string;
  registration_url?: string;
  agent_ids?: string[];
  tags?: string[];
  enabled?: boolean;
  hide_from_chat?: boolean;
  is_company_managed?: boolean;
  /** See `MCPServer`. Sending `null` withdraws the designation. */
  investigation_tool_name?: string | null;
  investigation_argument?: string | null;
  /** See `MCPServer.relay_group_id`. `null` = directly. */
  relay_group_id?: string | null;
}

/** Result of probing a remote MCP server for its OAuth metadata. */
export interface MCPOAuthDiscovery {
  authorize_url: string | null;
  token_url: string | null;
  /** The registration endpoint that would actually be used — advertised by the
   *  server, or the conventional `{origin}/register` when it advertises none. */
  registration_url: string | null;
  /** Whether `registration_url` was advertised rather than derived from
   *  convention. A derived one may simply 404. */
  registration_url_advertised?: boolean;
  resource: string | null;
  scopes: string | null;
  issuer: string | null;
  /** The server answered at least one well-known metadata probe. */
  discovered: boolean;
  /** A client ID can be obtained automatically — no credentials needed. */
  supports_dynamic_registration: boolean;
}

// Admin user management
export interface UserDetail {
  id: string;
  email: string;
  full_name: string;
  title: string | null;
  role: "admin" | "user";
  is_active: boolean;
  is_platform_admin: boolean;
  sso_provider: string | null;
  /** True when the account carries a local bcrypt password (registration,
   *  admin set-password, or password-reset). Hybrid accounts (SSO + local
   *  password) keep the password card available; only the SSO-only case
   *  (``sso_provider`` set AND ``has_local_password === false``) hides it.
   *  Optional so the type stays backward-compatible with cached payloads
   *  served by older backend builds; defaults to ``false`` in that case. */
  has_local_password?: boolean;
  preferred_llm_provider: string | null;
  preferred_llm_provider_id: string | null;
  preferred_llm_model: string | null;
  created_at: string;
  integration_count: number;
  mcp_server_count: number;
  agent_count: number;
  knowledge_base_count: number;
  /** TOTP two-factor status. Status only — the secret and recovery
   *  codes are never exposed, admins included. Drives the "Reset two-factor"
   *  action for a user who lost their device. */
  mfa_enabled?: boolean;
  mfa_confirmed_at?: string | null;
  provisioning_source?: ProvisioningSource;
  deactivated_at?: string | null;
  /** admin, scim, sso or system: who deactivated the account. */
  deactivation_source?: string | null;
  deprovisioned_at?: string | null;
}

export interface UserStats {
  agent_count: number;
  active_assignment_count: number;
  conversation_count: number;
  message_count: number;
  assignment_run_count: number;
  notification_count: number;
  integration_count: number;
  connected_integration_count: number;
  login_count: number;
  last_login_at: string | null;
  api_key_count: number;
  api_key_total_requests: number;
}

export interface UserAgentItem {
  id: string;
  name: string;
  template_slug: string | null;
  enabled: boolean;
  is_company_managed: boolean;
  created_at: string;
}

export interface UserKnowledgeBaseItem {
  id: string;
  name: string;
  document_count: number;
  chunk_count: number;
  is_company_managed: boolean;
  is_system_managed: boolean;
  created_at: string;
}

export interface UserIntegrationItem {
  id: string;
  provider: string;
  auth_type: string;
  status: string;
  name: string;
  is_company_managed: boolean;
  created_at: string;
  /** What `displayedIntegrationStatus` reads (#11837). */
  connection_test_status: string | null;
  has_access_token: boolean;
  scopes: string | null;
  required_scopes: string | null;
  missing_scopes: string[] | null;
}

export interface UserMCPServerItem {
  id: string;
  name: string;
  transport: string;
  is_company_managed: boolean;
  enabled: boolean;
  created_at: string;
}

export interface UserCustomToolItem {
  id: string;
  name: string;
  enabled: boolean;
  is_company_managed: boolean;
  created_at: string;
}

// Chat / Conversations
export interface ConversationListItem {
  id: string;
  channel: string;
  agent_id: string | null;
  agent_name: string | null;
  agent_icon: string | null;
  /** The agent answering an unbound conversation in the General Assistant's
   *  place: a Slack or Teams conversation continued on the web.
   *  Read through `conversationAgentId()`. */
  answering_agent_id?: string | null;
  transfer_source_agent_id: string | null;
  transfer_source_agent_name: string | null;
  title: string;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_preview: string | null;
  tool_count: number;
  /**
   * Presentation Studio: the deck this conversation is bound to, or
   * ``null`` for an ordinary chat. Bound conversations are the studio rail's
   * history for that deck; opened from the main chat list they still carry
   * the deck's context, just without the live preview beside them.
   */
  presentation_id?: string | null;
  /** Video Studio: the video this conversation is bound to, the same way. */
  video_project_id?: string | null;
  /** Dashboard Studio: the custom dashboard this conversation is bound to, the same way. */
  custom_dashboard_id?: string | null;
  /** Fleet page Modify with AI: the fleet this conversation changes, the same way (#10162). */
  fleet_id?: string | null;
  /**
   * The requester's access level on this conversation. ``read`` only
   * appears in the "shared with me" list; the user's own list is all
   * ``owner``.
   */
  access?: ConversationAccess;
  /**
   * Display name of the conversation's owner. Populated only for shared rows,
   * where "who sent me this" is the row's most useful context.
   */
  owner_name?: string | null;
  /** The workspace the conversation is filed in, or ``null``. */
  workspace_id?: string | null;
  /** The agent the conversation was held with when that agent was deleted,
   * and its last name: what to continue it from. */
  deleted_agent_id?: string | null;
  deleted_agent_name?: string | null;
}

/** One side of a change of agent: an agent, deleted maybe, or the General Assistant. */
export interface AgentSwitchSide {
  agent_id: string | null;
  agent_name: string | null;
  general_assistant: boolean;
  deleted: boolean;
}

/** How the platform resumed, by itself, the cut turn a reply finishes:
 *  the `resumed_from` of the message, also the `turn_resuming` frame's fields. */
export interface ResumedFromNote {
  /** `stop` (a platform restart) or `recycle` (a worker recycle). */
  cause?: string | null;
  /** Which automatic resume this is, of at most `of`. */
  resume?: number | null;
  of?: number | null;
  cut_at?: string | null;
}

/** The finished work a reply reports on when no message asked for it (#11796):
 *  the `wake` of the message, also on the wake turn's `thinking` frame. */
export interface WakeNote {
  /** `background_task_wake`. */
  source: string;
  /** The task's id, `a2a-partner` for a partner, or the work's label. */
  task_id?: string | null;
  /** The agent (or partner) that did the work. */
  agent_name?: string | null;
  /** `task`, `partner` (an A2A partner answered) or `job`; none on a note stored before the kind existed. */
  kind?: string | null;
}

/** The note written when a conversation continues with another agent. */
export interface AgentSwitchNote {
  /** Null when nothing recorded who held it before. */
  from_agent: AgentSwitchSide | null;
  to_agent: AgentSwitchSide | null;
  /** `continued` (the owner chose it) or `agent_deleted` (a deletion's successor). */
  reason: "continued" | "agent_deleted" | (string & {}) | null;
}

export interface ChatMessageAttachment {
  filename: string;
  type: string;
  size: number;
  file_id?: string | null;
  download_url?: string | null;
  /** The file's content was deleted (#396): the card shows it muted, never fetches it. */
  deleted_at?: string | null;
  /** `retention` | `owner_deleted` | `admin`. */
  deleted_reason?: string | null;
  content_type?: string | null;
  file_tag?: "working_file" | "download_file" | null;
  /** When the file was stored (ISO 8601); absent on messages saved before it was recorded. */
  created_at?: string | null;
  /** SHA-256 of the stored bytes (hex); absent when it was never computed. */
  sha256?: string | null;
  /** Client-only object URL for optimistic image previews (before upload
   *  resolves to a server `download_url`). Never sent by the backend. */
  preview?: string | null;
}

/**
 * Origin bucket for a conversation file, derived server-side from
 * ``FileAttachment.source``.
 *
 * ``uploaded``  – sent by the user (composer upload or an inbound channel
 *                 attachment from Slack / Teams / Discord / Telegram)
 * ``generated`` – produced by the agent (``generate_file``, generated media,
 *                 MCP tool output)
 * ``other``     – any source the backend does not classify; surfaced rather
 *                 than hidden so an unmapped writer is visible instead of
 *                 being silently mislabelled.
 */
export type ConversationFileOrigin = "uploaded" | "generated" | "other";

/** One file attached to, or produced inside, a conversation. */
export interface ConversationFile {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  created_at: string;
  /** The file's content was deleted (#396): the row stays muted, without a download. */
  deleted_at?: string | null;
  deleted_reason?: string | null;
  origin: ConversationFileOrigin;
  /** Raw backend ``source`` (e.g. ``chat``, ``channel:slack``, ``agent_generated``). */
  source: string;
  /**
   * Originating message, when the linkage exists. ``null`` for rows whose
   * ``message_id`` was never set or was dropped (it is ``ON DELETE SET NULL``),
   * in which case the panel hides its jump-to-message affordance.
   */
  message_id: string | null;
  download_url: string;
}

export interface ConversationFilesPage {
  files: ConversationFile[];
  total: number;
  /**
   * True when the requester is a non-owner admin viewing under an
   * acknowledged break-glass grant — keeps the confidentiality banner up for
   * the whole viewing session, not just at the moment of acknowledgement.
   */
  via_break_glass: boolean;
}

// Entity lists: library set vs. every row the viewer may open

/**
 * `?scope=` of the entity list endpoints and the run feeds. `visible` (the
 * default) is the library set - own, shared and company-managed rows - for
 * every viewer, admins included: what the library pages and the pickers list.
 * `all` is every row the viewer may open by id: the whole platform for an
 * admin, the library set for anyone else. Detail pages read `all` to show
 * what an entity links to (an agent's integrations and runs, the agents using
 * a knowledge base), so an admin sees there everything the detail routes open.
 */
export type EntityListScope = "visible" | "all";

/** Options of the entity list hooks (`useAgents`, `useIntegrations`, ...). */
export interface EntityListOptions {
  scope?: EntityListScope;
}

// Files repository (Work > Files)

/**
 * Where a file lives: bound to a conversation, produced by an assignment run,
 * or neither (dataset uploads, tool-test inputs, flow uploads, channel
 * attachments that never reached a conversation). ``other`` is surfaced, not
 * hidden — same rule as the origin bucket.
 */
export type FileContext = "conversation" | "run" | "other";

/** One row of the cross-context file repository. */
export interface RepositoryFile {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  created_at: string;
  /** Raw backend ``source`` — labels the ``other`` context (dataset, tool_test, flow, channel:*). */
  source: string;
  origin: ConversationFileOrigin;
  context: FileContext;
  conversation_id: string | null;
  conversation_title: string | null;
  assignment_run_id: string | null;
  /** The assignment's name, or the agent's for runs without one. */
  run_label: string | null;
  agent_name: string | null;
  download_url: string;
  /** Owner of the file, of its conversation, or admitted on its run — never a shared viewer. */
  can_delete: boolean;
  /** The file's content was deleted (#396): `retention` | `owner_deleted` | `admin`. */
  deleted_at?: string | null;
  deleted_reason?: string | null;
  /** When it goes if nobody uses it again; null when kept for good or already deleted. */
  expires_at?: string | null;
}

export interface RepositoryFilesPage {
  files: RepositoryFile[];
  total: number;
}

export interface RepositoryFilesQuery {
  limit?: number;
  offset?: number;
  search?: string;
  context?: FileContext;
  origin?: ConversationFileOrigin;
  /**
   * The visibility segment (`lib/visibility.ts`): `mine` = uploaded by me, in my
   * conversations or from my runs; `shared` = the rest I can read outside a
   * company-managed run; `company` = from a company-managed run, not mine.
   * Omitted = every file I can read.
   */
  vis?: "mine" | "shared" | "company" | "all";
  conversation_id?: string;
  assignment_run_id?: string;
  /** `live` (content kept), `deleted` (tombstones, #396); omitted = both. */
  status?: "live" | "deleted";
}

export type MessageFeedbackRating = "positive" | "negative";

export interface MessageFeedback {
  rating: MessageFeedbackRating;
  comment?: string | null;
}

/** Where a machine-activity card stands - `RECORD_STATUSES` on the backend. */
export type RunnerActivityStatus =
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "timeout"
  | "rejected"
  | "not_approved"
  | "refused"
  | "busy"
  | "unavailable"
  | "interrupted"
  | "stopped";

/** One entry of a folder listing a card shows. */
export interface RunnerActivityEntry {
  name: string;
  is_dir: boolean;
  size?: number;
}

/** What the agent asked the machine for, as a card displays it (bounded server-side). */
export interface RunnerActivityInput {
  command?: string;
  command_truncated?: boolean;
  cwd?: string;
  path?: string;
  /** `write_file`: the head of what was written. */
  content?: string;
  content_truncated?: boolean;
  content_bytes?: number;
  display?: string;
  action?: string;
  button?: string;
  browser?: string;
  x?: number;
  y?: number;
  x2?: number;
  y2?: number;
  amount?: number;
  keys?: string;
  url?: string;
  selector?: string;
  /** Typed text is carried as a length, never as text. */
  text_length?: number;
}

/** What came back from the machine, as a card displays it (bounded server-side). */
export interface RunnerActivityOutput {
  /** `run_shell` with `background: true`: the job it started on the machine. */
  background?: boolean;
  job_id?: string;
  pid?: number | null;
  /** The job's state when the card was written: `running`, `exited` or `stopped`. */
  job_status?: string;
  exit_code?: number | null;
  stdout?: string;
  stdout_truncated?: boolean;
  stderr?: string;
  stderr_truncated?: boolean;
  /** `read_file`: the head of the text read. */
  content?: string;
  content_truncated?: boolean;
  size?: number | null;
  binary?: boolean;
  bytes_written?: number | null;
  entries?: RunnerActivityEntry[];
  entry_count?: number;
  entries_truncated?: boolean;
  /** A capture or a binary read, downloadable through the chat file routes. */
  file_id?: string;
  width?: number;
  height?: number;
  url?: string;
  title?: string;
  content_chars?: number;
}

/**
 * One `computer_use` command the agent ran on a machine: the card the
 * transcript shows live (`runner_command_*` tool-progress frames) and keeps
 * with the message (`runner_activity`).
 */
export interface RunnerActivity {
  /** The runner command id (or a synthetic `local-...` key for a refusal that never reached a queue). */
  command_id: string;
  kind: string;
  status: RunnerActivityStatus;
  runner_id: string | null;
  runner_name: string | null;
  platform: RunnerPlatform | null;
  /** The machine the Desktop chat was sent from. */
  local: boolean;
  started_at: string | null;
  duration_ms: number | null;
  input: RunnerActivityInput;
  output: RunnerActivityOutput | null;
  error: string | null;
  /** Who stopped the command from its card; only on a `stopped` card, absent on one saved before. */
  stopped_by?: RunnerActivityStopper | null;
}

/** The person who stopped a runner command from its card. */
export interface RunnerActivityStopper {
  user_id: string;
  name: string | null;
}

/** A single tool-call execution record for the reasoning details panel. */
export interface ToolCallTraceEntry {
  name: string;
  /** JSON-serialized input arguments (already bounded server-side). */
  input: string;
  /** Tool result output (already bounded server-side). */
  output: string;
  success: boolean;
}

/** The agent a reply of a multi-agent turn is attributed to.
 */
interface MessageAttribution {
  agent_id: string;
  agent_name: string;
  agent_icon?: string;
}

/** An integration a reply's turn did not use for its user: not shared
 *  with them at all, its owner's personal connected account is not shared, or
 *  each person connects their own account and they have not.
 *  `name` only when the user can see the integration; `integration_id` for the
 *  last, whose page is where they connect. */
export interface UnavailableIntegration {
  provider: string;
  reason: "oauth_credentials_not_shared" | "integration_not_visible" | "own_account_not_connected";
  name?: string | null;
  integration_id?: string | null;
}

/** A conversation a message referenced with @. */
interface ConversationRef {
  conversation_id: string;
  title: string;
  /** What the message shows after ``@``: the title as a slug. */
  key: string;
}

/** `MessageResponse.content_format`: Slack's mrkdwn. */
type MessageContentFormat = "slack_mrkdwn";

/** The run a scheduled one-off's outcome message reports on (`scheduled_outcome`). */
interface ScheduledOutcomeNote {
  work_item_id: string;
  run_id?: string | null;
  state?: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  channel: string;
  channel_message_id: string | null;
  sequence: number;
  created_at: string;
  attachments?: ChatMessageAttachment[] | null;
  tool_names?: string[] | null;
  tool_call_count?: number | null;
  iterations?: number | null;
  transfer_chain?: { agent_id: string; agent_name: string }[] | null;
  /** Proposals this message asked about that nobody answered, retired because
   *  the conversation moved on (FR-002c4). Rendered read-only as
   *  **expired** rather than dropped: a gated call that never ran should not
   *  disappear from the conversation that produced it. Never answerable. */
  expired_approvals?: ActionProposal[] | null;
  user_feedback?: MessageFeedback | null;
  /** Per-tool-call execution trace (name, input, output, success). */
  tool_call_trace?: ToolCallTraceEntry[] | null;
  /** What the agent did on a machine during the turn, one card per command. */
  runner_activity?: RunnerActivity[] | null;
  /** True when the agent turn was cut short by the iteration budget. */
  is_truncated?: boolean | null;
  /** True when a server restart cut the turn: the message names what it did. */
  interrupted?: boolean | null;
  /** Accumulated model reasoning prose captured during the turn. */
  reasoning?: string | null;
  llm_trace_id: string | null;
  /** Platform-injected wake (hidden from the transcript). */
  hidden?: boolean | null;
  /** The agent that wrote this reply, on a turn several @mentioned agents answered. */
  answered_by?: MessageAttribution | null;
  /** A note written in place of a mentioned agent that could not answer. */
  mention_failed?: boolean | null;
  /** The note of a conversation continued with another agent. */
  agent_switch?: AgentSwitchNote | null;
  /** The reply of a turn the platform resumed by itself after a cut. */
  resumed_from?: ResumedFromNote | null;
  /** The reply of a turn finished background work woke, no message asking for it. */
  wake?: WakeNote | null;
  /** The run a scheduled one-off's outcome reports on: the chat's own "View run" action. */
  scheduled_outcome?: ScheduledOutcomeNote | null;
  /** The conversations a user message referenced with @, as it was
   *  sent - a reference to open, never the other conversation's content. */
  conversation_refs?: ConversationRef[] | null;
  /** The markup of a reply an agent wrote under a channel's format override;
   * absent: the Markdown the transcript renders, whatever the channel. */
  content_format?: MessageContentFormat | null;
  /** The integrations this reply's turn did not use for its user, and why -
   * shown under the reply, never a silent absence. */
  unavailable_integrations?: UnavailableIntegration[] | null;
  /** The chart blocks the reply carries, in the order they stand in
   *  `content`, which holds them as `xtm-chart` fences the chat draws in place
   *  (`components/chat/chat-chart.ts`). Read from the content, never apart. */
  charts?: ChatChartBlock[] | null;
  /** How full the context window was at the last model call of the run that
   *  wrote this reply: the composer's gauge. Char-derived estimates.
   *  `context_level` is the loop's own warning. */
  context_tokens?: number | null;
  context_window?: number | null;
  context_breakdown?: ContextBreakdown | null;
  context_level?: ContextUsageLevel | null;
}

/** What a context window holds, by kind of content. The buckets sum to
 *  the reading's tokens; an empty one is absent. */
interface ContextBreakdown {
  system?: number;
  tools?: number;
  dynamic_tools?: number;
  summary?: number;
  tool_results?: number;
  conversation?: number;
}

/** What the agent loop is doing to the conversation's context, decided
 *  by the loop on its compaction gate's own measure - never re-derived from
 *  the reading's numbers, which also count the tool definitions. */
type ContextUsageLevel = "normal" | "approaching" | "overflowed";

interface ToolOverrides {
  builtin_tool_ids: string[];
  custom_tool_ids: string[];
  integration_ids: string[];
  mcp_server_ids: string[];
  knowledge_base_ids: string[];
  /**
   * Snapshot of the agent / General Assistant default tool set at the time
   * these overrides were saved (flat union of default tool ids across
   * categories). Used on reopen to auto-enable tools bound to the agent
   * AFTER the last save without re-enabling tools the user explicitly
   * disabled. ``null``/absent for overrides saved before this field existed
   * (or sent without a baseline), in which case the restore falls back to the
   * saved enabled set as the baseline.
   */
  default_tool_ids?: string[] | null;
}

/** Persisted server-side compaction summary of a long conversation. */
interface ConversationCompactionInfo {
  directives: string;
  summary: string;
  compacted_count: number;
}

/**
 * A turn of the conversation that ended without its answer: its
 * process stopped (a restart, a deploy, a crash) or it was closed as
 * interrupted. Served until the next turn starts or the owner dismisses it.
 */
export interface InterruptedTurn {
  started_at: string | null;
  /** Null when its process stopped without a word. */
  interrupted_at: string | null;
  /** Completed tool calls the next turn resumes instead of running again. */
  tool_calls_kept: number;
  /** Queued for its automatic resume: it resumes by itself, no click. */
  auto_resume?: boolean;
}

/** One piece of work the conversation waits on, which resumes it once it ends. */
export interface AwaitedWorkItem {
  kind: "video_take" | "video_render" | "video_clip" | "video_music" | (string & {});
  id: string;
  name: string | null;
  status: "queued" | "running" | (string & {});
  percent: number;
  stage: string | null;
}

export interface ConversationDetail {
  id: string;
  /** The workspace this thread is filed under, or null when unfiled.
   *  Agents working in the same workspace share what they collect there. */
  workspace_id?: string | null;
  channel: string;
  agent_id: string | null;
  /** See `ConversationListItem.answering_agent_id`. */
  answering_agent_id?: string | null;
  /** See `ConversationListItem.deleted_agent_id`. */
  deleted_agent_id?: string | null;
  deleted_agent_name?: string | null;
  transfer_source_agent_id: string | null;
  transfer_source_agent_name: string | null;
  title: string;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
  tool_overrides: ToolOverrides | null;
  /** Presentation Studio: the deck this conversation is bound to. */
  presentation_id?: string | null;
  /** Video Studio: the video this conversation is bound to. */
  video_project_id?: string | null;
  /** Dashboard Studio: the custom dashboard this conversation is bound to. */
  custom_dashboard_id?: string | null;
  /** Fleet page Modify with AI: the fleet this conversation changes (#10162). */
  fleet_id?: string | null;
  is_processing?: boolean;
  /** Proposals an in-flight turn is paused on, so a client that reloaded
   *  through an approval prompt can render it again instead of facing a
   *  conversation that has silently stopped answering. */
  pending_approvals?: ActionProposal[];
  /** A turn that ended without its answer, when none is in flight. */
  interrupted_turn?: InterruptedTurn | null;
  /** True when messages older than the returned page exist on the server. */
  has_more?: boolean;
  /** Persisted compaction summary (null until the first compaction cycle). */
  compaction?: ConversationCompactionInfo | null;
  /**
   * The requester's access level. A ``read`` viewer gets the transcript
   * and its attachments and nothing else.
   *
   * This is a *reflection* of the server's decision, never the enforcement —
   * every write endpoint re-derives ownership for itself, so a tampered
   * client gains nothing but a UI that offers actions the API refuses.
   */
  access?: ConversationAccess;
  /** Link tier in force. Owner-only — ``null`` for a shared viewer. */
  link_access?: ConversationLinkAccess | null;
  /**
   * Display name of the owner, for the "Shared with you by X" banner.
   * ``null`` when the requester *is* the owner, or when the conversation has
   * no owner (channel-originated threads).
   */
  owner_name?: string | null;
  /**
   * Whether the web may send in this conversation: ``false`` for a
   * Slack or Teams group thread, which is continued on its channel only.
   */
  web_continuable?: boolean;
  /** The channels this conversation is also continued in, owner-only. */
  continued_in?: ContinuationChannel[];
}

/* ------------------------------------------------------------------ */
/*  Continuing a conversation in Slack / Teams                 */
/* ------------------------------------------------------------------ */

/** A channel a conversation can be continued in. */
export type ContinuationChannel = "slack" | "teams";

/** Why a conversation cannot be continued in a channel right now. */
export type ContinuationUnavailableReason =
  | "not_continuable"
  | "not_configured"
  | "direct_messages_off"
  | "agent_unavailable"
  | "teams_chat_unknown"
  | "started_there";

/** A direct-message thread the conversation is continued in. */
export interface ConversationChannelBinding {
  channel: ContinuationChannel;
  created_at: string;
  updated_at: string;
}

export interface ContinuationChannelState {
  channel: ContinuationChannel;
  available: boolean;
  reason: ContinuationUnavailableReason | null;
  /** The server's sentence, shown when the reason is not one the UI words. */
  detail: string | null;
}

export interface ConversationChannelBindings {
  bindings: ConversationChannelBinding[];
  channels: ContinuationChannelState[];
}

/* ------------------------------------------------------------------ */
/*  Conversation sharing                                        */
/* ------------------------------------------------------------------ */

/** What the requester may do with a conversation. */
type ConversationAccess = "owner" | "read";

/**
 * How far the conversation URL reaches on its own.
 *
 * ``private``  – owner only; pasting the link grants nothing.
 * ``org_read`` – any *authenticated* platform user holding the link may read
 *                it. Not anonymous access: the reader still signs in, still
 *                appears in the audit trail, and loses access the moment the
 *                owner switches back to ``private``.
 */
export type ConversationLinkAccess = "private" | "org_read";

/** One grant on a conversation, resolved for display. */
export interface ConversationShare {
  id: string;
  principal_type: "user" | "group";
  principal_id: string;
  principal_name: string;
  /** Users only — disambiguates two colleagues with the same display name. */
  principal_email: string | null;
  permission: "read";
  created_at: string;
}

/** The full sharing state of a conversation (owner-only). */
export interface ConversationSharesState {
  shares: ConversationShare[];
  link_access: ConversationLinkAccess;
}

/** A user or group the conversation can be shared with (picker row). */
export interface ConversationShareTarget {
  type: "user" | "group";
  id: string;
  name: string;
  email?: string | null;
}

export interface ConversationShareTargets {
  users: ConversationShareTarget[];
  groups: ConversationShareTarget[];
}

// Agentic step types for assignment run conversation log
interface AgenticToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

interface AgenticStepToolCalls {
  type: "tool_calls";
  content: string;
  tool_calls: AgenticToolCall[];
}

interface AgenticStepToolResult {
  type: "tool_result";
  name: string;
  tool_call_id: string;
  content: string;
}

interface AgenticStepResponse {
  type: "response";
  content: string;
}

export type AgenticStep = AgenticStepToolCalls | AgenticStepToolResult | AgenticStepResponse;

// Assignment Run Details
export interface AssignmentRunAttachment {
  file_id: string;
  filename: string;
  content_type: string;
  size: number;
  download_url: string;
  /** The file's content was deleted (#396). */
  deleted_at?: string | null;
  deleted_reason?: string | null;
  file_tag?: "working_file" | "download_file" | null;
  created_at?: string | null;
  sha256?: string | null;
}

/** One friction of a run's ledger (`services/frictions.serialize_friction`, #8528). */
export interface RunFriction {
  id: string;
  kind: string;
  tool: string;
  evidence: string;
  count: number;
  class: string;
  disposition: { kind: string; ref: string | null } | null;
  recorded_at: string | null;
  last_seen_at: string | null;
  disposed_at: string | null;
}

/**
 * Where an external run stands in its A2A task's exchange with the caller
 * (`a2a.run_exchange.run_exchanges`). A linked run id is null when the viewer
 * cannot open that run, or for a run answered before the links were recorded.
 */
export interface A2AExchange {
  /** This run asked its caller a question (`ask_the_caller`). */
  asked?: {
    question: string;
    /** `waiting`: the task is input-required on it; `withdrawn`: cancelled before an answer. */
    state: "waiting" | "answered" | "withdrawn";
    answered_by_run_id: string | null;
  };
  /** This run continues the task after its caller answered. */
  answers?: {
    question: string;
    answer: string;
    previous_run_id: string | null;
  };
}

export interface AssignmentRunDetail {
  id: string;
  assignment_id: string | null;
  assignment_name: string | null;
  agent_id: string;
  agent_name: string;
  agent_icon: string;
  status: string;
  /** What the run's end says about its agent (`run_outcome.run_outcome`):
   *  `interrupted` for a failed run the platform ended. */
  outcome?: "ok" | "failed" | "interrupted" | "active" | "other" | null;
  is_company_managed: boolean;
  notify_on_complete: boolean;
  show_in_digest: boolean;
  parent_run_id: string | null;
  child_run_count: number;
  parent_agent_name: string | null;
  parent_agent_icon: string | null;
  trigger_payload: Record<string, unknown> | null;
  trigger_context: Record<string, unknown> | null;
  /** The run's place in its A2A task's exchange with the caller; null on any other run. */
  a2a_exchange?: A2AExchange | null;
  actions_taken: ActionTaken[] | null;
  result_summary: string | null;
  rendered_prompt: string | null;
  agentic_steps: AgenticStep[] | null;
  attachments: AssignmentRunAttachment[] | null;
  /** The friction ledger of the run, oldest first; null when it recorded none. */
  frictions?: RunFriction[] | null;
  /** Frictions the run left with no disposition at its end; null: none. */
  frictions_undisposed?: number | null;
  /** The run's ROI record for the outcome review row; null when it recorded no unit. */
  roi?: RoiReviewBlock | null;
  error: string | null;
  /** Structured failure attribution — see `AssignmentRun`. */
  failure_cause: string | null;
  failure_determination: string | null;
  /** Why a completed run stopped early — see `AssignmentRun`. */
  stop_reason: string | null;
  human_input_question: string | null;
  human_input_response: string | null;
  human_input_history: HumanInputHistoryEntry[] | null;
  /** Populated when the run paused on the per-tool approval gate. */
  pending_approvals: ActionProposal[] | null;
  /** Decided already; waiting on the worker, not on the reviewer. */
  approval_answered?: boolean;
  langfuse_trace_id: string | null;
  llm_trace_id: string | null;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

/** Result of opening an interactive follow-up chat seeded from a run. */
export interface FollowupChatResult {
  conversation_id: string;
  agent_id: string;
}

// User API Keys (LLM Proxy)
/** A surface a personal API key can be scoped to. */
export type ApiKeySurface = "rest" | "llm_proxy" | "mcp";
export type ApiKeyQuotaPeriod = "daily" | "monthly";

interface ApiKeyAgent {
  id: string;
  /** `null` once the agent was deleted. */
  name: string | null;
}

export interface UserApiKey {
  id: string;
  name: string;
  key_prefix: string;
  is_active: boolean;
  is_from_env: boolean;
  last_used_at: string | null;
  request_count: number;
  created_at: string;
  /** The surfaces the key authenticates on: every surface the deployment serves when `all_surfaces`. */
  surfaces: ApiKeySurface[];
  all_surfaces: boolean;
  read_only: boolean;
  /** `null` = every agent the owner can use. */
  agent_ids: string[] | null;
  agents: ApiKeyAgent[];
  expires_at: string | null;
  expired: boolean;
  revoked_at: string | null;
  rate_limit_per_minute: number | null;
  quota_limit: number | null;
  quota_period: ApiKeyQuotaPeriod | null;
  /** Requests counted in the current quota period, `null` without a quota. */
  quota_used: number | null;
  /** Every surface, read-write, every agent: what a key created before scopes has. */
  full_scope: boolean;
  /** Lasts longer than the administrator's maximum lifetime allows a new key. */
  beyond_max_lifetime: boolean;
}

export interface UserApiKeyCreated extends UserApiKey {
  key: string;
}

/** What a new key may be given on this deployment. */
export interface ApiKeyPolicy {
  surfaces: ApiKeySurface[];
  /** 0 = no maximum lifetime. */
  max_lifetime_days: number;
  quota_periods: ApiKeyQuotaPeriod[];
  max_rate_limit_per_minute: number;
  max_quota_limit: number;
  max_agents: number;
}

/** A key's scopes and limits as written: `null` clears a scope or a limit. */
interface ApiKeyScopeFields {
  surfaces: ApiKeySurface[] | null;
  read_only: boolean;
  agent_ids: string[] | null;
  expires_at: string | null;
  rate_limit_per_minute: number | null;
  quota_limit: number | null;
  quota_period: ApiKeyQuotaPeriod | null;
}

export interface CreateApiKeyRequest extends Partial<ApiKeyScopeFields> {
  name: string;
}

/** Only the fields present change. */
export type UpdateApiKeyRequest = Partial<ApiKeyScopeFields> & { name?: string };

export interface AdminApiKey extends UserApiKey {
  owner_id: string;
  owner_email: string;
  owner_name: string | null;
  owner_active: boolean;
}

/** Totals over every key the filters match, not only the page served. */
interface AdminApiKeySummary {
  /** Neither revoked nor expired. */
  usable: number;
  request_count: number;
  last_used_at: string | null;
  last_used_key_name: string | null;
  most_active_key_name: string | null;
  most_active_request_count: number;
}

export interface AdminApiKeyPage {
  items: AdminApiKey[];
  total: number;
  /** Only when asked for with `withSummary`. */
  summary?: AdminApiKeySummary | null;
}

export type AdminApiKeyStatus = "all" | "active" | "expired" | "revoked";
export type AdminApiKeyIssue = "no_expiry" | "full_scope" | "beyond_max_lifetime";

// --- Runners (native, pull-based OS-control daemons) ---
export type RunnerPlatform = "windows" | "macos" | "linux";
export type RunnerCapability =
  | "screenshot"
  | "computer_action"
  | "run_shell"
  | "file_ops"
  | "browser_control";
export type RunnerPermissionMode = "restricted" | "full_control";
type RunnerStatus = "online" | "offline" | "revoked";

/** Loose shape of the host context a runner reports on connect. Everything is
 *  optional — the runner fills in what the OS exposes. */
interface RunnerHostFacts {
  os?: string;
  os_version?: string;
  os_build?: string;
  arch?: string;
  hostname?: string;
  user?: string;
  shells?: Record<string, string>;
  browsers?: { name: string; version?: string; authenticated?: boolean }[];
  monitors?: { width?: number; height?: number; primary?: boolean }[];
  timezone?: string;
  locale?: string;
  agent_version?: string;
  [key: string]: unknown;
}

export interface Runner {
  id: string;
  user_id: string | null;
  name: string;
  platform: RunnerPlatform;
  hostname: string | null;
  key_prefix: string;
  permission_mode: RunnerPermissionMode;
  capabilities: RunnerCapability[];
  allowed_shell_commands: string[] | null;
  blocked_shell_commands: string[] | null;
  allowed_paths: string[] | null;
  require_approval: boolean;
  labels: string[];
  tags: string[];
  host_facts: RunnerHostFacts | null;
  agent_version: string | null;
  status: RunnerStatus;
  online: boolean;
  last_seen_at: string | null;
  transport: "ws" | "long_poll" | null;
  is_active: boolean;
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  command_count: number;
  can_manage: boolean;
  enrolled_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunnerUpdate {
  name?: string;
  labels?: string[];
  tags?: string[];
  permission_mode?: RunnerPermissionMode;
  capabilities?: RunnerCapability[];
  allowed_shell_commands?: string[];
  blocked_shell_commands?: string[];
  allowed_paths?: string[];
  require_approval?: boolean;
  is_company_managed?: boolean;
  shared_group_ids?: string[];
  shared_group_permissions?: Record<string, string>;
  shared_user_ids?: string[];
  shared_user_permissions?: Record<string, string>;
  access_restricted?: boolean;
}

export interface RunnerCommand {
  id: string;
  runner_id: string;
  user_id: string | null;
  agent_id: string | null;
  conversation_id: string | null;
  assignment_run_id: string | null;
  kind: string;
  payload: Record<string, unknown> | null;
  status: string;
  result: Record<string, unknown> | null;
  result_file_id: string | null;
  error: string | null;
  created_at: string;
  dispatched_at: string | null;
  completed_at: string | null;
}

/** One background shell job a machine holds (`GET /runners/{id}/jobs`). */
export interface RunnerJob {
  /** The id of the `run_shell` command that started it: `POST /runners/commands/{job_id}/stop` stops it. */
  job_id: string;
  command: string;
  pid: number | null;
  started_at: string;
  status: "running" | "exited" | "stopped";
  ended_at: string | null;
  exit_code: number | null;
  output_bytes: number;
}

/** Why a machine's jobs could not be listed: offline, not_allowed, busy, revoked or no_answer. */
export interface RunnerJobsAnswer {
  available: boolean;
  reason: string | null;
  jobs: RunnerJob[];
}

export interface RunnerEnrollmentKey {
  id: string;
  name: string;
  key_prefix: string;
  max_uses: number | null;
  use_count: number;
  default_labels: string[];
  default_capabilities: RunnerCapability[];
  default_permission_mode: RunnerPermissionMode;
  /** Seeds `Runner.require_approval` on the enrolled machine: the key
   *  form's "Shell commands need approval" switch, off unless turned on - no
   *  preset implies it. Optional for backward-compatibility with older
   *  backends. */
  default_require_approval?: boolean;
  enroll_as_company_managed: boolean;
  is_active: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export interface RunnerEnrollmentKeyCreated extends RunnerEnrollmentKey {
  key: string;
}

export interface RunnerEnrollmentKeyCreate {
  name: string;
  max_uses?: number | null;
  default_labels?: string[];
  default_capabilities?: RunnerCapability[];
  default_permission_mode?: RunnerPermissionMode;
  default_require_approval?: boolean;
  enroll_as_company_managed?: boolean;
  ttl_seconds?: number | null;
}

// --- Relays ---------------------------------------------------------

/** A relay group: a customer network the platform reaches through its relays. */
export interface RelayGroup {
  id: string;
  user_id: string | null;
  name: string;
  slug: string;
  zone: string | null;
  description: string | null;
  /** Platform-side destination list; null = no restriction here (each
   *  relay's own local list still decides). */
  allowed_destinations: string[] | null;
  tags: string[];
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  relays_online: number;
  relays_total: number;
  /** Integrations, MCP servers and flows routed through the group. */
  routed_count: number;
  can_manage: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface RelayGroupCreate {
  name: string;
  zone?: string | null;
  description?: string | null;
  allowed_destinations?: string[] | null;
  tags?: string[];
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

export type RelayGroupUpdate = Partial<RelayGroupCreate>;

type RelayStatus = "online" | "offline" | "revoked" | string;

/** One enrolled relay (a machine or a container) of a group. */
export interface Relay {
  id: string;
  group_id: string;
  name: string;
  platform: string;
  hostname: string | null;
  key_prefix: string;
  host_facts: Record<string, unknown> | null;
  agent_version: string | null;
  protocol_version: number | null;
  capabilities: string[];
  /** The relay's own local allow-list, as it declared it. */
  declared_destinations: string[];
  allows_all_destinations: boolean;
  max_concurrent_requests: number | null;
  status: RelayStatus;
  online: boolean;
  last_seen_at: string | null;
  transport: "ws" | "long_poll" | null;
  is_active: boolean;
  request_count: number;
  inflight: number | null;
  /** Older than RELAY_MIN_SUPPORTED_VERSION. */
  outdated: boolean;
  enrolled_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  can_manage?: boolean;
}

export interface RelayEnrollmentKey {
  id: string;
  group_id: string;
  name: string;
  key_prefix: string;
  /** null = reusable (a container image rolled out to many replicas). */
  max_uses: number | null;
  use_count: number;
  is_active: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string | null;
}

export interface RelayEnrollmentKeyCreate {
  name: string;
  max_uses?: number | null;
  ttl_seconds?: number | null;
}

/** The key as created: the raw `rlenr-` secret is returned once. */
export interface RelayEnrollmentKeyCreated extends RelayEnrollmentKey {
  key: string;
  group_slug: string;
  server_url: string;
}

export interface RelayMeta {
  platforms: string[];
  capabilities: string[];
  reserved_capabilities: string[];
  source_types: string[];
  protocol_version: number;
  min_supported_version: string | null;
  /** The relay build this deployment serves (and self-updates to). */
  relay_version: string | null;
  server_url: string;
  /** Installer URLs, null when this deployment does not embed them. */
  install_scripts: { sh: string | null; ps1: string | null };
  can_create_groups: boolean;
}

export interface RelayRoutedEntry {
  id: string;
  name: string;
  /** Integrations only. */
  provider?: string;
}

/** What routes through a group: by name what the reader may see, else counted in `hidden`. */
export interface RelayRoutedResources {
  integrations: RelayRoutedEntry[];
  mcp_servers: RelayRoutedEntry[];
  flows: RelayRoutedEntry[];
  hidden: number;
  total: number;
}

export interface RelayGroupStats {
  hours: number;
  requests: number;
  errors: number;
  bytes_sent: number;
  bytes_received: number;
  avg_duration_ms: number | null;
  buckets: { at: string | null; requests: number; errors: number }[];
}

/** One relayed request: metadata only, never a header or a body. */
export interface RelayRequestLog {
  id: string;
  group_id: string;
  relay_id: string | null;
  source_type: string | null;
  source_id: string | null;
  method: string | null;
  scheme: string | null;
  host: string | null;
  port: number | null;
  path: string | null;
  status_code: number | null;
  error_code: string | null;
  bytes_sent: number;
  bytes_received: number;
  duration_ms: number | null;
  created_at: string | null;
}

export interface RelayRequestPage {
  items: RelayRequestLog[];
  total: number;
}

export interface RelayRequestFilters {
  /** The relays of the group whose requests are kept (any of them); none is every relay. */
  relay_id?: readonly string[];
  outcome?: "ok" | "error";
  host?: string;
  source_type?: string;
  /** Order by date: newest first (`desc`, the default) or oldest first. */
  direction?: "asc" | "desc";
}

export interface RelayTestResult {
  ok: boolean;
  status_code?: number;
  duration_ms?: number;
  relay_id?: string | null;
  error_code?: string;
  message?: string;
}

/**
 * What the XTM One Desktop native shell injects into the page it hosts
 * (`window.__XTM_ONE_DESKTOP__`). Read-only facts about the machine the
 * user is sitting at; `runner_id` is what a chat turn sends as
 * `desktop_runner_id` so the agent's `computer_use` tools default to it.
 * `null`s cover a shell that opened the window before enrollment finished.
 */
export interface DesktopBridge {
  /** Desktop app build (`MAJOR.YYMMDD.SEQ`). */
  version: string;
  platform: RunnerPlatform;
  arch: string;
  runner_id: string | null;
  runner_name: string | null;
  /** The instance URL the app is enrolled with (this origin). */
  server_url: string;
  /**
   * What the shell can do beyond the original bridge: `local_paths`
   * (open / show a local path), `attention` (flash the taskbar when a turn
   * ends in the background), `session` (keep the web session). Empty
   * for a shell that predates them.
   */
  features: string[];
  /** Extensions the shell opens with their default application (`local_paths`). */
  open_extensions: string[];
}

/**
 * The web session as XTM One Desktop keeps it for the page: the
 * session token and, while an administrator impersonates someone, the
 * administrator's own. Both `null` once the page signed out. Mirrors
 * `WebSession` in `apps/runner/src/web_session.rs`.
 */
export interface DesktopSession {
  token: string | null;
  admin_token: string | null;
}

/** The SSO providers the Desktop app can run in the system browser. */
export type DesktopBrowserLoginProvider = "oidc" | "saml";

/** What the shell answers when it opened a browser sign-in: the pending login's id and its window. */
export interface DesktopBrowserLoginStarted {
  login_id: string;
  expires_in_seconds: number;
}

/** How a browser sign-in ended, as the shell's `desktop-browser-login` event carries it. */
export type DesktopBrowserLoginOutcome =
  | { status: "completed"; login_id: string; access_token: string }
  | { status: "failed"; login_id: string; reason: string };

/**
 * What the browser's confirmation page may know about a pending Desktop
 * sign-in (`GET /desktop-login/{id}`, unauthenticated and deliberately thin):
 * which machine is asking and for how long - never its id, never its state.
 */
export interface DesktopLoginHandoff {
  runner_name: string;
  platform: RunnerPlatform | string;
  /** Display name of whoever enrolled the machine; `null` for a company-managed one. */
  enrolled_by: string | null;
  expires_in_seconds: number;
}

/**
 * A runner command the agent asked for and the machine's policy holds for the
 * user's decision, as the `runner_approval_required` tool-progress frame
 * carries it. Rendered as the inline approval card above the chat
 * composer; approve / reject go through the existing runner command routes.
 */
export interface RunnerApprovalRequest {
  command_id: string;
  runner_id: string;
  runner_name: string;
  kind: string;
  /** The shell command (with its cwd) / path / action the agent asked for. */
  summary: string;
  /** How long the agent's tool waits for the decision before moving on. */
  expires_in_seconds: number;
  /** `Date.now()`-scale instant the card was received (drives the countdown). */
  received_at_ms: number;
}

export interface RunnerCapabilitiesInfo {
  capabilities: RunnerCapability[];
  permission_modes: RunnerPermissionMode[];
  platforms: RunnerPlatform[];
}

/** Download manifest served by `GET /runners/update-manifest` — powers the
 *  XTM One Desktop page download card (version badge + per-OS installer buttons).
 *  Artifacts are built by CI and embedded in the platform image;
 *  `download_configured` is false on source/dev builds that carry no embedded
 *  distribution, in which case every `installer_url` is null and the buttons
 *  render an explanatory hint instead of a link. The runner's own bundle
 *  updater uses `GET /runners/updater/...`, not this. */
interface RunnerPlatformDownload extends RunnerArchDownload {
  /** One entry per architecture the release ships (`x64`, `arm64`). The flat
   *  fields above describe the x64 build — the default a browser gets. */
  architectures?: Record<string, RunnerArchDownload>;
  /** True when a single binary covers every architecture (the macOS universal
   *  bundle): the arm64 entry is then the SAME file, so offering it as a
   *  separate download would just duplicate the button. */
  universal?: boolean;
}

/** The downloads for ONE architecture of one OS. */
interface RunnerArchDownload {
  installer_url: string | null;
  /** Linux only: alternate package formats (appimage / deb / rpm). */
  formats?: Record<string, string | null>;
}

export interface RunnerDownloadManifest {
  version: string;
  /** Version of the runner build embedded in this deployment (null when none). */
  runner_version: string | null;
  min_supported_version: string | null;
  download_configured: boolean;
  platforms: Record<RunnerPlatform, RunnerPlatformDownload>;
  /** Bare-binary archives for command-line installs: os -> arch -> URLs.
   *  `headless` hangs off the same entry where that (os, arch) also ships the
   *  headless build — same machine, the flavor that links no desktop stack. */
  artifacts: Record<
    string,
    Record<
      string,
      {
        url: string;
        sha256_url?: string;
        signature_url?: string;
        headless?: { url: string; sha256_url?: string; signature_url?: string };
      }
    >
  >;
  /** Headless install scripts (sh = macOS/Linux, ps1 = Windows; null when not embedded). */
  install_scripts: { sh: string | null; ps1: string | null };
}

// Langfuse Configuration
export interface LangfuseConfig {
  enabled: boolean;
  has_public_key: boolean;
  has_secret_key: boolean;
  host: string;
  /** The keys read from a vault. */
  vault_secrets?: VaultSecretStatus | null;
  /**
   * True when seeded from LANGFUSE_* environment variables and therefore
   * read-only in the UI (mirrors the env-seeded LLM provider lock).
   */
  is_from_env: boolean;
}

/** Kind of a Settings > Access and security option (`app/core/security_options.py`). */
type SecurityOptionKind = "bool" | "choice" | "int" | "origins" | "modules" | "tools" | "certificates";
export type SecurityOptionValue = boolean | number | string | string[] | null;

/** One security option: the environment variable wins and locks it. */
export interface SecurityOption {
  key: string;
  env_var: string;
  kind: SecurityOptionKind;
  group: string;
  choices: string[];
  minimum: number | null;
  maximum: number | null;
  /** The one value outside the bounds that turns the option off (`0` for the idle timeout); `null` when none. */
  off_value: number | null;
  /** The effective value, the default, and what the database holds (null when nothing is stored). */
  value: SecurityOptionValue;
  default: SecurityOptionValue;
  stored: SecurityOptionValue;
  source: "platform_mode" | "environment" | "database" | "default";
  /** Read-only, a write is refused (403): see `locked_reason`. */
  locked: boolean;
  /**
   * Why it is read-only: the platform mode fixes it (in XTM One, quotas always
   * fail closed) or its environment variable sets it; `null` when editable.
   */
  locked_reason: "platform_mode" | "environment" | null;
}

export interface SecurityOptionsResponse {
  options: SecurityOption[];
  /** Option keys their environment variable decides (never a fixed one), like `GeneralAssistantConfig.env_managed_fields`. */
  env_managed_fields: string[];
}

export interface UpdateSecurityOptionsRequest {
  /** `null` resets an option to its default. */
  values: Record<string, SecurityOptionValue>;
}

/** A tool the prompt guard can screen: what the scan-scope editor offers. */
export interface ScannableTool {
  /** What "Tools screened by the prompt guard" stores for it: its name, or `custom:<id>` for a custom tool. */
  entry: string;
  /** Its current name. */
  name: string;
  description: string;
}

export interface ScannableToolGroup {
  /** `builtin`, `custom`, `mcp:<server id>` or an integration provider key. */
  key: string;
  label: string;
  tools: ScannableTool[];
}

export interface ScannableToolsResponse {
  groups: ScannableToolGroup[];
}

export interface ObservabilityConfig {
  trace_store_provider: "postgres" | "clickhouse";
  trace_store_health_status: "healthy" | "unhealthy";
  trace_store_health_message: string;
  trace_storage_enabled: boolean;
  trace_retention_days: number;
  audit_log_retention_days: number;
  trace_store_payloads: boolean;
  last_aggregation_at: string | null;
  aggregation_stale: boolean;
  pricing_last_updated_at: string | null;
  pricing_stale: boolean;
  pricing_staleness_days: number | null;
  models_without_pricing: string[];
}

interface ModelPricingExampleSource {
  key: string;
  label: string;
  path: string;
  exists: boolean;
}

export interface ModelPricingStatus {
  source_path: string;
  source_exists: boolean;
  source_readable: boolean;
  detected_schema_version: string | null;
  detected_updated_at: string | null;
  detected_model_count: number;
  examples: ModelPricingExampleSource[];
}

export interface ModelPricingSyncResult {
  status: string;
  source_path: string;
  synced_count: number;
  skipped_custom_count: number;
  pricing_last_updated_at: string | null;
  message: string;
}

export interface ModelPricingRepriceResult {
  status: string;
  dry_run: boolean;
  traces_updated: number;
  spans_updated: number;
  traces_skipped_no_price: number;
  traces_skipped_no_usage: number;
  traces_skipped_effective_from: number;
  spans_skipped_no_price: number;
  spans_skipped_no_usage: number;
  spans_skipped_effective_from: number;
  days_reaggregated: number;
  message: string;
}

export interface ModelPricingRow {
  id: string;
  provider: string;
  model_id: string;
  model_pattern: string;
  input_price_per_1m: number;
  output_price_per_1m: number;
  cache_read_price_per_1m: number | null;
  cache_write_price_per_1m: number | null;
  effective_from: string;
  is_custom: boolean;
  created_at: string;
}

export interface ModelPricingRowListResponse {
  items: ModelPricingRow[];
  total: number;
}

interface ModelPricingUnpricedModel {
  model: string;
  unresolved_count: number;
}

export interface ModelPricingUnpricedModelListResponse {
  items: ModelPricingUnpricedModel[];
  total: number;
}

// Dashboard
export interface AgentSummary {
  id: string;
  name: string;
  icon: string;
  enabled: boolean;
  is_company_managed: boolean;
  user_id: string | null;
  owner_name: string | null;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  assignment_count: number;
  runs_this_week: number;
  chat_query_count: number;
  chat_queries_this_week: number;
  last_run: { status: string; summary: string | null; created_at: string; error: string | null } | null;
}

export interface DigestItem {
  id: string;
  agent_name: string;
  agent_icon: string;
  assignment_name: string;
  summary: string;
  actions_count: number;
  created_at: string;
}

interface FailedRun {
  id: string;
  agent_name: string;
  assignment_name: string;
  error: string | null;
  created_at: string;
}

export interface RankedItem {
  name: string;
  value: number;
}

export interface TimeSeriesPoint {
  date: string;
  value: number;
}

export interface AdoptionTimeSeries {
  messages_over_time: TimeSeriesPoint[];
  executions_over_time: TimeSeriesPoint[];
  agents_over_time: TimeSeriesPoint[];
  api_key_calls_over_time: TimeSeriesPoint[];
}

export interface DashboardStats {
  agents: { total: number; active: number };
  assignments: { total: number; active: number };
  runs: { total: number; completed: number; failed: number; today: number; this_month: number; running: number };
  integrations_connected: number;
  knowledge_bases: number;
  document_count: number;
  custom_tool_count: number;
  mcp_server_count: number;
  skill_count: number;
  prompt_count: number;
  user_count: number;
  group_count: number;
  conversation_count: number;
  message_count: number;
  notification_count: number;
  agent_summaries: AgentSummary[];
  recent_digests: DigestItem[];
  failed_runs: FailedRun[];
  success_rate: number;
  top_users: RankedItem[];
  top_agents: RankedItem[];
  top_assignments: RankedItem[];
  top_api_key_users: RankedItem[];
  top_integrations: RankedItem[];
  top_channels: RankedItem[];
  diffs: {
    runs_yesterday: number;
    runs_prev_month_count: number;
    success_rate_yesterday: number;
    agents_prev_month: number;
    assignments_prev_month: number;
    integrations_prev_month: number;
    documents_prev_month: number;
    conversations_prev_month: number;
    custom_tools_prev_month: number;
    mcp_servers_prev_month: number;
    skills_prev_month: number;
    prompts_prev_month: number;
    users_prev_month: number;
    runs_total_prev_month: number;
    runs_completed_prev_month: number;
    runs_failed_prev_month: number;
    runs_failed_yesterday: number;
  };
}

// ── CTEM Command Center ───────────────────────────────────────────────────────
export type CtemBand =
  | "critical"
  | "at_risk"
  | "fair"
  | "good"
  | "strong"
  | "unknown";

export interface CtemFact {
  label: string;
  value: number | string | boolean | null;
  unit: string;
  kind: "fact" | "derived";
}

export interface CtemSeverityBucket {
  label: string;
  value: number;
}

interface CtemTopVulnerability {
  /** The vulnerability's OpenCTI id, the "Open in OpenCTI" pivot (#1357); absent on an older payload. */
  id?: string | null;
  name: string | null;
  cvss: number | null;
  severity: string | null;
  epss: number | null;
  kev: boolean;
}

/** CTEM exposure vertical. Cyber is the default; FIMI and fraud reshape the
 *  whole command center (KPIs, agents, briefing) for the user. */
export type CtemVertical = "cyber" | "fimi" | "fraud";

/** A widget of the CTEM command center grid (`CTEM_WIDGETS` in
 *  `pages/xtm-one/ctem/dashboard-layout.ts`, mirrored by the backend's
 *  `services/dashboard_layouts.py`). */
export type CtemWidgetId =
  | "hero"
  | "kpi_posture"
  | "kpi_exposure_1"
  | "kpi_exposure_2"
  | "kpi_exposure_3"
  | "kpi_agents"
  | "kpi_executions"
  | "briefing"
  | "lifecycle_header"
  | "stage_scoping"
  | "stage_discovery"
  | "stage_prioritization"
  | "stage_validation"
  | "stage_remediation"
  | "stage_governance"
  | "recommended_actions"
  | "live_activity"
  | "digests"
  | "fleet_activity"
  | "fleet_timeline"
  | "coverage_matrix"
  | "exposure_stats"
  | "value_band"
  | "act_now"
  | "done_for_you"
  | "lifecycle_band"
  | "stats_coverage"
  | "fleets_row"
  | "prioritization_funnel"
  | "cases";

/** Where a widget sits on a dashboard grid, in grid units
 *  (`components/dashboard-grid`). */
export interface DashboardLayoutItem<Id extends string = string> {
  i: Id;
  x: number;
  y: number;
  w: number;
  h: number;
  /** The widget's own values over the dashboard context (CTEM command center,
   *  dev-docs/ctem-stage-viz.md 8.3); absent: it reads the dashboard's. */
  settings?: CtemWidgetSettings;
}

/** The periods the CTEM context bar offers, in days. */
export type CtemContextPeriod = 7 | 30 | 90;

/** The context bar of the CTEM command center (dev-docs/ctem-stage-viz.md
 *  8.2), saved with the layout; an absent field is the default. */
export interface CtemDashboardContext {
  /** A kill chain name as the data carries it ("mitre-attack", "disarm"). */
  framework?: string;
  period?: CtemContextPeriod;
}

/** A widget's own values over the CTEM dashboard context (section 8.3). */
export interface CtemWidgetSettings {
  period?: CtemContextPeriod;
}

/** A dashboard a person built on the grid: the widgets shown and where, and
 *  the ones they removed. */
export interface DashboardLayout<Id extends string = string> {
  items: DashboardLayoutItem<Id>[];
  hidden: Id[];
  /** The CTEM command center's context bar (absent: the defaults). */
  context?: CtemDashboardContext;
}

export type CtemDashboardLayout = DashboardLayout<CtemWidgetId>;

/** A widget of the Copilot dashboard grid (`COPILOT_WIDGETS` in
 *  `pages/dashboard/copilot-dashboard-layout.ts`, mirrored by the backend's
 *  `services/dashboard_layouts.py`). */
export type CopilotWidgetId =
  | "hero"
  | "kpi_agents"
  | "kpi_conversations"
  | "kpi_executions"
  | "kpi_integrations"
  | "kpi_documents"
  | "agents"
  | "conversations"
  | "live_activity"
  | "digests"
  | "value"
  | "value_drivers"
  | "fleets";

export type CopilotDashboardLayout = DashboardLayout<CopilotWidgetId>;

/** One OpenAEV core attack domain of the Scoping ring (viz only, not a score input). */
export interface CtemAttackDomain {
  name: string;
  covered: boolean;
  contracts: number;
}

/** The outcome of one exercised technique on the Validation matrix. */
export type CtemMatrixOutcome = "prevented" | "detected" | "missed" | "untested";

/** One exercised technique of the Validation outcome matrix. */
export interface CtemMatrixItem {
  id: string;
  label: string;
  code: string;
  phases: string[];
  success: number;
  failed: number;
  pending: number;
  parts: [];
  prevention: { success: number; total: number };
  detection: { success: number; total: number };
  outcome: CtemMatrixOutcome;
}

/** A kill chain the tenant's data carries, its phases in `phase_order` (any framework). */
interface CtemMatrixChain {
  id: string;
  label: string;
  /** Exercised techniques under this chain. */
  count: number;
  default?: boolean;
  groups: { id: string; label: string }[];
}

/** The Validation hero payload, in the custom dashboard `outcomes` shape. */
export interface CtemOutcomeMatrix {
  chains: CtemMatrixChain[];
  items: CtemMatrixItem[];
}

/** A layer of the CTEM prioritization funnel (#11546, `services/ctem_funnel.py`). */
export type CtemFunnelLayerKey =
  | "known"
  | "relevant"
  | "exploitable"
  | "exposed"
  | "business_impact"
  | "policy"
  | "open_issues";

export interface CtemFunnelLayer {
  key: CtemFunnelLayerKey;
  /** Exact count; null when the layer is not configured or not available (never 0 for those). */
  count: number | null;
  /** What this layer removed from the live layer above it; null on the first layer and on a layer without a count. */
  drop: number | null;
  state: "live" | "not_configured" | "not_available";
  source: "opencti" | "organization" | "ctem_cases";
  /** The organization context category that unlocks a not configured layer. */
  category: string | null;
  /** The layer an exploitable count was taken on (`relevant`, or `known` without a tech stack). */
  basis?: string | null;
  /** The OpenCTI FilterGroup of the list a live layer counts, when it fits a link. */
  filters?: Record<string, unknown> | null;
  /** OpenAEV CVE findings, the asset-side entry of the first layer. */
  asset_findings?: number | null;
  /** Why a layer is not available: `assets_not_linked` on Exposed when the profile holds assets the counted vulnerabilities cannot be joined to. */
  reason?: string | null;
  /** The reachability split of the profile assets, on the Exposed layer (`services/asset_reachability.py`). */
  reachability?: CtemFunnelReachability | null;
}

/** How many organization profile assets are reachable, proven unreachable or of unknown exposure. */
interface CtemFunnelReachability {
  total: number;
  reachable: number;
  unreachable: number;
  unknown: number;
  /** User endpoints among the reachable: always reachable (phishing, browser, email). */
  endpoints: number;
}

export interface CtemFunnel {
  layers: CtemFunnelLayer[];
  /** The narrowest live layer: what needs action today. */
  needs_action: number;
  epss_threshold: number;
  /** A source answered more for a narrower layer and the count was capped. */
  capped: boolean;
}

export interface CtemStageViz {
  severity_distribution?: CtemSeverityBucket[];
  top_vulnerabilities?: CtemTopVulnerability[];
  /** The prioritization funnel (#11546); null before OpenCTI answers. */
  funnel?: CtemFunnel | null;
  expectation_buckets?: Record<string, Record<string, number>>;
  /** Scoping hero: one entry per OpenAEV core attack domain. */
  attack_domains?: CtemAttackDomain[];
  /** Validation hero: exercised techniques by kill chain phase. */
  outcome_matrix?: CtemOutcomeMatrix;
  /** Discovery (#11998): distinct threat techniques per ATT&CK tactic, from OpenCTI. */
  techniques_by_tactic?: CtemTacticCount[];
  /** Discovery: the threats using the most techniques (top-N, never a total). */
  top_threats?: CtemNamedCount[];
  /** Discovery: the sectors the tracked threats target (top-N). */
  target_sectors?: CtemNamedCount[];
  /** Discovery: the countries the tracked threats target (top-N). */
  target_countries?: CtemNamedCount[];
  /** Scoping (#11998): the most frequent observable types in OpenCTI (top-N). */
  observables_by_type?: CtemNamedCount[];
  /** Scoping: the vulnerabilities OpenCTI tracks, by CVSS base severity. */
  vulnerabilities_by_severity?: CtemSeverityBucket[];
  /** Prioritization (#11998): OpenAEV's open findings and those with a CVE; null without OpenAEV. */
  findings?: { total: number; with_cve: number } | null;
  /** Validation (#11998): per ATT&CK tactic, the tracked threats' techniques and those with an indicator or a detection rule in OpenCTI. */
  rule_coverage_by_tactic?: CtemTacticCoverage[];
  /** Validation: OpenCTI's detection content (indicators, detection-ready indicators, sightings), each as OpenCTI counts it. */
  detection_depth?: CtemNamedCount[];
  /** Mobilization (#11998): OpenCTI's response cases by kind (case_incidents, case_rfis), each above 0. */
  response_mix?: CtemNamedCount[];
  /** Mobilization: closed of OpenCTI's response cases and running of its playbooks; a pair only on a total above 0. */
  response_progress?: CtemResponseProgress | null;
  /** Mobilization: what OpenAEV still finds open (open_findings, failed_partial_expectations), each above 0. */
  fix_backlog?: CtemNamedCount[];
}

interface CtemResponseProgress {
  cases_closed?: number;
  cases_total?: number;
  playbooks_running?: number;
  playbooks_total?: number;
}

export interface CtemTacticCoverage {
  /** The ATT&CK tactic as OpenCTI names its kill chain phase (e.g. initial-access). */
  tactic: string;
  total: number;
  covered: number;
}

export interface CtemTacticCount {
  /** The ATT&CK tactic as OpenCTI names its kill chain phase (e.g. initial-access). */
  tactic: string;
  count: number;
}

export interface CtemNamedCount {
  name: string;
  value: number;
}

/** A single metric inside an AI-authored per-stage assessment. */
interface CtemStageMetric {
  label: string;
  value: string;
  trend?: "up" | "down" | "flat" | "unknown";
}

/** AI-authored per-stage assessment (from a seeded ctem-{stage}-analyst run). */
interface CtemStageAssessment {
  stage: string;
  vertical?: CtemVertical;
  headline: string;
  assessment: string;
  score_read?: "strong" | "good" | "fair" | "at_risk" | "critical" | "unknown";
  key_metrics?: CtemStageMetric[];
  recommended_action: string;
  coverage?: { opencti: boolean; openaev: boolean };
}

/** Status of a per-user CTEM AI artifact (briefing, stage assessment). The
 *  locked ones (ee_locked, ai_disabled, no_model) never start a run. */
type CtemAiArtifactStatus =
  | "ready" | "generating" | "pending" | "ee_locked" | "ai_disabled" | "no_model" | "unavailable";

export interface CtemStageAssessmentResponse {
  assessment: CtemStageAssessment | null;
  /** ready = an assessment exists for the active vertical; generating = a run is
   *  in flight; pending = none yet; ee_locked = needs an EE product;
   *  ai_disabled = AI is switched off; no_model = no default model;
   *  unavailable = stage analyst not seeded. */
  status: CtemAiArtifactStatus;
  generating?: boolean;
  agent_name?: string;
  agent_slug?: string;
  /** Effective agent (per-user override -> platform default -> seeded). Drives the
   *  "follow up / implement" action: route to a custom agent, or prompt to assign
   *  one when only the seeded (built-in) analyst is in place. */
  agent_id?: string;
  agent_is_builtin?: boolean;
  generated_at?: string;
  /** The stored assessment is past the weekly window (fresh_days): the stage
   *  page regenerates it on visit and marks it stale meanwhile. */
  stale?: boolean;
  fresh_days?: number;
}

export interface CtemStage {
  key: string;
  label: string;
  score: number | null;
  prev_score: number | null;
  band: CtemBand;
  covered: boolean;
  confidence: "high" | "medium" | "low" | "none";
  primary_products: string[];
  secondary_products: string[];
  missing_products: string[];
  products_present: string[];
  methodology: string;
  facts: CtemFact[];
  viz: CtemStageViz;
  /** The single analyst agent that owns this stage (seeded or EE override). */
  agent?: { id: string; name: string; slug: string } | null;
  /** The stage score read against the organization context (#11464); null
   *  without a quantitative score or without any of the context it reads. */
  qualitative_score?: number | null;
  qualitative_band?: CtemBand;
  /** The context fields this stage reads that the profile leaves empty. */
  context_missing?: string[];
}

/** The organization context behind the qualitative scores (#11464). */
export interface CtemContextState {
  state: "missing" | "partial" | "complete";
  /** The context fields the CTEM scores read that are empty, by contract path. */
  missing_fields: string[];
}

interface CtemProduct {
  present: boolean;
  url?: string | null;
  title?: string | null;
  version?: string | null;
  healthy?: boolean;
  last_sync?: string | null;
  coming_soon?: boolean;
}

/** AI-authored weekly exposure briefing (from the seeded Exposure Briefing
 *  Analyst agent). Response of GET /ctem-metrics/briefing. */
interface CtemBriefingNextStep {
  action: string;
  rationale?: string;
  priority?: "high" | "medium" | "low";
}

interface CtemBriefingFinding {
  title: string;
  detail?: string;
  recommendation?: string;
  severity: "critical" | "high" | "medium" | "low" | "info";
  source: "opencti" | "openaev" | "both";
  /** Optional concrete source the agent cited: the specific OpenCTI/OpenAEV
   *  entity, with a deep link. May be absent (no single linkable source). */
  source_ref?: { label?: string; url?: string };
}

export interface CtemBriefing {
  headline: string;
  summary: string;
  posture_trend: "improving" | "stable" | "declining" | "unknown";
  coverage: { opencti: boolean; openaev: boolean };
  key_findings: CtemBriefingFinding[];
  cross_product_insight?: string;
  recommended_focus: string;
  next_steps?: CtemBriefingNextStep[];
}

export interface CtemBriefingResponse {
  briefing: CtemBriefing | null;
  /** ready = a per-user briefing exists; generating = a run is in flight;
   *  pending = none yet; ee_locked = needs an EE product; ai_disabled = AI is
   *  switched off; no_model = no default model; unavailable = agent not seeded. */
  status: CtemAiArtifactStatus;
  generating?: boolean;
  agent_name?: string;
  agent_slug?: string;
  /** Effective agent (per-user override -> platform default -> seeded). Drives the
   *  "follow up / implement" action: route to a custom agent, or prompt to assign
   *  one when only the seeded (built-in) analyst is in place. */
  agent_id?: string;
  agent_is_builtin?: boolean;
  generated_at?: string;
  /** Past the daily freshness window: the command center regenerates it on
   *  landing and marks it stale meanwhile (#12183). */
  stale?: boolean;
  fresh_hours?: number;
}

interface CtemTrendPoint {
  date: string;
  score: number;
}

interface CtemBlindSpot {
  stage: string;
  label: string;
  missing_products: string[];
}

export interface CtemHeadline {
  // Cyber
  kev_count: number | null;
  /** Known-exploited restricted to the organization profile's tech stack, and
   *  the matched products (at most 3); absent or null without a tech stack. */
  kev_tailored_count?: number | null;
  kev_tailored_products?: string[] | null;
  critical_count: number | null;
  vulnerabilities_total: number | null;
  detection_coverage_pct: number | null;
  prevention_success_pct: number | null;
  detection_success_pct: number | null;
  exercised_coverage_pct: number | null;
  validation_success_pct?: number | null;
  // FIMI / disinformation
  narratives_total?: number | null;
  channels_total?: number | null;
  attribution_pct?: number | null;
  corroboration_pct?: number | null;
  incidents?: number | null;
  // Fraud
  phishing_assets?: number | null;
  credentials_total?: number | null;
  confirmed_pct?: number | null;
  containment_pct?: number | null;
  // Shared
  case_closure_pct?: number | null;
}

export interface CtemPosture {
  posture_score: number | null;
  posture_band: CtemBand;
  covered_stages: number;
  total_stages: number;
  vertical?: CtemVertical;
  weights: Record<string, number>;
  stages: CtemStage[];
  products: Record<string, CtemProduct>;
  trend: CtemTrendPoint[];
  blind_spots: CtemBlindSpot[];
  headline: CtemHeadline;
  last_sync: Record<string, string>;
  /** When this live snapshot was built (ISO) + how long it is cached, so the UI
   *  can show "last synced X ago - next in Y". */
  generated_at?: string;
  cache_ttl_seconds?: number;
  /** The server is rebuilding this (stale) copy in the background: the page
   *  spins and polls until the new generated_at lands (#12183). */
  refreshing?: boolean;
  /** The payload shape the server stamps on a cached posture (#11662); absent
   *  on a posture cached before the stamp, which the server rebuilds at once. */
  shape?: number;
  /** The posture read against the organization context (#11464). */
  qualitative_score?: number | null;
  qualitative_band?: CtemBand;
  context?: CtemContextState;
}

/** Vertical-aware CTEM analyst override row. Reports the inherited default
 *  (seeded analyst for the platform scope; the effective platform/seeded agent
 *  for the per-user scope), the active override and the effective agent. The
 *  `stage` field is the override key — a CTEM stage key, or the reserved
 *  `"briefing"` key for the Exposure Briefing Analyst row. */
export interface CtemStageAgentRow {
  stage: string;
  label: string;
  default_agent_id: string | null;
  default_agent_slug: string | null;
  override_agent_id: string | null;
  effective_agent_id: string | null;
  effective_agent_name: string | null;
}

/** Response of GET /ctem-metrics/stage-agents (platform, admin) and
 *  /ctem-metrics/my-stage-agents (per user) for one vertical: the per-stage
 *  analyst rows plus the Exposure Briefing Analyst row. */
export interface CtemStageAgentsResponse {
  vertical: CtemVertical;
  stages: CtemStageAgentRow[];
  briefing: CtemStageAgentRow;
}

export interface KBActivityLog {
  id: string;
  event: string;
  details: string | null;
  source_type: string | null;
  document_count: number | null;
  created_at: string;
}

// RAG test search
interface RAGSearchResult {
  content: string;
  source_type: string;
  source_id: string | null;
  score: number;
  created_at: string;
}

export interface RAGSearchResponse {
  query: string;
  results: RAGSearchResult[];
  total: number;
}

export interface GlobalRAGSearchResult {
  knowledge_base_id: string;
  knowledge_base_name: string;
  content: string;
  source_type: string;
  source_id: string | null;
  score: number;
  created_at: string;
}

export interface GlobalRAGSearchResponse {
  query: string;
  results: GlobalRAGSearchResult[];
  total: number;
}

export interface KBEmbeddingDimensionStat {
  provider: string | null;
  model: string | null;
  dimension: number;
  chunk_count: number;
}

// Agent export/import bundle
/** Portable agent formats. Mirrors `ExportFormat` in
 *  `backend/app/api/v1/schemas/agent.py`: `native` is the XTM One bundle
 *  (lossless, the default), `agentschema` is a Microsoft AgentSchema
 *  `kind: prompt` document. */
export type AgentExportFormat = "native" | "agentschema";

/** One agent capability the target format cannot carry as-is.
 *  `omitted` — absent from the exported document's standard fields entirely.
 *  `flattened` — the content survives but is merged into a spec field and is
 *  no longer a distinct, separately-managed entity. */
interface AgentPortabilityEntry {
  key: string;
  label: string;
  detail: string;
  disposition: "omitted" | "flattened";
}

/** What exporting an agent to a given format would not carry. Shown before
 *  the file is generated so the loss is never silent. */
export interface AgentExportPortability {
  format: AgentExportFormat;
  format_label: string;
  /** AgentSchema spec version the report was computed against; null for the
   *  native format, which has no external specification. */
  spec_version: string | null;
  entries: AgentPortabilityEntry[];
  /** True when the omitted data is round-tripped through the document's
   *  `metadata` namespace, so re-importing into XTM One is lossless. */
  preserved_in_metadata: boolean;
}

/** Result of importing an agent from an uploaded file, including which format
 *  auto-detection settled on. */
export interface AgentImportFileResult {
  agent: Agent;
  detected_format: AgentExportFormat;
  detected_format_label: string;
  /** Leniencies applied while reading the document (a missing `kind`, a
   *  shorthand `model`, ...). Informational — the import succeeded. */
  notes: string[];
  /** References the file makes that this instance cannot satisfy (external
   *  MCP servers, knowledge bases, tools with no implementation). */
  warnings: string[];
}

/** One step of the import verification loop, with its verdict. */
export interface AgentImportCheckStage {
  key: "parse" | "detect" | "schema" | "references";
  status: "passed" | "failed" | "skipped";
  /** Present on a failed stage: the message plus per-field problems. */
  problems: string[];
}

/** One optional, deterministic fix the user can apply before importing.
 *  The wording is rendered client-side from `kind` + `subject`. */
interface AgentImportCorrection {
  id: string;
  kind: "drop_custom_tool" | "drop_mcp_server";
  subject: string;
}

/** Dry-run verdict on an uploaded agent file — nothing was persisted. */
export interface AgentImportFileCheckResult {
  ok: boolean;
  /** Null when detection itself failed. */
  detected_format: AgentExportFormat | null;
  detected_format_label: string | null;
  agent_name: string | null;
  stages: AgentImportCheckStage[];
  notes: string[];
  warnings: string[];
  /** Placeholder entries the user may choose to drop before importing. */
  corrections: AgentImportCorrection[];
  /** The upload mapped onto the native bundle shape — pass it to the confirm
   *  endpoint to import exactly what was checked. */
  bundle: Record<string, unknown> | null;
}

// Audit Logs
export interface AuditLog {
  id: string;
  user_id: string;
  user_email: string;
  action: string;
  entity_type: string;
  entity_id: string | null;
  entity_name: string | null;
  summary: string;
  details: Record<string, unknown> | null;
  ip_address: string | null;
  created_at: string;
}

export interface AuditLogListResponse {
  items: AuditLog[];
  total: number;
}

// Security Audit (GET /security-audit, admin) — the audit gains checks over
// time, so `category` and `severity` are open strings: `critical` / `warning`
// / `info` / `pass` are the severities it emits today, not a closed set.
export interface SecurityAuditFinding {
  /** `<category>.<name>`, the same from one run to the next; absent from a check that sets none. */
  id?: string | null;
  category: string;
  severity: string;
  title: string;
  description: string;
  recommendation?: string | null;
  /** The id of the control the finding reports on (`SecurityControlStatus.id`), when it is about one. */
  control?: string | null;
  /** An administrator accepted it: it is out of every count and the score, and never notified. */
  dismissed?: SecurityAuditFindingDismissal | null;
  /** It was dismissed, then reported at a higher severity: it counts again and was notified again. */
  reopened?: SecurityAuditFindingReopening | null;
}

// Who accepted a finding, when and why.
export interface SecurityAuditFindingDismissal {
  /** ISO 8601; `null` on the dismissal a re-opening cleared. */
  at?: string | null;
  /** The severity the finding had when it was dismissed. */
  severity: string;
  reason?: string | null;
  by_user_id?: string | null;
  /** The administrator's name; `null` once the account is gone. */
  by?: string | null;
}

// A dismissal the finding escalated past.
export interface SecurityAuditFindingReopening {
  /** ISO 8601; `null` until the platform records the re-opening (hourly, or on the next dismiss / restore). */
  at: string | null;
  /** The severity that re-opened it. */
  severity: string;
  /** The dismissal it cleared. */
  dismissal?: SecurityAuditFindingDismissal | null;
}

// A security switch of the platform and its state, from the registry
// in `backend/app/core/security_controls.py`; `state` is an open string like
// `severity`.
export interface SecurityControlStatus {
  /** `<category>.<name>`, the same from one run to the next. */
  id: string;
  category: string;
  title: string;
  /** The environment variable of a config control, the stored column(s) of a stored one. */
  setting: string;
  /** `config`, `platform_settings` or `sso_config` today. */
  stored_in: string;
  /** Where a stored control is changed ("Settings > Integrations > MCP Endpoint"); empty for a config control. */
  where: string;
  /** The current value, as an administrator types it. */
  value: string;
  /** The value when nothing sets it, for this deployment's mode. */
  default: string;
  /** The value that enforces it (`true`, `closed`, `>= 12`, `empty`). */
  hardened: string;
  /** `enforced`, `monitoring` or `off` today. */
  state: string;
  /** The default leaves it off. */
  opt_in: boolean;
  /** What enforcing it changes for users. */
  impact: string;
  /** Where what enforcing it would block is recorded; empty when nothing records it. */
  evidence: string;
  /** The platform mode fixes its value, so nothing is to change; absent before that. */
  pinned?: boolean;
}

export interface SecurityAuditReport {
  /** When the audit ran (ISO 8601, UTC). Every request runs it again. */
  timestamp: string;
  /** `healthy`, `needs_attention` or `at_risk` today. */
  overall_score: string;
  findings: SecurityAuditFinding[];
  /** Findings per severity, dismissed findings excluded. */
  summary: Record<string, number>;
  /** How many reported findings an administrator dismissed; absent before that. */
  dismissed_count?: number;
  /** Every security control with its state; absent from an older platform. */
  controls?: SecurityControlStatus[];
  /** Controls per state. */
  controls_summary?: Record<string, number>;
}

/** One background task, assignment run or Video Studio job a
 *  `wait` watches, as the `wait_start` / `wait_status` frames describe it.
 *  `kind` is `null` for an id that names nothing the user may open (`status`
 *  is then `unknown`). */
export interface WaitItem {
  id: string;
  kind: "task" | "run" | "job" | "a2a" | null;
  label: string | null;
  status: string;
  started_at: string | null;
}

// Sub-Agent Background Tasks (delegated agent-to-agent work, run in the background)
type SubAgentTaskStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "incomplete";

export interface SubAgentTask {
  task_id: string;
  // Always present: every SubAgentTask[] the frontend consumes comes from the
  // list endpoint (list_background_tasks), which always serialises user_id, and
  // the backing column is non-nullable.
  user_id: string;
  parent_agent_id: string;
  parent_agent_name?: string | null;
  parent_agent_icon?: string | null;
  target_agent_id: string;
  target_agent_name?: string | null;
  target_agent_icon?: string | null;
  task_description: string;
  status: SubAgentTaskStatus;
  result: string | null;
  error: string | null;
  conversation_id?: string | null;
  assignment_run_id?: string | null;
  parent_run_id?: string | null;
  // The list endpoint serialises timestamps as `X.isoformat() if X else None`,
  // so all three can be null in the API contract (created_at is non-nullable on
  // the model, but the serializer's guard means callers must still null-check).
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  has_result?: boolean;
  /** Pending because agentic AI is unavailable here: the refusal code. */
  held_reason?: string | null;
  /**
   * A pending or running task only: its orchestration execution's
   * state (`none` when it has no execution row), id and last move. Absent on a
   * finished task.
   */
  execution_state?: string | null;
  execution_id?: string | null;
  last_progress_at?: string | null;
}

// Intents — AI capability routing (intent × vertical → agent bindings)
export interface IntentBinding {
  id: string;
  intent_id: string;
  agent_id: string;
  agent_name: string | null;
  agent_slug: string | null;
  vertical: string | null;
  user_id: string | null;
  is_locked: boolean;
  is_builtin: boolean;
  priority: number;
  priority_overrides?: Record<string, number> | null;
  created_at: string;
  updated_at: string;
}

export interface IntentItem {
  id: string;
  name: string;
  description: string | null;
  platforms: string[];
  bindings: IntentBinding[];
  is_builtin: boolean;
  created_at: string;
  updated_at: string;
}

export interface VerticalItem {
  id: string;
  name: string;
  source: string;
  platform_identifier: string | null;
  binding_count: number;
  created_at: string;
  updated_at: string;
}

/**
 * `GET /platform/registration-token` (admin). `state` says whether the value is
 * one to paste into a platform at all: only a `configured` token is - the
 * `published_default` authenticates nobody, and `unset` disables registration.
 */
type RegistrationTokenState = "unset" | "published_default" | "configured";

export interface RegistrationTokenInfo {
  registration_token: string;
  state: RegistrationTokenState;
  generate_command: string;
}

// Work Items — durable agentic task / work-item primitive
/** Why a work item needs a look (`work_item_stale_reason`, #9879). */
export type WorkItemStaleReason = "past_due" | "wait_never_releases" | "ref_wait_quiet" | "quiet";

export type WorkItemStatus =
  | "open"
  | "claimed"
  | "in_progress"
  | "blocked"
  | "done"
  | "cancelled";
export type WorkItemPriority = "low" | "medium" | "high" | "urgent";

/** One choice of a work item's ask: `key` is what the answer sends. */
export interface WorkItemAskOption {
  key: string;
  label: string;
  effect?: string | null;
}

/** `work_items.ask` (#9437): the question a person answers, as the agent wrote it. */
export interface WorkItemAsk {
  question: string;
  /** 2 to 4 plain lines for the reader, separated by line breaks (#776). */
  context?: string | null;
  options?: WorkItemAskOption[];
  /** The key of the option the agent recommends. */
  recommendation?: string | null;
  /** When the answer is needed, ISO-8601 UTC. */
  deadline?: string | null;
  /** The sources the reader opens to decide (#834): a Jira ticket, a pull request, a document. */
  links?: WorkItemAskLink[];
}

/** One source of a decision card, shown as a button under the question (#834). */
interface WorkItemAskLink {
  label: string;
  url: string;
}

/** What a decision card shows when its ask is missing or incomplete (#834): read from the item, no model. */
interface WorkItemCardFallback {
  /** The first lines of the item's brief, past the stamped notes. */
  brief: string;
  links: WorkItemAskLink[];
  /** The ask fields still missing, e.g. `ask.question`. */
  missing: string[];
}

export interface WorkItem {
  id: string;
  user_id: string | null;
  is_company_managed: boolean;
  owner_agent_id: string | null;
  created_by_agent_id: string | null;
  title: string;
  description: string | null;
  payload: Record<string, unknown>;
  /** Files its creator attached, read by whoever may read the item. */
  attachment_file_ids?: string[];
  status: WorkItemStatus;
  priority: WorkItemPriority;
  correlation_key: string | null;
  due_at: string | null;
  claimed_by_agent_id: string | null;
  claimed_at: string | null;
  /** The run that holds the claim: an agent run or a background task. */
  claimed_by_run_id?: string | null;
  /** That run as the read found it, with whether it is alive; null when not read. */
  claimed_by_run?: WorkItemHoldingRun | null;
  /** The platform's dated notes on the claim (released, taken over), oldest first. */
  claim_notes?: WorkItemClaimNote[];
  /** The ids of the work items it waits for; empty when none. */
  blocked_on?: string[];
  /** The agents its completion is routed to, as stored; null when nobody. */
  fleet_impact?: { targets: string[]; kind: WorkItemFleetImpactKind; instructions: string | null } | null;
  /** The pull requests and issues it waits for; empty when none. */
  blocked_on_refs?: WorkItemBlockedRef[];
  /** What a blocked item waits for in words. */
  blocked_reason?: string | null;
  /** Blocked on a person's decision. */
  needs_human?: boolean;
  /** The person a needs_human item waits for when not its user (#11893); null otherwise. */
  waits_for_user_id?: string | null;
  /** The decision itself, for an item waiting for a person (#9437); null on a legacy item. */
  ask?: WorkItemAsk | null;
  /** Set when the item needs a person and its ask is missing or incomplete (#834). */
  card_fallback?: WorkItemCardFallback | null;
  /** `work_item_blocked_refs.blocked_why`: null when not blocked; `missing` for a blocked item that says nothing. */
  blocked_why?: { text: string; missing: boolean } | null;
  source: string;
  result: string | null;
  is_stale: boolean;
  /** Why it is stale (`work_item_stale_reason`); null when it is not, absent from older servers. */
  stale_reason?: WorkItemStaleReason | null;
  /** Set when the platform runs the item at `due_at`; null otherwise. */
  scheduled: WorkItemScheduled | null;
  workspace_id?: string | null;
  /** Where it was created: the conversation, assignment run, flow run. */
  conversation_id?: string | null;
  origin_run_id?: string | null;
  origin_flow_run_id?: string | null;
  last_activity_at: string | null;
  /** When it reached done or cancelled; null while it is not terminal (#8558). */
  completed_at?: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** What the item means to the reader, on the REST list and detail. */
  context?: WorkItemContext;
  /** The item's ROI record for the outcome review row (detail only); null when none. */
  roi?: RoiReviewBlock | null;
  /** The files the item carries, by name (detail only, #12092). */
  attachments?: WorkItemAttachments;
  /** The agent behind each run its stamped notes name (detail only, #776). */
  note_runs?: Record<string, WorkItemNoteRun>;
}

/** The run a stamped note names: its agent as the reader may see it, and whether the reader may open it. */
export interface WorkItemNoteRun {
  agent: { id: string; name: string } | null;
  accessible: boolean;
}

/** One file a work item carries, as its dialog lists it. */
export interface WorkItemAttachedFile {
  file_id: string;
  filename: string;
  content_type: string;
  size: number;
}

/** The files a work item carries: the stored ones in the order attached, and how many are gone. */
export interface WorkItemAttachments {
  files: WorkItemAttachedFile[];
  gone: number;
}

/** One ROI record's review outcome; `null` while unreviewed (#9310). */
export type RoiOutcome = "accepted" | "modified" | "rejected";

/**
 * `roi_records.review_block` (#9335): what the outcome review row needs and no
 * figure. Carried by `GET /assignments/runs/{id}` and `GET /work-items/{id}`.
 */
export interface RoiReviewBlock {
  record_id: string;
  unit: string;
  unit_name: string;
  units: number;
  outcome: RoiOutcome | null;
  review_min: number | null;
  /** The unit's own review minutes, the placeholder of the field. */
  default_review_min: number | null;
  review_note: string | null;
  reviewed_at: string | null;
  can_review: boolean;
}

/** `services/work_item._holding_run_view`: the run holding a work item's claim. */
export interface WorkItemHoldingRun {
  id: string;
  /** "run" (an agent run, opened at /runs/<id>), "background_task", or null when no longer recorded. */
  kind: "run" | "background_task" | null;
  status: string | null;
  alive: boolean;
}

/** One dated note the platform wrote on a work item's claim. */
interface WorkItemClaimNote {
  at: string;
  text: string;
}

/** An agent a work item names, as the reader may see it: unnamed when they cannot open it. */
export interface WorkItemAgentRef {
  id: string | null;
  name: string | null;
  icon: string | null;
  accessible: boolean;
}

/** A run a work item came from; unnamed when the reader cannot open it. */
interface WorkItemRunRef {
  id: string | null;
  name: string | null;
  status: string | null;
  accessible: boolean;
  /** Flow runs only: the flow whose builder opens the run. */
  flow_id?: string | null;
}

/** `services/work_item_context.PICKUP_STATES`. */
export type WorkItemPickupState =
  | "scheduled"
  | "finished"
  | "in_progress"
  | "blocked"
  | "blocked_on_items"
  | "waiting_for_refs"
  | "waiting_for_person"
  | "waiting_for_ci"
  | "unassigned"
  | "automatic"
  | "waiting_until_due"
  | "filtered_out"
  | "manual";

/** An assignment or flow that drains the owner's queue, and whether it takes this item. */
export interface WorkItemInboxRef {
  kind: "assignment" | "flow";
  id: string | null;
  name: string | null;
  accessible: boolean;
  accepts: boolean;
  priority_min: WorkItemPriority | null;
  correlation_prefix: string | null;
}

type WorkItemVisibilityReason = "own" | "company_managed" | "workspace";

export interface WorkItemContext {
  owner_agent: WorkItemAgentRef | null;
  created_by_agent: WorkItemAgentRef | null;
  claimed_by_agent: WorkItemAgentRef | null;
  /** The person whose chat, run or flow created it. */
  person: { id: string; name: string | null; is_you: boolean } | null;
  visibility: {
    reason: WorkItemVisibilityReason;
    workspace: { id: string; name: string | null } | null;
  };
  origin: {
    kind: "chat" | "run" | "flow" | "manual" | "unknown";
    source: string;
    /** Only a chat of the reader's own. */
    conversation: { id: string; title: string | null } | null;
    /** A chat that is not the reader's: it exists, it is not linked. */
    conversation_hidden: boolean;
    run: WorkItemRunRef | null;
    flow_run: WorkItemRunRef | null;
  };
  pickup: {
    state: WorkItemPickupState;
    inboxes: WorkItemInboxRef[];
    /** When an inbox takes it (a future due date). */
    available_at: string | null;
    /** `waiting_for_ci` only: the pull request whose CI it waits for. */
    pull_request?: { repo: string; number: number; url: string | null } | null;
    /** `waiting_for_ci` only: every pull request whose CI it waits for, `pull_request` first. */
    pull_requests?: { repo: string; number: number; url: string | null }[];
    /** When the abandonment sweep cancels it if nobody picks it up. */
    auto_cancel_at: string | null;
    /** When the retention sweep deletes a finished item. */
    purge_at: string | null;
    dispatch_attempts: number;
    max_dispatch_attempts: number;
  };
  /** The items it waits for, unnamed when the reader cannot open one. */
  blocked_on?: WorkItemBlockerRef[];
  /** The agents its completion is routed to; null when nobody beyond its creator, owner and holder. */
  fleet_impact?: WorkItemFleetImpactRef | null;
}

/** `services/work_item_fleet_impact.FLEET_IMPACT_KINDS`: what a completion changes for its targets. */
export type WorkItemFleetImpactKind = "skill" | "persona" | "assignment" | "runbook" | "retest" | "state" | "capability";

/** `work_item_context._Lookup._fleet_impact`: the impacted agents as the reader may see them. */
export interface WorkItemFleetImpactRef {
  agents: (WorkItemAgentRef | null)[];
  kind: WorkItemFleetImpactKind | null;
  instructions: string | null;
  /** Read from a legacy follow-through section of its description, not set by an agent. */
  inferred: boolean;
}

/**
 * `work_item_blocked_refs.normalize_blocked_on_refs`: a pull request, issue,
 * deployment or workflow run a blocked item waits for. `state` is set once it
 * is satisfied (merged or closed, deployed, succeeded); the item goes back to
 * the queue when every one is. A deployment names its pull request (`number`)
 * or its commit (`sha`); a workflow run names its `workflow`, no number.
 */
export interface WorkItemBlockedRef {
  repo: string;
  kind: "pull_request" | "issue" | "deployment" | "workflow_run";
  number?: number | null;
  sha?: string | null;
  workflow?: string | null;
  release_on: "merged" | "closed" | "merged_or_closed" | "deployed" | "succeeded";
  state?: "merged" | "closed" | "deployed" | "succeeded" | null;
  at?: string | null;
}

/** `services/work_item_graph.py`: a work item of the Graph view. */
export interface WorkItemGraphItemNode {
  id: string;
  kind: "item";
  title: string;
  status: WorkItemStatus;
  priority: string | null;
  correlation_key: string | null;
  owner_agent: { id: string | null; name: string | null; icon: string | null } | null;
  needs_human: boolean;
  ask: { question: string; options: { key: string; label: string }[]; recommendation?: string | null } | null;
  is_program_root: boolean;
  /** A blocker the filter left out, drawn so the chain does not end on nothing. */
  outside_filter: boolean;
  /** What the Waiting for me filter reads (`waitsForReader`): the node's Waiting for you marker. */
  user_id: string | null;
  waits_for_user_id: string | null;
  owner_agent_id: string | null;
  due_at: string | null;
  scheduled: boolean;
}

/** A pull request, issue, deployment or workflow run an item waits for. */
export interface WorkItemGraphRefNode {
  id: string;
  kind: "ref";
  ref_kind: WorkItemBlockedRef["kind"];
  label: string;
  repo: string | null;
  number: number | null;
  state: WorkItemBlockedRef["state"];
  satisfied: boolean;
  url: string | null;
}

/** The person a `needs_human` item waits for. */
export interface WorkItemGraphPersonNode {
  id: string;
  kind: "person";
  name: string | null;
  is_you: boolean;
}

export type WorkItemGraphNode = WorkItemGraphItemNode | WorkItemGraphRefNode | WorkItemGraphPersonNode;

/** `blocked_on`, `ref` and `person` go from the waiter to what it waits for; `program` from the root to a child. */
export interface WorkItemGraphEdge {
  id: string;
  source: string;
  target: string;
  kind: "blocked_on" | "ref" | "person" | "program";
}

/** `GET /work-items/graph`. */
export interface WorkItemGraph {
  nodes: WorkItemGraphNode[];
  edges: WorkItemGraphEdge[];
  truncated: boolean;
}

/** `work_item_context._Lookup.blocked_on`: an item another one waits for. */
export interface WorkItemBlockerRef {
  id: string | null;
  title: string | null;
  status: WorkItemStatus | null;
  accessible: boolean;
}

/** `GET /work-items/owners`: an owner agent (null = unassigned) and its item count. */
export interface WorkItemOwnerCount {
  agent: WorkItemAgentRef | null;
  count: number;
}

/** What a scheduled work item shows, derived server-side. */
export type WorkItemScheduleState =
  | "scheduled"
  | "due"
  | "running"
  | "done"
  | "blocked"
  | "missed"
  | "cancelled";

export interface WorkItemScheduled {
  mode: "exact" | "instruction";
  tool: string | null;
  target: string | null;
  preview: string;
  state: WorkItemScheduleState;
  run_id: string | null;
  conversation_id: string | null;
  outcome_code: string | null;
  /** How often platform restarts made it go on by itself. */
  restarts?: { resumed: number; requeued: number };
}

/** A scheduled work item as the upcoming views list it, with its agent. */
export interface ScheduledWorkItem extends WorkItem {
  scheduled: WorkItemScheduled;
  agent_name: string | null;
  agent_icon: string | null;
}

// Shared State — generic cross-run shared state store
export type SharedStateStatus = "active" | "resolved";

export interface SharedState {
  id: string;
  user_id: string | null;
  is_company_managed: boolean;
  key: string;
  label: string | null;
  owner_agent_id: string | null;
  status: SharedStateStatus;
  is_stale: boolean;
  /** Characters of the value's JSON, the unit of the size limits (#10789). */
  size_chars?: number;
  /** Item count of the value's longest top-level list. */
  largest_list?: number;
  /** The size passes the soft limit (SHARED_STATE_VALUE_SOFT_LIMIT_CHARS). */
  over_soft_limit?: boolean;
  value: Record<string, unknown>;
  notes: { at?: string; agent_id?: string | null; note?: string }[];
  resolved_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ── Cross-agent user memory ───────────────────────────────────────────

/** One entry of the user memory: the profile (`global`), or a note an agent's work left. */
export interface UserMemoryEntry {
  id: string;
  text: string;
  /** `global` | `agent:<id>` */
  scope: string;
  owner_agent_id: string | null;
  owner_agent_name: string | null;
  created_at: string;
  last_verified_at: string;
  ttl_seconds: number | null;
  perishable: boolean;
}

/** One change the Memory Steward made to a memory lane (#8258). */
export interface MemoryConsolidation {
  id: string;
  agent_id: string;
  agent_name: string;
  operation: "merge" | "promote" | "demote" | "remove" | "flag";
  reason: "duplicate" | "contradiction" | "stale" | "recurring";
  applied_at: string;
  undone_at: string | null;
  undoable: boolean;
  entries: Array<{
    id: string;
    role: "merged" | "removed" | "demoted" | "flagged" | "kept" | "created";
    /** `null` once the entry is gone. */
    content: string | null;
    memory_type: string | null;
    exists: boolean;
  }>;
}

export interface MemoryConsolidationList {
  consolidations: MemoryConsolidation[];
  retention_days: number;
}

// ── Workset (#3074) — a scoped, searchable working corpus ────────────────────

type WorksetStatus = "active" | "resolved";
type WorksetScope = "run" | "flow" | "global";
type WorksetProcessing = "auto" | "agent" | "none";
export type WorksetItemStatus =
  | "pending"
  | "ready"
  | "consumed"
  | "discarded"
  | "evicted"
  | "failed";

export interface Workset {
  id: string;
  key: string;
  /** Opened by the runtime for an agent chat or an assignment: never sealed, it rolls. */
  implicit: boolean;
  label: string | null;
  scope: WorksetScope;
  scope_ref: string | null;
  status: WorksetStatus;
  processing: WorksetProcessing;
  workers: number;
  item_count: number;
  /** Items whose content was reclaimed to stay inside the budget. */
  evicted_count: number;
  content_chars: number;
  max_items: number;
  max_content_chars: number;
  /** The spend ceiling for the automatic summariser, and what it has used. */
  max_auto_calls: number;
  auto_calls_used: number;
  /** What the caller ANNOUNCED it would add — a claim, never reconciled. */
  declared_total: number | null;
  declared_total_source: string | null;
  /** Until this is set, nothing calls the corpus complete. */
  sealed_at: string | null;
  watermark_at: string | null;
  last_progress_at: string | null;
  expires_at: string | null;
  resolved_at: string | null;
  is_company_managed: boolean;
  owner_agent_id: string | null;
  created_at: string;
  updated_at: string;
}

interface WorksetCoverage {
  total: number;
  ready: number;
  pending: number;
  failed: number;
  evicted: number;
  discarded: number;
  sealed: boolean;
  /** Sealed, nothing pending, nothing failed. Conservative on purpose. */
  complete: boolean;
  /** Nothing can still progress — what a waiter watches. */
  settled: boolean;
  /** The automatic summariser has spent this corpus's call budget. */
  auto_budget_exhausted: boolean;
  description: string;
}

interface WorksetProgress {
  processed: number;
  remaining: number;
  in_progress: number;
  throughput_per_min: number | null;
  samples: number;
  /** Null until enough completions have been observed to mean one. */
  eta_seconds: number | null;
  eta_range: [number, number] | null;
  eta_confidence: "unknown" | "rough" | "stable";
  stalled: boolean;
  description: string;
}

export interface WorksetDetail extends Workset {
  coverage: WorksetCoverage;
  progress: WorksetProgress;
}

interface WorksetItem {
  id: string;
  title: string;
  status: WorksetItemStatus;
  priority_score: number | null;
  occurred_at: string | null;
  summary: string | null;
  corroboration_count: number;
  source_url: string | null;
  tags: string[];
  revision: number;
  chars: number;
}

export interface WorksetItemPage {
  total: number;
  offset: number;
  items: WorksetItem[];
  coverage: string;
}

// Agent Delegations — unified audit trail for all inter-agent interactions
type DelegationType = "consult" | "transfer" | "background_task";
type DelegationStatus = "pending" | "running" | "completed" | "failed";

export interface AgentDelegation {
  id: string;
  user_id: string | null;
  parent_agent_id: string;
  target_agent_id: string;
  type: DelegationType;
  conversation_id: string | null;
  message_id: string | null;
  description: string;
  status: DelegationStatus;
  result: string | null;
  error: string | null;
  /** `run_outcome.run_outcome` of the delegation: `interrupted` when the platform ended it. */
  outcome?: "ok" | "failed" | "interrupted" | "active" | "other" | null;
  duration_ms: number | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  has_result?: boolean;
}

// Agent Memory Entry (persistent memory with vector search)
export interface AgentMemoryEntry {
  id: string;
  agent_id: string;
  user_id: string | null;
  content: string;
  memory_type: "consolidated" | "fact" | "learned" | "style" | "preference" | "state" | "tradecraft";
  source: string;
  score?: number;
  match_type?: "semantic" | "text";
  created_at: string;
  updated_at: string | null;
  /** True when content is redacted (company-managed agent, non-admin user). */
  restricted?: boolean;
  /** The object the entry describes (`presentation:<id>`, `doc:X`). */
  subject_key?: string | null;
  /** The entry no longer holds: retired, or replaced by a newer entry. */
  superseded?: boolean;
  superseded_at?: string | null;
  /** The entry that replaced it (list), or `superseded_by` (search). */
  superseded_by_id?: string | null;
  superseded_by?: string | null;
}

/** Paginated response from GET /agents/{id}/memory */
export interface AgentMemoryList {
  entries: AgentMemoryEntry[];
  total_count: number;
}

/** Memory health statistics from GET /agents/{id}/memory/health */
export interface AgentMemoryHealth {
  total_entries: number;
  by_type: Record<string, number>;
  fact_count: number;
  oldest_fact: string | null;
  newest_fact: string | null;
  no_embedding_count: number;
  stale_count: number;
  over_cap: number;
  retention_days: number;
  max_facts_per_agent: number;
  dedup_similarity_threshold: number;
}

/** Response from POST /agents/{id}/memory/maintain */
export interface AgentMemoryMaintenanceResult {
  pruned: number;
  evicted: number;
  consolidated: number;
  message: string;
}

// Channel Routing Summary (admin-only, Settings → Channels)
interface ChannelRoutingAgent {
  id: string;
  name: string;
  icon: string;
  is_company_managed: boolean;
}
interface ChannelRoutingEntry {
  agents: ChannelRoutingAgent[];
  publish_agents: ChannelRoutingAgent[];
  general_assistant_enabled: boolean;
}
export type ChannelRoutingSummary = Record<string, ChannelRoutingEntry>;

// Agent Channel Chats (external channel conversations on agent detail)
interface AgentChatChannelBreakdown {
  channel: string;
  count: number;
}

interface AgentChatStats {
  total_conversations: number;
  total_messages: number;
  channels: AgentChatChannelBreakdown[];
  positive_feedback: number;
  negative_feedback: number;
  positive_feedback_ratio: number | null;
}

export interface AgentChatItem {
  id: string;
  channel: string;
  title: string;
  user_name: string | null;
  message_count: number;
  last_message_preview: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentChatListResponse {
  items: AgentChatItem[];
  total: number;
  stats: AgentChatStats;
}

interface AgentChatMessage {
  id: string;
  role: string;
  content: string;
  channel: string;
  sequence: number;
  created_at: string;
  attachments?: ChatMessageAttachment[] | null;
}

export interface AgentChatDetail {
  id: string;
  channel: string;
  title: string;
  user_name: string | null;
  message_count: number;
  created_at: string;
  updated_at: string;
  messages: AgentChatMessage[];
  has_more: boolean;
}

interface AgentFeedbackCommentItem {
  id: string;
  message_id: string;
  conversation_id: string;
  channel: string;
  user_name: string | null;
  user_email: string | null;
  rating: "positive" | "negative";
  comment: string;
  created_at: string;
  updated_at: string;
}

export interface AgentFeedbackCommentListResponse {
  items: AgentFeedbackCommentItem[];
  total: number;
  limit: number;
  offset: number;
}

// Annotations — one user's rating of a piece of agent work.
// Mirrors `AnnotationTargetType` in backend/app/services/annotations.py.
export type AnnotationTargetType =
  | "message"
  | "assignment_run"
  | "sub_agent_task"
  | "agent_delegation"
  | "agent_account_reply";

/** Where a rating was given: the web thumbs, or a reaction in a platform.
 *  Mirrors `Source` in backend/app/services/reaction_feedback.py. */
type AnnotationSource = "web" | "teams_agent_account";

/** One criterion's score in a rating: an option value, or a number. */
export interface AnnotationScore {
  criterion_id: string;
  value: number | string;
}

export interface Annotation {
  id: string;
  target_type: AnnotationTargetType;
  target_id: string;
  rating: MessageFeedbackRating;
  comment: string | null;
  scores: AnnotationScore[];
  /** 0-1: the weighted mean of the scores, or the thumbs when none. */
  weighted_score: number;
  created_at: string;
  updated_at: string;
}

export interface AnnotationUpsert {
  rating: MessageFeedbackRating;
  comment?: string | null;
  /** The whole set of scores; omitted, the stored ones are kept. */
  scores?: AnnotationScore[];
}

// Feedback criteria
export type FeedbackCriterionType = "categorical" | "numeric" | "scale";

interface FeedbackCriterionOption {
  value: string;
  label: string | null;
  /** What choosing this option is worth, 0-1. */
  score: number;
}

export interface FeedbackCriterion {
  id: string;
  key: string;
  name: string;
  description: string | null;
  type: FeedbackCriterionType;
  weight: number;
  options: FeedbackCriterionOption[] | null;
  min_value: number | null;
  max_value: number | null;
  sort_order: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface FeedbackCriterionCreate {
  name: string;
  description?: string | null;
  type: FeedbackCriterionType;
  weight?: number;
  options?: FeedbackCriterionOption[] | null;
  min_value?: number | null;
  max_value?: number | null;
  sort_order?: number;
  enabled?: boolean;
}

/** The type of a criterion never changes once created. */
export type FeedbackCriterionUpdate = Partial<Omit<FeedbackCriterionCreate, "type">>;

interface CriterionHistogramBucket {
  key: string;
  label: string;
  count: number;
}

export interface CriterionDay {
  date: string;
  count: number;
  mean_score: number;
}

export interface CriterionSummary {
  criterion_id: string;
  key: string;
  name: string;
  type: FeedbackCriterionType;
  weight: number;
  enabled: boolean;
  count: number;
  mean_score: number | null;
  histogram: CriterionHistogramBucket[];
  daily: CriterionDay[];
}

export interface AgentAnnotationItem {
  id: string;
  target_type: AnnotationTargetType;
  target_id: string;
  /** The conversation of a rated chat message; null for every other target. */
  conversation_id: string | null;
  /** Whether the viewer may open the rated work (its run page, the agent's chats). */
  can_open: boolean;
  rating: MessageFeedbackRating;
  comment: string | null;
  /** The XTM One user who rated; both null for a reaction in a platform. */
  user_name: string | null;
  user_email: string | null;
  source: AnnotationSource;
  /** The reaction that gave the rating, null for a web rating. */
  emoji: string | null;
  weighted_score: number;
  created_at: string;
  updated_at: string;
}

export interface AgentAnnotationListResponse {
  items: AgentAnnotationItem[];
  total: number;
  limit: number;
  offset: number;
}

interface AnnotationTypeSummary {
  target_type: AnnotationTargetType;
  positive: number;
  negative: number;
  total: number;
  positive_ratio: number | null;
}

export interface AgentAnnotationSummary {
  agent_id: string;
  positive: number;
  negative: number;
  total: number;
  positive_ratio: number | null;
  by_target_type: AnnotationTypeSummary[];
  criteria: CriterionSummary[];
  /** 0-1, or null when nothing was rated. */
  weighted_score: number | null;
}

// Improvement proposals (#8256): the persona self-improvement loop (#1044) is the `persona` type
export type ImprovementProposalStatus =
  | "pending_approval"
  | "rejected"
  | "applying"
  | "shadow"
  | "watching"
  | "kept"
  | "rolled_back"
  | "regression_kept"
  | "failed";

type ImprovementProposalType =
  | "persona"
  | "skill"
  | "assignment_prompt"
  | "flow_repair"
  | "flow_optimization"
  | "new_flow"
  | "agent_to_flow";

interface ImprovementProposalDiffLine {
  type: "add" | "remove" | "context";
  content: string;
  old_line: number | null;
  new_line: number | null;
}

export interface ImprovementProposal {
  id: string;
  proposal_type: ImprovementProposalType;
  target_type: "agent" | "skill" | "assignment" | "flow";
  target_id: string;
  target_name: string | null;
  agent_id: string | null;
  status: ImprovementProposalStatus;
  summary: string | null;
  base_content: string;
  proposed_content: string;
  diff: ImprovementProposalDiffLine[];
  added: number;
  removed: number;
  base_version_number: number | null;
  applied_version_number: number | null;
  rollback_version_number: number | null;
  /** The experiment it was filed with; `null` for a person's request, measured by its re-evaluation alone. */
  experiment_verdict: "progression" | "neutral" | "regression" | "unmeasured" | null;
  experiment: Record<string, unknown>;
  evidence_refs: { kind: string; id: string }[];
  experiment_refs: { kind: string; id: string }[];
  evaluation_id: string | null;
  evaluation_name: string | null;
  baseline_run_id: string | null;
  candidate_run_id: string | null;
  /** The run score the cycle compares, 0-100: `score` (Judge Quality); `f1_score` only on a cycle proposed while it still read the retired F1. */
  quality_metric: string;
  baseline_score: number | null;
  candidate_score: number | null;
  delta: number | null;
  verdict: "progression" | "neutral" | "regression" | null;
  noise_margin_points: number;
  outcome_note: string | null;
  author_agent_id: string | null;
  author_agent_name: string | null;
  requested_by: string | null;
  decided_by: string | null;
  /** Whether the caller may approve or reject it now. */
  can_decide: boolean;
  created_at: string;
  decided_at: string | null;
  applied_at: string | null;
  watch_until: string | null;
  settled_at: string | null;
}

export interface ImprovementProposalList {
  items: ImprovementProposal[];
  /** Whether the caller may ask for a persona rewrite of this agent now. */
  can_propose: boolean;
}

// Quotas
export interface QuotaTypeConfig {
  platform_limit?: number | null;
  default?: number | null;
  per_model?: Record<string, number> | null;
  period?: "monthly" | "daily" | "yearly";
  /** User / group overrides only: the explicit Unlimited choice (`default` is
   *  then `null`). An override with no limit and no flag inherits. */
  unlimited?: boolean | null;
  /** Stamped by the backend on every save; a value without it was saved
   *  before the current quota rules and keeps its previous meaning. */
  rules_version?: number | null;
}

/** Why a legacy quota value would change meaning on its next save, keyed by
 *  quota type (`legacy_zero_under_pool`, `legacy_group_no_limit`). */
export type QuotaLegacyNotices = Partial<Record<"llm_proxy" | "agentic", string>>;

export interface QuotaConfig {
  llm_proxy?: QuotaTypeConfig | null;
  agentic?: QuotaTypeConfig | null;
  // No `sandbox` field: sandbox usage has no enforceable limit or
  // configurable period of its own — it always shares the agentic entry's.
}

export interface QuotaUsageEntry {
  usage_type: string;
  model: string | null;
  period: string;
  count: number;
  // "user" = a per-user counter; "global" = a shared platform-wide pool
  // (e.g. the included agentic allowance) where `count` is the total across
  // all users and entry points, not just this viewer's usage.
  scope?: "user" | "global";
}

export interface UserQuotaInfo {
  // User's own raw override (``User.quota_config`` column). ``null``
  // when the user has no override and inherits from group / platform /
  // license. Distinct from ``resolved_config`` which is the effective
  // config after walking the full resolution chain — bind the editor
  // form to this field, not ``resolved_config``.
  user_config: QuotaConfig | null;
  user_config_notices?: QuotaLegacyNotices;
  resolved_config: QuotaConfig | null;
  usage: QuotaUsageEntry[];
  sources: Record<string, string>;
}

export interface QuotaSummary {
  type: string;
  limit: number | null;
  period: string;
  current_usage: number;
  active_users: number;
  total_users: number;
  users_near_limit: number;
  users_over_limit: number;
  is_global: boolean;
}

export interface QuotaHistoryEntry {
  period: string;
  agentic?: number;
  llm_proxy?: number;
}

export interface QuotaConsumer {
  name: string;
  email: string;
  count: number;
}

export interface QuotaOverQuotaUser {
  name: string;
  email: string;
  usage: number;
  limit: number;
  status: "over" | "near";
}

export interface QuotaMonitoring {
  quotas: QuotaSummary[];
  history: QuotaHistoryEntry[];
  top_consumers: Record<string, QuotaConsumer[]>;
  over_quota_users: Record<string, QuotaOverQuotaUser[]>;
}

// Agent failure pool — failed runs are refunded up to a bounded, per-agent
// allowance that renews once a year on the licence anniversary. The pool SIZE
// is an environment setting (no UI), so everything here is read-only except
// redeeming a Filigran support code, which recharges every pool at once.
interface AgentFailurePoolEntry {
  user_id: string;
  user_email: string | null;
  agent_id: string;
  // null when the counter outlived its agent (deleted between the CASCADE and
  // the key's expiry) — surfaced rather than hidden.
  agent_name: string | null;
  used: number;
  remaining: number;
  exhausted: boolean;
  // Credited back for free — no model was ever called: the provider was
  // unreachable / down / not configured, or the orchestration engine abandoned
  // the run (worker died, lease expired). Unbounded by design, so there is no
  // "remaining".
  free_refunds: number;
  // A seeded platform agent: its scheduled runs are quota-exempt, so only its
  // manually triggered runs can ever appear here. Flagged, not filtered.
  agent_is_builtin: boolean;
  // This user's effective agentic quota is unlimited: a failed run
  // costs them nothing, so the pool is not consumed for them and `exhausted`
  // is never set. `used` is history — slots spent before the rule landed or
  // under a limit since lifted — not a live ration.
  quota_unlimited: boolean;
}

interface AgentFailurePoolRedemption {
  code_id: string;
  kind: string;
  note: string | null;
  redeemed_at: string | null;
  redeemed_by: string | null;
}

export interface AgentFailurePoolStatus {
  enabled: boolean;
  pool_size: number;
  // Nothing platform-wide caps agentic actions — no license ceiling /
  // platform limit, no platform default. The pool then only applies
  // to users or groups carrying a limit of their own.
  platform_quota_unlimited: boolean;
  window: {
    start: string;
    end: string;
    // Where the anniversary comes from: the Filigran licence, this platform's
    // creation date, or the calendar year when neither is readable.
    source: "xtm_license" | "platform" | "calendar";
    days_remaining: number;
  };
  totals: {
    agents_with_failures: number;
    exhausted: number;
    // Two credit-back reasons, reported apart: only the first is rationed.
    failures_forgiven: number;
    free_credits: number;
    // Credits whose user AND agent were both deleted since: included
    // in the two totals above, in no row.
    stale_pairs?: number;
    stale_failures_forgiven?: number;
    stale_free_credits?: number;
  };
  /**
   * Per-cause decomposition of the two totals above. Descending by
   * count, and summing to their total: any shortfall arrives as a explicit
   * `not_recorded` entry rather than being dropped. Absent on a platform that
   * has not credited anything back yet.
   */
  forgiven_by_cause?: AgentFailurePoolCauseCount[];
  free_credits_by_cause?: AgentFailurePoolCauseCount[];
  entries: AgentFailurePoolEntry[];
  truncated: boolean;
  redemptions: AgentFailurePoolRedemption[];
}

/** One row of a credit-back total's per-cause breakdown. */
export interface AgentFailurePoolCauseCount {
  /** A `FailureCause` value, or the `not_recorded` remainder sentinel. */
  cause: string;
  count: number;
}

interface AgentFailurePoolRechargeResult {
  code_id: string;
  note: string | null;
  // Reported apart rather than as one number: the durable rows and the live
  // Redis keys overlap only partially, so neither is a total.
  rows_deleted: number;
  keys_cleared: number;
  // True when the Redis sweep hit its bound and some pools were NOT recharged.
  truncated: boolean;
  pool_size: number;
  window_start: string;
  window_end: string;
}

/** One platform-wide counter a `platform_quota_reset` support code zeroed. */
export interface PlatformQuotaResetCounter {
  quota_type: "agentic" | "sandbox" | "llm_proxy";
  period: "daily" | "monthly" | "yearly";
  /** The window that was reset: `2026-10-03`, `2026-10` or `2026`. */
  period_key: string;
  /** What the counter held before the reset. */
  previous_count: number;
}

export interface PlatformQuotaResetResult {
  code_id: string;
  note: string | null;
  quota_types: PlatformQuotaResetCounter["quota_type"][];
  periods: PlatformQuotaResetCounter["period"][];
  counters: PlatformQuotaResetCounter[];
}

/** What `POST /settings/quotas/support-codes/redeem` answers, by code kind. */
export type SupportCodeRedeemResult =
  | ({ kind: "agent_failure_pool_recharge" } & AgentFailurePoolRechargeResult)
  | ({ kind: "platform_quota_reset" } & PlatformQuotaResetResult);

// Groups
export interface Group {
  id: string;
  name: string;
  description: string | null;
  member_count: number;
  created_by: string | null;
  quota_config: QuotaConfig | null;
  quota_notices?: QuotaLegacyNotices;
  /** False when AI is switched off for the members (positive on the wire, like
   *  the platform switch); served by the admin routes only. */
  ai_enabled?: boolean;
  /** What the group is for the LLM analytics; `auto` = not classified. */
  kind?: GroupKind;
  created_at: string;
  updated_at: string;
}

export type GroupKind = "org_unit" | "access" | "distribution" | "auto";

export type GroupDetail = Group;

// Departments: the org units LLM analytics roll usage up to
export type DepartmentSource = "directory" | "groups";

interface DepartmentItem {
  id?: string | null;
  name: string;
  description?: string | null;
  /** Directory department values (any case) that resolve here besides the name. */
  directory_values: string[];
}

interface DepartmentRule {
  group_id: string;
  /** The department's name: a department of the same save may be new. */
  department: string;
  group_name?: string | null;
  group_kind?: GroupKind | null;
}

export interface DepartmentMapping {
  departments: DepartmentItem[];
  /** In priority order: a person's first rule whose group they are in decides. */
  rules: DepartmentRule[];
  settings: { sources: DepartmentSource[]; directory_attribute: string };
}

export interface DepartmentMappingResponse extends DepartmentMapping {
  backfill: {
    started: boolean;
    completed_at: string | null;
    cursor_date: string | null;
    rewrite_requested_at: string | null;
  };
}

interface DepartmentPreviewPerson {
  user_id: string;
  email: string;
  full_name: string | null;
  directory_department: string | null;
}

export interface DepartmentPreview {
  configured: boolean;
  user_count: number;
  resolved_count: number;
  unassigned_count: number;
  departments: { department_id: string; name: string; user_count: number; users: DepartmentPreviewPerson[] }[];
  unassigned: DepartmentPreviewPerson[];
}

export interface DirectoryImportResult {
  entries: number;
  updated: number;
  unknown_count: number;
  unknown_emails: string[];
}

export interface PaginatedGroupMembers {
  items: GroupMemberItem[];
  total: number;
}

/** One row of `GET /groups/{id}/members`: the Members table of a group. */
export interface GroupMemberItem {
  id: string;
  user_id: string;
  email: string;
  full_name: string;
  role: string;
  added_at: string;
  /** Also one of the group's managers. */
  is_manager?: boolean;
}

/** A group's manager: reads the AI usage of the group's current members. */
export interface GroupManager {
  user_id: string;
  email: string;
  full_name: string;
  /** A manager need not be a member of the group they manage. */
  is_member: boolean;
  assigned_at: string;
}

interface ManagedGroupRef {
  id: string;
  name: string;
}

export interface UserGroupItem {
  id: string;
  name: string;
  description: string | null;
  member_count: number;
  added_at: string;
}

// ---------------------------------------------------------------------------
// Public Catalog
// ---------------------------------------------------------------------------

export interface PublicCatalogSummary {
  counts: Record<string, number>;
  total: number;
}

export interface PaginatedCatalogResponse<T> {
  items: T[];
  total: number;
}

export interface PublicCatalogTags {
  agents: string[];
  prompts: string[];
  skills: string[];
  mcp_servers: string[];
  custom_tools: string[];
}

export interface PublicCatalogAgent {
  id: string;
  name: string;
  description: string | null;
  icon: string;
  tags: string[];
  output_format: string;
  output_schema: Record<string, unknown> | null;
  assignment_count: number;
  shared_by: string | null;
  created_at: string;
}

export interface CatalogResourceRef {
  id: string;
  name: string;
  description?: string | null;
  is_company_managed: boolean;
}

export interface CatalogIntegrationRef {
  provider: string;
  name?: string;
  integration_id?: string;
  is_company_managed?: boolean;
}

export interface PublicCatalogAgentDetail extends PublicCatalogAgent {
  persona: string | null;
  disable_chat: boolean;
  shared_by: string | null;
  updated_at: string;
  assignments: {
    name: string;
    description: string | null;
    trigger_type: string;
    prompt: string | null;
  }[];
  integration_providers?: CatalogIntegrationRef[];
  knowledge_bases?: CatalogResourceRef[];
  mcp_servers?: CatalogResourceRef[];
  custom_tools?: CatalogResourceRef[];
  skills?: CatalogResourceRef[];
  prompts?: CatalogResourceRef[];
  variables?: {
    id: string;
    key: string;
    description: string | null;
    tags: string[];
    is_secret: boolean;
    value?: string;
  }[];
  builtin_tool_ids?: string[];
  /** Custom tools the download leaves out: the publisher kept their code. */
  download_omitted_custom_tools?: string[];
}

export interface Variable {
  id: string;
  name: string;
  value: string | null;
  is_secret: boolean;
  description: string | null;
  tags: string[];
  user_id: string | null;
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** A secret value read from a vault, and its recent failures. */
  vault_secrets?: VaultSecretStatus | null;
  created_at: string;
  updated_at: string;
}

export interface PublicCatalogPrompt {
  id: string;
  name: string;
  description: string | null;
  tags: string[];
  shared_by: string | null;
  created_at: string;
}

export interface PublicCatalogPromptDetail extends PublicCatalogPrompt {
  content: string;
  updated_at: string;
}

export interface PublicCatalogSkill {
  id: string;
  name: string;
  description: string | null;
  url: string | null;
  compatibility: string | null;
  tags: string[];
  shared_by: string | null;
  created_at: string;
}

export type {
  LLMSpan,
  LLMSpanListResponse,
  MediaReference,
  PayloadField,
  PayloadFieldMeta,
  TracePayloadResponse,
  TraceAnalyticsCostByAgentItem,
  TraceAnalyticsCostByAgentResponse,
  TraceAnalyticsCostByGroupItem,
  TraceAnalyticsCostByGroupResponse,
  TraceAnalyticsCostByModelItem,
  TraceAnalyticsCostByModelResponse,
  TraceAnalyticsCostByUserItem,
  TraceAnalyticsCostByUserResponse,
  TraceAnalyticsUsageTotals,
  TraceAnalyticsCostOverTimeResponse,
  TraceAnalyticsErrorItem,
  TraceAnalyticsErrorsResponse,
  TraceAnalyticsFilters,
  TraceAnalyticsLatencyByModelItem,
  TraceAnalyticsLatencyResponse,
  TraceAnalyticsPeriod,
  TraceAnalyticsRefreshResponse,
  TraceAnalyticsSummary,
  TraceAnalyticsVolumeOverTimeResponse,
  TraceFilters,
  TraceSession,
  TraceSessionExplorerResponse,
  TraceSessionFilters,
  TraceSessionListResponse,
  LLMTrace,
  LLMTraceListResponse,
} from "./traces";

export interface PublicCatalogSkillDetail extends PublicCatalogSkill {
  content: string | null;
  updated_at: string;
}

export interface PublicCatalogMCPServer {
  id: string;
  name: string;
  transport: string;
  url: string | null;
  tags: string[];
  tool_count: number;
  shared_by: string | null;
  created_at: string;
}

export interface PublicCatalogMCPServerDetail extends PublicCatalogMCPServer {
  description?: string | null;
  command: string | null;
  args: string[] | null;
  tools: { name: string; description: string }[];
  updated_at: string;
}

export interface PublicCatalogCustomTool {
  id: string;
  name: string;
  description: string | null;
  icon_url: string | null;
  tags: string[];
  shared_by: string | null;
  created_at: string;
}

export interface PublicCatalogCustomToolDetail extends PublicCatalogCustomTool {
  input_schema: JsonSchema | null;
  js_function: string | null;
  language?: string;
  timeout?: number;
  updated_at: string;
}

/** A variable an agent references that has no value yet, or - when
 *  `deleted` - that no variable carries any more, with no name. */
export interface VariableNeedingValue {
  id: string;
  name: string | null;
  is_secret: boolean;
  deleted?: boolean;
}

export interface AddToAccountResponse {
  id: string;
  name: string;
  entity_type: string;
  /** Variables an agent install created without a value: a secret is never copied. */
  variables_needing_value?: VariableNeedingValue[];
  /** What an agent install did not copy, or copied only in part, and why -
   *  one sentence per kind, counts only. */
  warnings?: string[];
}

export type CatalogEntityType = "agents" | "prompts" | "skills" | "mcp-servers" | "custom-tools";

// --- Admin Objects (platform-wide entity browser) ---
// Mirrors the keys of `ENTITY_MAP` in `backend/app/api/v1/admin_objects.py`
// (pinned by `backend/tests/test_admin_objects.py`).
export type PlatformObjectType =
  | "agent"
  | "knowledge_base"
  | "prompt"
  | "skill"
  | "mcp_server"
  | "custom_tool"
  | "integration"
  | "presentation"
  | "video"
  | "template_container"
  | "runner"
  | "flow"
  | "dataset"
  | "evaluation"
  | "variable"
  | "channel_bot"
  | "assignment"
  | "assignment_run"
  | "conversation"
  | "report"
  | "file"
  | "workspace"
  | "workset"
  | "work_item"
  | "flow_run"
  | "browser_profile"
  | "fleet";

export interface PlatformObjectItem {
  id: string;
  name: string;
  object_type: PlatformObjectType;
  user_id: string | null;
  user_email: string | null;
  user_name: string | null;
  is_company_managed: boolean;
  created_at: string;
  updated_at: string;
  /** Per-type detail, `null` on the types that have no such notion. A machine
   *  (`runner`) fills them: its live status (`online` / `offline` /
   *  `revoked`, read like the Desktop page), its OS family as `subtype`, when
   *  it was last seen and the runner build it runs as `version`. The other
   *  types put their own state in `status` (a run's, a work item's)
   *  and their kind in `subtype` (a trigger, a channel, a file's type). */
  status: string | null;
  subtype: string | null;
  last_seen_at: string | null;
  version: string | null;
}

export interface PlatformObjectListResponse {
  items: PlatformObjectItem[];
  total: number;
}

// --- Datasets ---

export interface Dataset {
  id: string;
  user_id: string | null;
  name: string;
  description: string | null;
  evaluation_instructions: string | null;
  /**
   * The answer shape every entry is expected to match. Owned by the
   * dataset, so every evaluation reusing it reads the same declaration.
   */
  output_schema: OutputSchema | null;
  /** The output family the entries are shaped for; `null` until declared. */
  family: EvaluationFamily | null;
  /** Declared label set of a `CLASSIFICATION` dataset; `null` otherwise. */
  label_vocabulary: string[] | null;
  /** `true` while `family` is unset: an unresolved problem, not an "any family" state. */
  needs_family: boolean;
  /** Set only when a family change conflicts with a linked evaluation;
   * reported, never auto-resolved. */
  family_conflicts?: { evaluation_id: string; evaluation_name: string; message: string }[] | null;
  content_hash: string | null;
  current_version: number;
  is_company_managed: boolean;
  is_read_only: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  evaluation_count: number;
  entry_count: number;
  created_at: string;
  updated_at: string;
}

export interface OutputSchema {
  type: "json_schema";
  definition: unknown;
}

export interface DatasetEntry {
  id: string;
  dataset_id: string;
  position: number;
  description: string | null;
  input_prompt: string;
  expected_answer: string;
  evaluation_prompt: string | null;
  file_ids: string[] | null;
  output_schema: OutputSchema | null;
  files: { id: string; filename: string; size: number; content_type: string }[] | null;
  created_at: string;
}

interface DatasetVersionEntry {
  id: string;
  entry_id: string | null;
  position: number;
  description: string | null;
  input_prompt: string | null;
  expected_answer: string;
  evaluation_prompt: string | null;
  file_ids: string[] | null;
  output_schema: OutputSchema | null;
}

export interface DatasetVersion {
  id: string;
  dataset_id: string;
  version_number: number;
  entry_count: number;
  created_at: string;
}

export interface DatasetVersionDetail extends DatasetVersion {
  entries: DatasetVersionEntry[];
}

export interface DatasetDetail extends Dataset {
  entries: DatasetEntry[];
}

export interface LinkedDataset {
  id: string;
  dataset_id: string;
  dataset_name: string;
  dataset_description: string | null;
  entry_count: number;
  position: number;
  created_at: string;
  /** The dataset's output family; `null` while it has none, which blocks a launch (`needs_family`). */
  family: EvaluationFamily | null;
  needs_family: boolean;
  /** Set when the dataset's family and the evaluation's are both declared and differ:
   *  left by a later family change, reported, never fixed. */
  family_conflict: string | null;
}

/**
 * How a single JSON field is scored. Derived from the field's schema unless
 * an author overrides it; a closed vocabulary, offered as a picker.
 */
export const FIELD_SCORING_MODES = [
  "exact",
  "enum",
  "enum_set",
  "free_text",
  "resource_address",
  "object_list",
  "excluded",
] as const;

export type FieldScoringMode = (typeof FIELD_SCORING_MODES)[number];

/** The entries Overall Quality considers and covers, and why the others are out. */
export interface QualityCoverage {
  entries: number;
  counted: number;
  /** Reason (a code, or the backend's own sentence) -> entries left out for it. */
  left_out: Record<string, number>;
}

/** Per-field scoring intent on an evaluation, keyed by JSON Pointer. */
export interface FieldScoringOverrides {
  field_modes?: Record<string, FieldScoringMode> | null;
  field_weights?: Record<string, number> | null;
}

export interface CreateDatasetRequest {
  name: string;
  description?: string | null;
  evaluation_instructions?: string | null;
  /**
   * `{}` clears the schema on a `PUT`, where `null` or omitted means "leave it alone".
   * A create never sends `{}`.
   */
  output_schema?: OutputSchema | Record<string, never> | null;
  /** The family the entries are shaped for. */
  family?: EvaluationFamily | null;
  /** Settable only while `family === "CLASSIFICATION"`. */
  label_vocabulary?: string[] | null;
  is_company_managed?: boolean;
  is_read_only?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  entries?: {
    input_prompt: string;
    expected_answer: string;
    evaluation_prompt?: string | null;
    description?: string | null;
  }[];
}

export interface HFSplitsResponse {
  splits: { dataset: string; config: string; split: string; num_rows: number }[];
}

export interface HFPreviewResponse {
  rows: Record<string, unknown>[];
  features: { name: string; type: { _type: string } }[];
}

// --- Evaluations ---

export interface Evaluation {
  id: string;
  user_id: string | null;
  name: string;
  description: string | null;
  trigger_config: { schedule: string | null; on_agent_change: boolean };
  use_agent_default_model: boolean;
  model_ids: string[];
  disabled_model_ids: string[];
  // The models of `model_ids` no provider of the owner offers: a launch refuses them.
  unavailable_model_ids?: string[];
  execution_mode: "llm_only" | "full_loop";
  // Whether a Full Loop run checks prompts and answers with the prompt guard.
  apply_prompt_guards: boolean;
  // Whether a Full Loop run holds tool calls that need approval.
  require_tool_approval: boolean;
  run_mode: "parallel" | "sequential";
  evaluator_model: string | null;
  evaluator_prompt: string | null;
  evaluation_instructions: string | null;
  performance_tolerance: number;
  sub_score_weights: Record<string, number> | null;
  quality_source: "judge" | "f1";
  penalty_factor: number;
  timeout_seconds: number;
  judge_max_tokens: number;
  /** Automatic runs: off pauses the schedule and on-agent-change, never a run started by hand. */
  enabled: boolean;
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  agent_ids: string[];
  /** Flow subjects (#8257), linked by the Platform Health; never in `agent_ids`. */
  flows?: { id: string; name: string; tags?: Record<string, string> | null; is_baseline?: boolean }[];
  /** Per-agent prediction extractors (#3290). Absent = score the raw output. */
  agent_extractors?: Record<string, ExtractorSpec> | null;
  agents: {
    id: string;
    name: string;
    slug: string | null;
    icon: string | null;
    // Cascading tags + agent-scoped baseline
    tags?: Record<string, string> | null;
    is_baseline?: boolean;
  }[];
  entry_count: number;
  dataset_count: number;
  has_active_runs: boolean;
  last_triggered_at: string | null;
  created_at: string;
  updated_at: string;
  // Evaluation Pipeline v1
  custom_params: Record<string, unknown> | null;
  field_scoring_overrides: FieldScoringOverrides | null;
  scorer_selection: string[] | null;
  reuse_default: "off" | "on";
  /**
   * Headline composition. Tri-state: `null` inherits every metric the
   * family measures, `[]` composes from none (N/A), a subset refines. Never
   * normalise `[]` to `null`: that re-adds every metric the author removed.
   */
  overall_quality_metrics: string[] | null;
  overall_performance_metrics: string[] | null;
  // Output family driving package metric selection
  family: EvaluationFamily | null;
  // Cascading tags
  tags?: Record<string, string> | null;
  /** Placebo-controlled method; null when the evaluation runs without placebo arms. */
  placebo_config?: EvaluationPlaceboConfig | null;
  /** What an import could not carry over; empty on every other answer. */
  warnings?: string[];
}

export type PlaceboTargetKind = "persona" | "prompts" | "skills" | "skill";

export interface PlaceboTarget {
  kind: PlaceboTargetKind;
  skill_id: string | null;
}

export interface EvaluationPlaceboConfig {
  targets: PlaceboTarget[];
  include_no_target: boolean;
  trials_per_entry: number;
  custom_text: string | null;
}

/** `real` on every run outside a placebo comparison. */
export type EvaluationArm = "real" | "placebo" | "no_target";

/** Why a metric did not contribute to a headline. Open by design, see `MetricExclusion`.
 *
 * `not_applicable` and `failed` come from the metric package's status vocabulary and
 * describe this run: no re-run can recover the first, one might recover the second, so
 * only the second is offered a re-run. Quality emits them against an explicit selection;
 * performance also against the default one, for a metric some runs lack.
 *
 * `missing_from_other_runs` (performance only): this run measured the metric, but
 * Overall Performance counts only metrics every run has, and the runs in `run_ids` lack it.
 *
 * `not_a_default_contributor`: the metric scored but was not selected; it composes
 * the headline only when asked for explicitly.
 *
 * `no_performance_data` and `too_few_comparable_runs`, `no_metric_selected` and
 * `only_opt_in_metrics_shared` describe a run, not a metric. They carry
 * `RUN_LEVEL_EXCLUSION_KEY` as their `key` and must be filtered out of any view that maps
 * exclusions onto metric rows.
 */
export type KnownExclusionReason =
  | "not_applicable"
  | "failed"
  | "not_a_default_contributor"
  | "missing_from_other_runs"
  | "no_performance_data"
  | "too_few_comparable_runs"
  | "no_metric_selected"
  | "only_opt_in_metrics_shared";

/** The `key` a run-level exclusion carries; package metric keys are dotted, so it cannot
 * collide with one. Mirrors `RUN_LEVEL_EXCLUSION_KEY` in
 * `backend/app/services/evaluation/aggregation.py`, pinned by
 * `test_evaluation_responses.py`. */
export const RUN_LEVEL_EXCLUSION_KEY = "__run__";

/** One metric that could not contribute to a headline. */
export interface MetricExclusion {
  key: string;
  // Open vocabulary: `(string & {})` keeps the neutral-rendering branch reachable
  // in the type system, so it is not deleted as dead code.
  reason: KnownExclusionReason | (string & {});
  // Which runs the exclusion concerns. Always served for performance; optional because
  // the run-search payload carries no exclusions.
  run_ids?: string[];
}

/** The mode an evaluation run executed in, as the run recorded it. */
type EvaluationRunMode = "llm_only" | "full_loop" | "skip_llm";

export interface EvaluationRun {
  id: string;
  evaluation_id: string;
  evaluation_name: string;
  /** The subject (#8257): an agent, or a flow (`flow_id`); `agent_name` names either. */
  agent_id: string | null;
  flow_id?: string | null;
  subject_kind?: "agent" | "flow";
  agent_name: string;
  agent_slug: string;
  agent_user_name: string | null;
  model_id: string;
  is_default_model: boolean;
  status: string;
  trigger_reason: string;
  score: number | null;
  speed_score: number | null;
  overall_score: number | null;
  f1_score: number | null;
  // Headline metrics. `null` means "not measured" and renders as "N/A",
  // never 0.
  //
  // `quality_score` is absolute. `performance_score` is comparative: normalised
  // against the evaluation's other runs, which every surface showing it discloses.
  quality_score: number | null;
  performance_score: number | null;
  // Metrics that actually composed each headline; empty when it is N/A.
  quality_metric_keys: string[];
  performance_metric_keys: string[];
  // What each contributing metric put into the headline mean. Not the raw mean the
  // Metrics panel shows: quality applies a consistency penalty and performance is
  // normalised across the evaluation. Never recomputed here: the browser lacks the
  // sibling runs and the penalty factor.
  quality_contributions: Record<string, number>;
  performance_contributions: Record<string, number>;
  // The entries Overall Quality considers, those it covers, and per reason how many it
  // leaves out. `null` on a run scored before it was recorded.
  quality_coverage?: QualityCoverage | null;
  // Selected metrics that could not contribute, per headline: a metric missing from
  // Overall Quality says nothing about Overall Performance.
  quality_aggregation_excluded: MetricExclusion[];
  performance_aggregation_excluded: MetricExclusion[];
  quality_source: "judge" | "f1";
  duration_seconds: number | null;
  avg_sub_scores: Record<string, number>;
  completed_entries: number;
  total_entries: number;
  error: string | null;
  /** Why the last rescore failed: a rescore leaves the run's status and error as they were. */
  rescore_error?: string | null;
  /** Pending because agentic AI is unavailable here: the refusal code. */
  held_reason?: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  persona_version_number?: number | null;
  prompt_versions?: Record<string, number> | null;
  agent_config_versions?: Record<string, number | null> | null;
  // Version of the vendored metric suite that scored this run. Null renders
  // as unknown, never as current.
  metric_suite_version?: string | null;
  // Which scorers produced this run's scores; null reads as judge-on. Read
  // this, not the evaluation's mutable selection, to explain an N/A judge headline.
  scorer_selection?: string[] | null;
  can_replay: boolean;
  replay_block_reason?: string | null;
  // Evaluation Pipeline v1
  pinned_dataset_versions?: Record<string, number> | null;
  tags?: Record<string, string> | null;
  // Cascading tags: read-time merge of evaluation ⊕ agent ⊕ run tags, plus
  // the agent-derived baseline flag.
  effective_tags?: Record<string, string>;
  is_baseline?: boolean;
  has_dataset_drift?: boolean;
  // Autonomous evaluation mode
  anomaly_count?: number;
  // The mode this run recorded, not the evaluation's current setting;
  // null when none was recorded.
  execution_mode?: EvaluationRunMode | null;
  // Whether this run applied the prompt guard; null when recorded before the setting.
  apply_prompt_guards?: boolean | null;
  // Whether this run held tool calls that need approval; null when recorded before the setting.
  require_tool_approval?: boolean | null;
  // Entries that fell back to LLM Only, a fallback that no longer exists.
  fallback_entries?: number;
  // Reuse visibility
  reused_entries?: number;
  // Judge scoring reliability
  judge_failure_count?: number;
  // Entries the platform could not run, left out of every headline.
  platform_error_count?: number;
  // True while this run's rescore batch awaits the coalesced Overall Performance
  // pass that lands its final `performance_score`. Set by the
  // run-detail endpoint only once the run reads terminal; see `useEvaluationRun`.
  rescore_pending?: boolean;
  // A parked episode's callback can still rewrite this run's headlines after it
  // reads terminal. Bounded so the poll cannot run forever: by the sweep
  // that expires a parked episode, then by the resume execution. Set under the
  // same terminal-only condition as `rescore_pending`.
  callback_pending?: boolean;
  // A placebo comparison's run: its arm, its trial, the target a control arm
  // replaces or removes, and its comparison. `real`, 0 and null on every other run.
  arm?: EvaluationArm;
  trial_index?: number;
  placebo_target?: PlaceboTarget | null;
  placebo_comparison_id?: string | null;
}

/** One step of the legacy per-result execution timeline (`execution_details.traces`). */
export interface EvaluationExecutionTrace {
  // `judge_skipped`: the judge did not run on the entry (judge off, or an anomaly).
  type: "llm" | "tool" | "judge" | "judge_skipped" | "wait";
  description: string;
  duration_ms: number;
  ttft_ms?: number;
  model?: string;
  tool_name?: string;
  tool_input?: string;
  tool_output?: string;
  phase?: string;
  // Judge-only fields (populated since evaluation_engine v2)
  provider?: string;
  system_prompt?: string;
  user_prompt?: string;
  raw_response?: string;
  // Available on all LLM traces (full_loop: last call; llm_only: single call)
  stop_reason?: string | null;
  usage?: Record<string, number>;
  // Judge-specific truncation flags
  truncated?: boolean;
  is_judge_failure?: boolean;
}

/**
 * `EvaluationResult.execution_details`: an entry's timings and execution timeline, its
 * package metric outcomes and, with an extractor, the prediction it produced.
 */
interface EvaluationExecutionDetails {
  total_duration_ms: number;
  agent_duration_ms: number;
  // Absent when the judge did not run.
  judge_duration_ms?: number;
  ttft_ms: number | null;
  execution_mode: string;
  // The tools whose calls were held for approval; absent when none was.
  approval_required_tools?: string[];
  traces: EvaluationExecutionTrace[];
  // Package metric outcomes by metric key. The per-field JSON breakdown is
  // the `detail` of `json.field_accuracy`; other metrics have their own shapes, so
  // the payload is read structurally.
  metric_results?: Record<string, PersistedMetricResult> | null;
  // What the agent's output extractor produced. Absent with no extractor
  // (the raw output scored) or on failure (see `metric_results['extraction.failed']`).
  extracted_prediction?: ExtractedPrediction | null;
}

export interface EvaluationResult {
  id: string;
  run_id: string;
  entry_id: string | null;
  description: string | null;
  input_prompt: string;
  expected_answer: string;
  files: { id: string; filename: string; size: number; content_type: string }[] | null;
  agent_output: string;
  trace_ids: string[];
  score: number;
  duration_seconds: number | null;
  speed_score: number | null;
  overall_score: number | null;
  f1_score: number | null;
  // Evaluation Pipeline v1
  is_reused: boolean;
  reused_from_result_id: string | null;
  // Answered in LLM Only after its Full Loop run failed, or reused from such an
  // answer (a fallback that no longer exists).
  fell_back_to_llm_only?: boolean;
  // Autonomous evaluation mode: whether `execution_anomaly_kind` is set.
  is_execution_anomaly?: boolean;
  // Anomaly subtype: an excluded infra hiccup, or a judged and counted
  // confirmation-seeking response. Null for a non-anomalous result.
  execution_anomaly_kind?: "transient_error" | "confirmation_seeking" | "platform_error" | null;
  // Judge scoring reliability
  is_judge_failure?: boolean;
  sub_scores: {
    tool_selection_quality: number;
    response_completeness: number;
    response_accuracy: number;
    instruction_adherence: number;
    coherence: number;
  };
  evaluator_reasoning: string | null;
  execution_details: EvaluationExecutionDetails | null;
  created_at: string;
}

export interface EvaluationRunDetail extends EvaluationRun {
  results: EvaluationResult[];
  /** The placebo a placebo arm ran with (FR-32). */
  placebo_snapshot?: PlaceboSnapshot | null;
}

/** One part of a placebo element: its original's length and its own (FR-6). */
interface PlaceboPartLength {
  original: number;
  placebo: number;
  deviation: number | null;
  within_tolerance: boolean;
  repeated: boolean;
}

/** The placebo a placebo arm ran with (FR-32). */
export interface PlaceboSnapshot {
  kind: PlaceboTargetKind;
  elements: {
    name: string;
    description: string;
    content: string;
    always_load: boolean;
    ref_id: string | null;
    version: number | null;
    lengths: Record<string, PlaceboPartLength>;
  }[];
  language: string;
  language_detected: boolean;
  corpus_version: string;
  seed: string;
  unit: "tokens" | "chars";
  custom_text: boolean;
  warnings: string[];
  leakage_check?: string;
  /** What the token probes cost (unit `tokens` only). */
  probe_usage?: { calls: number; input_tokens: number; output_tokens: number };
}

/** What a placebo trigger would run, and what would refuse it (FR-12). Writes nothing. */
export interface PlaceboEstimate {
  /** Sent back as the trigger's `placebo_seed` to launch what was estimated. */
  seed: string;
  runs: number;
  agent_executions: number;
  judge_calls: number;
  probe_calls: number;
  /** `null` while one agent x model was never measured. */
  estimated_tokens: number | null;
  execution_cap: number;
  /** Agent name -> the tool sources whose calls the comparison repeats unapproved. */
  write_tool_sources: Record<string, string[]>;
  skipped: { agent_id: string; agent_name: string; target: string; reason: string }[];
  warnings: string[];
  refusals: string[];
}

/** An effect with its 95 % bootstrap interval and p-values (FR-23 to FR-25). */
export interface PlaceboEffect {
  estimate: number;
  ci_low: number;
  ci_high: number;
  p: number;
  /** Holm-adjusted; null on a cell without a verdict. */
  p_adjusted: number | null;
}

export interface PlaceboPairStatistics {
  quality: PlaceboEffect | null;
  tokens: PlaceboEffect | null;
  entries: number;
  quality_keys: string[];
}

/** One arm's plain means over its trials (FR-22's secondary measures). */
export interface PlaceboArmSummary {
  entries: number;
  trials: number;
  tokens: number | null;
  judge: number | null;
  latency: number | null;
  tool_calls: number | null;
  /** For a skill target: the share of trials that opened it; null when not measured. */
  skill_loaded: number | null;
}

export type PlaceboVerdict =
  | "beats_placebo"
  | "trade_off"
  | "worse_than_placebo"
  | "no_better_than_placebo"
  | "not_computable";

/** One agent x model x target of a comparison. */
export interface PlaceboComparisonRow {
  agent_id: string;
  agent_name: string;
  model_id: string;
  target: PlaceboTarget;
  target_key: string;
  entries: number;
  arms: { real: PlaceboArmSummary; placebo: PlaceboArmSummary; no_target?: PlaceboArmSummary };
  warnings: string[];
  vs_placebo: PlaceboPairStatistics;
  vs_no_target: PlaceboPairStatistics | null;
  placebo_vs_no_target: PlaceboPairStatistics | null;
  verdict: PlaceboVerdict;
  rests_on_tokens_alone: boolean;
}

/** A comparison warning: a `code`, with the agent and target of a `target_skipped` one. */
interface PlaceboComparisonWarning {
  code: string;
  agent_id?: string;
  agent_name?: string;
  target?: string;
  reason?: string;
}

export interface PlaceboComparison {
  id: string;
  evaluation_id: string;
  /** `running` until no run is pending or running; then `completed`, `failed` or `cancelled`. */
  status: string;
  seed: string;
  corpus_version: string | null;
  config: EvaluationPlaceboConfig | null;
  warnings: PlaceboComparisonWarning[];
  results: {
    method: { resamples: number; confidence: number; alpha: number; multiple_comparisons: string; quality_selection: string[] | null };
    rows: PlaceboComparisonRow[];
  } | null;
  created_by: string | null;
  created_at: string;
  completed_at: string | null;
  runs: {
    id: string;
    agent_id: string;
    model_id: string;
    arm: EvaluationArm;
    trial_index: number;
    placebo_target: PlaceboTarget | null;
    status: string;
  }[];
}

/** One run's outcome from `POST /evaluations/{id}/rescore-from-runs`.
 * Rescoring is a background job: a valid run is set to `status: "running"`, and its
 * score is known once the run's own status settles. `error` is set only on a synchronous
 * failure: `run_not_found`, `no_predictions`, `placebo_comparison_run`, or `run_in_progress` for a run already
 * pending or running. */
export interface RescoreFromRunsResult {
  run_id: string;
  status: "running" | "failed";
  error?: string;
}

export type EvaluationDetail = Evaluation;

interface EvaluationChartPoint {
  triggered_at: string;
  trigger_reason: string;
  /**
   * Keyed by "agent/model". A run with no value for the headline is absent, never 0:
   * a missing key means "not measured".
   */
  scores: Record<string, number>;
}

/** One agent/model pair on a two-headline correlation view. */
export interface EvaluationCorrelationPoint {
  series: string;
  run_id: string;
  agent_id: string | null;
  flow_id?: string | null;
  model_id: string;
  x: number;
  y: number;
}

export type HeadlineTrendKey = "quality" | "performance" | "judge";
export type CorrelationKey = "quality_performance" | "judge_performance" | "quality_judge";

/**
 * The unit the metric package declares for a performance value; quality
 * scores (0-100) carry none. Never derived from a key prefix.
 */
export type MetricUnit = "milliseconds" | "count" | "tokens" | "currency" | "ratio";

/** One selectable metric for the headline composition selector. */
export interface MetricCatalogEntry {
  key: string;
  label: string;
  kind: "quality" | "performance";
  /**
   * `null` when the package declares none. Such a performance metric is not
   * selectable: an unoriented value cannot be normalised.
   */
  direction: "higher_is_better" | "lower_is_better" | null;
  /**
   * Whether this metric composes the headline when the selection is `null`.
   * `false` only means an author must opt in: each family has a few default
   * measures of the answer, and validity checks or redundant views are opt-in.
   */
  default_contributor: boolean;
}

export interface EvaluationChartResponse {
  /** Echoed back so the UI can state the window it shows. */
  range: string;
  /** All three bounded by the same window. */
  trends: Record<HeadlineTrendKey, EvaluationChartPoint[]>;
  /** Unwindowed: a point is each pair's latest run, so a filter drops pairs. */
  correlations: Record<CorrelationKey, EvaluationCorrelationPoint[]>;
  /** Pairs dropped from each view for want of one axis. */
  correlations_omitted: Record<CorrelationKey, number>;
  /** `0` alongside a non-zero `total_runs` means "filtered out", not "never run". */
  runs_in_range: number;
  total_runs: number;
}

// --- Evaluation Pipeline v1 ---

interface RunParam {
  key: string;
  value: string | null;
  source: "auto" | "evaluation" | "manual";
}

export interface RunParamsResponse {
  run_id: string;
  params: RunParam[];
}

interface RunParamDiffEntry {
  key: string;
  value_a: string | null;
  value_b: string | null;
}

export interface RunDiff {
  run_a: string;
  run_b: string;
  changed: RunParamDiffEntry[];
  identical: RunParamDiffEntry[];
}

export interface RunMetric {
  key: string;
  value: number | null;
  // Which headline this metric composes, `null` for neither; as on `MetricAggregate`.
  // Optional so a response from an older backend still parses.
  half?: "quality" | "performance" | null;
  // The declared unit of a performance metric, `null` for a quality score and the
  // platform's own keys. Optional so a response from an older backend still parses.
  unit?: MetricUnit | null;
  // On an entry's metrics only: whether one entry's value means anything on its
  // own, and the name the package gives it there. Absent reads as shown under its own name.
  shown_on_entry?: boolean;
  entry_label?: string | null;
}

export interface MetricAggregate {
  key: string;
  // Which headline this metric belongs to, `null` for execution telemetry. Decided
  // server-side from the package registry, never from a key prefix.
  half: "quality" | "performance" | null;
  // As on `RunMetric`.
  unit?: MetricUnit | null;
  // The run's value pools the entries' counts, so it can differ from `mean`.
  // Optional so a response from an older backend still parses.
  pooled?: boolean;
  // The run's value is the macro F1 of each class summed over the run, so it
  // can differ from `mean` too.
  macro?: boolean;
  mean: number | null;
  p50: number | null;
  p95: number | null;
  min: number | null;
  max: number | null;
  count: number;
}

export interface RunMetricsAggregate {
  run_id: string;
  aggregates: MetricAggregate[];
}

type RunTags = Record<string, string>;

export interface RunTagsResponse {
  run_id: string;
  tags: RunTags;
}

// Cascading tags: agent-scoped baseline + agent tags
export interface AgentTagsResponse {
  evaluation_id: string;
  agent_id: string;
  tags: RunTags;
  is_baseline: boolean;
}

// --- Evaluation output families ---

// The list is served by `GET /evaluations/families`; this union only types the id.
export type EvaluationFamily =
  | "STIX_BUNDLE"
  | "STRUCTURED_JSON"
  | "CLASSIFICATION"
  | "PROSE";

/** The package's four-status metric contract (scored/invalid/skipped/unavailable). */
type MetricStatus = "scored" | "invalid" | "skipped" | "unavailable";

/** One metric outcome as persisted inside `execution_details.metric_results`. */
export interface PersistedMetricResult {
  status: MetricStatus;
  value: number | null;
  detail?: Record<string, unknown> | null;
  /** Present on `extraction.failed` only: bounded metadata, never payload. */
  error?: ExtractionFailure | null;
}

/** The metric key a failed prediction extraction is recorded under.
 *
 * Not a package metric, so never selectable or aggregated. An `invalid` outcome writes
 * no metric row, so the run detail reads it from `execution_details.metric_results`.
 */
export const EXTRACTION_FAILED_KEY = "extraction.failed";

/** Why an extraction could not produce a prediction. */
export interface ExtractionFailure {
  reason:
    | "source_not_present"
    | "payload_unparseable"
    | "selection_empty"
    | "type_mismatch"
    | "limit_exceeded"
    | "label_invalid"
    | "family_incompatible";
  message: string;
  agent_id?: string | null;
  operation_index?: number | null;
  operation?: string | null;
  path?: string | null;
  expected_kind?: string | null;
  actual_kind?: string | null;
}

/** The prediction an extractor produced, persisted beside the score it fed.
 *
 * Same shape as `ExtractorPreviewResult`'s success half. Stored whole; the renderer
 * bounds it.
 */
export interface ExtractedPrediction {
  prediction: string;
  output_kind: string | null;
  item_count: number | null;
}

/** One agent's declarative prediction extractor. */
export interface ExtractorSpec {
  source: { kind: "agent_output" | "tool_output" | "tool_input"; tool_name?: string };
  operations: ExtractorOperation[];
  output_kind: "object" | "array" | "label_set";
}

/** One stored trace step as the extractor preview reads it: a successful tool call. */
interface ExtractorPreviewToolStep {
  type: "tool";
  tool_name: string;
  outcome: "success";
  tool_output?: string;
  tool_input?: string;
}

/** A pasted sample to preview an extractor on (dev-docs/reference/evaluations.md §9, Output extractors). */
export interface ExtractorPreviewSample {
  agent_output?: string;
  traces?: ExtractorPreviewToolStep[];
}

export interface ExtractorOperation {
  op:
    | "find_json"
    | "parse_json"
    | "select_path"
    | "object_keys"
    | "object_values"
    | "collect_keys"
    | "flatten"
    | "dedupe_sort";
  path?: string[];
}

export interface ExtractorPreviewResult {
  status: "extracted" | "failed";
  prediction: string | null;
  output_kind: string | null;
  item_count?: number | null;
  failure?: ExtractionFailure | null;
}

/** A stored result offered as a preview source. Labels only. */
export interface ExtractorSampleResult {
  result_id: string;
  created_at: string;
  /** The entry's description, else its 0-based `position`; both null for a deleted entry. */
  description: string | null;
  position: number | null;
}

/** One field's contribution to `json.field_accuracy`.
 *
 * `score` is null for a field not compared (excluded, or absent on a side). Its weight
 * is still reported, to show an excluded field was carrying weight. */
export interface JsonFieldScore {
  pointer: string;
  mode: string;
  score: number | null;
  weight: number;
  status: "matched" | "missing" | "extra" | "excluded";
}

/** The `detail` of a `json.field_accuracy` outcome. */
export interface JsonFieldAccuracyDetail {
  per_field?: JsonFieldScore[];
  exact_object_match?: boolean;
}

/** The flat PRF payload of `stix.entity_prf` and `stix.relationship_prf`.
 *
 * `precision` / `recall` / `f1` are on a 0-100 scale; `tp` / `fp` / `fn` are counts. */
export interface StixPrfDetail {
  precision: number;
  recall: number;
  f1: number;
  tp: number;
  fp: number;
  fn: number;
  /** Missed / spurious entity ids, or `[source, type, target]` triples for the
   *  relationship metric. Carried in the payload, deliberately not rendered. */
  missing?: unknown[];
  spurious?: unknown[];
}

export interface StixPerTypeEntry {
  tp: number;
  fp: number;
  fn: number;
  f1: number;
}

/** `stix.entity_prf.by_type`. Carries no top-level `tp`/`fp`/`fn` and no
 *  missing/spurious lists: its counts exist only per STIX object type, under
 *  `classes` (`per_type` on a result scored before `classes` existed). */
export interface StixByTypePrfDetail {
  precision: number;
  recall: number;
  f1: number;
  classes: Record<string, StixPerTypeEntry>;
}

// ── Comparing evaluations of one family, and the STIX bundle review (#12282) ──

/** A pooled quality metric of the compared family: its run figure adds entry counts up. */
export interface ComparedMetric {
  key: string;
  label: string;
  direction: string | null;
}

interface ComparedSubject {
  id: string;
  kind: "agent" | "flow";
  name: string;
  is_baseline: boolean;
  builtin_tools: string[];
  custom_tool_count: number;
  mcp_server_count: number;
  skills: string[];
  max_iterations: number | null;
  temperature: number | null;
  model: string | null;
}

interface ComparedDataset {
  id: string;
  name: string;
  entry_count: number;
  instructions_digest: string | null;
  description?: string | null;
}

export interface ComparedRun {
  id: string;
  evaluation_id: string;
  subject_id: string | null;
  subject_name: string | null;
  model_id: string;
  is_default_model: boolean;
  created_at: string | null;
  /** Why it ran (`manual`, `schedule`...): runs of one minute and one trigger are one launch. */
  trigger_reason?: string | null;
  metric_suite_version: string | null;
  execution_mode: string | null;
  quality_score: number | null;
  completed_entries: number;
  total_entries: number;
  completed_at?: string | null;
  /** The judge's run score; null when the run's judge was off. */
  judge_score?: number | null;
  /** Runs with one key ran one setup (subject version, prompts, skills, settings, mode): reruns. */
  setup_key?: string | null;
  setup?: Record<string, unknown>;
  /** Why the last re-score failed, null when it did not. */
  rescore_error?: string | null;
}

export interface ComparedEvaluation {
  id: string;
  name: string;
  created_at?: string | null;
  family: EvaluationFamily | null;
  execution_mode: string | null;
  timeout_seconds: number | null;
  judge_on: boolean;
  evaluator_model: string | null;
  judge_weights: Record<string, number> | null;
  /** A fingerprint of the judge's instructions: equal digests, same instructions. */
  judge_instructions_digest: string | null;
  subjects: ComparedSubject[];
  datasets: ComparedDataset[];
  /** The runs compared: the newest completed one per subject and model, or the caller's. */
  runs: ComparedRun[];
  /** The completed runs the page offers to pick from, newest first. */
  run_choices: ComparedRun[];
}

interface ComparedEntryMember {
  evaluation_id: string;
  entry_id: string | null;
  position: number;
  has_file: boolean;
}

/** One entry, paired across evaluations when their expected answers are the same ground truth. */
export interface ComparedEntry {
  key: string;
  label: string;
  members: ComparedEntryMember[];
}

/** One run's result on one entry: each pooled metric's `[numerator, denominator]`. */
export interface ComparedCell {
  run_id: string;
  entry_key: string;
  result_id: string;
  /** Whether it counts in the run's figures (no anomaly, no judge failure). */
  counted: boolean;
  anomaly: string | null;
  metrics: Record<string, [number, number]>;
  cost: number | null;
  seconds: number | null;
  /** `[tp, fp, fn]` of the metrics that report them (counted results only). */
  counts?: Record<string, [number, number, number]>;
  /** The scorer could not read the answer (it counts as everything missing). */
  unreadable?: boolean;
  /** The judge's score of the answer; null without a verdict. */
  judge_score?: number | null;
}

/** GET /evaluations/compare */
export interface EvaluationComparison {
  family: EvaluationFamily;
  /** The metric suite the platform scores with now; a run scored by another reads
   *  differently in the bundle review until it is re-scored. */
  metric_suite_version?: string | null;
  metrics: ComparedMetric[];
  evaluations: ComparedEvaluation[];
  entries: ComparedEntry[];
  cells: ComparedCell[];
}

/** One object of a bundle review: type, name, canonical id, aliases, ATT&CK id, hashes. */
interface ReviewObject {
  t: string;
  n: string;
  c: string;
  a?: string[];
  x?: string;
  h?: Record<string, string>;
}

/** A relationship row cell: type, source object, target object, duplicates. */
type ReviewRelationshipCell = [string, number, number, number];
/** `[source group, target group, {source key: cell}]`. */
type ReviewRelationshipRow = [number, number, Record<string, ReviewRelationshipCell>];

export interface BundleReviewAnswer {
  key: string;
  result_id: string;
  run_id: string;
  evaluation_id: string;
  evaluation_name: string;
  model_id: string;
  subject_id: string | null;
  /** The answer's agent (or flow), by name. */
  subject_name: string | null;
  created_at: string | null;
  /** Null when there is no judge verdict: the judge was off, failed, or the entry was left out. */
  judge_score: number | null;
  /** The judge's reasoning (absent from a batch read without text). */
  judge_reasoning?: string | null;
  /** The suite the answer's run was scored by. */
  metric_suite_version?: string | null;
  anomaly: string | null;
  metrics: Record<string, number | null>;
  /** `[tp, fp, fn]` per metric that reports them. */
  counts?: Record<string, [number, number, number]>;
  unreadable?: boolean;
  cost?: number | null;
  seconds?: number | null;
}

/** GET /evaluations/bundle-review: an entry's expected STIX bundle against answers.
 *  Sources are `"expected"` and each answer's `key`. */
export interface BundleReview {
  /** The suite this review reads the answers with (the platform's current one). */
  metric_suite_version?: string | null;
  entry: {
    label: string;
    /** The entry's reference key, as `GET /compare` names its entries. */
    key?: string | null;
    position?: number;
    entry_count?: number | null;
    dataset_name?: string | null;
    dataset_description?: string | null;
    /** The text the agent read: its prompt, else the attached file's text layer, one string per page. */
    pages: string[];
    file_name: string | null;
    text_source: "prompt" | "file" | null;
  };
  answers: BundleReviewAnswer[];
  objects: ReviewObject[];
  /** The group of each object: an expected object and what pairs with it as a near-miss. */
  groups: number[];
  sources: Record<string, number[]>;
  relationships: ReviewRelationshipRow[];
  /** Answers the scorer could not read as a bundle. */
  unreadable: string[];
}

/** One selectable output family, from GET /evaluations/families. */
export interface FamilyInfo {
  id: EvaluationFamily;
  label: string;
  description: string;
  metric_keys: string[];
  structured_output: boolean;
  /** The output kinds an extractor may produce for this family; empty = none in this version. */
  extractor_output_kinds: ExtractorSpec["output_kind"][];
}

interface RunSearchMetricFilter {
  key: string;
  agg: "mean" | "p50" | "p95" | "min" | "max";
  op: "lt" | "lte" | "gt" | "gte" | "eq";
  value: number;
}

interface RunSearchScoreFilter {
  // Mirrors `_SCORE_FIELDS` in `backend/app/services/evaluation/run_search.py`.
  // `speed_score` and `overall_score` are retired but still stored, so a saved filter
  // on them keeps matching older runs.
  field:
    | "score"
    | "quality_score"
    | "performance_score"
    | "speed_score"
    | "overall_score"
    | "f1_score";
  op: "lt" | "lte" | "gt" | "gte" | "eq";
  value: number;
}

export interface RunSearchFilters {
  evaluation_id?: string | null;
  params?: Record<string, string> | null;
  metrics?: RunSearchMetricFilter[] | null;
  scores?: RunSearchScoreFilter[] | null;
  tags?: Record<string, string> | null;
  // Cascading tags
  baseline_only?: boolean;
  dataset_version?: number | null;
  created_after?: string | null;
  created_before?: string | null;
  limit?: number;
}

interface RunSearchResultItem {
  id: string;
  evaluation_id: string;
  agent_id: string | null;
  flow_id?: string | null;
  model_id: string;
  status: string;
  score: number | null;
  speed_score: number | null;
  overall_score: number | null;
  f1_score: number | null;
  // Headline metrics. `null` = N/A, never 0.
  quality_score: number | null;
  performance_score: number | null;
  created_at: string;
  tags: RunTags | null;
  // Cascading tags
  effective_tags?: Record<string, string>;
  is_baseline?: boolean;
  // Autonomous evaluation mode
  anomaly_count?: number;
  // Entries the platform could not run, left out of every headline.
  platform_error_count?: number;
}

export interface RunSearchResponse {
  runs: RunSearchResultItem[];
  count: number;
}

export interface CreateEvaluationRequest {
  name: string;
  /** Per-agent extractors; `null` for an agent clears its extractor. */
  agent_extractors?: Record<string, ExtractorSpec | null> | null;
  description?: string | null;
  // Output family driving package metric selection
  family?: EvaluationFamily | null;
  trigger_config?: { schedule: string | null; on_agent_change: boolean };
  use_agent_default_model?: boolean;
  model_ids?: string[];
  disabled_model_ids?: string[];
  execution_mode?: string;
  apply_prompt_guards?: boolean;
  require_tool_approval?: boolean;
  run_mode?: "parallel" | "sequential";
  evaluator_model?: string | null;
  evaluator_prompt?: string | null;
  evaluation_instructions?: string | null;
  performance_tolerance?: number;
  sub_score_weights?: Record<string, number> | null;
  quality_source?: "judge" | "f1";
  penalty_factor?: number;
  timeout_seconds?: number;
  judge_max_tokens?: number;
  enabled?: boolean;
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  agent_ids?: string[];
  dataset_ids?: string[];
  // Evaluation Pipeline v1
  custom_params?: Record<string, unknown> | null;
  field_scoring_overrides?: FieldScoringOverrides | null;
  scorer_selection?: string[] | null;
  reuse_default?: string;
  // Headline composition. Send `null` explicitly to revert to
  // inherit-all; omitting the key means "no change".
  overall_quality_metrics?: string[] | null;
  overall_performance_metrics?: string[] | null;
  // Cascading tags
  tags?: Record<string, string> | null;
  /** Placebo-controlled method: `null` removes it; left out, it is kept. */
  placebo_config?: EvaluationPlaceboConfig | null;
}

// --- Platform MCP Servers (OpenCTI / OpenAEV / OpenCRQ) ---

export interface PlatformMCPServer {
  provider: string;
  name: string;
  description: string;
  url: string;
  tool_count: number;
  connected: boolean;
  healthy: boolean;
  platform_title: string | null;
  platform_version: string | null;
}

/**
 * XTM One's own MCP server (/mcp/xtm). Not a `PlatformMCPServer`: it is not a
 * connected platform, so it has no registration, no heartbeat and no static
 * tool count — its surface is resolved per caller. It also carries its own
 * admin toggle, independent of `platform_mcp_enabled`.
 */
interface XtmMCPServer {
  enabled: boolean;
  name: string;
  description: string;
  url: string;
}

export interface PlatformMCPServers {
  /** Global admin toggle (PlatformSettings.platform_mcp_enabled). */
  enabled: boolean;
  servers: PlatformMCPServer[];
  xtm_server: XtmMCPServer;
}

// --- AI Adoption Monitoring ---

export interface PlatformSettings {
  instance_id: string;
  platform_name: string | null;
  /** Preferred base64 data URL for the platform logo. */
  platform_logo: string | null;
  /** @deprecated Legacy http(s) URL; falls back when `platform_logo` is null. */
  platform_logo_url: string | null;
  platform_description: string | null;
  /** Markdown login-screen message. Null falls back to the mode default. */
  platform_login_message: string | null;
  platform_theme_color: string | null;
  platform_accent_color: string | null;
  platform_background_color: string | null;
  platform_favicon: string | null;
  platform_og_image: string | null;
  /** Platform-wide UI language: "auto" (browser detection) or a locale code. */
  platform_language: string;
  has_db_license: boolean;
  has_env_license: boolean;
  has_env_providers: boolean;
  /**
   * Resolved, not the raw `MODELS_ENV_PROVIDING_ONLY` variable: `BYOLLM_ACTIVATED`
   * hands provider management back to administrators, and the backend serves the
   * answer the API will actually give. Gate the add / edit / delete controls on
   * this alone — never on `byollm_activated`.
   */
  models_env_providing_only: boolean;
  /**
   * The deployment told administrators to bring their own LLM: providers,
   * model catalogs and every platform default are theirs to change, and
   * `DEFAULT_*` variables only seed an empty value once. Informational — the
   * controls are already unlocked through `models_env_providing_only` and the
   * empty `env_managed_fields`.
   */
  byollm_activated: boolean;
  /**
   * The administrator's explicit choice, not the resolved state — `null` means
   * "follow the deployment's PLATFORM_MODE", which is a third value and not a
   * `false`. This is the *only* source of the resolved state until T025 puts
   * `ai_enabled` on `/platform/config`, so nothing else can gate on it yet —
   * `AiEnablementCard` computes `value ?? deploymentDefault` locally. Sending
   * `false` here where `null` was meant
   * pins the platform to disabled and hides every later change to the
   * deployment default.
   */
  ai_enabled: boolean | null;
  /** What clearing the override resolves to, so the reset can say so. */
  ai_enabled_default: boolean;
  /**
   * Platform-wide kill switch for the per-tool approval gate. When
   * false, no tool call pauses for anyone; calls are still audit-logged with
   * an explicit outcome.
   */
  tool_approvals_enabled: boolean;
  /** What an agent with no `approve_tool_calls` of its own does. */
  approve_tool_calls_default: boolean;
  /** Agent runs: longest single `wait`, in seconds (the environment winning). */
  maximum_wait_time?: number;
  /** Total seconds one run may spend in `wait`. */
  maximum_total_wait_time?: number;
  /** A wait naming the tasks or runs it waits for is not charged to the total. */
  wait_unbounded_with_until?: boolean;
  maximum_wait_time_ceiling?: number;
  maximum_total_wait_time_ceiling?: number;
  /** Wait fields pinned by AGENT_MAXIMUM_WAIT_TIME / AGENT_MAXIMUM_TOTAL_WAIT_TIME. */
  wait_env_locked_fields?: string[];
  /**
   * Company-managed presentation template a deck adopts when the agent names
   * none. `null` = the seeded brand default (Filigran, then XTM One).
   */
  default_presentation_template_id: string | null;
}

/** Agentic AI adoption on the platform (#11380: no development providers). */
export interface AiAdoptionOverview {
  period_days: number;
  total_tracked_users: number;
  total_metrics: number;
  active_user_count: number;
  prev_active_user_count: number;
  top_users: {
    user_id: string;
    name: string;
    email: string | null;
    activity_score: number;
  }[];
}

interface AiAdoptionGapSummary {
  inactive_count: number;
  low_activity_count: number;
  active_count: number;
  avg_active_days: number;
}

interface AiAdoptionTrendPoint {
  date: string;
  active_users: number;
  inactive_users: number;
  total_users: number;
  adoption_rate: number;
}

/** Agentic AI adoption gaps: one section, no development split (#11380). */
export interface AiAdoptionGaps {
  period_days: number;
  total_users: number;
  summary: AiAdoptionGapSummary;
  prev_summary: AiAdoptionGapSummary | null;
  inactive: { user_id: string; name: string; email: string; score: number }[];
  low_activity: { user_id: string; name: string; email: string; score: number }[];
  trend: AiAdoptionTrendPoint[];
  volume: { date: string; value: number }[];
  activity_threshold: number;
}

// ---------------------------------------------------------------------------
// Template containers
//
// Non-vectorized, reusable template metadata with assets stored as file
// attachments. Deliberately NOT a KnowledgeBase: nothing here is chunked or
// embedded, and lookup is structured metadata filtering, never RAG.
// ---------------------------------------------------------------------------

export interface TemplateAsset {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  /** Structured role label: "font" | "image" | "logo" | "background" | "icon" | … */
  asset_role: string | null;
  /** What the asset depicts — the one-line caption an agent picks it by. */
  description: string | null;
  /** Changes when the binary is replaced in place (the download's ETag). */
  version: string;
  /** The stored geometry of an image asset - what a logo wall crops and sizes by. */
  image_meta?: DeckAssetMeta | null;
  created_at: string;
}

export interface TemplateContainer {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  description: string | null;
  /**
   * What the template dresses: `"presentation"` (decks and pages) or
   * `"report"` — the closed choice in `TEMPLATE_KINDS`. A row created
   * before the choice closed may still carry another value (`asset-library`);
   * it is displayed under its own label and can no longer be created.
   */
  kind: string;
  tags: string[];
  /**
   * LEGACY free-text search label. It renders nothing and the UI no
   * longer writes it; it is only shown - with a way to clear it - on a
   * template that already carries one, so a saved `?theme=<label>` filter
   * keeps matching. The colour schemes a template supports are
   * `supported_schemes`.
   */
  theme: string | null;
  theme_config: Record<string, unknown>;
  aspect_ratio: string | null;
  layouts: Record<string, unknown>[];
  extra_metadata: Record<string, unknown>;
  /**
   * The colour schemes this template can dress a deck in:
   * the designer's DECLARATION in `extra_metadata.supported_schemes` when
   * present, otherwise READ off `theme_config` on every response (the
   * palette's `background` decides the scheme it is designed for, a
   * `theme_variants` palette adds the other one). Empty for a container that
   * is not a presentation template (a report dresses no deck).
   */
  supported_schemes: PresentationThemeScheme[];
  /** Bumped each time an agent retrieves the container via `search_templates`. */
  lookup_count: number;
  asset_count: number;
  is_company_managed: boolean;
  /**
   * Seeded by the platform and full-synced on every boot, so it is read-only
   * for everyone (admins included) — duplicate it to get an editable copy.
   */
  is_builtin: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`; a built-in
   *  stays read-only whatever it says. */
  can_manage?: boolean;
  created_at: string;
  updated_at: string;
}

export interface TemplateContainerDetail extends TemplateContainer {
  assets: TemplateAsset[];
}

// ---------------------------------------------------------------------------
// "Generate with AI" template authoring
//
// `POST /ai/generate-template` returns a CANDIDATE, never a saved row: the
// form applies it (create or edit), previews it through the real deck renderer,
// and can send it back as `current` for another refinement round.
// ---------------------------------------------------------------------------

export interface TemplateCandidate {
  name: string;
  description: string | null;
  kind: string;
  /** Always `null` - the generator no longer invents a style label. */
  theme: string | null;
  aspect_ratio: string | null;
  tags: string[];
  /** Already run through the renderer whitelist + value guard server-side. */
  theme_config: Record<string, string>;
  /** Layout ids validated against the catalog; `fields` re-derived server-side. */
  layouts: Record<string, unknown>[];
  extra_metadata: Record<string, unknown>;
}

export interface GenerateTemplateRequest {
  brief: string;
  kind?: string;
  /** Free-form colour hints: "navy", "#0b1020", "warm and earthy". */
  palette_hints?: string[];
  /** A template image asset whose dominant colours seed the palette. */
  logo_file_id?: string | null;
  /** Fields of the template being refined (edit mode / follow-up round). */
  current?: Partial<TemplateCandidate> | null;
  /** Name already typed in the form - kept over the model's suggestion. */
  name?: string | null;
}

export interface GenerateTemplateResponse {
  candidate: TemplateCandidate;
  /** What the validators pruned from the model's proposal, one line each. */
  warnings: string[];
  /** Hex colours read from the logo asset, most frequent first. */
  palette_from_logo: string[];
}

export interface CreateTemplateContainerRequest {
  name: string;
  description?: string | null;
  kind?: string;
  tags?: string[];
  theme?: string | null;
  theme_config?: Record<string, unknown>;
  aspect_ratio?: string | null;
  layouts?: Record<string, unknown>[];
  extra_metadata?: Record<string, unknown>;
  is_company_managed?: boolean;
  shared_group_ids?: string[];
  shared_group_permissions?: Record<string, string>;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

/** Update payload — deliberately NOT `Partial<CreateTemplateContainerRequest>`.
 *
 *  The backend distinguishes "field absent" (leave unchanged) from "field
 *  explicitly null" (clear it) via Pydantic's `model_fields_set`, so the
 *  nullable fields must accept `null` here. Typing them as `string[] | undefined`
 *  would drop the key from the JSON body and silently turn "remove all group
 *  shares" into a no-op. Mirrors `TemplateContainerUpdate` in
 *  `backend/app/api/v1/schemas/template_container.py`. */
export interface UpdateTemplateContainerRequest {
  name?: string;
  description?: string | null;
  kind?: string;
  tags?: string[];
  theme?: string | null;
  theme_config?: Record<string, unknown>;
  aspect_ratio?: string | null;
  layouts?: Record<string, unknown>[];
  extra_metadata?: Record<string, unknown>;
  is_company_managed?: boolean;
  /** Owner transfer — admin only. */
  user_id?: string;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

export interface TemplateContainerFilters {
  /** Case-insensitive substring. */
  name?: string;
  /** Exact match. */
  kind?: string;
  /** Repeatable; any-of unless `match_all_tags`. */
  tag?: string[];
  /**
   * A colour scheme (`dark` / `light`) keeps the presentation templates whose
   * `supported_schemes` include it; any other value is matched exactly
   * against the legacy free-text label.
   */
  theme?: string;
  /** Exact match. */
  aspect_ratio?: string;
  match_all_tags?: boolean;
}

// ── Presentations ─────────────────────────────────────────────────────
/** Agent-generated interactive slide decks, rendered with `@revealjs/react`.
 *
 *  These mirror the Pydantic models in
 *  `backend/app/services/presentation_spec.py`, which is the AUTHORITY on the
 *  accepted shape (the tool validates there before anything is stored). Kept in
 *  sync by hand, like `TEMPLATE_ASSET_ROLES` — no shared codegen. The renderer
 *  still narrows each slide by `layout` at runtime so a spec written by an older
 *  backend can never crash the deck. */

export type SlideLayout =
  | "title"
  | "section"
  | "agenda"
  | "content"
  | "roadmap"
  | "takeaways"
  | "dashboard"
  | "closing";

/** The deck's COLOUR SCHEME - the brand comes from the template. The
 *  backend normalises the legacy `filigran` preset to `dark` on every read, so
 *  it never reaches the frontend. */
export type PresentationTheme = "dark" | "light";

// ── v2 value types ────────────────────────────────────────────────────
/** Every framework "marker class" is exactly one typed prop; these are the value
 *  sets (mirroring the `Literal` aliases in `presentation_spec.py`). */
export type DeckTone = "accent" | "info" | "success" | "warning" | "error" | "muted";
export type DeckDensity = "normal" | "compact" | "tight";
export type DeckSize = "sm" | "md" | "lg";
export type DeckMediaSize = "sm" | "md" | "lg" | "full";
type DeckSpacerSize = "xs" | "sm" | "md" | "lg" | "xl";
export type DeckAlign = "start" | "center" | "end";
export type DeckVAlign = "start" | "center" | "end" | "stretch";
export type DeckListStyle = "bullet" | "check" | "arrow" | "cross" | "numbered" | "plain";
type DeckRatio = "equal" | "wide_left" | "wide_right";
export type DeckTrend = "up" | "down" | "flat";
/** @public */
export type DeckDeltaTone = "success" | "error" | "warning" | "muted";
/** The type size of a dashboard's tile row - mirrors `DashboardTileSize`. */
export type DeckDashboardTileSize = "sm" | "md";
export type DeckContactKind = "web" | "email" | "linkedin" | "x" | "github" | "phone";
type DeckBadgeVariant = "outline" | "solid";
type DeckBannerVariant = "gradient" | "solid" | "soft" | "outline";
/** Deck formats: `16:9` / `4:3` are slides, `A4` / `letter` portrait pages
 *  (product brief, one-pager) — mirrors `PRESENTATION_ASPECT_RATIOS`. */
export type DeckAspectRatio = "16:9" | "4:3" | "A4" | "letter";

/** Inline-markup string: `**bold**`, `_em_`, `` `code` ``, `[[tone:text]]`,
 *  `\n`. Rendered by `inline-markup.tsx` to React nodes — never as HTML. */
export type DeckMarkup = string;
/** A Lucide icon id in kebab-case (`shield-check`), validated server-side
 *  against the installed catalog. */
export type DeckIconName = string;

// ── v2 blocks ─────────────────────────────────────────────────────────────────

export interface DeckTextBlock {
  type: "text";
  content: DeckMarkup;
  size?: DeckSize;
  align?: DeckAlign;
  tone?: DeckTone;
}

export interface DeckHeadingBlock {
  type: "heading";
  content: DeckMarkup;
  level?: 2 | 3 | 4;
  icon?: DeckIconName;
  align?: DeckAlign;
  tone?: DeckTone;
}

/** The looks of a quote; `card` is the historical rendering. */
export type DeckQuoteVariant = "card" | "statement" | "pull" | "testimonial";
/** Steps of the deck type scale; `hero` is the title-slide headline. */
export type DeckQuoteSize = "md" | "lg" | "xl" | "hero";
export type DeckQuoteMarks = "none" | "inline" | "decorative";
/** A KPI tile's look: the card, or `plain` - unboxed. */
export type DeckMetricVariant = "card" | "plain";

export interface DeckQuoteBlock {
  type: "quote";
  content: DeckMarkup;
  /** Server default "card". The look fields left out follow it
   *  (`QUOTE_VARIANT_DEFAULTS` in `blocks/quote-style.ts`). */
  variant?: DeckQuoteVariant;
  size?: DeckQuoteSize;
  align?: "start" | "center";
  italic?: boolean;
  quote_marks?: DeckQuoteMarks;
  /** The credit, drawn "Author, Role, Organization"; never with `attribution`. */
  author?: DeckMarkup;
  role?: DeckMarkup;
  organization?: DeckMarkup;
  /** Legacy one-line credit. */
  attribution?: DeckMarkup;
  /** A company logo / a portrait beside the credit (template asset file ids). */
  logo_file_id?: string;
  photo_file_id?: string;
  /** 0-4 unboxed value / label pairs under the credit. */
  figures?: DeckQuoteFigure[];
}

/** A figure under a quote's credit (`QuoteFigure`): drawn as a plain tile, so a value, a label and a tone only. */
type DeckQuoteFigure = Pick<DeckMetricItem, "value" | "label" | "tone">;

/** What the deck response says a picture file is (`Presentation.picture_labels`). */
export interface DeckPictureLabel {
  description: string | null;
  filename: string | null;
}

interface DeckListItem {
  text: DeckMarkup;
  tone?: DeckTone;
  icon?: DeckIconName;
  /** A platform file (an SVG flag, a small logo) in place of the marker. */
  logo_file_id?: string | null;
  /** A 1 px hairline around the picture. */
  logo_border?: boolean;
}

export interface DeckListBlock {
  type: "list";
  items: DeckListItem[];
  style?: DeckListStyle;
  density?: DeckDensity;
  columns?: 1 | 2;
  size?: DeckSize;
  /** Spread the rows over the available height; off = packed at the top. */
  fill?: boolean;
}

export interface DeckMetricItem {
  value: string;
  label: DeckMarkup;
  sublabel?: DeckMarkup;
  icon?: DeckIconName;
  tone?: DeckTone;
  delta?: string;
  trend?: DeckTrend;
  /** Whether the change is good news: colours the delta and its arrow,
   *  which keeps pointing the way `trend` says. Unset, the trend colours it. */
  delta_tone?: DeckDeltaTone;
  /** Recent values (oldest first, 2-24 points) drawn as a small line beside
   *  the value - the tile's trend at a glance. */
  sparkline?: number[];
}

export interface DeckMetricBlock extends DeckMetricItem {
  type: "metric";
  /** Server default "card". */
  variant?: DeckMetricVariant;
  align?: "start" | "center";
}

export interface DeckMetricsGridBlock {
  type: "metrics_grid";
  items: DeckMetricItem[];
  columns?: 2 | 3 | 4;
  density?: DeckDensity;
  /** Tile type and padding on top of the density, the same on every slide; server default "md". */
  size?: DeckSize;
  /** Server default "card". */
  variant?: DeckMetricVariant;
  align?: "start" | "center";
  /** Spread the rows over the available height; off = packed at the top. */
  fill?: boolean;
}

interface DeckTableHeader {
  text: DeckMarkup;
  icon?: DeckIconName;
  align?: DeckAlign;
  /** Tints the header cell and its whole column (the edition being sold). */
  tone?: DeckTone;
}

interface DeckTableCell {
  text?: DeckMarkup;
  tone?: DeckTone;
  icon?: DeckIconName;
}

export interface DeckTableBlock {
  type: "table";
  headers: DeckTableHeader[];
  /** A `true` / `false` cell is normalised server-side to a check / cross icon cell. */
  rows: DeckTableCell[][];
  density?: DeckDensity;
  /** Header, cell and footnote type plus the cell padding; `md` is the look before the field. */
  size?: DeckSize;
  highlight_first_column?: boolean;
  footnote?: DeckMarkup;
  /** Spread the body rows over the available height; off = packed at the top. */
  fill?: boolean;
}

// ── Native charts ─────────────────────────────────────────────────────
/** Mirrors `ChartKind` in `presentation_spec.py`: `pie` / `donut` / `gauge`
 *  plot exactly one series, the rest are axis charts; a `waterfall` is an
 *  axis chart of one series of changes; a `butterfly` mirrors two
 *  series about a central spine, both read as positive sizes; a `map`
 *  colours the countries its categories name - ISO 3166-1 alpha-2 codes - by
 *  one series. */
export type DeckChartKind =
  | "bar"
  | "stacked_bar"
  | "line"
  | "area"
  | "pie"
  | "donut"
  | "gauge"
  | "combo"
  | "waterfall"
  | "butterfly"
  | "map";
/** Where a butterfly writes its category labels: in a column on its
 *  spine (`center`, unset) or on its left axis. Mirrors `ButterflySpine`. */
export type DeckButterflySpine = "center" | "left";
/** The part of the world a `map` chart is zoomed on; unset = `world`. Mirrors `MapRegion`. */
export type DeckMapRegion = "world" | "europe" | "americas" | "apac";
/** How a `map` chart colours its countries and draws its legend; unset = `ramp`. Mirrors `MapLegendStyle`. */
export type DeckMapLegendStyle = "ramp" | "classes";
/** How a `combo` series is drawn. */
export type DeckSeriesKind = "bar" | "line";
/** The value axis a series reads against: the left-hand one, or the right-hand
 *  one of a chart that plots two units. Mirrors `SeriesAxis`. */
export type DeckSeriesAxis = "primary" | "secondary";
/** How a chart writes a number: `number` = locale thousands + `unit` suffix,
 *  `currency` = the raw amount compact (`EUR 16.5M`), `percent` = the value is
 *  already a percentage (`86` -> `86%`). */
export type DeckValueFormat = "number" | "currency" | "percent";
/** How many decimals every value of an axis is written with. Mirrors `ChartDecimals`. */
type DeckChartDecimals = 0 | 1 | 2 | 3;
/** The one compact letter every currency amount of an axis is written in;
 * `auto` compacts each amount by its own size. Mirrors `ChartUnitScale`. */
type DeckChartUnitScale = "auto" | "k" | "M" | "B";
/** Where a chart's legend sits: `top` is today's placement (the panel
 *  title's row in a dashboard panel, above the plot on a slide), `bottom` one
 *  centred line under the x-axis labels, `right` a column beside the plot.
 *  Mirrors `LegendPosition`. */
export type DeckLegendPosition = "top" | "bottom" | "right";
/** How the values of the bars are fitted when they do not fit at the asked
 *  size: `auto` sets them smaller, then turns them on their side;
 *  `stagger` keeps the asked size and moves only a value that would touch
 *  another or a combo's line - a row higher above upright bars, along
 *  its row on a horizontal chart, where a too-short segment's value is written
 *  past its stack; `upright` keeps it in place - above upright bars,
 *  a row higher where that row is clear - and leaves out a value
 *  that still touches another or the line, or that its segment cannot hold.
 *  Bar, stacked bar and combo charts, upright or horizontal. Mirrors `ValueLabelFit`. */
export type DeckValueLabelFit = "auto" | "stagger" | "upright";
/** The size the values on an axis chart are tried at first: a text
 *  scale's values (`sm` = 18 down to 10), or a whole number of canvas px from
 *  10 to 32 (`VALUE_LABEL_SIZE_MIN` / `_MAX`). Mirrors `ValueLabelSize`. */
export type DeckValueLabelSize = DeckSize | number;

export interface DeckChartSeries {
  name: string;
  /** Exactly one value per category, validated server-side. */
  values: number[];
  /** Semantic colour; unset series take the template's chart palette in order. */
  tone?: DeckTone;
  /** `combo` charts only. */
  kind?: DeckSeriesKind;
  /** `bar` / `line` / `combo` charts; unset = `primary`. */
  axis?: DeckSeriesAxis;
  /**
   * Write this series' values whatever the chart's `show_values` says; unset follows it. Unstacked bar / line / area /
   * combo, and a stacked bar's segments - its stack's totals follow the chart.
   */
  show_values?: boolean;
}

/** A native chart: series over categories, drawn by `blocks/chart.tsx` in the
 *  template's palette and fonts, exported as an editable PowerPoint chart. */
export interface DeckChartBlock {
  type: "chart";
  kind: DeckChartKind;
  series: DeckChartSeries[];
  categories: string[];
  unit?: string;
  format?: DeckValueFormat;
  /** `undefined` = auto: two significant digits under 10, and never one label for two values. */
  decimals?: DeckChartDecimals;
  /** `currency` on an axis chart: every amount in this one letter; `undefined` / `auto` = each by its own size. */
  unit_scale?: DeckChartUnitScale;
  stacked?: boolean;
  horizontal?: boolean;
  show_values?: boolean;
  /** `undefined` = auto: shown when there is more than one series. */
  show_legend?: boolean;
  /** Where the legend sits; unset = `top`, today's placement. Nothing when no legend is drawn. */
  legend_position?: DeckLegendPosition;
  /** A reference line on an axis chart; the maximum of a gauge. */
  target?: number;
  goal_label?: string;
  /** `waterfall` only: the categories drawn from zero (opening, subtotals, ending). */
  totals?: number[];
  /** `waterfall` only: how far a total may miss the running sum, in % of the
   *  steps since the previous total (`DEFAULT_WATERFALL_TOLERANCE`). */
  tolerance?: number;
  /** `waterfall` only: draw a total's miss as a gap step instead of refusing it. */
  allow_gap?: boolean;
  /** `butterfly` only: where the category labels sit; unset = `center`, on the spine. */
  spine?: DeckButterflySpine;
  /** `butterfly` only: `false` ends each side at its own largest value (one scale still); unset = both to one extent. */
  symmetric?: boolean;
  /** `butterfly` only: each side's total under its legend entry. */
  show_totals?: boolean;
  /** `map` only: the part of the world drawn; unset = `world`. */
  region?: DeckMapRegion;
  /** `map` only: a continuous ramp (unset) or flat classes listed beside the plot. */
  legend_style?: DeckMapLegendStyle;
  /** `map` with `legend_style: "classes"` only: how many classes, 2-7; unset = up to 5. */
  classes?: number;
  /** `map` only: false draws no dot over a country too small to see; unset = true. */
  markers?: boolean;
  /** How the right-hand axis writes the series on `axis: "secondary"`, as `format` / `unit` do the left-hand one. */
  secondary_format?: DeckValueFormat;
  secondary_unit?: string;
  secondary_decimals?: DeckChartDecimals;
  secondary_unit_scale?: DeckChartUnitScale;
  /** A reference line read against the right-hand axis. */
  secondary_target?: number;
  /** The value axis' range, pinned; unset ends are the chart's own. It keeps zero and every value it plots. */
  y_min?: number;
  y_max?: number;
  /** The right-hand axis' range, as `y_min` / `y_max` pin the left-hand one's. */
  secondary_y_min?: number;
  secondary_y_max?: number;
  /** Drawn top-left inside the frame, as a dashboard panel's title; not drawn in a panel, which carries its own. */
  title?: DeckMarkup;
  /** A note under the plot. */
  caption?: DeckMarkup;
  size?: DeckMediaSize;
  /** How large the axis text and, unless `value_label_size` says otherwise, the values read: `sm` as on a dashboard card; unset = `md`. */
  label_size?: DeckSize;
  /** How the values of the bars are fitted; unset = `auto`, today's. Bar / stacked bar / combo, upright or horizontal. */
  value_label_fit?: DeckValueLabelFit;
  /**
   * The size the values on the figure are tried at first, the ticks and category labels staying at
   * `label_size`; unset = `label_size`'s. Bar / stacked bar / line / area / combo / waterfall / butterfly.
   */
  value_label_size?: DeckValueLabelSize;
  fill?: boolean;
  /** On a card, as a dashboard panel; `false` draws it straight on the slide. Unset = `true`. */
  framed?: boolean;
}

interface DeckBadge {
  text: DeckMarkup;
  icon?: DeckIconName;
  tone?: DeckTone;
}

export interface DeckBadgesBlock {
  type: "badges";
  items: DeckBadge[];
  variant?: DeckBadgeVariant;
  size?: DeckSize;
  align?: DeckAlign;
}

interface DeckKeyValueItem {
  label: DeckMarkup;
  value: DeckMarkup;
  icon?: DeckIconName;
  tone?: DeckTone;
}

export interface DeckKeyValueBlock {
  type: "key_value";
  items: DeckKeyValueItem[];
  columns?: 1 | 2;
  density?: DeckDensity;
  framed?: boolean;
  /** Spread the rows over the available height; off = packed at the top. */
  fill?: boolean;
}

export interface DeckPricingTier {
  name: DeckMarkup;
  price: string;
  period?: DeckMarkup;
  description?: DeckMarkup;
  features?: DeckMarkup[];
  cta?: DeckMarkup;
  badge?: DeckMarkup;
  icon?: DeckIconName;
  tone?: DeckTone;
  highlight?: boolean;
}

export interface DeckPricingTiersBlock {
  type: "pricing_tiers";
  tiers: DeckPricingTier[];
  density?: DeckDensity;
}

/** One audience card: the role, what hurts today, what changes for them. */
export interface DeckPersona {
  name: DeckMarkup;
  tagline?: DeckMarkup;
  icon?: DeckIconName;
  pains?: DeckMarkup[];
  gains?: DeckMarkup[];
  tone?: DeckTone;
}

export interface DeckPersonasBlock {
  type: "personas";
  items: DeckPersona[];
  /** Heading of every card's pains list (server default "Challenges"). */
  pains_label?: DeckMarkup;
  /** Heading of every card's gains list (server default "Outcomes"). */
  gains_label?: DeckMarkup;
  columns?: 1 | 2 | 3 | 4;
  density?: DeckDensity;
  /** Spread the rows over the available height; off = packed at the top. */
  fill?: boolean;
}

/** One fact about a person: drawn on ONE line behind a 32 px tile -
 *  a logo by file id, else a Lucide icon, else the tone dot. */
export interface DeckProfileLine {
  text: DeckMarkup;
  icon?: DeckIconName;
  logo_file_id?: string;
}

/** One person of a `profiles` block: portrait, name, title, lines. */
export interface DeckProfile {
  name: DeckMarkup;
  title?: DeckMarkup;
  /** Portrait file id (a `people_photo` template asset); the initials without one. */
  photo_file_id?: string;
  lines: DeckProfileLine[];
  tone?: DeckTone;
}

/** People on a grid - founders, speakers, a leadership team (1-12). */
export interface DeckProfilesBlock {
  type: "profiles";
  items: DeckProfile[];
  /** Omitted = a balanced grid: 5-6 people 3 per row, 9 people 3 x 3. */
  columns?: 1 | 2 | 3 | 4;
  density?: DeckDensity;
  /** Portrait diameter: sm 96 px, md 112 px, lg 128 px (server default "md"). */
  photo_size?: DeckSize;
}

export interface DeckBannerAction {
  label: DeckMarkup;
  /** `https://` or `mailto:` only — validated server-side. */
  url?: string;
  icon?: DeckIconName;
  /** Filled button. When no action says so, the first one is. */
  primary?: boolean | null;
}

export interface DeckBannerBlock {
  type: "banner";
  title: DeckMarkup;
  subtitle?: DeckMarkup;
  eyebrow?: DeckMarkup;
  icon?: DeckIconName;
  actions?: DeckBannerAction[];
  variant?: DeckBannerVariant;
  tone?: DeckTone;
  align?: DeckAlign;
  size?: DeckSize;
}

interface DeckFeatureItem {
  /** Required unless `logo_file_id` is given. */
  icon?: DeckIconName | null;
  title: DeckMarkup;
  description?: DeckMarkup;
  tone?: DeckTone;
  /** A platform file (an SVG flag, a small logo) in place of the icon tile. */
  logo_file_id?: string | null;
  /** A 1 px hairline around the picture. */
  logo_border?: boolean;
}

export interface DeckFeatureListBlock {
  type: "feature_list";
  items: DeckFeatureItem[];
  columns?: 1 | 2;
  size?: DeckSize;
  /** Spread the rows over the available height; off = packed at the top. */
  fill?: boolean;
}

interface DeckGoal {
  label: DeckMarkup;
  value: DeckMarkup;
}

export interface DeckPhase {
  icon?: DeckIconName;
  title: DeckMarkup;
  subtitle?: DeckMarkup;
  items?: DeckMarkup[];
  goal?: DeckGoal;
  tone?: DeckTone;
}

export interface DeckPhaseCardBlock extends DeckPhase {
  type: "phase_card";
  fill?: boolean;
}

export interface DeckNumberedCard {
  number?: number;
  title: DeckMarkup;
  description?: DeckMarkup;
  icon?: DeckIconName;
}

export interface DeckNumberedCardsBlock {
  type: "numbered_cards";
  items: DeckNumberedCard[];
  columns?: 1 | 2 | 3;
  tone?: DeckTone;
  /** Spread the rows over the available height; off = packed at the top. */
  fill?: boolean;
}

export interface DeckTimelineItem {
  marker?: string;
  label?: DeckMarkup;
  title: DeckMarkup;
  description?: DeckMarkup;
  tone?: DeckTone;
  icon?: DeckIconName;
  /** The step's logo, drawn under its description, centred on the node. */
  logo_file_id?: string;
  /** Server default `sm`. */
  logo_height?: DeckSize;
  /** Server default true. */
  framed?: boolean;
}

export interface DeckTimelineBlock {
  type: "timeline";
  items: DeckTimelineItem[];
  style?: "phases" | "events";
  density?: DeckDensity;
  fill?: boolean;
}

interface DeckFlowStep {
  icon?: DeckIconName;
  title: DeckMarkup;
  description?: DeckMarkup;
  tone?: DeckTone;
}

export interface DeckProcessFlowBlock {
  type: "process_flow";
  steps: DeckFlowStep[];
  direction?: "horizontal" | "vertical";
  loop?: boolean;
}

export interface DeckSectionHeaderBlock {
  type: "section_header";
  title: DeckMarkup;
  icon?: DeckIconName;
  tone?: DeckTone;
}

/** Inner padding of an image block; `none` is the historical flush look. */
type DeckPadding = "none" | "sm" | "md" | "lg";
/** The showcase chrome of an image block - mirrors `ImageFrame` in
 *  `presentation_spec.py`: `none` is the bare picture, `browser` a window bar,
 *  `browser_slim` the same window with a bar a third as tall, `device` a bezel;
 *  all on a brand-gradient glow, hugging the picture. */
export type DeckImageFrame = "none" | "browser" | "browser_slim" | "device";

export interface DeckImageBlock {
  type: "image";
  file_id: string;
  alt?: string;
  fit?: "contain" | "cover";
  size?: DeckMediaSize;
  fill?: boolean;
  align?: DeckAlign;
  padding?: DeckPadding;
  frame?: DeckImageFrame;
  caption?: DeckMarkup;
}

/** A video that plays inside the slide - mirrors `VideoBlock` in
 *  `presentation_spec.py`. `video_id` names a Video Studio video; the save
 *  fills `file_id` (its ready render), `poster_file_id` and
 *  `captions_file_id` from it. */
export interface DeckVideoBlock {
  type: "video";
  video_id?: string | null;
  file_id?: string | null;
  /** An https link: a YouTube / Vimeo video (embedded player), or an MP4 /
   *  WebM file the save imported as `file_id`. */
  url?: string | null;
  poster_file_id?: string | null;
  captions_file_id?: string | null;
  alt?: string | null;
  caption?: DeckMarkup | null;
  autoplay?: boolean;
  /** Unset: muted when it autoplays. */
  muted?: boolean | null;
  loop?: boolean;
  controls?: boolean;
  captions?: boolean;
  start?: number | null;
  end?: number | null;
  size?: DeckMediaSize;
  fill?: boolean;
  align?: DeckAlign;
  frame?: DeckImageFrame;
}

/** A problem and its answer as two stacked cards joined by an arrow:
 *  the challenge in the error tone over the answer in the success tone, one
 *  width, one left edge. `icon` leads the challenge card. */
export interface DeckChallengeAnswerBlock {
  type: "challenge_answer";
  challenge: DeckMarkup;
  answer: DeckMarkup;
  icon?: DeckIconName;
  /** Eyebrow of the top card (server default "Challenge"). */
  challenge_label?: DeckMarkup;
  /** Eyebrow of the bottom card (server default "Answer"). */
  answer_label?: DeckMarkup;
  /** The two cards share the available height. */
  fill?: boolean;
}

export interface DeckLogoItem {
  file_id: string;
  /** The logo's alternative text; drawn under it only with `show_labels`. */
  label?: DeckMarkup;
  /** Multiplies this logo's computed height (0.5-1.5): the per-logo escape hatch. */
  scale?: number;
}

/** Mirrors `LogoHeight` / `LogoTone` / `LogoAlign` in `presentation_spec.py`. */
export type DeckLogoHeight = "sm" | "md" | "lg" | "xl";
export type DeckLogoTone = "original" | "mono" | "muted" | "ink";
export type DeckLogoAlign = "start" | "center" | "even" | "stretch";

/** A grid of logos by file id (customers, partners, integrations), sized by
 *  the logo's trimmed mark - see `blocks/logos.tsx`. */
export interface DeckLogoGridBlock {
  type: "logo_grid";
  items: DeckLogoItem[];
  /** Logos per row; unset = balanced rows of at most six (or derived from `rows`). */
  columns?: 2 | 3 | 4 | 5 | 6;
  /** Alias of `logo_height` (sm / md / lg); `logo_height` wins. */
  size?: DeckSize;
  framed?: boolean;
  caption?: DeckMarkup;
  logo_height?: DeckLogoHeight;
  optical_balance?: boolean;
  /** Every logo at one visual weight (`logoWeightHeightPx`); wins over `optical_balance`. */
  optical_weight?: boolean;
  /** Canvas px of every cell, whatever it holds; unset = the cell hugs its logo. */
  tile_height?: number;
  rows?: number;
  gap?: DeckSize;
  /** Unset: `even` with `columns`, `stretch` without (`logoGridAlign`). */
  align?: DeckLogoAlign;
  fill?: boolean;
  tone?: DeckLogoTone;
  show_labels?: boolean;
}

/** One card of a `logo_wall`: a sector or category and its logos. */
interface DeckLogoWallGroup {
  title: DeckMarkup;
  icon?: DeckIconName;
  tone?: DeckTone;
  logos: DeckLogoItem[];
}

/** Logos grouped by sector / category, one card per group. */
export interface DeckLogoWallBlock {
  type: "logo_wall";
  groups: DeckLogoWallGroup[];
  /** Cards per row; unset = all of them up to three, 2 x 2 for four. */
  columns?: 1 | 2 | 3;
  /** Logos per row inside a card; unset = balanced rows. */
  per_row?: number;
  logo_height?: DeckLogoHeight;
  optical_balance?: boolean;
  /** Every logo at one visual weight (`logoWeightHeightPx`); wins over `optical_balance`. */
  optical_weight?: boolean;
  /** Canvas px of every cell across the cards. */
  tile_height?: number;
  tone?: DeckLogoTone;
  framed?: boolean;
  show_labels?: boolean;
  /** The cards share the height the slide leaves (server default true). */
  fill?: boolean;
  caption?: DeckMarkup;
}

/** Mirrors `FormulaLogo`: one logo of a term's row and its light-slide variant (a bare id from an older spec). */
export type DeckFormulaLogo = string | { file_id: string; logo_light_file_id?: string | null };

/** Mirrors `FormulaTerm` in `presentation_spec.py`: ONE of icon, logo, logos. */
interface DeckFormulaTerm {
  title: DeckMarkup;
  subtitle?: DeckMarkup;
  icon?: DeckIconName;
  logo_file_id?: string;
  /** The logo's variant for a light slide (a white wordmark's dark twin). */
  logo_light_file_id?: string;
  logos?: DeckFormulaLogo[];
  /** The bar on the card's top edge (server default `accent`): a tone or a palette colour. */
  tone?: DeckTone | DeckPaletteColor;
  /** `result` = brand gradient, white text; one term at most, usually the last. */
  variant?: "card" | "result";
  /** `false`: a wordmark logo names the term and the title stays its alt text (server default `true`). */
  show_title?: boolean;
}

/** Cards joined by operators: "A + B + C = the offer". */
export interface DeckFormulaBlock {
  type: "formula";
  terms: DeckFormulaTerm[];
  /** One per gap; unset = `+` then a final `=`. */
  operators?: ("+" | "=" | "-" | "x")[];
  /** A card's mark, title and subtitle (server default `center`). */
  align?: "start" | "center";
  fill?: boolean;
}

/** One row of a configuration card: a logo or a label and its status chip. */
interface DeckConfigRow {
  label?: DeckMarkup;
  logo_file_id?: string;
  /** The logo's variant for a light slide (a white wordmark's dark twin). */
  logo_light_file_id?: string;
  chip?: DeckMarkup;
  /** A solid chip in this tone or palette colour (server default `accent`). */
  chip_tone?: DeckTone | DeckPaletteColor;
  /** The row wears a wash of the chip's colour and the chip is its text ("Included"). */
  tinted?: boolean;
  /** `dimmed` = grey logo, dashed chip (available, not used). */
  state?: "active" | "dimmed";
}

interface DeckConfigCard {
  title: DeckMarkup;
  subtitle?: DeckMarkup;
  tone?: DeckTone | DeckPaletteColor;
  rows: DeckConfigRow[];
  /** A tinted band under the rows: its label. */
  footer?: DeckMarkup;
  /** The footer band's value, at its end. */
  footer_value?: DeckMarkup;
}

/** 2-4 configuration cards side by side. */
export interface DeckConfigCardsBlock {
  type: "config_cards";
  cards: DeckConfigCard[];
  /** A label over the cards with a rule running to the edge ("Example configurations"). */
  heading?: DeckMarkup;
  /** `outlined` (server default): active rows framed in the card tone, dimmed rows on a grey surface. */
  row_style?: "plain" | "outlined";
  fill?: boolean;
}

/** The stored geometry of a logo file (`image_geometry.py`): the size
 *  it was read at, the box of its non-transparent pixels as fractions
 *  `[x0, y0, x1, y1]`, the mean opacity inside that box, and `rev`, which
 *  names the bytes (a file replaced in place keeps its id, not its `rev`).
 *  A file with no box (unreadable, too big, not read yet) carries `rev` alone
 *  and is drawn at the size it loads at. */
export interface DeckAssetMeta {
  w?: number;
  h?: number;
  trim?: [number, number, number, number];
  ink?: number;
  rev?: string;
}

export interface DeckDiagramBlock {
  type: "diagram";
  /** Mermaid source, rendered client-side with `securityLevel: "strict"`. */
  source: string;
  caption?: DeckMarkup;
  size?: DeckMediaSize;
  fill?: boolean;
}

// ── The native architecture diagram ───────────────────────────────────
/** Mirrors the `NodeShape` / `NodeEmphasis` / `EdgeArrows` / `EdgeRoute` /
 *  `EdgeSide` / `LegendMarker` aliases in `presentation_spec.py`. */
export type DeckNodeShape = "card" | "pill" | "circle";
export type DeckNodeEmphasis = "normal" | "core" | "ghost";
export type DeckEdgeArrows = "end" | "both" | "none";
type DeckEdgeRoute = "auto" | "straight" | "orthogonal" | "curve";
export type DeckEdgeSide = "top" | "right" | "bottom" | "left";
export type DeckLegendMarker = "arrow" | "line" | "dashed" | "node";
/** A template palette colour, named by its `theme_config` key - mirrors
 *  `PaletteColor` in `presentation_spec.py`. */
export type DeckPaletteColor =
  | "brand_1"
  | "brand_2"
  | "brand_3"
  | "brand_4"
  | "chart_1"
  | "chart_2"
  | "chart_3"
  | "chart_4"
  | "chart_5"
  | "chart_6";

/** A box on the architecture grid (1-based `row` / `col`, `col_span` columns). */
export interface DeckArchitectureNode {
  id: string;
  label: DeckMarkup;
  sublabel?: DeckMarkup;
  /** Shown as a detail card when the node is hovered or focused (live deck only). */
  description?: DeckMarkup;
  icon?: DeckIconName;
  /** A platform file (a product logo asset) drawn in place of the icon. */
  logo_file_id?: string;
  /** A card's or band's logo height (sm 28 / md 40 / lg 56 / xl 72 px), cropped to its ink with optical balance; unset keeps the fixed box. */
  logo_height?: DeckLogoHeight;
  /** `false` draws a card's icon inline before the label, without the tile, so the label stays vertically centred. */
  icon_tile?: boolean;
  shape?: DeckNodeShape;
  tone?: DeckTone;
  /** Worn in place of `tone`: border, tint, icon, chips and a core's glow. */
  color?: DeckPaletteColor;
  emphasis?: DeckNodeEmphasis;
  /** `false` draws the content alone - no border, surface, shadow or glow; the box is kept for the grid and the edges meet the content. */
  frame?: boolean;
  chips?: DeckMarkup[];
  row: number;
  col: number;
  col_span?: number;
}

export interface DeckArchitectureEdge {
  /** A node id, or `zone:<id, label or index>`: the edge then meets that zone's border, nearest the node at its other end. */
  source: string;
  target: string;
  /** The side the edge leaves / enters, honoured even when it faces away from the other end. */
  source_side?: DeckEdgeSide;
  target_side?: DeckEdgeSide;
  /** 1-6: the edges of one pair of nodes on different lanes run parallel, each at one offset on both nodes. */
  lane?: number;
  label?: DeckMarkup;
  arrows?: DeckEdgeArrows;
  dashed?: boolean;
  tone?: DeckTone;
  route?: DeckEdgeRoute;
}

/** A titled panel behind a rectangle of grid cells: a label, a logo, or the logo then the label - and a row of chips. */
export interface DeckArchitectureZone {
  /** Names the zone for an edge end (`zone:<id>`), unique among the zones. */
  id?: string;
  label?: DeckMarkup;
  icon?: DeckIconName;
  /** A platform file (the platform's brand mark) drawn in the top-left corner in place of the icon. */
  logo_file_id?: string;
  tone?: DeckTone;
  /** Worn in place of `tone`: border, tint, icon and chips. */
  color?: DeckPaletteColor;
  row: number;
  col: number;
  row_span?: number;
  col_span?: number;
  /** One row of pills on the head row, never wrapped, taking no grid cell. */
  chips?: DeckMarkup[];
  /** Where the chips sit on the head row: `end` (default) the top-right corner, `start` after the logo / label. */
  chips_align?: DeckAlign;
}

export interface DeckArchitectureLegendItem {
  label: DeckMarkup;
  marker?: DeckLegendMarker;
  tone?: DeckTone;
}

/** Nodes on a grid, labeled edges and zones, drawn as inline SVG by
 *  `blocks/Architecture.tsx` from this data — never from markup. */
export interface DeckArchitectureBlock {
  type: "architecture";
  nodes: DeckArchitectureNode[];
  edges?: DeckArchitectureEdge[];
  zones?: DeckArchitectureZone[];
  legend?: DeckArchitectureLegendItem[];
  caption?: DeckMarkup;
  size?: DeckMediaSize;
  fill?: boolean;
}

// ── The native org chart ───────────────────────────────────────────────────────
/** Mirrors the `OrgNodeKind` / `OrgOrientation` / `OrgChartStyle` / `OrgCountMode` /
 *  `OrgLegend` / `OrgSpine` aliases in `presentation_spec.py`. */
export type DeckOrgNodeKind = "person" | "vacant" | "planned" | "group" | "bubble" | "external";
export type DeckOrgOrientation = "top_down" | "left_right" | "radial";
type DeckOrgChartStyle = "cards" | "compact" | "bubbles" | "hybrid";
export type DeckOrgCountMode = "current" | "target" | "both";
type DeckOrgLegend = "auto" | "none" | "custom";
type DeckOrgSpine = "auto" | "always" | "never";

/** One seat of an org chart; `manager` names the seat it reports to. */
export interface DeckOrgChartNode {
  id: string;
  kind: DeckOrgNodeKind;
  manager?: string;
  /** Secondary managers, drawn as dashed lines. */
  dotted_to?: string[];
  name?: DeckMarkup;
  role?: DeckMarkup;
  photo_file_id?: string;
  icon?: DeckIconName;
  chips?: DeckMarkup[];
  tone?: DeckTone;
  color?: DeckPaletteColor;
  count?: number;
  count_label?: string;
  period?: string;
  location?: string;
  /** Shown in the tooltip only. */
  note?: string;
  collapsed?: boolean;
  emphasis?: DeckNodeEmphasis;
}

interface DeckOrgChartLegendItem {
  label: DeckMarkup;
  kind?: DeckOrgNodeKind;
  tone?: DeckTone;
  dotted?: boolean;
}

/** A tree of seats drawn as inline SVG by `blocks/OrgChart.tsx` through the
 *  one engine of `blocks/org-chart-layout.ts` - never from markup. */
export interface DeckOrgChartBlock {
  type: "org_chart";
  nodes: DeckOrgChartNode[];
  orientation?: DeckOrgOrientation;
  style?: DeckOrgChartStyle;
  depth_limit?: number;
  collapse_below?: number;
  expand?: string[];
  show_counts?: boolean;
  count_mode?: DeckOrgCountMode;
  group_by_branch?: boolean;
  legend?: DeckOrgLegend;
  legend_items?: DeckOrgChartLegendItem[];
  roots?: string[];
  size?: DeckMediaSize;
  fill?: boolean;
  label_size?: DeckSize;
  photo_size?: DeckSize;
  show_dotted?: boolean;
  spine?: DeckOrgSpine;
  caption?: DeckMarkup;
}

export interface DeckSpacerBlock {
  type: "spacer";
  size?: DeckSpacerSize;
  grow?: boolean;
}

export interface DeckDividerBlock {
  type: "divider";
  label?: DeckMarkup;
  tone?: DeckTone;
}

/** Mirrors `PollQuestion` in `presentation_spec.py` (xtm-one #10157). */
type DeckPollQuestionKind = "choice" | "quiz" | "word_cloud" | "rating";

export interface DeckPollQuestion {
  id: string;
  kind?: DeckPollQuestionKind;
  prompt: DeckMarkup;
  options?: DeckMarkup[];
  /** Quiz only: the index of the right option. */
  correct?: number | null;
  max_words?: number;
}

/** A live audience poll: short link + QR, results drawn as votes arrive. */
export interface DeckPollBlock {
  type: "poll";
  id: string;
  questions: DeckPollQuestion[];
  show_results?: "live" | "after_lock";
  show_qr?: boolean;
  /** `all`: every question side by side with its results, the link on top. */
  layout?: "active" | "all";
  /** `none` (#10315): no presenter bar, toast or key while presenting fullscreen. */
  controls?: "hover" | "none";
  /** `live` (#10315): the quiz answer is marked from the start; `after_votes`, `after_seconds`, `never` (#10411). */
  reveal?: "manual" | "live" | "after_votes" | "after_seconds" | "never";
  /** `reveal: after_votes`: the vote count of a question that marks its answer. */
  reveal_after_votes?: number | null;
  /** `reveal: after_seconds`: seconds after a question's first vote (20 when unset). */
  reveal_after_seconds?: number | null;
}

/** One question's tally, as `services/presentation_polls.py` returns it. */
interface PollQuestionResults {
  id: string;
  kind: DeckPollQuestionKind;
  total: number;
  counts?: number[];
  average?: number | null;
  words?: { text: string; count: number }[];
}

export interface PollQuestionState {
  id: string;
  kind: DeckPollQuestionKind;
  prompt: string;
  options: string[];
  max_words: number;
  correct?: number | null;
  /** Quiz only: the answer is marked now, per the block's `reveal` (#10411). Absent from an older server. */
  revealed?: boolean;
  /** Quiz with `reveal` after_votes / after_seconds: the condition alone is met (the presenter's reveal aside). */
  condition_met?: boolean;
  results?: PollQuestionResults;
}

/** The live state of one poll (slide and audience page). */
export interface PollState {
  poll_id: string;
  code: string;
  url: string;
  /** The QR code of `url` as an SVG data URI (the slide's state only; null on the audience page). */
  qr?: string | null;
  /** `all`: every question open at once, no active question (xtm-one #10235). Absent from an older server = `active`. */
  layout?: "active" | "all";
  /** The question the presenter opened; null on an `all` poll. */
  active_question: string | null;
  locked: boolean;
  revealed: boolean;
  show_results: "live" | "after_lock";
  voters: number;
  max_voters: number;
  questions: PollQuestionState[];
}

export interface DeckPollsResponse {
  can_control: boolean;
  polls: PollState[];
}

/** Blocks that hold no other block.
 * @public
 */
export type DeckAtomicBlock =
  | DeckTextBlock
  | DeckHeadingBlock
  | DeckQuoteBlock
  | DeckListBlock
  | DeckMetricBlock
  | DeckMetricsGridBlock
  | DeckTableBlock
  | DeckChartBlock
  | DeckKeyValueBlock
  | DeckBadgesBlock
  | DeckFeatureListBlock
  | DeckPhaseCardBlock
  | DeckNumberedCardsBlock
  | DeckPricingTiersBlock
  | DeckPersonasBlock
  | DeckProfilesBlock
  | DeckTimelineBlock
  | DeckProcessFlowBlock
  | DeckSectionHeaderBlock
  | DeckBannerBlock
  | DeckChallengeAnswerBlock
  | DeckImageBlock
  | DeckVideoBlock
  | DeckLogoGridBlock
  | DeckLogoWallBlock
  | DeckFormulaBlock
  | DeckConfigCardsBlock
  | DeckDiagramBlock
  | DeckArchitectureBlock
  | DeckOrgChartBlock
  | DeckPositioningMapBlock
  | DeckPollBlock
  | DeckSpacerBlock
  | DeckDividerBlock;

// ── The native positioning map ────────────────────────────────────────
/** Mirrors `MapAxes` / `MapEmphasis` / `PositioningMapBlock` in `presentation_spec.py`. */
type DeckMapAxes = "center" | "edges";
type DeckMapEmphasis = "normal" | "highlight" | "ghost";
export interface DeckMapAxis {
  title: DeckMarkup;
  low_label?: DeckMarkup;
  high_label?: DeckMarkup;
}
export interface DeckMapQuadrant {
  label: DeckMarkup;
  tone?: DeckTone;
  color?: DeckPaletteColor;
}
export interface DeckMapItem {
  label: DeckMarkup;
  sublabel?: DeckMarkup;
  /** 0 = the x axis' low end (left), 100 = its high end (right). */
  x: number;
  /** 0 = the y axis' low end (bottom), 100 = its high end (top). */
  y: number;
  logo_file_id?: string;
  icon?: DeckIconName;
  emphasis?: DeckMapEmphasis;
  /** Why it sits there, one sentence: shown on hover, never drawn. */
  note?: string;
}
export interface DeckMapCallout {
  title: DeckMarkup;
  text?: DeckMarkup;
}
export interface DeckPositioningMapBlock {
  type: "positioning_map";
  title?: DeckMarkup;
  x_axis: DeckMapAxis;
  y_axis: DeckMapAxis;
  axes?: DeckMapAxes;
  quadrants?: {
    top_left?: DeckMapQuadrant;
    top_right?: DeckMapQuadrant;
    bottom_left?: DeckMapQuadrant;
    bottom_right?: DeckMapQuadrant;
  };
  items: DeckMapItem[];
  /** 1-4 differentiators drawn with the map (below it, or beside it). */
  callouts?: DeckMapCallout[];
  callouts_position?: "below" | "right";
  caption?: DeckMarkup;
  size?: DeckMediaSize;
  fill?: boolean;
}

/** Mirrors `CalloutVariant` / `CalloutHeader` in `presentation_spec.py`. */
type DeckCalloutVariant = "tinted" | "card" | "outline" | "plain";
type DeckCalloutHeader = "inline" | "stacked" | "none";

export interface DeckCalloutBlock {
  type: "callout";
  /** Colours the icon and the title - and the box only when `variant` is `tinted`. */
  tone?: DeckTone;
  icon?: DeckIconName;
  title?: DeckMarkup;
  subtitle?: DeckMarkup;
  content?: DeckMarkup;
  children?: DeckAtomicBlock[];
  density?: DeckDensity;
  fill?: boolean;
  /** The box (server default `tinted`, the historical look). */
  variant?: DeckCalloutVariant;
  /** Unset = the icon in a column left of the text (the historical layout). */
  header?: DeckCalloutHeader;
  /** The tone bar on the left edge; unset = on for `tinted` only. */
  accent_bar?: boolean;
  /** Sets `icon_size` and `title_size` together. */
  size?: DeckSize;
  icon_size?: DeckSize;
  title_size?: DeckSize;
  padding?: DeckSize;
  children_gap?: DeckSize;
}

export interface DeckComparisonSide {
  label: DeckMarkup;
  icon?: DeckIconName;
  tone?: DeckTone;
  blocks: DeckAtomicBlock[];
}

export interface DeckComparisonBlock {
  type: "comparison";
  left: DeckComparisonSide;
  right: DeckComparisonSide;
  equal_height?: boolean;
}

/** Any block except `columns` (what a column or a footer slot may hold). */
export type DeckLeafBlock = DeckAtomicBlock | DeckCalloutBlock | DeckComparisonBlock;

export interface DeckColumnsBlock {
  type: "columns";
  columns: DeckLeafBlock[][];
  ratio?: DeckRatio;
  equal_height?: boolean;
  /** The Nth block of every column shares one height. */
  equal_rows?: boolean;
  align?: DeckVAlign;
  gap?: DeckSize;
}

export type DeckBlock = DeckLeafBlock | DeckColumnsBlock;
export type DeckBlockType = DeckBlock["type"];

/**
 * Persistent deck furniture (logo, footer, slide numbers) — ONE deck setting,
 * drawn on every slide, never text on a slide.
 *
 * When the deck's template defines a footer / header pattern
 * (`TemplateChrome`), the bars are that pattern filled from the deck title,
 * the opening slide's subtitle, `event_label` and `fields` — the values of the
 * variables the template declares, keyed by variable key. Without a pattern the
 * footer reads `event_label` and `footer_text`, as it always did.
 */
export interface DeckChrome {
  show_logo?: boolean;
  /** Where the logo sits on the title and closing slides; body
   *  slides keep the corner. Absent reads as `auto`. */
  logo_position?: DeckLogoPosition;
  show_footer?: boolean;
  show_slide_numbers?: boolean;
  event_label?: string;
  footer_text?: string;
  /** Template variable values, `{ classification: "Confidential" }`; a value
   *  is one short line (`MAX_FIELD_VALUE_CHARS`). */
  fields?: Record<string, string>;
}

/**
 * Where the brand logo sits on the OPENING and CLOSING slides — the
 * two whose bottom row carries nothing else, so the corner is a choice there.
 * `auto` follows the slide's own composition (the title slide's `align`, the
 * closing slide's centred layout); `start` / `center` / `end` pin it. Body
 * and section slides always keep the corner: the footer bar and the slide
 * number share their row. Mirrors `LogoPosition` in
 * `backend/app/services/presentation_spec.py`; resolved per slide by
 * `resolveLogoPosition()` in `components/presentation/logo-position.ts`.
 */
export type DeckLogoPosition = "auto" | "start" | "center" | "end";

/**
 * One variable a template's footer / header pattern reads from the deck
 * settings: `{{key}}` in the pattern, `label` on the field the deck
 * editor shows, `default` when the deck sets nothing. Mirrors `ChromeVariable`
 * in `backend/app/services/presentation_chrome.py`.
 */
export interface TemplateChromeVariable {
  key: string;
  label: string;
  hint?: string;
  default?: string;
}

/**
 * The footer and header a TEMPLATE defines for every deck it dresses:
 * `extra_metadata.chrome` on the template, served resolved on every deck
 * response as `template_chrome` (like the palette — the link is live). The
 * built-in placeholders `{{title}}`, `{{subtitle}}` and `{{event}}` read the
 * deck itself; every other placeholder is one of `variables`, filled from
 * `DeckChrome.fields`. `watermark` draws the template's mark faint on every
 * body slide (`SlideWatermark`), `emblem` draws it small in the top-right
 * corner of the content and section slides (`DeckChrome`), both hidden with
 * the logo. Mirrors `TemplateChrome` on the backend.
 */
export interface TemplateChrome {
  footer: string | null;
  header: string | null;
  variables: TemplateChromeVariable[];
  /** Absent on a declaration that never turned it on. */
  watermark?: boolean;
  /** Absent on a declaration that never turned it on. */
  emblem?: boolean;
  /** The footer's right-corner pattern: may read `{{page}}` and `{{year}}`;
   *  drawn instead of the `n / total` number. */
  footer_right?: string;
  /** The emblem's height in canvas px (24 to 96). */
  emblem_size?: number;
  /** `margin` (default): in the top margin; `title`: its top on the slide title's top. */
  emblem_align?: "margin" | "title";
  /** `mark` (default): the asset's own colours; `accent`: one colour, the slide's
   *  accent on light and its foreground on dark. */
  emblem_ink?: "mark" | "accent";
}

/**
 * A template's design tokens, `extra_metadata.design` as the deck
 * response serves it (`template_design`). Every key optional; an absent key is
 * the renderer's own look. Mirrors `TemplateDesign` in
 * `backend/app/services/presentation_design.py`.
 */
export interface TemplateDesign {
  /** `false`: no drop shadow, inner shadow or text shadow on any block. */
  shadows?: boolean;
  /** `false`: no glow, halo, blur or card light. */
  glow?: boolean;
  /** `false`: no small label drawn above a title (banner, section, cover). */
  eyebrows?: boolean;
  /** `never`: strokes, bars, rules and axes in one colour (the pricing tier and
   *  a highlighted callout keep their gradient border). */
  gradient_strokes?: "allowed" | "never";
  /** Canvas px: `card`, `inner`, `chip`, `table`. */
  radius?: Record<string, number>;
  /** Canvas px on the 1920 x 1080 canvas: margins, gaps, paddings. */
  spacing?: Record<string, number>;
  /** `top`: the body is one group under the title, free height below it. */
  body_align?: "top" | "center";
  /** `false`: no bar under a slide title. */
  title_underline?: boolean;
  /** `brand`: slide titles in the foreground on dark, the accent on light. */
  title_ink?: "brand";
  /** `brand`: the brand cover and closing (logo top centre, closing logo
   *  bottom centre, emblem at 7%, Sky Blue into Royal Blue bottom right). */
  cover?: "brand";
  /** `outline`: title left, large outline number right, the body footer. */
  section?: "outline";
  /** `plain`: numbered grid without cards, 2 columns (3 from 7 items). */
  agenda?: "plain";
  /** The mark of the `cover: brand` backdrop on the cover and the closing:
   * `scale` in % of the slide height (20 to 140), `anchor`
   *  `bottom_right` (cropped by the corner) or `right` (inside the right
   *  edge), `opacity` in % (1 to 15). An absent key is the stylesheet's own. */
  cover_emblem?: TemplateCoverEmblem;
}

export interface TemplateCoverEmblem {
  scale?: number;
  anchor?: "bottom_right" | "right";
  opacity?: number;
}

export interface DeckSpeaker {
  name: DeckMarkup;
  title?: DeckMarkup;
  credentials?: DeckMarkup[];
}

/** A title slide's speaker: the closing slide's speaker plus a portrait. */
export interface DeckCoverSpeaker extends DeckSpeaker {
  /** Platform file id of the portrait (a `people_photo` asset or an upload). */
  photo_file_id?: string;
}

export interface DeckAgendaItem {
  number?: number;
  title: DeckMarkup;
  description?: DeckMarkup;
  icon?: DeckIconName;
}

interface DeckContact {
  kind: DeckContactKind;
  value: string;
  label?: DeckMarkup;
}

export type PresentationTransition =
  | "none"
  | "fade"
  | "slide"
  | "convex"
  | "concave"
  | "zoom";

/**
 * The backdrop of a title or closing slide, mirrors `Hero` in
 * `presentation_spec.py`. Every variant draws in the slide's RESOLVED scheme
 * (`resolveSlideScheme`): `plain` is the flat paper every deck had, `gradient`
 * the brand gradient washed over it with the template's mark as a faint
 * watermark, `split` a text panel next to a gradient / image panel,
 * `spotlight` a radial accent glow behind the headline with two thin arcs,
 * `mosaic` pixel blocks clustered in the corners the text leaves free, and
 * its shapes: `cascade` stepping down from the top-right corner (the logo
 * large top left, the headline low over a rule and the speaker row),
 * `horizon` a skyline along the bottom edge (the logo top left), `pillar` a
 * column down the right edge.
 */
export type DeckHero = "plain" | "gradient" | "split" | "spotlight" | "mosaic" | "cascade" | "horizon" | "pillar";

/**
 * The colour a hero is drawn in, mirrors `HeroTone`: `brand` is the
 * template's gradient (`gradient_from` -> `gradient_to`), every other value one
 * palette colour alone - named by its `theme_config` key or its tone.
 */
export type DeckHeroTone =
  | "brand"
  | "accent"
  | "gradient_from"
  | "gradient_to"
  | "info"
  | "success"
  | "warning"
  | "error"
  | "muted";

/** The scrim between a slide's background image and its content,
 *  mirrors `Overlay`: nothing, a soft wash or a strong one, in the scheme's ink. */
export type DeckOverlay = "none" | "soft" | "strong";

interface SlideCommon {
  /** Stable per-slide identifier assigned by the backend. Reorders and
   *  in-place edits keep it, so it is the React key — an index key would remount
   *  every slide after the edit point and reset reveal's position. Absent on
   *  decks stored before it existed. */
  id?: string;
  /** Speaker notes — what the presenter says over the slide. Shown in the
   *  native speaker view (S key) and the notes handout, never on the
   *  slide. Inline markup + `- ` list lines; `speaking-time.ts` measures it. */
  notes?: string;
  /** Platform file id of a full-slide background image. */
  background_image_file_id?: string;
  /** This slide's own colour scheme. Absent = inherit the deck theme;
   *  set, it always wins over the deck theme and a deck theme change leaves it
   *  alone. Resolved per slide by `resolveSlideScheme()` in `theme.ts`. */
  theme?: PresentationTheme;
  /** Scrim over `background_image_file_id` in the slide's scheme; absent = `none`. */
  overlay?: DeckOverlay;
  /** The slide's own palette inside its template; absent = the template's. */
  palette?: DeckSlidePalette;
}

/** A slide palette: mirrors `SlidePalette` in `presentation_spec.py`. */
export type DeckSlidePalette = "xtm_one";

export interface TitleSlide extends SlideCommon {
  layout: "title";
  title: DeckMarkup;
  subtitle?: DeckMarkup;
  footer?: DeckMarkup;
  // v2 additions
  event_label?: DeckMarkup;
  /** Who presents, in speaking order: 1-4. The one-speaker form a
   *  deck was stored with reads as this list (the backend's read hook). */
  speakers?: DeckCoverSpeaker[];
  icon_strip?: DeckIconName[];
  align?: "start" | "center";
  /** The cover backdrop; absent = `plain`. */
  hero?: DeckHero;
  /** The colour the hero is drawn in; absent = `brand`. */
  hero_tone?: DeckHeroTone;
  /** Draw the title's last line (after the last line break; the last word of a
   *  one-line title) in the accent tone without markup - "Uncover Threats. /
   *  Take Action.". */
  hero_accent_word?: boolean;
}

export interface SectionSlide extends SlideCommon {
  layout: "section";
  number?: number;
  label?: DeckMarkup;
  title: DeckMarkup;
  subtitle?: DeckMarkup;
  icon?: DeckIconName;
}

/** The agenda's two looks: `list` is the numbered chip in front of the
 *  title, `grid` the board-deck cell — `N. | TITLE` in accent caps with a thin
 *  separator bar and the description under it. Mirrors `AgendaStyle`. */
export type DeckAgendaStyle = "list" | "grid";
/** Fill order of the agenda grid: `row` reads 1 2 3 / 4 5 6 like the PPTX,
 *  `column` runs down the first column first. Mirrors `AgendaFlow`. */
export type DeckAgendaFlow = "row" | "column";

export interface AgendaSlide extends SlideCommon {
  layout: "agenda";
  title?: DeckMarkup;
  subtitle?: DeckMarkup;
  items: DeckAgendaItem[];
  /** Omitted = auto (`agendaColumns()`): 1 up to 4 items, 2 for 5-6 or 10,
   *  the 3-column grid for 7-9. Three columns need 6 to 9 items. */
  columns?: 1 | 2 | 3;
  /** Omitted = auto (`agendaStyle()`): `grid` at 3 columns, `list` otherwise. */
  style?: DeckAgendaStyle;
  /** Spread the rows evenly over the body height (default true); `false`
   *  packs them to the top. */
  distribute?: boolean;
  /** Fill order of the grid (default `row`). */
  flow?: DeckAgendaFlow;
  /** 1-based item highlighted when the agenda recurs between chapters. */
  current?: number;
  footer_block?: DeckLeafBlock;
}

export interface ContentSlide extends SlideCommon {
  layout: "content";
  title?: DeckMarkup;
  subtitle?: DeckMarkup;
  /** Single-flow body — exclusive with `columns`. */
  blocks?: DeckBlock[];
  /** 2-3 columns of leaf blocks — exclusive with `blocks`. */
  columns?: DeckLeafBlock[][];
  ratio?: DeckRatio;
  vertical_align?: DeckVAlign;
  /** Spread the blocks evenly over the body height (default false; the free
   *  height lands between blocks). Wins over `vertical_align` for the run. */
  distribute?: boolean;
  equal_height?: boolean;
  /** The Nth block of every column shares one height; takes over from `distribute` there. */
  equal_rows?: boolean;
  footer_block?: DeckLeafBlock;
  hide_footer?: boolean;
  /** Bento grid: 3-5 blocks as cells, the first image the large one. */
  bento?: boolean;
}

export interface RoadmapSlide extends SlideCommon {
  layout: "roadmap";
  title: DeckMarkup;
  subtitle?: DeckMarkup;
  phases: DeckPhase[];
  footer_block?: DeckLeafBlock;
  hide_footer?: boolean;
}

export interface TakeawaysSlide extends SlideCommon {
  layout: "takeaways";
  title?: DeckMarkup;
  subtitle?: DeckMarkup;
  items: DeckNumberedCard[];
  columns?: 1 | 2 | 3;
  footer_block?: DeckLeafBlock;
  hide_footer?: boolean;
}

/** What a dashboard panel may show (`PanelBlock` in `presentation_spec.py`):
 *  a figure, never prose. */
type DeckPanelBlock = DeckChartBlock | DeckTableBlock | DeckMetricBlock | DeckMetricsGridBlock | DeckOrgChartBlock;

/** Columns of a dashboard's panel grid - mirrors `DashboardPanelColumns`.
 * @public
 */
export type DeckDashboardPanelColumns = 2 | 3;

/** One cell of the dashboard's panel grid. */
export interface DeckDashboardPanel {
  title?: DeckMarkup;
  /** Columns taken on the panel grid, at most the slide's `panel_columns`; `undefined` = 1. */
  span?: 1 | 2 | 3;
  block: DeckPanelBlock;
}

/** A KPI tile row over a two-row grid of chart / table / metric panels -
 * the layout a Metabase or Board dashboard is reproduced on. */
export interface DashboardSlide extends SlideCommon {
  layout: "dashboard";
  title: DeckMarkup;
  subtitle?: DeckMarkup;
  /** 2-8 tiles across the top. */
  tiles: DeckMetricItem[];
  /** `sm` draws the step a 7- or 8-tile row takes with 16px notes, so 5-6
   *  tiles keep a long sublabel or delta on one line; absent = `md`. */
  tile_size?: DeckDashboardTileSize;
  /** 1-6 panels; the spans fit two rows of the panel grid. */
  panels: DeckDashboardPanel[];
  /** Columns of the panel grid: 2 draws two equal ones; absent = 3. */
  panel_columns?: DeckDashboardPanelColumns;
  footer_block?: DeckLeafBlock;
  hide_footer?: boolean;
}

export interface ClosingSlide extends SlideCommon {
  layout: "closing";
  title?: DeckMarkup;
  subtitle?: DeckMarkup;
  speaker?: DeckSpeaker;
  contacts?: DeckContact[];
  tagline?: DeckMarkup;
  /** The closing's composition: `start` the left layout; absent or
   *  `center` the centred closing. */
  align?: "start" | "center";
  /** The closing backdrop; absent = `plain`. */
  hero?: DeckHero;
  /** The colour the hero is drawn in; absent = `brand`. */
  hero_tone?: DeckHeroTone;
}

/**
 * Every slide a deck can hold - one member per entry in the backend's
 * `SLIDE_LAYOUTS`. The v1 layouts (`two_columns`, `comparison`, `generic`, and
 * the v1 `dashboard` SHAPE - `metrics` + `rich_text`) are retired:
 * a stored slide wearing one is upgraded to a current layout when the row is
 * read, so no response ever carries them. The `dashboard` id has a v2 model
 * of its own (`tiles` + `panels`).
 */
export type PresentationSlide =
  | TitleSlide
  | SectionSlide
  | AgendaSlide
  | ContentSlide
  | RoadmapSlide
  | TakeawaysSlide
  | DashboardSlide
  | ClosingSlide;

export interface PresentationSpec {
  title: string;
  theme: PresentationTheme;
  aspect_ratio: DeckAspectRatio;
  transition: PresentationTransition;
  /** Absent on older decks — the renderer applies the defaults. */
  chrome?: DeckChrome;
  slides: PresentationSlide[];
}

/** Brand overrides resolved from the deck's linked template container.
 *  The backend whitelists these keys and rejects CSS-escaping values; the
 *  renderer maps each one to a CSS custom property on the deck container. */
export interface PresentationThemeConfig {
  background?: string;
  foreground?: string;
  accent?: string;
  muted?: string;
  heading_font?: string;
  body_font?: string;
  // Further keys (see `THEME_CONFIG_KEYS` in `presentations.py`)
  mono_font?: string;
  surface?: string;
  line?: string;
  info?: string;
  success?: string;
  warning?: string;
  error?: string;
  /** The feedback tones as TEXT (`FEEDBACK_TEXT_THEME_CONFIG_KEYS`):
   *  what a title, list item, badge, delta or table cell drawn in the tone
   *  takes; the plain tone keeps fills, borders, bars and chart series.
   *  Absent: the plain tone. */
  success_text?: string;
  warning_text?: string;
  error_text?: string;
  gradient_from?: string;
  gradient_to?: string;
  /** The six ordered chart series colours (`CHART_THEME_CONFIG_KEYS`):
   *  `chart_1` is the primary series (the template accent on the brand deck),
   *  untoned series take them in order. Absent: the renderer derives a
   *  sequence from the accent. */
  chart_1?: string;
  chart_2?: string;
  chart_3?: string;
  chart_4?: string;
  chart_5?: string;
  chart_6?: string;
  /** The brand hues (`BRAND_COLOR_THEME_CONFIG_KEYS`): product or
   *  sub-brand colours an architecture node or zone wears through `color`.
   *  Absent: the chart series of the same rank. */
  brand_1?: string;
  brand_2?: string;
  brand_3?: string;
  brand_4?: string;
  /** Platform file id of the logo drawn by the deck chrome. */
  logo_file_id?: string;
  logo_light_file_id?: string;
  /** Platform file id of the brand MARK (the symbol without the wordmark) the
   *  `gradient` hero draws as a faint watermark; resolved from the
   *  template's `logo_mark` asset when the config names none. */
  mark_file_id?: string;
}

/**
 * A colour scheme (`THEME_SCHEMES` in `presentations.py`): `dark` or `light`.
 * The deck's theme IS its scheme - the two types coincide - and a
 * template carries one palette per scheme it supports, so the theme picker
 * only offers what the linked template can dress.
 */
export type PresentationThemeScheme = PresentationTheme;

/** The caller's own access tier on a deck, computed server-side. */
type PresentationPermission = "owner" | "config" | "read_only";

/**
 * The family a presentation belongs to, derived server-side from its
 * `aspect_ratio` (`presentation_kind` in `presentation_spec.py`): a `deck` is
 * PRESENTED — 16:9 / 4:3 slides with transitions, fullscreen and a speaker
 * view — while a `page` is READ — A4 / letter portrait pages stacked in a
 * document viewer, zoomed and printed. The frontend picks the viewer, the chat
 * card and the library badge from this, never from the aspect ratio directly
 * (`presentationKind()` in `components/presentation/geometry.ts` mirrors the
 * backend rule for specs that have not been round-tripped yet).
 */
export type PresentationKind = "deck" | "page";

/**
 * Three-tier visibility of a deck — the platform's standard sharing
 * columns plus the caller's resolved tier, so the UI never re-derives who may
 * edit what: `can_manage` is the answer.
 */
interface PresentationSharing {
  user_id: string | null;
  owner_name: string | null;
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids: string[] | null;
  shared_user_permissions: Record<string, string> | null;
  access_restricted: boolean;
  permission: PresentationPermission | null;
  can_manage: boolean;
  /**
   * A public link opens the deck for anyone, without an account. Every
   * reader is told; only a manager reads the link (`usePresentationPublicLink`).
   * `false` while an administrator has public links turned off. Absent on
   * responses cached before the field existed.
   */
  has_public_link?: boolean;
}

/**
 * A deck's public link as its managers read it -
 * `GET /presentations/{id}/public-link`. `available` is the admin switch: off,
 * a kept link (`enabled`) opens nothing and none can be created. `path` is the
 * web app page without the base path (`absoluteUrl()` adds it); both `path`
 * and `url` are `null` while the link is off, or when the platform can no
 * longer read the stored link (regenerate it).
 */
export interface PresentationPublicLink {
  available: boolean;
  enabled: boolean;
  path: string | null;
  url: string | null;
  created_at: string | null;
  created_by_name: string | null;
  /** Its visitors may also download the PDF of the slides. Off on a new link. */
  allow_pdf_download: boolean;
}

/**
 * A deck as its public link shows it -
 * `GET /public/presentations/{token}`: what the renderer needs and nothing
 * about who owns, edits or may open it. The spec carries no speaker notes.
 */
export type PublicPresentation = Pick<
  Presentation,
  | "id"
  | "title"
  | "theme"
  | "aspect_ratio"
  | "kind"
  | "slide_count"
  | "revision"
  | "spec"
  | "theme_config"
  | "theme_configs"
  | "supported_schemes"
  | "template_chrome"
  | "template_design"
  | "asset_meta"
  | "picture_labels"
  | "updated_at"
> & {
  /** The link offers the PDF of these slides: the page shows its download. */
  pdf_download?: boolean;
};

export interface Presentation extends PresentationSharing, PresentationCollaboration {
  id: string;
  title: string;
  theme: PresentationTheme;
  /** Lifted out of the spec so a list row knows its format without the spec. */
  aspect_ratio: DeckAspectRatio;
  kind: PresentationKind;
  /** Slides of a deck, pages of a page document — see `kind`. */
  slide_count: number;
  /** Bumped on every in-place edit — "same deck, new slides". */
  revision: number;
  conversation_id: string | null;
  assignment_run_id: string | null;
  template_container_id: string | null;
  spec: PresentationSpec;
  /** The template palette resolved for the SAVED theme — what a reader renders. */
  theme_config: PresentationThemeConfig;
  /**
   * The same palette per colour scheme. The renderer picks
   * `theme_configs[themeScheme(spec.theme)]` (`themeConfigFor()` in
   * `components/presentation/theme.ts`), so the editor's draft of `theme:
   * light` previews with the template's light palette before it is saved —
   * the inline palette outranks the renderer's theme tokens, so without this
   * the preview would stay dark and merely call itself light. Absent on
   * responses cached before the field existed; the renderer then falls back
   * to `theme_config`.
   */
  theme_configs?: Partial<Record<PresentationThemeScheme, PresentationThemeConfig>>;
  /**
   * The colour schemes the theme picker may offer: both for a free
   * deck, only what the linked template can dress otherwise. Canonical order,
   * never empty. Absent on responses cached before the field existed - the
   * editor then offers both.
   */
  supported_schemes?: PresentationThemeScheme[];
  /** Name of the linked template as the reader sees it; `null` for a free deck. */
  template_name?: string | null;
  /**
   * The footer / header the linked template defines, resolved on every read
   * like the palette; `null` for a free deck, a template the reader
   * cannot see, or a template that defines none. The renderer fills it from the
   * spec (`chromeText()` in `components/presentation/chrome-pattern.ts`).
   */
  template_chrome?: TemplateChrome | null;
  /**
   * The linked template's design tokens, resolved on every read like
   * the chrome; `null` or absent keeps the renderer's own look
   * (`designRootAttributes()` in `components/presentation/design-tokens.ts`).
   */
  template_design?: TemplateDesign | null;
  /**
   * What a quote picture no visible credit names is announced as: the
   * file's description or filename, keyed by file id, for the quote pictures
   * the spec references (`load_quote_picture_labels`). Read through
   * `useDeckPictureLabel()`; absent on older responses.
   */
  picture_labels?: Record<string, DeckPictureLabel>;
  /**
   * The layouts the editor's per-slide layout picker may move a slide to:
   * the linked template's advertised catalog restricted to the
   * platform's layouts, the whole catalog for a free deck or a template that
   * advertises nothing renderable. Catalog order, never empty. Absent on
   * responses cached before the field existed - the picker then offers the
   * whole catalog (`SLIDE_LAYOUTS`).
   */
  supported_layouts?: SlideLayout[];
  /**
   * How many slides the deck may hold: its template's `max_slides`
   * (`extra_metadata`), the platform default for a free deck or a template
   * that sets none. "Add slide" and Duplicate stop here (`deckSlideCap`).
   * Absent on responses cached before the field existed - the editor then
   * falls back to `DEFAULT_SLIDE_CAP`.
   */
  max_slides?: number;
  /**
   * The covers the hero picker may offer: the shared heroes, plus the
   * ones the linked template declares as its own (`extra_metadata.heroes`).
   * Catalog order. A cover in `spec` naming another hero is served on the
   * template's default for it. Absent on responses cached before the field
   * existed - the picker then offers the shared heroes (`SHARED_HEROES`).
   */
  supported_heroes?: DeckHero[];
  /**
   * The stored geometry of the logos the deck draws, keyed by the
   * file id as the spec spells it: each `logo_grid` / `logo_wall` logo is
   * cropped to its mark and sized by its trimmed ratio and ink before its
   * image loads. A logo absent here is drawn from the file's own size.
   */
  asset_meta?: Record<string, DeckAssetMeta>;
  created_at: string | null;
  updated_at: string | null;
}

/**
 * `POST /presentations/{id}/slide-layout`: ONE slide as the editor
 * panel holds it (the unsaved draft, not the stored spec) and the layout it
 * should become. Nothing is written - the converted slide comes back into the
 * draft and the save is the ordinary PATCH with its `expected_revision`.
 */
export interface SlideLayoutSwitchRequest {
  slide: PresentationSlide;
  layout: SlideLayout;
}

/**
 * The converted slide plus the account of the move, in the person's words:
 * what was carried as is, what was read from the slide's content into the new
 * shape, and what the new layout has no place for. A refusal (a title slide
 * asked to be an agenda has nothing to list) is a 422 with the reason as
 * `detail`, never a slide padded with placeholders.
 */
export interface SlideLayoutSwitchResult {
  slide: PresentationSlide;
  kept: string[];
  derived: string[];
  dropped: string[];
}

/**
 * The deck editor's unsaved draft as the stage renders it: the spec the
 * PATCH would save plus the DESIGN a template change brings with it - the
 * linked template, its palette per colour scheme and the schemes it allows -
 * so picking another template (or none) restyles every slide on screen before
 * anything is saved. Laid over the stored `Presentation` by the viewer
 * (`{ ...presentation, ...draft }`); `null` once the draft is pristine.
 */
export interface PresentationDraftOverlay {
  spec: PresentationSpec;
  title: string;
  template_container_id: string | null;
  template_name: string | null;
  theme_config: PresentationThemeConfig;
  theme_configs: Record<PresentationThemeScheme, PresentationThemeConfig>;
  supported_schemes: PresentationThemeScheme[];
  /** The tried template's footer / header pattern, so the stage draws the
   *  footer the save would produce. */
  template_chrome: TemplateChrome | null;
  /** The tried template's design tokens. */
  template_design?: TemplateDesign | null;
}

/**
 * The collaboration facts every deck response carries: who made the
 * last edit (the owner's agent leaves it `null` when the owner is the editor —
 * the library only prints a name that is not the reader's own) and how many
 * review threads are still open, for the badge on the card and the toolbar.
 */
interface PresentationCollaboration {
  updated_by_user_id: string | null;
  updated_by_name: string | null;
  open_comment_count: number;
}

export interface PresentationSummary extends PresentationSharing, PresentationCollaboration {
  id: string;
  title: string;
  theme: PresentationTheme;
  aspect_ratio: DeckAspectRatio;
  kind: PresentationKind;
  slide_count: number;
  revision: number;
  conversation_id: string | null;
  assignment_run_id: string | null;
  template_container_id: string | null;
  /** Name of the template the deck wears as the reader sees it;
   *  `null` for a free deck or a template the reader cannot see. */
  template_name?: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** `GET /presentations`: the decks the user can open, a page at a time, the last changed first. */
export interface PresentationsQuery {
  /** Only the decks generated in one conversation. */
  conversation_id?: string;
  /** Every word in the title, the template or the owner's name. */
  search?: string;
  /** The visibility segment (`lib/visibility.ts`); the server's [DEPRECATED] `visibility` alias is not sent. */
  vis?: "all" | "mine" | "shared" | "company";
  /** Only decks (landscape, presented) or only pages (portrait, read), the server's `kind`. */
  kind?: PresentationKind;
  limit: number;
  offset: number;
  /** The order of the whole match (the server's default: `updated_at`, newest first). */
  sort?: PresentationSortKey;
  direction?: "asc" | "desc";
}

/** The orders `GET /presentations?sort=` offers (`_LIST_SORT_COLUMNS`). */
export type PresentationSortKey = "updated_at" | "created_at" | "title" | "slide_count";

/**
 * Where a revision came from (`VERSION_SOURCES` in `models/presentation.py`):
 * the agent's `generate_presentation`, the manual editor panel, a restore of an
 * earlier revision, the copy a duplicate started from, or a portable package
 * imported from another install.
 */
export type PresentationVersionSource = "agent" | "manual" | "restore" | "duplicate" | "import";

/**
 * One entry of a deck's revision history. `spec` is only present on
 * the single-version read (`usePresentationVersion`) — the list carries the
 * headlines. A `restore` names the revision it brought back. A run of manual
 * saves by one person is ONE entry, an edit session: `created_at` is
 * when it began, `updated_at` its latest save (`null` for an entry saved once).
 */
export interface PresentationVersion {
  id: string;
  presentation_id: string;
  revision: number;
  title: string;
  slide_count: number;
  source: PresentationVersionSource;
  summary: string | null;
  restored_from_revision: number | null;
  author_id: string | null;
  author_name: string | null;
  created_at: string | null;
  updated_at: string | null;
  spec?: PresentationSpec;
}

/**
 * A review note on a slide, on the deck as a whole (`slide_id: null`) or a
 * reply in a thread (`parent_id` set; one level deep). `slide_position` and
 * `slide_title` are resolved against the CURRENT spec on every read, so a
 * reorder moves the number and `detached` flags a note whose slide is gone.
 * `can_manage` is the caller's right to edit / resolve / delete, computed
 * server-side (author or deck manager).
 */
export interface PresentationComment {
  id: string;
  presentation_id: string;
  slide_id: string | null;
  slide_position: number | null;
  slide_title: string | null;
  detached: boolean;
  parent_id: string | null;
  author_id: string | null;
  author_name: string | null;
  body: string;
  resolved: boolean;
  resolved_at: string | null;
  resolved_by_name: string | null;
  can_manage: boolean;
  created_at: string | null;
  updated_at: string | null;
}

/** `POST /presentations/{id}/comments` — `slide_id` is the slide's STABLE id. */
export interface PresentationCommentCreate {
  body: string;
  slide_id?: string | null;
  parent_id?: string | null;
}

/** `PATCH /presentations/{id}/comments/{cid}` — edit the text and / or resolve. */
export interface PresentationCommentUpdate {
  body?: string;
  resolved?: boolean;
}

/**
 * "Generate with AI" in the comment composer. The slide's content is
 * never sent — the server reads it from the deck; the client names the anchor
 * (a slide id, or the thread a reply answers) and says what the note should
 * convey. `current_text` is a draft already typed, improved rather than replaced.
 */
export interface PresentationCommentDraftRequest {
  brief: string;
  slide_id?: string | null;
  parent_id?: string | null;
  current_text?: string | null;
}

export interface PresentationCommentDraftResponse {
  body: string;
}

/**
 * What a viewer is doing to the deck: `viewing` (open, idle — an open
 * studio rail or editor panel with nothing pending is viewing), `editing`
 * (unsaved manual edits, a save is coming), `assistant` (an agent turn in
 * flight in one of their studio conversations — read by the server off the
 * chat turn marker, never claimed by a client).
 * @public
 */
export type PresentationPresenceActivity = "viewing" | "editing" | "assistant";

/** One person with the deck open right now (Redis-backed). */
export interface PresentationPresenceEntry {
  user_id: string;
  name: string | null;
  activity: PresentationPresenceActivity;
  /** `activity !== "viewing"` — the boolean the sort and the badge key on. */
  editing: boolean;
  slide_index: number | null;
  /** The stable id of the slide they are on, while their editor is open. */
  slide_id: string | null;
  /** The spec path of the block they have selected on it (`blocks.2`), if any. */
  block: string | null;
  last_seen_at: number;
  is_self: boolean;
}

export interface PresentationPresence {
  viewers: PresentationPresenceEntry[];
  heartbeat_seconds: number;
  ttl_seconds: number;
  /** The deck's current revision: a newer one than the cached deck is refetched. */
  revision: number;
}

/**
 * `PATCH /presentations/{id}` body. Content fields are merged into the
 * stored spec and re-validated server-side exactly like an agent's call;
 * `expected_revision` makes a content write conditional (409 when a
 * collaborator or the agent got there first). Sharing fields never bump the
 * revision. There is deliberately no raw `spec` field.
 */
export interface PresentationUpdate {
  title?: string;
  theme?: PresentationTheme;
  transition?: PresentationTransition;
  aspect_ratio?: DeckAspectRatio;
  chrome?: DeckChrome | null;
  slides?: PresentationSlide[];
  template_container_id?: string | null;
  expected_revision?: number;
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

/**
 * Server-side export formats (`EXPORT_FORMATS` in
 * `backend/app/services/presentation_export.py`): a vector PDF, a
 * pixel-perfect PPTX (one image per slide) or an editable PPTX (native shapes
 * measured from the render).
 */
export type PresentationExportFormat = "pdf" | "pptx" | "pptx_native";

/**
 * What `POST /presentations/import` did with a package
 * (`PresentationImportReport` in `api/v1/schemas/presentation.py`). `template`
 * says whether the packaged template was found here (`matched`), replaced by
 * the install's default (`default`) or absent (`none`); the asset counters
 * split references re-pointed at files this install already held from those
 * copied out of the package's embedded bytes.
 */
export interface PresentationImportReport {
  assets_total: number;
  assets_matched: number;
  assets_copied: number;
  template: "matched" | "default" | "none";
  template_name: string | null;
  warnings: string[];
}

/** The imported deck plus its report — the `POST /presentations/import` response. */
export interface PresentationImportResult extends Presentation {
  import_report: PresentationImportReport;
}

/**
 * What kind of thing the layout check saw go wrong on a slide
 * (`FindingKind` in `backend/app/services/presentation_layout.py`): the body
 * had to be shrunk to fit (`fit`), text is cut by its own clipping box
 * (`clipped`), a one-line label runs past its box (`nowrap`), an element is
 * painted past the canvas edge (`overflow`), a content body leaves its bottom
 * bare (`sparse`), logos are too small to read or for their box
 * (`logo`), a statement quote runs past four lines (`lines`),
 * a block of four columns had to wrap them 2 x 2 (`columns`), a
 * chart is drawn too short to read, 0px high included (`chart`), a
 * chart writes fewer values than its block asked for (`values`),
 * writes them turned on their side (`rotated`) or - a pie or a donut -
 * sets them under the size asked (`shrunk`), or a block that sets its
 * size had to shrink with its body (`size`). An org chart is refused
 * for type under the minimum (`org_scale`), drawn smaller because a level is
 * wider than its room (`org_width`), cuts a name or a role inside a word
 * (`org_label`), draws one manager's cards at different heights
 * (`org_siblings`), runs a line through a seat (`org_crossing`), lets a
 * bubble's label meet a seat (`org_bubble`), declares counts its reports do
 * not add up to (`org_counts`), draws many seats with none folded
 * (`org_crowded`) or a level of planned seats with no period
 * (`org_undated`); an architecture block drawn as a reporting tree reads as
 * an org chart (`org_hint`), a block whose room kept moving was frozen
 * (`unsettled`), the labels of a positioning map found no free side
 * (`map_overlap`), an architecture zone's row of chips does not
 * fit its head row (`zone_chips`), and the cards of one architecture row
 * differ in height (`arch_row`). A body or panel that asked for its room
 * (stretch, distribute, a fill table or map) stops far short of it
 * (`unfilled`). Balance, advisory: the columns of a
 * content slide end far apart (`uneven`), its one block floats under the
 * title over a bare body (`floating`), its blocks sit in the top of the room
 * with more than a third of the body bare (`top_heavy`), or a card title or
 * a name wraps to a second line (`wrapped`), a slide, panel or chart title
 * wraps (`wrap_title`) or a chart legend takes more than one row
 * (`wrap_legend`).
 */
export type PresentationLayoutFindingKind = "fit" | "clipped" | "nowrap" | "overflow" | "sparse" | "logo" | "lines" | "columns" | "chart" | "values" | "rotated" | "shrunk" | "size" | "org_scale" | "org_width" | "org_label" | "org_siblings" | "org_crossing" | "org_bubble" | "org_counts" | "org_crowded" | "org_undated" | "org_hint" | "unsettled" | "map_overlap" | "zone_chips" | "arch_row" | "uneven" | "floating" | "top_heavy" | "wrapped" | "wrap_title" | "wrap_legend" | "unfilled";

/** `dense` is a slide the audience reads with difficulty; `overloaded` one they
 *  cannot; `advisory` one that reads fine and looks unfinished (`sparse`). */
export type PresentationLayoutSeverity = "dense" | "overloaded" | "advisory";

/** One finding of the layout check, addressed to a slide (`PresentationLayoutFinding` schema). */
export interface PresentationLayoutFinding {
  slide_index: number;
  slide_number: number;
  slide_id: string | null;
  layout: string | null;
  title: string;
  kind: PresentationLayoutFindingKind;
  severity: PresentationLayoutSeverity;
  message: string;
  /** `fit`: the ratio the body was shrunk to (0.8 = needed 25% more height);
   *  `size`: the ratio the block was drawn at, below its size. */
  scale: number | null;
  /** `fit`: excess height in percent; `sparse`: the empty share of the slide
   *  in percent; `logo`: the smallest logo's height in canvas pixels when
   *  dense, the share of their box the logos cover in percent when advisory;
   *  `lines`: the statement's line count; `columns`: the width the widest
   *  column needs in canvas pixels; `chart`: the height the chart is drawn in
   *  canvas pixels; `values`: how many of the chart's values are not written;
   *  the others: the cut in canvas pixels. */
  amount: number | null;
}

/**
 * `POST /presentations/{id}/layout-check`: the deck rendered headless
 * and measured slide by slide. `skipped` names the reason when the check could
 * not run to completion (render timed out, no headless browser, the deck
 * changed while it rendered) — the findings are then empty and mean nothing.
 * `revision` is the one the render measured (`null` when the page did not say),
 * which is what the panel marks a report stale against.
 */
export interface PresentationLayoutCheck {
  slide_count: number;
  skipped: string | null;
  /** The noun the messages use, from the deck's kind. */
  unit: "slide" | "page";
  revision: number | null;
  findings: PresentationLayoutFinding[];
}

// ── Reports ───────────────────────────────────────────────────────────
/** Agent-generated executive reports, rendered as printable documents.
 *
 *  These mirror the Pydantic models in `backend/app/services/report_spec.py`,
 *  which is the AUTHORITY on the accepted shape (the tool validates there before
 *  anything is stored). Kept in sync by hand, like the presentation types above —
 *  no shared codegen. The renderer still narrows each block by `block` at runtime
 *  so a spec written by an older backend can never crash the document. */

type ReportTheme = "light" | "dark" | "filigran";

type ReportConfidence = "high" | "medium" | "low";

type ReportRiskLevel =
  | "critical"
  | "elevated"
  | "moderate"
  | "controlled";

interface ReportBlockCommon {
  /** Section heading. Absent when a block continues the previous section. */
  title?: string;
  /** Section number beside the heading ("01", "R", "A.2") — a string, because
   *  real briefs number an annex "R". Also what the derived TOC cites. */
  number?: string;
}

interface ReportVerdict {
  grade: string;
  label?: string;
  rich_text?: string;
}

interface ReportHeader {
  title: string;
  eyebrow?: string;
  subtitle?: string;
  tags?: string[];
  verdict?: ReportVerdict;
}

interface ReportKpiItem {
  value: string;
  label: string;
  note?: string;
}

interface ReportAlert {
  label?: string;
  rich_text: string;
}

interface ReportDecisionItem {
  title: string;
  rich_text?: string;
  text?: string;
  label?: string;
  confidence?: ReportConfidence;
  confidence_label?: string;
}

interface ReportChainStep {
  title: string;
  text?: string;
}

interface ReportAssessmentRow {
  statement: string;
  confidence: ReportConfidence;
  confidence_label?: string;
  /** What KIND of evidence backs the statement — free text, not an enum. */
  nature?: string;
  appraisal?: string;
}

interface ReportScenarioItem {
  title: string;
  text?: string;
  impact?: string;
}

interface ReportRiskRow {
  situation: string;
  level: ReportRiskLevel;
  posture?: string;
  level_label?: string;
}

interface ReportTimelinePhase {
  label: string;
  headline?: string;
  items?: string[];
}

interface ReportFillInField {
  label: string;
  /** Absent renders a blank rule for the reader to complete. */
  value?: string;
}

export interface ReportBlockTable {
  headers: string[];
  /** Validated backend-side to have exactly one cell per header. */
  rows: string[][];
}

interface ReportSourceItem {
  label: string;
  /** Validated backend-side to be a plain http(s) URL. */
  url?: string;
  note?: string;
  /** Citation handle ("S01") so body text can point at this entry. */
  ref?: string;
}

interface ReportMatrixCell {
  label: string;
  level: ReportRiskLevel;
}

interface ReportMatrixRow {
  label: string;
  /** Validated backend-side to have exactly one cell per column label. */
  cells: ReportMatrixCell[];
}

export interface ReportKpiRowBlock extends ReportBlockCommon {
  block: "kpi_row";
  items: ReportKpiItem[];
}

export interface ReportProseBlock extends ReportBlockCommon {
  block: "prose";
  lead?: string;
  rich_text?: string;
  alert?: ReportAlert;
}

export interface ReportDecisionsBlock extends ReportBlockCommon {
  block: "decisions";
  items: ReportDecisionItem[];
}

export interface ReportChainBlock extends ReportBlockCommon {
  block: "chain";
  steps: ReportChainStep[];
  note?: string;
}

export interface ReportAssessmentTableBlock extends ReportBlockCommon {
  block: "assessment_table";
  rows: ReportAssessmentRow[];
}

export interface ReportScenariosBlock extends ReportBlockCommon {
  block: "scenarios";
  items: ReportScenarioItem[];
}

export interface ReportRiskTableBlock extends ReportBlockCommon {
  block: "risk_table";
  rows: ReportRiskRow[];
}

export interface ReportMatrixBlock extends ReportBlockCommon {
  block: "matrix";
  axis_label?: string;
  column_labels: string[];
  rows: ReportMatrixRow[];
  note?: string;
}

export interface ReportCalloutBlock extends ReportBlockCommon {
  block: "callout";
  rich_text?: string;
  score?: string;
  score_label?: string;
}

export interface ReportTimelineBlock extends ReportBlockCommon {
  block: "timeline";
  phases: ReportTimelinePhase[];
}

export interface ReportChecklistBlock extends ReportBlockCommon {
  block: "checklist";
  items: string[];
  columns?: 1 | 2;
}

export interface ReportFillInBoxBlock extends ReportBlockCommon {
  block: "fill_in_box";
  fields: ReportFillInField[];
  notes?: string[];
}

export interface ReportAnnexBlock extends ReportBlockCommon {
  block: "annex";
  summary: string;
  items?: string[];
  rich_text?: string;
  table?: ReportBlockTable;
  note?: string;
  open?: boolean;
}

export interface ReportSourcesBlock extends ReportBlockCommon {
  block: "sources";
  intro?: string;
  items: ReportSourceItem[];
  limits?: string;
  open?: boolean;
}

export type ReportBlock =
  | ReportKpiRowBlock
  | ReportProseBlock
  | ReportDecisionsBlock
  | ReportChainBlock
  | ReportAssessmentTableBlock
  | ReportScenariosBlock
  | ReportRiskTableBlock
  | ReportMatrixBlock
  | ReportCalloutBlock
  | ReportTimelineBlock
  | ReportChecklistBlock
  | ReportFillInBoxBlock
  | ReportAnnexBlock
  | ReportSourcesBlock;

interface ReportSpec {
  title: string;
  theme: ReportTheme;
  header: ReportHeader;
  blocks: ReportBlock[];
  footer?: string;
}

/** Brand overrides resolved from the report's linked template container.
 *  The deck's six keys plus the document surfaces and semantic risk colours a
 *  report needs. The backend whitelists these and rejects CSS-escaping values;
 *  the renderer maps each one to a CSS custom property on the report container. */
export interface ReportThemeConfig {
  background?: string;
  foreground?: string;
  accent?: string;
  muted?: string;
  heading_font?: string;
  body_font?: string;
  paper?: string;
  card?: string;
  line?: string;
  critical?: string;
  elevated?: string;
  moderate?: string;
  controlled?: string;
}

export interface Report {
  id: string;
  title: string;
  theme: ReportTheme;
  block_count: number;
  /** Bumped on every in-place edit — "same report, new blocks". */
  revision: number;
  conversation_id: string | null;
  assignment_run_id: string | null;
  template_container_id: string | null;
  spec: ReportSpec;
  theme_config: ReportThemeConfig;
  created_at: string | null;
  updated_at: string | null;
}


// ---------------------------------------------------------------------------
// Custom dashboards — pages of widgets an agent builds for its user
// ---------------------------------------------------------------------------

/** A dashboard in a list: everything but its widgets. */
export interface CustomDashboardSummary {
  id: string;
  title: string;
  widget_count: number;
  /** Bumped on every write; an edit names the revision it read. */
  revision: number;
  /** The conversation it was built in, if any. */
  conversation_id: string | null;
  /** The assignment run that last refreshed it, if any. */
  assignment_run_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** The owner (null once the account is deleted). */
  user_id: string | null;
  is_company_managed: boolean;
  /** Shared with people or groups. */
  is_shared: boolean;
  /** The caller's tier: `owner` / `manage` / `config` edit (`owner` and `manage` also delete), `read_only` reads. */
  permission: "owner" | "manage" | "config" | "read_only";
  /** What it is filed under (lower-cased). */
  tags: string[];
}

/** A dashboard with its stored spec. The spec is read through
 *  `normalizeDashboard` (`components/custom-dashboard/spec.ts`), never as is. */
export interface CustomDashboard extends CustomDashboardSummary {
  /** The definition (version 2): data sources, visualizations, inputs, tabs and their layouts. */
  spec: unknown;
  /** Its visualizations as widgets, in reading order: what a reader of widgets draws. */
  widgets: Record<string, unknown>[];
  /** Whether the caller may rearrange, refine and share it (owner, `config`
   *  grantee, an admin on a company dashboard). Deleting is the owner's. */
  can_manage: boolean;
  /** Whether the caller may show it on their /dashboard (read as themselves). */
  can_use_as_home: boolean;
  /** Who it is shared with: filled for those who may change it. */
  shared_user_ids: string[];
  shared_user_permissions: Record<string, string>;
  shared_group_ids: string[];
  shared_group_permissions: Record<string, string>;
  access_restricted: boolean;
}

/** One kept version of a dashboard (`spec` when one version is read). */
export interface CustomDashboardVersion {
  revision: number;
  title: string;
  widget_count: number;
  source: "agent" | "manual" | "restore" | "import";
  summary: string | null;
  restored_from_revision: number | null;
  author_id: string | null;
  author_name?: string | null;
  created_at: string | null;
  spec?: unknown;
}

/** `PUT /custom-dashboards/{id}/sharing`: the sharing fields (unset = kept). */
export interface CustomDashboardSharingUpdate {
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  access_restricted?: boolean;
}

/** One human edit of a dashboard: remove a widget, move it after another
 *  (`"start"`: to the top), or upsert one from the studio's inspector. */
export interface CustomDashboardOperation {
  op: "remove" | "move" | "upsert" | "layout" | "set" | "delete";
  /** The widget a remove or a move names. */
  id?: string;
  after?: string;
  /** The whole stored widget an upsert writes (validated server-side like an agent's). */
  widget?: Record<string, unknown>;
  /** A layout: the widgets' new places on the 12-column grid (a drag or a resize). */
  items?: { id: string; x: number; y: number; w: number; h: number }[];
  /** The layout (tab) a layout or an upsert places in; the first one when omitted. */
  layoutId?: string;
  /** A set or a delete: the element it writes, e.g. `visualizations.kev`, `defaults`, `title`. */
  path?: string;
  /** A set: the element's whole new value, validated with the whole definition. */
  value?: unknown;
}

/** A file uploaded to a dashboard for its canvases (`POST /custom-dashboards/{id}/assets`). */
export interface CustomDashboardAsset {
  id: string;
  filename: string;
  content_type: string;
  size: number;
}

export interface CustomDashboardUpdate {
  revision: number;
  title?: string;
  subtitle?: string;
  operations?: CustomDashboardOperation[];
}

/** A dashboard widget pack: ready-made widgets, built-in or imported. */
export interface WidgetPack {
  id: string;
  name: string;
  description: string | null;
  tags: string[];
  origin: "upload" | "opencti" | "openaev" | "dashboard" | "builtin";
  /** The platforms its live widgets read. */
  sources: ("opencti" | "openaev" | "opencrq")[];
  widget_count: number;
  builtin: boolean;
  is_company_managed: boolean;
  user_id: string | null;
  can_manage: boolean;
  /** What the import changed or left out. */
  notes: string[];
  created_at: string | null;
  updated_at: string | null;
  /** Its widgets (one pack read): platform ones carry no `integration_id`. */
  widgets?: Record<string, unknown>[];
  /** The entity inputs its widgets read (an OpenCTI custom view's "current entity"), unbound. */
  inputs?: Record<string, unknown>[];
}

/** An integration a pack's live widgets can read through. */
export interface PackIntegration {
  id: string;
  provider: "opencti" | "openaev" | "opencrq" | "xtm_hub";
  name: string;
}

/** The time ranges a viewer may draw a live platform widget over. */
export type WidgetDataRange = "1d" | "7d" | "30d" | "1m" | "90d" | "3m" | "6m" | "1y";

/** Why a data source has no data (`dashboard_live.ERROR_CODES`, `dashboard_sources.ERROR_CODES`). */
export type WidgetDataErrorCode =
  | "integration_unavailable"
  | "access_denied"
  | "timeout"
  | "upstream_error"
  | "unsupported"
  | "rate_limited"
  | "tenant_required"
  | "no_result"
  | "not_run"
  | "running"
  | "invalid_result"
  | "waiting";

/**
 * What a module's widget draws besides its values, in the platform's vocabulary
 * (`dashboard_live.extras`): a colour (a strict hex or a named colour,
 * `charts/colors.resolveColor`), an entity type, a link built by the server,
 * an icon (a Lucide name).
 */
interface WidgetItemExtras {
  color?: string;
  entityType?: string;
  link?: string;
  icon?: string;
}
/** A Lucide icon name and its named colour. */
interface WidgetIcon {
  name: string;
  color?: string;
}
export interface WidgetNumberData {
  value: number;
  change: number | null;
  /** The value `change` is measured from, and over which period (a day before, the interval before). */
  previous?: number;
  period?: "day" | "interval";
  icon?: WidgetIcon;
  /** What it counts, and over what span its change is measured, when its module says it beside the change. */
  subject?: "endpoint" | "expectation-inject" | "finding" | "inject" | "scenario" | "simulation" | "vulnerable-endpoint";
  span?: "1d" | "7d" | "1m" | "3m" | "6m" | "1y" | "all" | "custom";
}
export interface WidgetSeriesData {
  /** A point's `key` is what a click on it filters on. */
  series: { name: string; color?: string; points: { x: string; y: number; key?: string }[] }[];
}
export interface WidgetDistributionData {
  /** An item's `key` is what a click on it filters on. */
  items: ({ label: string; value: number; key?: string } & WidgetItemExtras)[];
}
/** How a rows column draws its cells (`dashboard_live.CELL_KINDS`); absent: text. */
export type WidgetCellKind = "number" | "date" | "datetime" | "entityType" | "chip" | "chips";
export interface WidgetChip {
  label: string;
  color?: string;
  icon?: string;
}
/** A cell: text, a number, an ISO moment or an entity type (`string`), a chip or chips; `null` when empty. */
export type WidgetCell = string | number | WidgetChip | WidgetChip[] | null;
export interface WidgetRowsData {
  columns: { key: string; label: string; kind?: WidgetCellKind }[];
  /** Cells by column key; `_url`, when present, is a link into the platform built by the server,
   *  `_entityType` and `_icon` (a Lucide name) what the row is, `_color` what its entity is drawn in. */
  rows: (Record<string, WidgetCell> & { _url?: string; _entityType?: string; _icon?: string; _color?: string })[];
  total: number | null;
}
/** An answer in words: an agent's, an assignment run's, a variable's. */
interface WidgetTextData {
  text: string;
}

/** Points on the world and values by region (an ISO 3166-1 alpha-2 code, or an uploaded SVG's element id). */
export interface WidgetGeoData {
  points: { label: string; lat: number; lon: number; value: number | null; group: string | null }[];
  /** `level` (0-9) ranks a region among the others, as a threat map colours it. */
  regions: { code: string; label: string; value: number; level?: number }[];
}
/** Nodes and the edges between them; every edge's ends are nodes. */
export interface WidgetGraphData {
  nodes: { id: string; label: string; group: string | null; value: number | null }[];
  edges: { source: string; target: string; label: string | null; value: number | null }[];
}
/** Events in time, by start: an ISO date (`2026-01-31`) or UTC date-time (`2026-01-31T10:00:00Z`). */
export interface WidgetEventsData {
  events: ({ label: string; start: string; end: string | null; lane: string | null; value: number | null; secondary?: string } & WidgetItemExtras)[];
}
/** Values on a hierarchy of at most three levels. */
export interface WidgetTreeData {
  items: { path: string[]; value: number }[];
}
/** Validation outcomes by label (a technique, a security domain, an expectation type), each with its parts. */
export interface WidgetOutcomeCounts {
  label: string;
  success: number;
  failed: number;
  pending: number;
}
/** What a count or a status row is: a check that succeeded, failed or waits. */
export type WidgetOutcomeStatus = "success" | "failed" | "pending";
/** A part's row by status (a domain's expectation type: "Prevented", "Not Prevented"), its `value` a share of the part's `total`. */
interface WidgetOutcomeRow {
  label: string;
  value: number;
  status: WidgetOutcomeStatus;
  key?: string;
}
export interface WidgetOutcomePart extends WidgetOutcomeCounts {
  /** A Lucide icon name. */
  icon?: string;
  /** What a click on it filters on. */
  key?: string;
  /** The whole its rows are shares of. */
  total?: number;
  rows?: WidgetOutcomeRow[];
}
export interface WidgetOutcome extends WidgetOutcomeCounts {
  parts: WidgetOutcomePart[];
  /** The heading it sits under (a technique's tactic), in the order groups come. */
  group?: string;
  /** The outcome, and the ids of those it leads to (an attack path's next techniques). */
  id?: string;
  links?: string[];
  /** A short code (a technique's external id). */
  code?: string;
  icon?: string;
  /** Strict hex or a named colour. */
  color?: string;
  /** What a click on it filters on. */
  key?: string;
  /** A link the server built. */
  link?: string;
  /** What a success is called for it ("prevented"). */
  successLabel?: string;
  /** The ids of the chain groups it sits under. */
  phases?: string[];
  /** The ids rolled up into it (a technique's sub-techniques). */
  subIds?: string[];
}
/** A named sequence of groups (a kill chain and its phases, in order). */
interface WidgetOutcomeChain {
  id: string;
  label: string;
  /** The chain shown first. */
  default?: boolean;
  groups: { id: string; label: string }[];
}
/** A label the widget lists whether or not it has outcomes (every technique of a chain, every security domain). */
export interface WidgetOutcomeEntry {
  id: string;
  label: string;
  code?: string;
  icon?: string;
  phases?: string[];
  subIds?: string[];
  /** The parts shown while it has no outcome. */
  parts?: { label: string; icon?: string }[];
}
export interface WidgetOutcomesData {
  items: WidgetOutcome[];
  chains?: WidgetOutcomeChain[];
  reference?: WidgetOutcomeEntry[];
  /** When the outcomes ran (ISO moments). */
  timeline?: { start: string | null; end: string | null };
  /** The links offered beside the outcomes. */
  actions?: { label: string; caption?: string; icon?: string; color?: string; link: string }[];
  /** What circles the score: the tools that answer the checks. */
  orbit?: { label: string; type?: string; color?: string }[];
  /** The widget cannot be drawn until it is told which run it reads. */
  needsScope?: boolean;
}

/** The data of the charts, values and tables (`dashboard_live`'s first four shapes). */
export type WidgetChartData =
  | { shape: "number"; data: WidgetNumberData }
  | { shape: "series"; data: WidgetSeriesData }
  | { shape: "distribution"; data: WidgetDistributionData }
  | { shape: "rows"; data: WidgetRowsData }
  | { shape: "text"; data: WidgetTextData }
  | { shape: "outcomes"; data: WidgetOutcomesData };

/** The data of maps, graphs and flows. */
export type WidgetStudioData =
  | { shape: "geo"; data: WidgetGeoData }
  | { shape: "graph"; data: WidgetGraphData }
  | { shape: "events"; data: WidgetEventsData }
  | { shape: "tree"; data: WidgetTreeData };

/** How a widget's module draws it: options laid under the visualization's own when it is drawn as `view`. */
interface WidgetPresentation {
  view: string;
  options: Record<string, unknown>;
}

/** A live platform widget's data, as the viewer may read it, or the reason it has none. */
export type WidgetData =
  | ({ status: "ok"; fetched_at: string; open_url: string | null; presentation?: WidgetPresentation | null } & (WidgetChartData | WidgetStudioData))
  | { status: "error"; code: WidgetDataErrorCode; open_url: string | null };

// ---------------------------------------------------------------------------
// Platform widget editors: one form, described by each platform's adapter
// ---------------------------------------------------------------------------
//
// `GET /custom-dashboards/platform-forms` (`dashboard_platform/forms.py`) says
// how each platform's own widget editor builds a widget; the studio draws it
// with one generic form (`components/custom-dashboard/platform-form.ts`) that
// knows no platform. An expression is JSON: an object whose one key starts
// with `$` is an operation, anything else a literal; a condition is an object
// keyed by its operator. Labels are i18n keys.

/** A value computed from the configuration, the type, a parameter... (`platform-form.ts` `evaluate`). */
export type PlatformFormExpr = unknown;
/** A condition (`all`, `any`, `not`, `eq`, `ne`, `in`, `gt`, `gte`, `lt`, `truthy`, `absent`, `isArray`, `isString`, `endsWith`). */
export type PlatformFormCond = Record<string, unknown>;

/** Which filter of a group a control is about. */
export interface PlatformFormMatch {
  key: PlatformFormExpr;
  operator?: string;
  /** The key is a list of that key only (or that very string). */
  exact?: boolean;
}

/** An effect a change runs after writing its value. */
export type PlatformFormEffect =
  | { set: string; value: PlatformFormExpr }
  | { remove: string }
  | { truncate: string; max: PlatformFormExpr }
  | { each: string; when?: PlatformFormCond; do: PlatformFormEffect[] }
  | { if: PlatformFormCond; then?: PlatformFormEffect[]; else?: PlatformFormEffect[] }
  | { change: string; value: PlatformFormExpr }
  | { rebuild: Record<string, PlatformFormExpr>; carry?: string[] };

/** A schema question a scope asks (`POST /custom-dashboards/platform-schema` kinds). */
export interface PlatformFormSource {
  kind: "entity_types" | "filter_keys" | "attributes" | "saved_filters";
  perspective?: PlatformFormExpr;
  types?: PlatformFormExpr;
  when?: PlatformFormCond;
}

/** One control; which keys it reads depends on its `kind`. */
export interface PlatformFormField {
  kind:
    | "select" | "multiselect" | "segmented" | "text" | "number" | "switch" | "day" | "values"
    | "filters" | "columns" | "sorts" | "row" | "list" | "section" | "compose" | "note";
  id?: string;
  label?: PlatformFormExpr;
  when?: PlatformFormCond;
  hint?: PlatformFormExpr | { text: PlatformFormExpr; when?: PlatformFormCond };
  path?: string;
  bind?:
    | { filterValues: { group: string; match: PlatformFormMatch; new?: PlatformFormExpr; single?: boolean; clearOthers?: boolean } }
    | { param: string };
  onChange?: PlatformFormEffect[];
  default?: PlatformFormExpr;
  options?: PlatformFormExpr;
  sort?: boolean;
  /** A select drawn as a list, or as tiles (each option's `icon`, a Lucide name). */
  display?: "list" | "tiles";
  placeholder?: PlatformFormExpr;
  emptyLabel?: PlatformFormExpr;
  ariaLabel?: PlatformFormExpr;
  /** Text: the longest; number: the bounds. */
  max?: PlatformFormExpr;
  min?: PlatformFormExpr;
  /** What emptying a text or number writes. */
  empty?: "keep" | "remove" | "null" | "default" | "min";
  decimal?: boolean;
  invert?: boolean;
  filterKey?: string;
  single?: boolean;
  keys?: PlatformFormExpr;
  hidden?: PlatformFormMatch[];
  perspective?: PlatformFormExpr;
  items?: "objects" | "strings";
  valueKey?: string;
  labelKey?: string;
  available?: PlatformFormExpr;
  names?: PlatformFormExpr;
  reset?: PlatformFormExpr;
  choices?: PlatformFormExpr;
  directions?: PlatformFormExpr;
  new?: PlatformFormExpr;
  fieldKey?: string;
  directionKey?: string;
  columns?: number;
  title?: PlatformFormExpr;
  removeLabel?: PlatformFormExpr;
  count?: PlatformFormExpr;
  footerWhen?: PlatformFormCond;
  badge?: { text: PlatformFormExpr; when?: PlatformFormCond };
  add?: { label: PlatformFormExpr; item: PlatformFormExpr; when?: PlatformFormCond; disabled?: PlatformFormCond }[];
  vars?: Record<string, PlatformFormExpr>;
  sources?: Record<string, PlatformFormSource>;
  params?: Record<string, PlatformFormExpr>;
  build?: PlatformFormExpr;
  text?: PlatformFormExpr;
  fields?: PlatformFormField[];
}

/** How a platform writes its filter groups, and the operators its editor offers. */
export interface PlatformFilterDialect {
  keyForm: "list" | "string";
  nested: boolean;
  maxDepth: number;
  ids: boolean;
  newFilterMode: "and" | "or";
  groupMode: boolean;
  operators: { when?: PlatformFormCond; value: PlatformFormExpr }[];
  defaultOperator?: { when?: PlatformFormCond; value: PlatformFormExpr }[];
  dateInput: "text" | "datetime";
  dateHint?: string;
  searched?: PlatformFormCond;
  /** Keys whose values are `{key, values}` pairs, each part searched on its own. */
  pairs?: Record<string, { key: string; label: string; search: string }[]>;
}

/** A platform's widget editor. */
export interface PlatformFormDescription {
  version: number;
  typePath: string;
  types: { value: string; label?: string; attrs?: Record<string, unknown>; new?: PlatformFormExpr }[];
  default: { type: string; params?: Record<string, unknown> };
  carries?: PlatformFormCond;
  templates?: Record<string, PlatformFormExpr>;
  filters?: PlatformFilterDialect;
  vars?: Record<string, PlatformFormExpr>;
  sources?: Record<string, PlatformFormSource>;
  fields: PlatformFormField[];
}

/** `GET /custom-dashboards/platform-forms`. */
export interface PlatformFormsResponse {
  forms: { source: string; name: string; form: PlatformFormDescription }[];
}

// ---------------------------------------------------------------------------
// Notifications and linked phones
// ---------------------------------------------------------------------------

/**
 * A notification kind (`NOTIFICATION_KINDS`, `services/notification_feed.py`).
 * `decision` is every human gate (#842, S4); `hitl`, `flow_hitl` and
 * `work_item_waiting` stay readable for one release as its aliases.
 */
export type NotificationKind =
  | "decision"
  | "hitl"
  | "flow_hitl"
  | "run"
  | "analytics_refresh"
  | "flow"
  | "auth_failure"
  | "stale_reference"
  | "continuation_refused"
  | "security_audit"
  | "api_key"
  | "agent_account"
  | "reminder"
  | "agent_message"
  | "run_action_request"
  | "work_item_waiting"
  | "builtin_tools_outside_allowlist";

/** One notification as the server presents it: the page it opens included. */
export interface NotificationItem {
  id: string;
  kind: NotificationKind;
  created_at: string;
  /** A path of the web app. */
  route: string;
  title: string;
  subtitle: string | null;
  summary: string | null;
  /** A security audit notification announcing a critical finding. */
  critical: boolean;
  /** The run a run decision waits on, or the flow run a flow decision asks from. */
  run_id: string | null;
  /** What a `decision` entry waits on (`DECISION_SOURCES`). */
  source?: NotificationDecisionSource | null;
  /** A decision still waiting: unread until answered, never read by opening it (#991). */
  awaiting_answer?: boolean;
  unread: boolean;
}

/** The source of a `decision` notification. */
export type NotificationDecisionSource = "run" | "flow_run" | "work_item";

export interface NotificationFeed {
  items: NotificationItem[];
  unread_count: number;
  read_at: string | null;
}

/** A phone the person linked (`models/mobile_device.py`). */
/** The companion's PIN lock for the caller and its session (`GET /app-lock`). */
export interface AppLockStatus {
  /** The PIN applies to the person: their choice, or the organization's policy. */
  applies: boolean;
  /** The organization's policy requires it (it cannot be turned off). */
  required: boolean;
  has_pin: boolean;
  enabled: boolean;
  /** This session is a phone's (the mobile companion). */
  companion: boolean;
  locked: boolean;
  /** The policy requires a PIN this person has not chosen yet. */
  setup_required: boolean;
  attempts_left: number;
  min_digits: number;
  max_digits: number;
}

export interface LinkedPhone {
  id: string;
  name: string;
  platform: "android" | "ios" | "other";
  push_enabled: boolean;
  muted_kinds: NotificationKind[];
  quiet_hours: { start: string; end: string; timezone: string } | null;
  last_seen_at: string | null;
  last_push_at: string | null;
  created_at: string | null;
  /** The phone this page runs on. */
  current: boolean;
}
// ---------------------------------------------------------------------------
// Flows — the visual flow builder
// ---------------------------------------------------------------------------
//
// The canvas document is stored in the platform's own explicit shape, NOT React
// Flow's: transient UI state (`selected`, `dragging`, `measured`) has no
// business being persisted. `pages/flow-builder/graph-adapter.ts` maps between
// the two.
//
// Node kinds, their ports, their configuration fields and the variables they
// publish all come from the server (`GET /flows/node-catalog`) — the frontend
// renders the library panel and every config form from that payload rather than
// hard-coding a second copy that could drift from the validator.

/** Channel type carried by a port. Their intersection decides the edge kind. */
export type FlowPortType =
  | "flow"
  | "branch"
  | "error"
  | "handover"
  | "chat"
  | "capability_tool"
  | "capability_integration"
  | "capability_skill"
  | "capability_knowledge_base"
  | "capability_output"
  | "capability_memory"
  | "capability_guardrails"
  | "capability_llm"
  | "capability_shared_state"
  | "capability_work_items"
  | "capability_workset"
  | "capability_budget"
  | "capability_workspace"
  | "param";

export type FlowEdgeKind =
  | "data"
  | "handover"
  | "condition_branch"
  | "error"
  | "capability"
  | "value";

export type FlowStatus = "draft" | "active" | "archived";

export interface FlowPort {
  id: string;
  label: string;
  types: FlowPortType[];
  /** False = the port accepts a single connection. */
  multiple: boolean;
}

type FlowFieldType =
  | "text"
  | "textarea"
  | "variable_text"
  | "variable_textarea"
  | "number"
  | "slider"
  | "boolean"
  | "select"
  | "cron"
  | "json"
  | "key_value"
  | "multi_select";

/** One conversational entry the chat picker shows for an ACTIVE flow. */
export interface FlowChatEntry {
  flow_id: string;
  flow_name: string;
  description: string | null;
  agent_id: string;
  greeting: string | null;
}

/** One callable operation of a connected integration (a ToolDefinition). */
export interface FlowIntegrationOperation {
  name: string;
  description: string;
  input_schema: {
    type?: string;
    properties?: Record<
      string,
      {
        type?: string;
        description?: string;
        enum?: (string | number)[];
        default?: unknown;
      }
    >;
    required?: string[];
  };
}

/** One directly callable tool (built-in or custom) with its signature. */
export interface FlowCallableTool {
  name: string;
  label: string;
  description?: string;
  source: string;
  input_schema: FlowIntegrationOperation["input_schema"];
  /** A custom tool's id, stored next to the name it is picked for. */
  id?: string | null;
  /** The custom tool of this name a node storing the name alone runs. */
  designated?: boolean;
  /** Why the caller can use it and whose it is, to tell tools of one name apart. */
  origin?: "own" | "company" | "shared" | "other" | null;
  owner_name?: string | null;
}

/** One typed field of a declared flow event — the signature's parameter. */
export interface FlowEventField {
  name: string;
  type: "string" | "number" | "boolean" | "json";
  required: boolean;
  description?: string;
}

/** A declared, typed event signature (the UE Event Dispatcher pattern). */
export interface FlowEventType {
  id: string;
  user_id: string | null;
  name: string;
  description: string;
  fields: FlowEventField[];
  is_company_managed: boolean;
  created_at: string;
  updated_at: string;
}

export interface FlowFieldOption {
  value: string;
  label: string;
  /** List-view metadata (multi-select fields): one line under the name. */
  description?: string | null;
  /** A lucide icon name, or an http(s) URL for a custom icon. */
  icon?: string | null;
  /**
   * Model options only: which configured provider publishes this
   * model. Model ids are not unique across providers, so the model list is
   * narrowed by this once an LLM node pins a provider.
   */
  provider_id?: string | null;
}

export interface FlowConfigField {
  name: string;
  label: string;
  type: FlowFieldType;
  required: boolean;
  help: string | null;
  placeholder: string | null;
  default: unknown;
  options: FlowFieldOption[];
  /** Dynamic option list resolved per caller (`agents`, `integrations`, ...). */
  source: string | null;
  /** Range bounds for a `slider` (and optionally a `number`). */
  min?: number | null;
  max?: number | null;
  step?: number | null;
  /** A `select` override that can defer to the platform default (inherit). */
  inheritable?: boolean;
  /**
   * Restricts the field to one of a node's modes — a condition node is
   * `"deterministic"` until an LLM is wired into it and `"llm"` after.
   * Absent/null = always shown.
   */
  mode?: string | null;
}

interface FlowOutputVariable {
  name: string;
  label: string;
  description: string;
  /** What the value is at run time when the node always publishes the same
   *  kind of value. A `text` output has no fields to read. Absent on
   *  older backends: treat as `any`. */
  type?: "text" | "number" | "boolean" | "object" | "list" | "any";
}

export interface FlowNodeKind {
  kind: string;
  category: string;
  label: string;
  description: string;
  /** Lucide icon name — resolved through `FLOW_NODE_ICONS`. */
  icon: string;
  inputs: FlowPort[];
  outputs: FlowPort[];
  config_fields: FlowConfigField[];
  output_variables: FlowOutputVariable[];
  /** A free-floating annotation (a note): no ports, never executed. */
  is_annotation: boolean;
  /** Whether placing this node makes the flow depend on a model or embedding
   *  provider. Absent on older backends — treat a missing value as
   *  `true`, matching the backend default, so an unknown node is never
   *  presented as safe. */
  requires_ai?: boolean;
  searchable_terms: string[];
}

export interface FlowNodeCatalog {
  categories: { id: string; label: string }[];
  node_kinds: FlowNodeKind[];
  edge_kinds: FlowEdgeKind[];
  sources: Record<string, FlowFieldOption[]>;
  limits: { max_nodes: number; max_edges: number };
}

export interface FlowNode {
  id: string;
  kind: string;
  /** Group membership: id of a `util.group` node (position then relative). */
  parent_id?: string;
  position: { x: number; y: number };
  config: Record<string, unknown>;
  label?: string;
  /** Present on imported nodes: where they came from. An `unresolved` node
   *  keeps its original definition here; `review` lists what an importer
   *  asked a human to check before the node may run, `notes` what it could
   *  only approximate. */
  source_payload?: Record<string, unknown>;
}

export interface FlowEdge {
  id: string;
  source: string;
  source_handle: string;
  target: string;
  target_handle: string;
  kind: FlowEdgeKind;
  label?: string;
  /** How the edge is drawn. Absent = bezier (the default). */
  path_style?: "straight" | "step" | "smoothstep";
}

export interface FlowGraph {
  nodes: FlowNode[];
  edges: FlowEdge[];
  viewport: { x: number; y: number; zoom: number };
}

export interface FlowValidationIssue {
  code: string;
  message: string;
  /** `error` blocks the save; `warning` is pinned on the node and saves fine. */
  severity: "error" | "warning";
  node_id: string | null;
  edge_id: string | null;
  field: string | null;
}

/** `POST /flows/output-schemas`: node id → output name → the JSON
 *  Schema that output declares, resolved by the server with the run's rules
 *  and the caller's visibility. */
export interface FlowOutputSchemas {
  nodes: Record<string, Record<string, Record<string, unknown>>>;
}

export interface FlowSummary {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  description: string;
  status: FlowStatus;
  version_number: number;
  node_count: number;
  tags: string[];
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  /** The requesting user's right to edit it, share tiers included, computed
   *  server-side. Read it through `canManageEntity()`. */
  can_manage?: boolean;
  /** Not company-managed and its owner's account is deactivated: nothing
   *  starts it until the account is reactivated. Derived server-side. */
  paused_by_owner_deactivation?: boolean;
  created_at: string;
  updated_at: string;
}

export interface Flow extends FlowSummary {
  graph: FlowGraph;
  /** Validation issues computed server-side when the flow was loaded/saved. */
  issues: FlowValidationIssue[];
}

export interface CreateFlowRequest {
  name: string;
  description?: string;
  graph?: FlowGraph;
  tags?: string[];
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
}

export interface UpdateFlowRequest {
  name?: string;
  description?: string;
  graph?: FlowGraph;
  status?: FlowStatus;
  tags?: string[];
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  user_id?: string;
  commit_message?: string;
  /** Optimistic lock: the version the client loaded (409 when stale). */
  base_version?: number;
}

interface FlowVersionSummary {
  id: string;
  version_number: number;
  author_id: string | null;
  author_name: string | null;
  commit_message: string | null;
  is_active: boolean;
  is_locked: boolean;
  node_count: number;
  created_at: string;
}

export interface FlowVersionList {
  items: FlowVersionSummary[];
  total: number;
  max_entries: number;
}

/** Says how much of an imported document survived. Never silently partial. */
interface FlowImportReport {
  format: "native" | "langflow" | "n8n";
  nodes_imported: number;
  nodes_unresolved: number;
  /** Mapped nodes carrying review notes (`source_payload.review`): they do
   *  not run until someone clears them. */
  nodes_to_review: number;
  edges_imported: number;
  edges_dropped: number;
  /** What the import could not carry over, flow-wide (credentials, ...). */
  warnings: string[];
}

export interface FlowImportResult {
  flow: Flow;
  report: FlowImportReport;
}


/** What a detach actually did. A split cuts both ways, so both halves report. */
interface FlowDetachReport {
  nodes_detached: number;
  edges_detached: number;
  /** Edges that crossed the selection boundary and could not survive. */
  edges_dropped: number;
  /** `{{node.field}}` references removed from the DETACHED flow. */
  references_removed: string[];
  /** References removed from the flow left behind. */
  parent_references_removed: string[];
}

export interface FlowDetachResult {
  flow: Flow;
  parent: Flow;
  report: FlowDetachReport;
}

// --- Flow execution ---

export type FlowRunStatus =
  | "pending"
  | "running"
  | "awaiting"
  | "completed"
  | "failed"
  | "cancelled"
  /** Held back before it started — today only "this flow needs AI and AI is
   *  disabled". Never an active status: it never ran. */
  | "skipped";

type FlowRunTriggerType =
  | "manual"
  | "schedule"
  | "webhook"
  | "integration_event"
  | "flow_call";

export interface FlowRunSummary {
  id: string;
  flow_id: string;
  flow_version_number: number;
  status: FlowRunStatus;
  trigger_type: FlowRunTriggerType;
  depth: number;
  parent_run_id: string | null;
  result_summary: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  /** Filled on the cross-flow listing (Operations → Executions). */
  flow_name?: string | null;
}

export interface FlowRunList {
  runs: FlowRunSummary[];
  total: number;
}

export interface FlowRunStep {
  id: string;
  seq: number;
  node_id: string;
  node_kind: string;
  iteration: number;
  status: "running" | "completed" | "failed" | "skipped";
  /** The node's RENDERED configuration — the values it computed with. */
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  error: string | null;
  /** A completed step whose answer read as an error (not a failure). */
  warning?: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface FlowRunDetail extends FlowRunSummary {
  /** Debugger: the node the run is paused on (breakpoint / step mode). */
  paused_node_id?: string | null;
  step_mode?: boolean;
  /** Human-in-the-loop: the Ask-user node awaiting an answer + its question. */
  awaiting_input_node_id?: string | null;
  awaiting_input_question?: string | null;
  /** The Ask-user node's structured ask when it lists options (#842, S6); null for free text. */
  awaiting_input_ask?: WorkItemAsk | null;
  trigger_payload: Record<string, unknown> | null;
  trigger_context: Record<string, unknown> | null;
  steps: FlowRunStep[];
}

/** Activation response. The webhook secret appears HERE ONLY — it is never
 * serialized into list/detail payloads. */
export interface FlowActivateResult {
  flow: Flow;
  webhook_url: string | null;
  webhook_secret: string | null;
  warnings: FlowValidationIssue[];
}

// ── OpenRouter console (`/integrations/:id/openrouter/...`) ─────────────────

export type OpenRouterLimitReset = "daily" | "weekly" | "monthly";

/** A part of the overview OpenRouter refused or could not answer. */
export interface OpenRouterConsoleError {
  code: string;
  message: string;
}

export interface OpenRouterKeyInfo {
  label: string | null;
  limit: number | null;
  limit_remaining: number | null;
  limit_reset: OpenRouterLimitReset | null;
  include_byok_in_limit: boolean;
  usage: number;
  usage_daily: number;
  usage_weekly: number;
  usage_monthly: number;
  byok_usage: number;
  byok_usage_daily: number;
  byok_usage_weekly: number;
  byok_usage_monthly: number;
  is_free_tier: boolean;
  is_management_key: boolean;
  expires_at: string | null;
  free_model_daily_requests: { limit: number | null; remaining: number | null; used: number | null } | null;
  allowed_data_regions: string[];
}

interface OpenRouterCredits {
  total_credits: number;
  total_usage: number;
  remaining: number;
}

/** The settings a Supabase database access probe tries; a field left out is the stored one. */
export interface SupabaseProbeRequest {
  project_ref?: string;
  db_host?: string;
  db_port?: string;
  db_name?: string;
  db_role?: string;
  /** Left out (or the mask): the stored password. */
  db_password?: string;
  db_ca_cert?: string;
}

export interface SupabaseProbeFunction {
  schema: string;
  name: string;
  signature: string;
  returns: string;
  returns_set: boolean;
  security_definer: boolean;
  volatility: string;
  comment: string | null;
}

export type SupabaseConnectionMode = "session" | "transaction" | "direct";

/** What a Supabase database role is chosen from; a part the probe could not reach is `null`, its reason in `errors`. */
export interface SupabaseProbeResponse {
  projects: { ref: string; name: string; region: string; status: string }[] | null;
  roles: { name: string; bypass_rls: boolean }[] | null;
  connections: { host: string; port: number; mode: SupabaseConnectionMode }[] | null;
  databases: string[] | null;
  role_bypasses_rls: boolean | null;
  functions: SupabaseProbeFunction[] | null;
  readable_relations: { schema: string; name: string; kind: string; rls_enabled: boolean }[] | null;
  errors: Record<string, string>;
}

export interface OpenRouterOverview {
  key: OpenRouterKeyInfo | null;
  key_error: OpenRouterConsoleError | null;
  credits: OpenRouterCredits | null;
  credits_error: OpenRouterConsoleError | null;
  has_api_key: boolean;
  has_management_key: boolean;
  /** The viewer may create, edit and delete the account's API keys. */
  can_manage_keys: boolean;
  /** The viewer may feed the platform LLM provider from this integration. */
  can_link_provider: boolean;
  /** Why the integration may not feed it, when it may not. */
  link_refusal: string | null;
  llm_providers: LLMProvider[];
  adoptable_providers: { id: string; display_name: string | null; default_model: string | null }[];
}

// ── LM Studio console (`/integrations/:id/lmstudio`) ─────────────────────────

export interface LMStudioOverview {
  /** The integration is routed through a relay group (the console needs it). */
  routed: boolean;
  relay_group_id: string | null;
  /** The server URL as stored, or `null` when unset or invalid. */
  base_url: string | null;
  /** Why the server cannot be called, when it cannot (no route, no URL...). */
  server_refusal: string | null;
  has_api_token: boolean;
  /** The viewer may load, unload and download models. */
  can_manage_server: boolean;
  /** The viewer may feed the platform LLM provider from this integration. */
  can_link_provider: boolean;
  /** Why the integration may not feed it, when it may not. */
  link_refusal: string | null;
  llm_providers: LLMProvider[];
  adoptable_providers: { id: string; display_name: string | null; default_model: string | null }[];
}

export type LMStudioModelType = "llm" | "embedding" | "decision";

interface LMStudioLoadedInstance {
  id: string;
  context_length: number | null;
  flash_attention: boolean | null;
  parallel: number | null;
}

export interface LMStudioModel {
  key: string;
  type: LMStudioModelType;
  display_name: string;
  publisher: string | null;
  architecture: string | null;
  format: string | null;
  quantization: string | null;
  bits_per_weight: number | null;
  size_bytes: number | null;
  params: string | null;
  max_context_length: number | null;
  vision: boolean;
  tool_use: boolean;
  reasoning_options: string[];
  description: string | null;
  loaded_instances: LMStudioLoadedInstance[];
  /** The platform provider fed by this integration offers this model. */
  offered: boolean;
}

export interface LMStudioLoadRequest {
  model: string;
  context_length?: number;
  flash_attention?: boolean;
  eval_batch_size?: number;
  offload_kv_cache_to_gpu?: boolean;
}

type LMStudioDownloadStatus = "downloading" | "paused" | "completed" | "failed" | "already_downloaded";

export interface LMStudioDownloadJob {
  job_id: string | null;
  status: LMStudioDownloadStatus;
  total_size_bytes: number | null;
  downloaded_bytes: number | null;
  bytes_per_second: number | null;
  started_at: string | null;
  estimated_completion: string | null;
  completed_at: string | null;
}

export interface OpenRouterActivityRow {
  date: string;
  model: string;
  provider_name: string;
  usage: number;
  byok_usage_inference: number;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
}

/** USD per million tokens; `request` / `image` / `web_search` are per unit. */
interface OpenRouterPricing {
  prompt: number | null;
  completion: number | null;
  input_cache_read: number | null;
  input_cache_write: number | null;
  internal_reasoning: number | null;
  request: number | null;
  image: number | null;
  web_search: number | null;
}

export interface OpenRouterModel {
  id: string;
  name: string;
  author: string;
  created: string | null;
  description: string | null;
  context_length: number | null;
  max_completion_tokens: number | null;
  is_moderated: boolean;
  input_modalities: string[];
  output_modalities: string[];
  pricing: OpenRouterPricing;
  variable_pricing: boolean;
  is_free: boolean;
  supports_tools: boolean;
  supports_reasoning: boolean;
  supports_structured_outputs: boolean;
  supported_parameters: string[];
  knowledge_cutoff: string | null;
  expiration_date: string | null;
}

export type OpenRouterCatalogKind = "chat" | "embeddings";

export interface OpenRouterEndpoint {
  provider_name: string;
  tag: string | null;
  quantization: string | null;
  context_length: number | null;
  max_completion_tokens: number | null;
  status: number | null;
  uptime_last_30m: number | null;
  uptime_last_1d: number | null;
  latency_p50_ms: number | null;
  throughput_p50: number | null;
  supports_implicit_caching: boolean;
  supports_tools: boolean;
  pricing: { prompt: number | null; completion: number | null; input_cache_read: number | null };
}

export type OpenRouterKeyUse = "integration" | "llm_provider";

export interface OpenRouterApiKey {
  hash: string;
  name: string;
  label: string;
  disabled: boolean;
  limit: number | null;
  limit_remaining: number | null;
  limit_reset: OpenRouterLimitReset | null;
  include_byok_in_limit: boolean;
  usage: number;
  usage_daily: number;
  usage_weekly: number;
  usage_monthly: number;
  created_at: string | null;
  updated_at: string | null;
  expires_at: string | null;
  /** What on this platform runs on the key: it cannot be disabled or deleted. */
  in_use: OpenRouterKeyUse[];
}

export interface CreateOpenRouterKeyRequest {
  name: string;
  limit?: number | null;
  limit_reset?: OpenRouterLimitReset | null;
  include_byok_in_limit?: boolean;
  /** ISO 8601 with a time zone, in the future. */
  expires_at?: string | null;
}

export type UpdateOpenRouterKeyRequest = Partial<{
  name: string;
  disabled: boolean;
  limit: number | null;
  limit_reset: OpenRouterLimitReset | null;
  include_byok_in_limit: boolean;
}>;

// ── OpenCRQ version card (`/integrations/:id/opencrq/compatibility`) ──

/** `registration`: the OpenCRQ registered with XTM One, which holds its manifest. `none`: no registration
 *  owns the integration, the manifest is read from the instance at the first OpenCRQ call of a
 *  conversation or run. */
type OpenCrqManifestSource = "registration" | "none";

/** `current`: the stored manifest is the one the last registration reported. `pending`: a fingerprint came,
 *  its manifest has not. `unavailable`: the registration reports no fingerprint. `not_accepted_default_token`:
 *  the published default registration token is in force, so nothing is stored. */
export type OpenCrqManifestState = "current" | "pending" | "unavailable" | "not_accepted_default_token";

interface OpenCrqHiddenTool {
  name: string;
  /** The OpenCRQ procedures the tool sends that the connected instance does not list. */
  missing_procedures: string[];
}

interface OpenCrqLimitedField {
  procedure: string;
  /** The field's path in the tool's input, e.g. `ownerId` or `rows[].key`. */
  path: string;
  /** `unknown`: the instance does not accept the field; `missing`: it requires one the tool never sends. */
  kind: "unknown" | "missing";
}

interface OpenCrqLimitedTool {
  name: string;
  fields: OpenCrqLimitedField[];
}

/** The pairing state of a WhatsApp linked-device gateway (the agent tools' mapping). */
export type WhatsAppPairingState = "connected" | "qr" | "banned" | "sync_error" | "starting" | "unknown";

/** Why the pairing card could not read the gateway. */
export type WhatsAppPairingErrorCode = "gateway_unreachable" | "no_channel_token" | "token_unreadable" | "unknown_vendor";

/** `GET /integrations/{id}/whatsapp/pairing`. */
export interface WhatsAppPairing {
  vendor: string;
  state: WhatsAppPairingState;
  /** Why the gateway could not be read, as a code the card translates; null when it answered. */
  error_code?: WhatsAppPairingErrorCode | null;
  /** The raw detail behind `error_code` (English, shown only as a tooltip); null when it answered. */
  error: string | null;
  /** Whether the caller may manage the integration: the QR and the webhook path are for them only. */
  can_manage: boolean;
  /** The login QR as a PNG data URI, while `state` is `qr`, for a manager. */
  qr: string | null;
  /** `/api/channels/whatsapp/gateway/<id>?token=<secret>`, for a manager when a secret is set. */
  webhook_path: string | null;
  has_webhook_secret: boolean;
}

export interface OpenCrqCompatibility {
  /** What the registration reported (`1.2.3`, `main-1a2b3c4d`, "" for a local build); null: no registration. */
  reported_version: string | null;
  manifest_source: OpenCrqManifestSource;
  /** Null when `manifest_source` is `none`. */
  manifest_state: OpenCrqManifestState | null;
  /** The first 12 hex characters of the fingerprint the registration reported; null: none reported. */
  fingerprint: string | null;
  /** When the manifest in force was received; only when `manifest_state` is `current`. */
  received_at: string | null;
  tool_count: number;
  /** Only when `manifest_state` is `current`. */
  hidden_tools: OpenCrqHiddenTool[];
  /** Only when `manifest_state` is `current`. */
  limited_tools: OpenCrqLimitedTool[];
}

// Nova prompt-safety rules — active pre-LLM guard (`.nov` detection rules).
export type NovaRuleAction = "block" | "log";

export interface NovaRule {
  id: string;
  user_id: string | null;
  owner_name: string | null;
  name: string;
  description: string;
  /** Free-form category from the rule's `meta.category` (e.g. "Jailbreak"). */
  category: string;
  action: NovaRuleAction;
  /** The `.nov` rule source, validated by the Nova parser on write. */
  rule_text: string;
  enabled: boolean;
  tags: string[];
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  created_at: string;
  updated_at: string;
  /** True when the rule carries a `semantics:` pattern. */
  has_semantics?: boolean;
}

export interface CreateNovaRuleRequest {
  name: string;
  description?: string;
  category?: string;
  action?: NovaRuleAction;
  rule_text: string;
  enabled?: boolean;
  tags?: string[] | null;
  is_company_managed?: boolean;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
}

export interface UpdateNovaRuleRequest {
  name?: string;
  description?: string;
  category?: string;
  action?: NovaRuleAction;
  rule_text?: string;
  enabled?: boolean;
  tags?: string[] | null;
  is_company_managed?: boolean;
  user_id?: string | null;
  shared_group_ids?: string[] | null;
  shared_group_permissions?: Record<string, string> | null;
}

/** Result of a dry `.nov` parse — used for live builder feedback. */
export interface NovaRuleValidation {
  valid: boolean;
  error: string | null;
  name: string | null;
  category: string | null;
  keyword_count: number;
  /** Tiers present in the rule; `semantics`/`llm` are inert until the engine is installed. */
  tiers: string[];
}

/** Which Nova rule tiers can fire, and the models the advanced ones use. */
export interface NovaEngineStatus {
  /** True when the Nova engine is installed (semantics:/llm: tiers can run). */
  engine_available: boolean;
  tiers: string[];
  /** Null = the platform default model. */
  llm_model: string | null;
  semantic_model: string | null;
  /** Whether the rules' semantic thresholds fit the embedding model in use. */
  semantic_calibration?: {
    calibrated_for: string;
    /** Null when no embedding model is configured. */
    configured_model: string | null;
    calibrated: boolean;
    /** The Nova override that is not the model scoring (the tier fell back to the platform one). */
    override_ignored?: string | null;
    /** Why: `another_provider` or `no_embedding`. */
    override_reason?: string | null;
  };
}

/** A GitHub repo of `.nov` rules synced into company-managed Nova rules. */
export interface NovaRuleSource {
  id: string;
  repo: string;
  branch: string;
  path: string;
  sync_interval: string;
  enabled: boolean;
  last_synced_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
  last_sync_rules: number;
}

// ── Waiting arcade ──────────────────────────────────────────────────
// Other players are known by their arcade callsign alone: the API never sends
// another user's id, name or email.

export type ArcadePeriod = "day" | "week" | "all";

/** A player to beat on the weekly board, handed to a starting game.
 * @public
 */
export interface ArcadeRival {
  callsign: string;
  score: number;
  rank: number;
  is_self: boolean;
}

export interface ArcadeSessionStart {
  session_id: string;
  heartbeat_seconds: number;
  personal_best: number;
  callsign: string;
  hidden: boolean;
  /** Players right above the caller on the weekly board, closest first. */
  ladder: ArcadeRival[];
  leader: ArcadeRival | null;
  week_rank: number | null;
  /** Achievements already unlocked. */
  achievements: string[];
}

export interface ArcadeLivePlayer {
  callsign: string;
  score: number;
  wave: number;
  is_self: boolean;
}

export interface ArcadeHeartbeat {
  live: ArcadeLivePlayer[];
  /** When to beat next. Optional: a server that does not say keeps the last cadence. */
  heartbeat_seconds?: number;
}

export interface ArcadeRunSubmission {
  score: number;
  wave: number;
  kills: number;
  shots: number;
  hits: number;
  ui_destroyed: number;
  bosses: number;
  max_combo: number;
  ufo_kills: number;
  duration_ms: number;
  cheat: boolean;
  achievements: string[];
}

export interface ArcadeFinishResult {
  score_id: string;
  ranked: boolean;
  personal_best: boolean;
  previous_best: number;
  new_achievements: string[];
  /** The caller's position on each board after the run (`null` = not on it). */
  ranks: Partial<Record<ArcadePeriod, number | null>>;
}

export interface ArcadeLeaderboardEntry {
  rank: number;
  callsign: string;
  best_score: number;
  best_wave: number;
  games: number;
  kills: number;
  ui_destroyed: number;
  achievements: number;
  is_self: boolean;
}

export interface ArcadeTitleHolder {
  callsign: string;
  value: number;
  is_self: boolean;
}

export interface ArcadeLeaderboard {
  period: ArcadePeriod;
  entries: ArcadeLeaderboardEntry[];
  me: ArcadeLeaderboardEntry | null;
  total_players: number;
  live: ArcadeLivePlayer[];
  titles: {
    top_vandal: ArcadeTitleHolder | null;
    sharpshooter: ArcadeTitleHolder | null;
    marathon: ArcadeTitleHolder | null;
  };
}

interface ArcadePlayerStats {
  games: number;
  best_score: number;
  best_wave: number;
  total_kills: number;
  total_ui_destroyed: number;
  total_play_ms: number;
  bosses: number;
}

export interface ArcadeProfile {
  callsign: string | null;
  default_callsign: string;
  display_callsign: string;
  hidden: boolean;
  /** {achievement id: ISO-8601 unlock time}. */
  achievements: Record<string, string>;
  stats: ArcadePlayerStats;
}

// ── Deep Investigation ──────────────────────────────────────────────

/**
 * A step's state. Seven values, and the distinctions matter more than the
 * count: `empty` is a source that answered and found nothing (NOT a success),
 * `degraded` a partial read, `skipped` one the run never reached. Only
 * `completed` may render as a success — see `InvestigationSummary.all_succeeded`.
 */
export type InvestigationStepStatus =
  | "pending"
  | "active"
  | "completed"
  | "empty"
  | "degraded"
  | "error"
  | "skipped";

/**
 * Machine-readable reason for a step's or a run's outcome, so the UI renders it
 * in the reader's language instead of the English sentence the backend happened
 * to write. Paired with a `*_params` dict that fills its placeholders.
 *
 * The free-text `status_detail` / `end_reason` stay as the fallback for the one
 * family that cannot be localized: a message lifted from a third-party
 * exception. A code wins wherever one exists.
 */
export type InvestigationDetailCode =
  | "run.all_sources_queried"
  | "run.budget_spent"
  | "run.no_covering_source"
  | "run.cancelled"
  | "run.interrupted"
  | "source.timed_out"
  | "source.http_status"
  | "source.truncated"
  | "source.thin_response"
  | "source.querier_error"
  | "source.kb_provider_unreachable"
  | "source.kb_search_failed"
  | "source.kb_capped"
  | "source.kb_unavailable"
  | "source.kb_below_floor"
  | "source.mcp_bad_server_id"
  | "source.mcp_server_missing"
  | "source.mcp_server_disabled"
  | "source.mcp_tool_missing"
  | "source.mcp_argument_ambiguous"
  | "source.mcp_tool_error"
  | "source.fingerprint_matched"
  | "source.fingerprint_capped"
  | "source.vt_unavailable"
  | "source.passive_dns_filtered"
  | "source.passive_dns_filtered_more"
  | "source.passive_dns_more"
  | "source.passive_dns_seeds"
  | "source.free_http_refused"
  | "source.free_http_capped"
  | "source.opencti_unavailable"
  | "source.opencti_known"
  | "source.opencti_none_known";

/** Why a step failed. Mirrors the backend's `STEP_ERROR_TYPES` taxonomy. */
export type InvestigationErrorType =
  | "timeout"
  | "rate_limited"
  | "not_found"
  | "auth_error"
  | "unreachable"
  | "invalid_response";

/**
 * One source query.
 *
 * The state field is `step_status`, not `status`, because an
 * `investigation_step` frame spreads this same payload alongside its own
 * top-level `status` (the frame kind) — one step has one shape in a REST
 * answer and on the stream, so frames merge into fetched objects without a
 * translation layer.
 */
/**
 * One citation a step produced.
 *
 * `label` is always present; `href` only when there is genuinely somewhere to
 * go. A knowledge-base document and an MCP tool result have no address, so the
 * block renders them as text — a citation that looks like a link and leads
 * nowhere is the same overstatement the step statuses exist to prevent.
 */
export interface InvestigationEvidence {
  kind: "url" | "document" | "tool_result";
  label: string;
  href: string | null;
  /** The passage this citation rests on, verbatim. Absent where the source
   *  gave none. */
  quote?: string;
  /** Half-open range into the step's `answer`. Present together or not at
   *  all: a citation with a span backs THAT sentence; one without was merely
   *  consulted, and rendering the two alike would put a marker where nothing
   *  supports it. */
  start?: number;
  end?: number;
}

export interface InvestigationStep {
  step_id: string | null;
  position: number;
  step_status: InvestigationStepStatus;
  source_name: string;
  source_pack_slug: string | null;
  /** The URL actually fetched, once known. Null while still only planned. */
  query: string | null;
  error_type: InvestigationErrorType | null;
  /** Skip reason, or failure detail. The FALLBACK: rendered only when
   *  `detail_code` is null, which happens for an untranslatable third-party
   *  exception message. Never a stack trace. */
  status_detail: string | null;
  detail_code: InvestigationDetailCode | null;
  detail_params: Record<string, string | number> | null;
  findings_count: number;
  /** Null whenever it could not be derived from findings — never defaulted. */
  confidence: number | null;
  /** What the step cites. See `InvestigationEvidence`. */
  evidence: InvestigationEvidence[];
  /** The prose this source wrote, when it wrote any — a provider web search
   *  answers in sentences. Null for a fetched page or a corpus passage, which
   *  cite a document instead. The `start`/`end` offsets on `evidence` index
   *  into this string. */
  answer?: string | null;
  iteration: number | null;
  /** Set when this step came from another step's findings (multi-hop lead). */
  lead_from_step_id: string | null;
  /** What a goal-plan hop queried when it is not the run's own subject: the
   *  IP a pivot reads, a neighbouring site being fingerprinted. */
  subject_value?: string | null;
  /** The goal-plan action this step serves (`InvestigationGoalAction.slug`). */
  action?: string | null;
}

/** One action of a goal plan, as A* ordered it. */
export interface InvestigationGoalAction {
  slug: string;
  /** English, and a catalog key: rendered through `t_i18n`. */
  label: string;
  description: string;
  cost: number;
  preconditions: string[];
  effects: string[];
  /** A key of the backend's closed `GOAL_ACTION_ICONS` set. */
  icon: string;
  /** The action whose work is the run's report rather than a source query. */
  produces_report: boolean;
  /** False when no source of this run can do the action's work. */
  servable?: boolean;
}

/**
 * The goal a run pursues and the path to it, computed server-side: the A*
 * path of the plan the run's pack declares, or the default plan read from the
 * kinds of source the run planned. Every run carries one.
 *
 * Whether a predicate HOLDS is not here: the block derives it from the steps
 * of the action that sets it, so the state shown can never run ahead of what
 * the sources answered.
 */
export interface InvestigationGoalPlan {
  /** The pack that declared the plan; null for the default plan. */
  pack_slug: string | null;
  /** False for the default plan, read from the kinds of source the run
   *  planned rather than declared by a pack. */
  declared: boolean;
  /** False when no action sequence reaches the goal from the subject's state. */
  reachable?: boolean;
  /** A catalog key with a `{value}` placeholder for the subject. */
  objective: string;
  predicates: { slug: string; label: string }[];
  initial_state: string[];
  goal_state: string[];
  actions: InvestigationGoalAction[];
  path_cost: number;
}

/** A passage the report cites. `label` / `href` are third-party text. */
interface InvestigationReportSource {
  n: number;
  source_name: string;
  label: string;
  quote?: string;
  href?: string | null;
}

interface InvestigationSummary {
  total: number;
  by_status: Partial<Record<InvestigationStepStatus, number>>;
  /** True only when every step is `completed`. Computed server-side. */
  all_succeeded: boolean;
  failed: number;
  /** Steps that never reached a terminal state (a cancelled run). */
  unfinished: number;
  confidence: number | null;
  /** How many steps `confidence` is the mean of. Unweighted, and only over
   * steps that carry one — render it alongside, or "0.9" can mean one step
   * out of eleven. */
  confidence_samples: number;
}

type InvestigationRunStatus =
  | "planning"
  | "running"
  | "completed"
  | "cancelled"
  | "aborted";

/** A whole run — sufficient to render the block with no stream frames at all. */
export interface Investigation {
  id: string;
  subject_type: string;
  subject_value: string;
  source_pack_slugs: string[];
  status: InvestigationRunStatus;
  /** Free-text fallback; prefer `end_reason_code`. */
  end_reason: string | null;
  end_reason_code: InvestigationDetailCode | null;
  end_reason_params: Record<string, string | number> | null;
  /** Leads found but never planned, because the plan cap was reached. */
  leads_dropped: number;
  max_iterations: number;
  iterations_used: number;
  /** Bumped on every state write — discriminates "same run, new states". */
  revision: number;
  /** Measured wall clock; null while unfinished. Never step-count derived. */
  duration_seconds: number | null;
  conversation_id: string | null;
  assignment_run_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  /** Set when this run continued an earlier one whose budget ran out. */
  continues_investigation_id?: string | null;
  /** The plan the block draws. Absent only from an older server. */
  goal_plan?: InvestigationGoalPlan | null;
  /** What "Generate executive summary" sends to the conversation's agent:
   *  the instruction, naming the built-in XTM One deck. Null until the run
   *  has a report. */
  executive_summary?: { template_name: string; prompt: string } | null;
  /** Whether an OpenCTI registered with this platform is available to the
   *  reader. */
  opencti_available?: boolean;
  /** "Send to OpenCTI": the instruction the conversation's agent receives,
   *  and the OpenCTI integration its turn needs. Null without a report or an
   *  OpenCTI. */
  opencti_record?: { integration_id: string; prompt: string } | null;
  /** Where the cited report stands: on its writer's word ("writing" until it
   *  returns), "none" when no report is coming, "unknown" when the server
   *  could not read that word. */
  report_status?: "written" | "writing" | "none" | "unknown";
  /** The cited synthesis, written once the run ended. Null until then, and
   *  null for good when nothing could back one. */
  report?: string | null;
  report_sources?: InvestigationReportSource[];
  report_claims?: number | null;
  report_uncited_claims?: number | null;
  steps: InvestigationStep[];
  /** The read was capped, so `steps` is a sample and `summary.all_succeeded`
   *  is false regardless of what those steps say. */
  steps_truncated: boolean;
  summary: InvestigationSummary;
}

/** One source a previous run planned and its budget never reached. */
interface ContinuationStepOption {
  step_id: string;
  source_name: string;
  source_pack_slug: string | null;
  /** Position in the parent's plan, so the remainder is offered in the order
   *  the analyst already saw rather than re-sorted. */
  position: number;
  /** `http` | `knowledge_base` | `mcp_tool` | `web_search`. Shown because
   *  "no external source was reached" is a different gap from "no corpus was
   *  searched", and the kind is what says which one this would close. */
  kind: string;
}

/** What the parent run reached — counted server-side, never judged.
 *
 * Numbers rather than a sentence on purpose. The assessment an analyst reads
 * is composed from these in their own language, and there is no model call
 * behind them: a hallucinated "not worth continuing" would close an
 * investigation nobody decided to close.
 */
export interface ContinuationCoverage {
  queried: number;
  unqueried: number;
  findings: number;
  /** Sources that answered "nothing found". Neutral, not failure — and the
   *  outcome that most often makes continuing worthwhile. */
  empty: number;
  failed: number;
  packs_reached: string[];
  /** Packs the plan named and the budget never touched at all. The structural
   *  gap: a verdict resting on whichever sources came first is a different
   *  claim from one that consulted every kind the plan named. */
  packs_untouched: string[];
}

/** The enriched block shown when a run stopped short of its plan. */
export interface ContinuationOffer {
  investigation_id: string;
  subject_value: string;
  subject_type: string;
  available: boolean;
  /** `chain_budget` | `chain_depth` | `nothing_left` | `nothing_addressable`.
   *  A code, so the block can say why in the reader's language. */
  blocked_reason: string | null;
  /** Queries this chain has already spent, summed across every run in it. */
  spent: number;
  chain_depth: number;
  remaining_allowance: number;
  /** The cumulative cap, so spend renders against it rather than as a bare
   *  remainder. This is the number that actually binds the feature. */
  chain_max: number;
  /** Human label per pack slug, from the server. A client-side slug -> label
   *  table drifted the moment a second built-in pack existed, so the labels
   *  travel with the offer. */
  pack_labels: Record<string, string>;
  steps: ContinuationStepOption[];
  /** `[name, reason]` for sources that cannot be offered — `no_address`,
   *  `unreadable_address`, `no_longer_permitted`. Named rather than dropped: a
   *  remainder that silently shrinks is the gap this feature exists to close. */
  withheld: string[][];
  coverage: ContinuationCoverage;
}

/** The child run a continuation produced. */
export interface ContinuationStarted {
  investigation_id: string;
  continues_investigation_id: string;
  /** Sources actually queued, after the chain cap truncated the selection. */
  queued: number;
  /** Selected ids that could not be run — stale, or withheld between
   *  rendering the block and clicking it. */
  rejected_step_ids: string[];
  message: string;
}

/**
 * One source pack as the picker renders it.
 *
 * `applicable_source_count` is what keeps the picker honest: a pack's affinity
 * can cover a subject type while none of its *sources* apply to it. The
 * built-in `web-general` declares every subject type but has no source for a
 * file hash, so it is "recommended" for a hash and would query nothing.
 */
/** One value of a pack option, as the server describes it.
 */
interface PackChoice {
  value: string;
  label: string;
  description: string;
}

/**
 * A choice a pack offers before a run, e.g. how deep a pivot goes.
 * Rendered from the server so the picker names no pack; the chosen values go
 * back as `source_pack_options`.
 */
export interface PackOption {
  key: string;
  label: string;
  description: string;
  default: string;
  choices: PackChoice[];
}

/** One source of a built investigation pack's step. Fields not of its
 *  kind are refused by the server. */
export interface PackSourceSpec {
  kind: "opencti" | "passive_dns" | "site_fingerprint" | "web_search" | "knowledge_base" | "mcp_tool" | "reference" | "http";
  name?: string | null;
  subject_types?: string[] | null;
  reference?: string | null;
  hostname_keywords?: string | null;
  max_pages?: number | null;
  pivot_scope?: "shared" | "all";
  pivot_step?: string | null;
  fingerprint_set?: string | null;
  search_prompt?: string | null;
  kb_slug?: string | null;
  kb_tags?: string[] | null;
  knowledge_base_ids?: string[] | null;
  query_template?: string | null;
  min_similarity?: number | null;
  server_id?: string | null;
  url_template?: string | null;
  value_transform?: "host" | "slug" | null;
}

export interface PackStepSpec {
  key: string;
  label: string;
  description?: string;
  icon?: string;
  optional?: boolean;
  default_on?: boolean;
  subject_types?: string[] | null;
  sources: PackSourceSpec[];
}

export interface PackExposedSetting {
  step: string;
  source: number;
  setting: "pivot_scope" | "max_pages" | "hostname_keywords";
  label?: string | null;
  default: string;
}

interface PackDefinition {
  steps: PackStepSpec[];
  exposed?: PackExposedSetting[];
}

/** What a built pack carries besides its sharing (`PackFields`). */
export interface InvestigationPackFields {
  name: string;
  description: string;
  subject_types: string[];
  recommended: boolean;
  max_iterations: number;
  definition: PackDefinition;
}

export interface InvestigationPack extends InvestigationPackFields {
  id: string;
  slug: string;
  user_id: string | null;
  owner_name: string | null;
  version_number: number;
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids: string[] | null;
  shared_user_permissions: Record<string, string> | null;
  access_restricted?: boolean;
  can_manage: boolean;
  created_at: string;
  updated_at: string;
}

export interface InvestigationPackVersionItem {
  version_number: number;
  author_id: string | null;
  author_name: string | null;
  commit_message: string | null;
  created_at: string;
  is_current: boolean;
}

export interface InvestigationPackVersionDetail extends InvestigationPackVersionItem {
  subject_types: string[];
  definition: PackDefinition;
}

export interface BuiltinPackItem {
  slug: string;
  label: string;
  description: string;
  subject_types: string[];
}

/** What the builder offers, from the server (`/investigation-packs/catalog`). */
export interface PackBuilderCatalog {
  subject_types: string[];
  kinds: { kind: PackSourceSpec["kind"]; label: string; description: string; subject_types: string[] }[];
  reference_sources: { name: string; subject_types: string[] }[];
  hostname_keywords: { value: string; label: string }[];
  fingerprint_sets: { value: string; label: string }[];
  icons: string[];
  settings: { setting: PackExposedSetting["setting"]; label: string; choices: { value: string; label: string }[] }[];
  limits: { steps: number; sources_per_step: number; exposed_settings: number; max_iterations: number };
}

interface PackPreviewAction {
  slug: string;
  label: string;
  description: string;
  icon: string;
  produces_report: boolean;
  servable: boolean;
  sources: { name: string; kind: string }[];
}

export interface PackPreview {
  subject: string;
  subject_type: string;
  reachable: boolean;
  actions: PackPreviewAction[];
  description: string;
}

/** The options confirmed in the picker: pack slug -> option key -> value. */
export type SourcePackOptions = Record<string, Record<string, string>>;

export interface SourcePackOption {
  slug: string;
  label: string;
  description: string;
  subject_types: string[];
  recommended: boolean;
  applicable_source_count: number;
  total_source_count: number;
  applicable_source_names: string[];
  max_iterations: number;
  /** Choices this pack offers for the classified subject; may be absent. */
  options?: PackOption[];
  /** Whose the pack is, from its row (never its text). */
  origin?: "builtin" | "mine" | "company" | "shared";
  /** The owner's name, for a `shared` pack. */
  owner_name?: string | null;
  /** Hosts a user typed that the run sends the subject to (free HTTP sources). */
  destinations?: string[];
}

/** Server-side classification plus the packs that could investigate a subject. */
export interface SubjectPlan {
  subject: string;
  subject_type: string;
  /** How the run names the subject: a list's summary, else the subject. */
  subject_summary?: string;
  packs: SourcePackOption[];
  /** False when no pack contributes a single applicable source. */
  has_applicable_source: boolean;
}

/**
 * A named environment scoping what agents share.
 *
 * `is_default` is a per-user property enforced by a partial unique index in
 * Postgres — exactly one per owner, always. That is why the UI never has to
 * handle "two defaults" or "no default": the list endpoint creates one on
 * first use, and the database refuses a second.
 */
export interface Workspace {
  id: string;
  user_id: string | null;
  owner_name?: string | null;
  name: string;
  /** Derived from the name at creation, then stable — a rename does not re-slug. */
  slug: string;
  description: string;
  icon: string;
  color: string;
  is_default: boolean;
  is_archived: boolean;
  is_company_managed: boolean;
  shared_group_ids: string[] | null;
  shared_group_permissions: Record<string, string> | null;
  shared_user_ids?: string[] | null;
  shared_user_permissions?: Record<string, string> | null;
  /** A company-managed workspace narrowed to its grants. */
  access_restricted?: boolean;
  /** Whether the requesting user may edit it (`helpers.can_manage`). */
  can_manage: boolean;
  /** What every agent turn in a filed conversation receives. */
  instructions: string;
  created_at: string;
  updated_at: string;
  /** What the workspace holds. Absent — not zeroed — when the endpoint did
   *  not count: "nobody counted" and "this is empty" answer the question
   *  "is deleting this safe?" differently. Only a list asked `with_contents`
   *  counts. */
  contents?: WorkspaceContents | null;
  /** The fleets that coordinate in this workspace and that the caller can
   *  see. A fleet lives in one workspace; the workspace survives the
   *  fleet, not the reverse. */
  hosted_fleets?: WorkspaceHostedFleet[];
  /** Every fleet hosted here, seen or not: any of them blocks the delete. */
  hosted_fleet_count?: number;
  /** The live (not archived) ones: any of them blocks the archive. */
  live_hosted_fleet_count?: number;
}

/** A fleet a workspace hosts. */
interface WorkspaceHostedFleet {
  id: string;
  name: string;
  slug: string;
  is_archived: boolean;
}

/** How much of each scoped thing a workspace holds. */
export interface WorkspaceContents {
  conversations: number;
  /** The workspace's work items: filed in it, or produced by a home member
   *  of the fleet it hosts, as its Operations tab lists them. */
  work_items: number;
  /** The items FILED in the workspace: what blocks a delete, because the
   *  foreign key is RESTRICT. */
  held_work_items: number;
  /** Live work among `work_items`. */
  open_work_items: number;
  shared_states: number;
  worksets: number;
  knowledge_bases: number;
}

/** A knowledge base bound to a workspace. The binding is not ownership. */
export interface WorkspaceKnowledgeBaseRef {
  knowledge_base_id: string;
  name: string;
  description: string;
}

/** What a workspace gives its agents, as its knowledge panel shows it. */
export interface WorkspaceKnowledgeSummary {
  instructions: string;
  /** Whether the caller may change any of it - the workspace's own right. */
  can_manage: boolean;
  /** The Sources knowledge base, written through the knowledge-base routes;
   *  `null` until the first source is added. */
  sources_knowledge_base_id: string | null;
  sources: { count: number; ready: number; pending: number; failed: number };
  /** Linked skills and bound knowledge bases the caller can see. */
  skills: number;
  knowledge_bases: number;
  /** Filed conversations searchable by the workspace's agents, and those
   *  still waiting to be indexed. */
  conversations: { indexed: number; pending: number };
}

/** A library skill linked to a workspace. */
export interface WorkspaceSkillRef {
  skill_id: string;
  name: string;
  description: string;
}

/** The platform-wide workspace knowledge settings. */
export interface WorkspaceKnowledgeConfig {
  /** Share of the model's context window sources may fill inline (5-40). */
  sources_context_percent: number;
  /** Minutes a filed conversation stays idle before it is indexed (5-1440). */
  conversation_idle_minutes: number;
}

/** Where the asset seed reads more brand packs: a GitHub folder and the integration reading it. */
export interface BrandAssetSourceConfig {
  /** `https://github.com/<owner>/<repo>[/tree/<ref>[/<path>]]`; null or empty clears the source. */
  url: string | null;
  /** The connected GitHub integration whose token reads the folder. */
  integration_id: string | null;
}

/** What one download of the source read, and what the seed then uploaded. */
export interface BrandAssetSourceSync {
  packs: string[];
  /** Packs of the folder the platform also ships: its own are used. */
  shadowed: string[];
  error: string | null;
  assets_uploaded: number;
}

/** `GET /settings/brand-asset-source`; `sync` is set by a save or a sync. */
export interface BrandAssetSource extends BrandAssetSourceConfig {
  integration_name: string | null;
  sync: BrandAssetSourceSync | null;
}

// --- XTM Hub registration (Settings > Platform, home dashboard pill) ---

export type XtmHubRegistrationStatus =
  | "unregistered"
  | "registered"
  | "lost_connectivity"
  | "needs_reregistration";

/** `GET /xtm-hub/registration` (admin): never the platform token. */
export interface XtmHubRegistration {
  status: XtmHubRegistrationStatus;
  hub_url: string | null;
  default_hub_url: string;
  registered_at: string | null;
  last_check_at: string | null;
  /** What the Hub last answered: `active`, `inactive`, `unreachable` or `refused`. */
  last_check_result: string | null;
  /** The public values the Hub's register page is opened with. */
  register_params: {
    platform_id: string;
    platform_version: string;
    platform_identifier: string;
  };
}

/** `GET /xtm-hub/registration/status` (any user). */
export interface XtmHubRegistrationStatusInfo {
  status: XtmHubRegistrationStatus;
  hub_url: string | null;
  /** The backend's `XTM_HUB_DEFAULT_URL`: the XTM Hub the product switcher links to. */
  default_hub_url: string;
}

// --- Agent accounts ---------------------------------------------

/** `on_behalf`: the agent answers for a person. `ai_user`: the account is the agent. */
export type AgentAccountMode = "on_behalf" | "ai_user";
export type AgentAccountResponseMode = "auto" | "approval" | "shadow";
/** A mailbox reads them as an email to the account alone, and one with other recipients. */
export type AgentAccountConversationType = "direct" | "group" | "channel";
/** What a provider's conversations are: chats, or the emails of a mailbox. */
export type AgentAccountProviderKind = "chat" | "mailbox";

/** What a mailbox files on each email the agent triaged (`services/agent_accounts/triage.py`). */
export interface AgentAccountMailboxActions {
  flag_needs_owner: boolean;
  notify_needs_owner: boolean;
  mark_read_replied: boolean;
  mark_read_no_reply: boolean;
  /** Outlook categories; empty: none. */
  category_replied: string;
  category_needs_owner: string;
  category_no_reply: string;
}

/** A conversation or a person a rule targets: an id, and what to show for it. */
export interface AgentAccountTarget {
  id: string;
  name?: string;
  email?: string;
  type?: AgentAccountConversationType;
  team_id?: string;
}

/** One rule, as the engine stores it (`services/agent_accounts/rules.py`). */
export interface AgentAccountRule {
  id: string;
  name: string;
  enabled: boolean;
  action: "respond" | "ignore";
  conversation_types: AgentAccountConversationType[];
  conversations: AgentAccountTarget[];
  senders: AgentAccountTarget[];
  /** XTM One groups whose members this rule answers, besides the people named. */
  groups: AgentAccountTarget[];
  exclude_senders: AgentAccountTarget[];
  trigger: "mention" | "any";
  reply: "always" | "decide";
  continue_threads: boolean;
  response_mode: AgentAccountResponseMode | null;
  ai_label: boolean | null;
  grace_period_seconds: number | null;
  instructions: string;
  /** What the agent may use for the messages this rule answers; null = everything it has. */
  access: AgentAccountRuleAccess | null;
}

/** The capabilities a rule leaves the agent: each list a subset of the agent's own, null = all of them. */
export interface AgentAccountRuleAccess {
  integrations: string[] | null;
  knowledge_base_ids: string[] | null;
  mcp_server_ids: string[] | null;
  builtin_tool_ids: string[] | null;
  custom_tool_ids: string[] | null;
  skill_ids: string[] | null;
  memory: "on" | "off";
}

interface AgentAccountActiveHoursWindow {
  /** ISO weekdays, 1 (Monday) to 7 (Sunday). */
  days: number[];
  start: string;
  end: string;
}

export interface AgentAccountActiveHours {
  timezone: string;
  mode: "inside" | "outside";
  windows: AgentAccountActiveHoursWindow[];
}

/** What keeps an agent account from hearing conversations (`services/agent_accounts/problems.KINDS`). */
export type AgentAccountSyncProblemKind =
  | "integration"
  | "reauthorize"
  | "operated"
  | "no_public_address"
  | "unreachable"
  | "refused"
  | "taken_by_account"
  | "taken_elsewhere"
  | "not_found"
  | "not_subscribed"
  | "throttled"
  | "unavailable"
  | "error"
  | "reactions_refused";

/** A conversation a sync problem affects: every chat of the account, a mailbox's inbox, one channel, or every channel of a team. */
export interface AgentAccountSyncTarget {
  covers: "chats" | "inbox" | "channel" | "team";
  name: string | null;
}

export interface AgentAccountSyncProblem {
  kind: AgentAccountSyncProblemKind;
  count: number;
  /** The permission a refused subscription needs. */
  permission: string | null;
  /** The agent account of the same owner, or the host of the other address, holding the conversation. */
  holder: string | null;
  targets: AgentAccountSyncTarget[];
  /** The provider's own words, for the technical details. */
  details: string[];
  /** The platform's own reasons, as codes worded from the catalogs; absent on reports stored before. */
  notes?: AgentAccountSyncNote[];
}

/** A reason the platform words itself: a `SYNC_PROBLEM_NOTES` code and the values it reads. */
interface AgentAccountSyncNote {
  code: string;
  values: Record<string, string | number>;
}

export interface AgentAccountSyncReport {
  checked_at: string | null;
  /** The subscriptions the rules need, and those of them the provider holds. */
  wanted: number;
  heard: number;
  problems: AgentAccountSyncProblem[];
}

export interface AgentAccount {
  id: string;
  user_id: string;
  owner_name: string | null;
  name: string;
  description: string | null;
  provider: string;
  provider_label: string | null;
  integration_id: string | null;
  integration_name: string | null;
  agent_id: string | null;
  agent_name: string | null;
  mode: AgentAccountMode;
  enabled: boolean;
  response_mode: AgentAccountResponseMode;
  ai_label_enabled: boolean;
  ai_label_text: string | null;
  ai_label_preview: string;
  ai_label_position: "prefix" | "suffix";
  grace_period_seconds: number;
  owner_takeover_minutes: number;
  max_replies_per_hour: number;
  /** Agent turns per hour, every conversation together; 0 = no limit. */
  max_turns_per_hour: number;
  /** The people a reply may reach besides the sender; 0 = no limit. */
  max_reply_recipients: number;
  context_messages: number;
  active_hours: AgentAccountActiveHours | null;
  instructions: string | null;
  rules: AgentAccountRule[];
  /** The setting in effect, the defaults when never set. */
  reaction_feedback: ReactionFeedbackSetting;
  /** The owner is notified of each reply the account posts on its own. */
  notify_on_send: boolean;
  /** In effect, the defaults when never set; read on a mailbox only. */
  mailbox_actions: AgentAccountMailboxActions;
  /** Where the signature of its replies comes from. */
  signature_source: AgentAccountSignatureSource;
  signature_integration_id: string | null;
  signature_integration_name: string | null;
  account_external_id: string | null;
  account_display_name: string | null;
  account_email: string | null;
  transport: "push" | "poll" | "mixed" | "none";
  status: "active" | "paused" | "error" | "setup";
  /** The last failure of a reply, or why the account was paused. */
  last_error: string | null;
  /** What the last sync found; null before the first one. */
  sync_report: AgentAccountSyncReport | null;
  last_event_at: string | null;
  last_reply_at: string | null;
  pending_count: number;
  subscription_count: number;
  can_manage: boolean;
  created_at: string;
  updated_at: string;
}

/** The editable settings of `POST` / `PUT /agent-accounts`. */
interface AgentAccountWrite {
  name?: string;
  description?: string;
  integration_id?: string;
  agent_id?: string;
  enabled?: boolean;
  response_mode?: AgentAccountResponseMode;
  ai_label_enabled?: boolean;
  ai_label_text?: string;
  ai_label_position?: "prefix" | "suffix";
  grace_period_seconds?: number;
  owner_takeover_minutes?: number;
  max_replies_per_hour?: number;
  max_turns_per_hour?: number;
  max_reply_recipients?: number;
  context_messages?: number;
  active_hours?: AgentAccountActiveHours | null;
  instructions?: string;
  rules?: AgentAccountRule[];
  reaction_feedback?: ReactionFeedbackSetting;
  /** Notify the owner of each reply the account posts on its own. */
  notify_on_send?: boolean;
  /** What a mailbox files on each email the agent triaged. */
  mailbox_actions?: AgentAccountMailboxActions;
  /** Where the signature of its replies comes from; `""` clears the integration. */
  signature_source?: AgentAccountSignatureSource;
  signature_integration_id?: string;
}

/**
 * Where an account's reply signature comes from: the account's own (a
 * mailbox: copied from its sent mail), a signature integration read when each
 * reply is sent, or none (the mail server signs).
 */
export type AgentAccountSignatureSource = "own" | "integration" | "none";

/** What reactions on an agent's answers mean: validated by the server (422). */
export interface ReactionFeedbackSetting {
  enabled: boolean;
  positive: string[];
  negative: string[];
}

export interface CreateAgentAccountRequest extends AgentAccountWrite {
  name: string;
  provider: string;
  mode: AgentAccountMode;
  integration_id: string;
  agent_id: string;
}

export interface UpdateAgentAccountRequest extends AgentAccountWrite {
  mode?: AgentAccountMode;
  clear_active_hours?: boolean;
}

interface AgentAccountPreset {
  response_mode: AgentAccountResponseMode;
  ai_label_enabled: boolean;
  grace_period_seconds: number;
  owner_takeover_minutes: number;
  max_replies_per_hour: number;
  max_turns_per_hour: number;
  max_reply_recipients: number;
  context_messages: number;
  rules: AgentAccountRule[];
}

export interface AgentAccountProviderInfo {
  key: string;
  label: string;
  integration_provider: string;
  kind: AgentAccountProviderKind;
  capabilities: Record<string, boolean>;
  push_reachable: boolean;
  /** The two modes' starting points on this provider. */
  presets: Record<AgentAccountMode, AgentAccountPreset>;
}

export interface AgentAccountCatalog {
  providers: AgentAccountProviderInfo[];
  /** A chat provider's starting points (each provider carries its own). */
  presets: Record<AgentAccountMode, AgentAccountPreset>;
  default_labels: Record<AgentAccountMode, string>;
  /** What a mailbox files until its owner changes it. */
  mailbox_defaults: AgentAccountMailboxActions;
  /** The integration providers that hold email signatures. */
  signature_providers: string[];
}

/** A decision of the rule engine (`Decision.as_dict`). */
export interface AgentAccountDecision {
  respond: boolean;
  reason: string;
  rule_id: string | null;
  rule_name: string | null;
  must_reply: boolean;
  addressed: boolean;
  thread_continuation: boolean;
  response_mode: AgentAccountResponseMode;
  ai_label: boolean;
  grace_period_seconds: number;
  access?: AgentAccountRuleAccess | null;
  /** Who the sender was found to be; only when a rule names groups. */
  sender?: AgentAccountSenderMatch;
}

/** The XTM One user behind a sender's addresses, and which of the groups the rules name they are in. */
export interface AgentAccountSenderMatch {
  known: boolean;
  /** Every address the platform knows the sender by, the primary first. */
  emails: string[];
  groups: { id: string; name: string }[];
}

/** What the page groups an outcome under (`services/agent_accounts/activity.GROUPS`). */
export type AgentAccountActivityGroup =
  | "answered"
  /** The agent handed the message to the account's owner. */
  | "needs_owner"
  | "awaiting_approval"
  | "shadow"
  | "dropped"
  | "silent"
  | "waiting"
  /** Another pipeline of the agent (an assignment run, another of its accounts) took the mail first. */
  | "already_handled"
  | "left"
  | "failed";

/** One message the account heard and what it did with it. */
export interface AgentAccountLoggedDecision {
  id: string;
  at: string;
  updated_at: string;
  message_at: string | null;
  /** What became of the message: the reply's status when it produced one, else the decision's outcome or reason. */
  outcome: string;
  group: AgentAccountActivityGroup;
  reason: string | null;
  respond: boolean;
  /** Went through a grace period before its outcome. */
  waited: boolean;
  conversation_type: AgentAccountConversationType;
  conversation_id: string;
  conversation_name: string | null;
  sender_id: string | null;
  sender_name: string | null;
  preview: string;
  mentions_account: boolean;
  addressed: boolean;
  thread_continuation: boolean;
  rule_id: string | null;
  rule_name: string | null;
  sender: AgentAccountSenderMatch | null;
  /** The agent's own line on its triage of the message. */
  note: string | null;
  /** Who took a message another pipeline of the agent answers (`already_handled`). */
  holder_kind?: "assignment" | "agent_account" | null;
  /** The agent account that took it, when `holder_kind` is `agent_account`. */
  holder_account_id?: string | null;
  reply_id: string | null;
  reply_status: AgentAccountReply["status"] | null;
  reply_text: string | null;
  reply_error: string | null;
  /** `reply`: a message the account heard; `proactive`: a message the agent sent on its own. */
  kind?: AgentAccountSentKind;
  /** What produced a proactive send; null for a message the account heard. */
  origin?: AgentAccountSendOrigin | null;
}

/** A reply to a message the account heard, or a message the agent sent through it on its own. */
export type AgentAccountSentKind = "reply" | "proactive";

/** What produced a proactive send: the tool, the run's source, agent, conversation, run or assignment, the recipients. */
export interface AgentAccountSendOrigin {
  tool?: string;
  source?: string;
  agent_id?: string;
  conversation_id?: string;
  run_id?: string;
  assignment_id?: string;
  recipients?: string[];
}

export interface AgentAccountDecisionPage {
  items: AgentAccountLoggedDecision[];
  total: number;
}

/** The filters of the activity log (`GET /agent-accounts/{id}/decisions`). */
export interface AgentAccountDecisionFilters {
  /** Any of these outcome groups (repeated `group`). */
  groups?: AgentAccountActivityGroup[];
  /** Any of these kinds of conversation (repeated `conversation_type`). */
  conversationTypes?: AgentAccountConversationType[];
  conversationId?: string;
  /** A sender's platform id, or the name of one the platform named only. */
  sender?: string;
  q?: string;
  /** The column the server orders by; newest first when omitted. */
  sort?: "date" | "conversation" | "sender";
  direction?: "asc" | "desc";
}

export interface AgentAccountReplyFilters {
  /** Any of these statuses (repeated `status`). */
  status?: AgentAccountReply["status"][];
  kind?: AgentAccountSentKind;
  q?: string;
  /** The column the server orders by; newest first when omitted. */
  sort?: "date" | "conversation";
  direction?: "asc" | "desc";
}

export interface AgentAccountReplyPage {
  items: AgentAccountReply[];
  total: number;
}

export type AgentAccountActivityPeriod = "24h" | "7d" | "30d";

/** What an account did over a period (`GET /agent-accounts/{id}/activity`). */
export interface AgentAccountActivity {
  period: AgentAccountActivityPeriod;
  bucket: "hour" | "day";
  timezone: string;
  since: string;
  until: string;
  heard: number;
  /** Messages a rule answers, whatever the agent then did. */
  for_agent: number;
  groups: Record<AgentAccountActivityGroup, number>;
  outcomes: Record<string, number>;
  left_reasons: Record<string, number>;
  by_conversation_type: Partial<Record<AgentAccountConversationType, Record<AgentAccountActivityGroup, number>>>;
  /** Bucket starts in the requested time zone, without an offset. */
  timeline: { at: string; counts: Record<AgentAccountActivityGroup, number> }[];
  conversations: {
    conversation_id: string;
    conversation_name: string | null;
    conversation_type: AgentAccountConversationType;
    /** Someone who wrote there besides the account, to name a direct chat. */
    with_name: string | null;
    heard: number;
    for_agent: number;
    answered: number;
  }[];
  senders: { sender: string; sender_name: string | null; heard: number; for_agent: number; answered: number }[];
  rules: { rule_id: string; rule_name: string | null; matched: number; answered: number; silent: number }[];
  replies: Partial<Record<AgentAccountReply["status"], number>>;
  median_answer_seconds: number | null;
  median_approval_seconds: number | null;
  ratings: { positive: number; negative: number };
}

export interface AgentAccountSimulateRequest {
  mode: AgentAccountMode;
  /** Whose starting points fill what the draft leaves out. */
  provider?: string;
  settings: AgentAccountWrite;
  event: {
    conversation_type: AgentAccountConversationType;
    conversation_id?: string;
    sender_id?: string;
    sender_email?: string | null;
    text: string;
    mentions_account: boolean;
    from_account?: boolean;
    from_application?: boolean;
  };
  state: {
    thread_active: boolean;
    owner_active: boolean;
    replies_last_hour: number;
    /** Empty: the groups of the XTM One user whose email the event carries. */
    sender_group_ids?: string[];
  };
}

export interface AgentAccountSimulateResponse {
  decision: AgentAccountDecision;
  listening: {
    chats: boolean;
    channels: { id: string; mentions_only: boolean }[];
    all_channels: boolean;
    all_channels_mentions_only: boolean;
  };
  sender?: AgentAccountSenderMatch | null;
}

export interface AgentAccountReply {
  id: string;
  account_id: string;
  account_name: string | null;
  /** The account's provider, which says what its conversations are. */
  provider: string | null;
  status: "pending" | "sent" | "rejected" | "expired" | "failed" | "shadow";
  conversation_type: AgentAccountConversationType;
  conversation_name: string | null;
  sender_name: string | null;
  sender_email: string | null;
  inbound_text: string;
  draft_text: string | null;
  sent_text: string | null;
  rule_name: string | null;
  ai_label: boolean;
  error: string | null;
  decided_at: string | null;
  created_at: string;
  /** Ratings of the posted answer, reactions included. */
  rating_positive: number;
  rating_negative: number;
  /** `reply` answers a message the account heard; `proactive` was sent on the agent's own. */
  kind?: AgentAccountSentKind;
  /** What produced a proactive send; null for a reply. */
  origin?: AgentAccountSendOrigin | null;
}

/** The account an integration is signed in as, and the agent account already operating it. */
export interface AgentAccountPickerIdentity {
  external_id: string;
  display_name: string;
  email: string | null;
  /** The caller's own agent account operating it; another user's is never named. */
  operated_by_account_id: string | null;
  operated_by_account_name: string | null;
  operated_by_other_user: boolean;
}

export interface AgentAccountPickerConversation {
  id: string;
  name: string;
  type: AgentAccountConversationType;
  team_id: string | null;
}

export interface AgentAccountPickerPerson {
  id: string;
  name: string;
  email: string | null;
}

// ---------------------------------------------------------------------------
// Video Studio - backend: app/services/video_studio, api/v1/video_studio.py
// ---------------------------------------------------------------------------

export type VideoJobStatus = "pending" | "running" | "ready" | "failed" | "cancelled";
export type VideoRenderQuality = "draft" | "standard" | "high" | "ultra";
/** A saved session is `active` or `expired` by the last probe of its start URL, never by its cookies. */
export type BrowserSessionStatus =
  | "none"
  | "unverified"
  | "active"
  | "expired"
  | "awaiting_mfa"
  | "signing_in"
  | "failed";

/** The music a render played: `none`, a built-in `track`, an AI track (`music`, with the file it played) or an uploaded `file`. */
interface VideoRenderMusic {
  kind: "none" | "track" | "music" | "file";
  ref?: string;
  file_id?: string;
}

export interface VideoRender {
  id: string;
  project_id: string;
  revision: number;
  quality: VideoRenderQuality;
  status: VideoJobStatus;
  progress: number;
  stage: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  duration_ms: number | null;
  size_bytes: number | null;
  file_id: string | null;
  poster_file_id: string | null;
  captions_file_id: string | null;
  /** The captions are in the frame: the player leaves the WebVTT track off. */
  captions_burned_in: boolean;
  notes: string[];
  /** Kept out of the render retention by a manager. */
  keep: boolean;
  /** Who asked for it. */
  user_id: string | null;
  error: string | null;
  /** The newer render of the same video that cancelled this one when it started. */
  superseded_by_id?: string | null;
  /** A preview of this scene - a 720p draft, or 1080p (`standard`) - never what the video plays; `null` for a whole render. */
  scene_id?: string | null;
  /** A scene preview's first, middle and last frames. */
  frame_file_ids?: string[];
  /** Where each scene takes over and its name; empty for a scene preview and older renders. */
  chapters?: VideoChapter[];
  /** The music it played: its source and, for an AI track, the file; `null` for older renders. */
  music?: VideoRenderMusic | null;
  /** What its audio and picture checks found; `null` for renders made before them. */
  checks?: VideoRenderChecks | null;
  /** Started past its quality gate (`force`). */
  forced?: boolean;
  /** The times it stood still and was resumed in place, its finished frames kept. */
  stalls?: VideoRenderStall[];
  /** When an unfinished whole render should end; `null` for a scene preview or a finished render. */
  eta?: VideoRenderEta | null;
  created_at: string | null;
  started_at?: string | null;
  /** When it last moved forward: a render standing still too long is resumed in place, then failed. */
  progress_at?: string | null;
  finished_at: string | null;
  /** Render lists: the render the video plays - its cards, its chat card, its public link. */
  is_current?: boolean;
  /** Render lists: what keeps it from the retention; `null` in progress, or past its days (the next night deletes it). */
  kept_by?: VideoRenderKeptBy | null;
  /** Render lists: when the retention deletes it; `null` when it will not. */
  expires_at?: string | null;
}

/** What keeps a render from the retention (`render_retention.kept_by`). */
type VideoRenderKeptBy = "latest_ready" | "public_link" | "retention" | "author";

/**
 * When an unfinished whole render should end (`render_preflight.eta_of`): as its pre-flight estimated, then
 * at the pace its frames measured once a tenth of them are drawn (`measured`).
 */
export interface VideoRenderEta {
  source: "preflight" | "measured";
  /** `null` while no renderer has started it: `expected_seconds` is then what it should take. */
  at: string | null;
  expected_seconds: number | null;
  /** `measured`: its frames' end, before its checks, and the frames the pace was measured on. */
  frames_at?: string | null;
  drawn?: number | null;
  frames?: number | null;
  per_second?: number | null;
}

/** A time a render stood still (`render_process.record_stall`): where it was and what a resume kept. */
interface VideoRenderStall {
  at: string;
  progress: number;
  stage: string | null;
  drawing: string[];
  /** How long it stood still before it was stopped. */
  seconds: number;
  /** The frames its finished segments held, which the resume did not draw again. */
  frames_kept: number;
  /** `false` for the stall it was failed on, past its resumes. */
  resumed: boolean;
}

/** One finding of a render's checks (`render_checks.Finding`): its time in the render's MP4. */
interface VideoRenderFinding {
  kind:
    | "voice_segment"
    | "voice_fallback"
    | "transcript"
    | "pronunciation"
    | "repeated_sentence"
    | "stutter"
    | "frozen"
    | "brightness_dip"
    | "double_image"
    | "near_empty"
    | "static_flicker"
    | "music_level"
    | "audio_mix"
    | "insert_foreground"
    | "insert_padded"
    | "logo_lost"
    | "surface_lost"
    | "insert_missing"
    | "insert_colour_shift"
    | "logo_duplicated"
    | "insert_alignment"
    | "insert_jitter"
    | "insert_residual_key"
    | "insert_flicker"
    | "insert_soft"
    | "insert_halo"
    | "insert_rim"
    | "insert_cropped"
    | "insert_band_key_colour"
    | "insert_flatness"
    | "insert_dropped"
    | "insert_gap"
    | "insert_measurement_lost"
    | "insert_through_foreground"
    | "insert_ghost"
    | "insert_matte_halo"
    | "insert_matte_lag"
    | "insert_occluded"
    | "key_spill_outside_quad"
    | "duplicate_logo"
    | "invented_text";
  /** `fail` fails a whole render (unless forced); `warn` leaves it ready. */
  severity: "fail" | "warn";
  at: number;
  end: number | null;
  scene_id: string | null;
  /** The backend's English words: what shows when `code` is absent or unknown to this build. */
  message: string;
  /** The translatable message's key and values (`findingMessage` in `video-format.ts`). */
  code?: string | null;
  params?: Record<string, string | number> | null;
}

/** A render's audio and picture checks (`render_checks.Checks`). */
interface VideoRenderChecks {
  status: "passed" | "failed" | "findings" | "skipped";
  findings: VideoRenderFinding[];
  notes?: string[];
  /** The lexicon's terms in the final voice. */
  pronunciation?: VideoRenderPronunciation;
  /** The audio check's outcome; its `music` section says how loud the music plays. */
  audio?: { status?: string; music?: VideoRenderMusicLevel };
}

/** How loud a render's music plays (`music_level.MusicLevel.summary`): LUFS on the master's scale. */
interface VideoRenderMusicLevel {
  status: "passed" | "findings";
  /** Where no one speaks; `null` for a silent track. */
  integrated_lufs: number | null;
  /** Ducked, under the narration; `null` without one. */
  under_voice_lufs: number | null;
  voice_lufs: number | null;
  below_voice_db: number | null;
  /** The longest stretch under `quiet_lufs`, and where it is in the MP4. */
  quiet_seconds: number;
  quiet_from: number | null;
  quiet_to: number | null;
  quiet_lufs: number;
  volume: number;
  duck_db: number;
  master_gain_db: number;
  /** A fading bed's decay under the closing card: a quiet stretch starting in the video's last
   * `closing_seconds`, by design and never a finding; absent when there is none. */
  decay_seconds?: number;
  decay_from?: number;
  decay_to?: number;
  closing_seconds?: number;
}

/** A narration's takes a render does not play that failed on a term, per scene. */
interface VideoRenderTermFailedTakes {
  scene_id: string;
  times: number;
  heard: string[];
}

/** Each pronunciation lexicon term of a render's voice, as heard back, with the takes it failed in. */
interface VideoRenderPronunciation {
  status: "passed" | "failed" | "skipped";
  reason?: string;
  terms?: {
    term: string;
    occurrences: number;
    passed: number;
    heard: string[];
    attempts_failed?: VideoRenderTermFailedTakes[];
  }[];
}

/** How a term is matched in a narration (`spec.Pronunciation.match`). */
export type PronunciationMatch = "exact" | "word" | "case_insensitive";
/** What reads a pronunciation entry (`spec.Pronunciation.apply_to`). */
export type PronunciationApplyTo = "voice" | "captions" | "both";

/** A pronunciation lexicon entry: a video's `voiceover.pronunciations`, or the platform's. */
export interface PronunciationEntry {
  text: string;
  say: string;
  ipa?: string | null;
  match?: PronunciationMatch;
  apply_to?: PronunciationApplyTo;
}

/** The platform's pronunciation lexicon, its newest version (`GET /video-studio/pronunciation-lexicon`). */
export interface PronunciationLexicon {
  /** `null` before its first version. */
  version: number | null;
  entries: PronunciationEntry[];
  note: string | null;
  created_at: string | null;
  created_by: string | null;
  can_edit: boolean;
}

/** One version of the platform's pronunciation lexicon. */
export interface PronunciationLexiconVersion {
  version: number;
  created_at: string | null;
  created_by: string | null;
  note: string | null;
  /** How many entries it holds. */
  entries: number;
}

/** A term spoken as a video's voice-over says it (`POST /video-studio/pronunciations/speak`). */
export interface SpokenPronunciation {
  text: string;
  audio_url: string;
  seconds: number | null;
  /** `ssml_phoneme`, `ipa_instruction`, `ssml_sub`, `provider_lexicon` or `text_rewrite` (joined with `+`); `null` when no entry applies. */
  mechanism: string | null;
  /** The exact text sent to the speech model. */
  sent: string;
  lexicon_version: number | null;
}

/** Why the quality gate refused a render (409 `render_gate`); the editor offers to force it. */
export interface VideoRenderGateReason {
  /** `preflight`: what the worker that draws it cannot hold (its memory, the stall window, the deadline). */
  code: "media_not_ready" | "quality" | "narration_failed" | "open_findings" | "scene_changed" | "preflight";
  /** The English sentence the agent reads; the editor's fallback when it cannot build the reason from `params`. */
  message: string;
  scene_id: string | null;
  /** `open_findings`: what the editor builds the reason from in the reader's language (`renderGateReasonText`). */
  params?: {
    count: number;
    quality: string;
    created_at: string | null;
    findings: { kind: string; at: number | null }[];
  } | null;
}

/** Where a queued scene preview stands in its lane of the render queue (`jobs.QueuePlace`). */
interface VideoRenderQueuePlace {
  /** From 1: the next one a free slot of its lane takes. */
  position: number;
  ahead: number;
  running: number;
  per_worker: number;
  /** The lane it waits in: scene previews have slots of their own beside the whole renders. */
  lane?: "render" | "preview";
  /** A queued scene preview: the previews it waits behind - being made, and dealt before it - by video. */
  videos_ahead?: VideoRenderQueueVideo[];
  /** A queued scene preview: the workers that draw previews and their slots, as the running workers said. */
  preview_workers?: number;
  preview_slots?: number;
  /** A queued scene preview: the seconds it is expected to wait at the lane's pace, and the median draw time of the
   * last previews drawn it is estimated with; `null` when not known. */
  expected_wait_seconds?: number | null;
  median_draw_seconds?: number | null;
}

/** One video whose scene previews a queued preview waits behind (`preview_lane.VideoAhead`). */
interface VideoRenderQueueVideo {
  video_id: string;
  title: string;
  previews: number;
  being_made: number;
  /** The queued preview's own video. */
  own: boolean;
}

/** A render just started: the video's renders it replaced, now cancelled. */
/** A render of an older revision still in progress, which a start asks before cancelling (409 `render_replace`). */
interface VideoRenderReplaced {
  id: string;
  revision: number;
  quality: VideoRenderQuality;
  progress: number;
}

export interface VideoRenderReplaceQuestion {
  /** The revision the new render draws. */
  revision: number;
  renders: VideoRenderReplaced[];
}

export interface VideoRenderStarted extends VideoRender {
  superseded_render_ids: string[];
  /** The render of this revision at this quality already in progress, answered instead of a twin. */
  reused?: boolean;
  /** The render of this revision at this quality no worker resumed, queued again as the same job from its kept segments. */
  relaunched?: boolean;
  /** The ready render that already covers this revision, answered with nothing queued: the revision it was drawn for. */
  covered_from?: number | null;
  /** The ready render that drew this revision's scenes as they are now, drawn by an older release, so drawn again: why. */
  redrawn?: string | null;
  /** A scene preview's place in the render queue. */
  queue?: VideoRenderQueuePlace | null;
}

/** The render retention: the days a render nothing else keeps is kept after it ended; `0` keeps it for good. */
export interface VideoRenderRetention {
  failed_cancelled_days: number;
  previews_days: number;
  superseded_ready_days: number;
}

/** `GET /video-studio/render-retention`: the policy, and whether the reader may change it (administrators). */
export interface VideoRenderRetentionSettings extends VideoRenderRetention {
  can_edit: boolean;
}

/** What a render cleanup selects (`render_cleanup.SELECTORS`), the first the default. */
export type VideoRenderCleanupSelector = "all_but_latest_ready" | "failed_and_cancelled" | "previews" | "all";

/** `POST /video-studio/projects/{id}/renders/cleanup`: `delete_video_renders`' arguments. */
export interface VideoRenderCleanupRequest {
  render_ids?: string[];
  select?: VideoRenderCleanupSelector;
  keep_quality?: VideoRenderQuality;
  before?: string;
  dry_run?: boolean;
  confirm?: boolean;
}

/** Why a cleanup does not delete a render it considered (`render_cleanup.REASONS`). */
export type VideoRenderCleanupReason =
  | "in_progress"
  | "not_yours"
  | "public_link"
  | "handing_over"
  | "latest_ready"
  | "kept_by_hand"
  | "busy";

export interface VideoRenderCleanupItem {
  render_id: string;
  quality: VideoRenderQuality;
  revision: number;
  status: VideoJobStatus;
  size_bytes: number;
  scene_id?: string;
  action: "delete" | "keep" | "refuse";
  reason?: VideoRenderCleanupReason;
  why?: string;
}

interface VideoRenderCleanupServed {
  render_id: string;
  quality: VideoRenderQuality;
  revision: number;
  size?: string;
  size_bytes?: number;
}

/** What a cleanup did, or would do (`dry_run`): the same list either way. */
export interface VideoRenderCleanupResult {
  video_id: string;
  dry_run: boolean;
  select: VideoRenderCleanupSelector | null;
  keep_quality: VideoRenderQuality | null;
  renders: VideoRenderCleanupItem[];
  unknown_render_ids: string[];
  freed?: { renders: number; bytes: number };
  would_free?: { renders: number; bytes: number };
  serves: {
    current: VideoRenderCleanupServed | null;
    current_changed: boolean;
    public_link: boolean;
    latest_ready: Partial<Record<VideoRenderQuality, VideoRenderCleanupServed>>;
  };
}

/** A video's tier for the reader: `config` may edit, render and share. */
type VideoPermission = "owner" | "config" | "read_only";

/**
 * A video's public link as its managers read it -
 * `GET /video-studio/projects/{id}/public-link`, the deck's shape
 * (`PresentationPublicLink`) with a download option instead of the PDF.
 */
export interface VideoPublicLink {
  available: boolean;
  enabled: boolean;
  path: string | null;
  url: string | null;
  created_at: string | null;
  created_by_name: string | null;
  /** Its visitors may also download the MP4 and its captions. Off on a new link. */
  allow_download: boolean;
}

/**
 * A video as its public link shows it - `GET /public/videos/{token}`:
 * the current render as the player needs it, nothing about who made it.
 */
export interface PublicVideo {
  title: string;
  rendered: boolean;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  has_poster: boolean;
  has_captions: boolean;
  captions_burned_in: boolean;
  rendered_at: string | null;
  download: boolean;
  size_bytes: number | null;
  chapters?: VideoChapter[];
}

/** One chapter of a render (`timeline.chapters`): where its scene takes over, in seconds. */
export interface VideoChapter {
  start: number;
  /** The scene's type as authored: the player words a chapter with no `title` by it. */
  type: string;
  title: string | null;
}

interface VideoSceneSummary {
  id?: string;
  type: "title" | "statement" | "recording" | "slide" | "figure" | "clip" | "image" | "outro" | "motion";
  title?: string;
  narration?: string;
  take_id?: string;
  clip_id?: string;
  presentation_id?: string;
  [key: string]: unknown;
}

interface VideoTemplateRef {
  id: string;
  name: string;
  kind: string;
}

/** What a video template is made for (`templates.TEMPLATE_PURPOSES`). */
export type VideoTemplatePurpose = "product_demo" | "brand_film" | "event" | "launch" | "explainer" | "social";

/** A video template as the picker reads it (`templates.template_summary`). */
export interface VideoTemplateSummary {
  id: string;
  name: string;
  description: string | null;
  kind: string;
  is_default: boolean;
  is_builtin: boolean;
  supported_schemes: ("dark" | "light")[];
  aspect_ratios: string[];
  purposes: VideoTemplatePurpose[];
  locked: string[];
  intro: boolean;
  outro: boolean;
  background: string | null;
  /** How its videos move (`theme.motion` it presets); `null` for the platform's own. */
  motion?: VideoMotionStyle | null;
  /** The corner it draws its mark in on a video that leaves `theme.watermark` unset; `null` for none. */
  watermark?: "top_left" | "top_right" | "bottom_left" | "bottom_right" | null;
  /** The art direction of its AI shots, or `null` when it declares none. */
  shots: VideoShotDirection | null;
  guidance: string | null;
  swatches: string[];
  series: string[];
  heading_font: string | null;
}

/** How a video moves (`spec.MotionStyle`, `theme.motion`). */
export interface VideoMotionStyle {
  personality?: "precise" | "calm" | "energetic" | "cinematic" | "playful";
  speed?: number;
  title?: "lines" | "words" | "letters";
  logo?: "rise" | "fade" | "scale" | "mask";
  accent?: "none" | "sweep";
  beat_sync?: "off" | "cuts" | "all";
  motion_blur?: "off" | "on";
}

/** How every AI shot of a video wearing a template looks (`templates.ShotDirection`). */
export interface VideoShotDirection {
  /** The look added to the prompt of a shot drawn from text. */
  style?: string;
  /** What no shot shows: added to the negative prompt of a model that takes one; a shot showing one says so. */
  avoid?: string[];
}

export interface VideoProject {
  id: string;
  user_id: string | null;
  title: string;
  /** The chat that made it, when an assistant did. */
  conversation_id?: string | null;
  /** The spec as written: what edits change. */
  spec: { title: string; scenes: VideoSceneSummary[]; [key: string]: unknown };
  template: VideoTemplateRef | null;
  freestyle: boolean;
  /** Detail reads: the spec the template dresses, what renders. */
  effective_spec?: { title: string; scenes: VideoSceneSummary[]; [key: string]: unknown };
  /** Detail reads: ids of the scenes the template adds (its intro / outro). */
  template_scenes?: string[];
  /** Detail reads: what the template changed against the spec. */
  template_notes?: string[];
  revision: number;
  scene_count: number;
  estimated_duration_ms: number | null;
  latest_render: VideoRender | null;
  /** The current render: what the video plays (a manager may pick an older one). */
  ready_render: VideoRender | null;
  is_company_managed: boolean;
  shared_group_ids: string[];
  shared_group_permissions: Record<string, string>;
  shared_user_ids: string[];
  shared_user_permissions: Record<string, string>;
  access_restricted: boolean;
  /** The reader's tier. */
  permission: VideoPermission | null;
  owner_name: string | null;
  /** A public link plays it for anyone, now; `false` while public links are off. */
  has_public_link: boolean;
  /** Detail reads: the render retention the renders are kept under. */
  render_retention?: VideoRenderRetention;
  /** Detail reads: the version of the quality check - a revision's report computed under another is read again. */
  quality_version?: number;
  can_manage: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface VideoTake extends VideoRetention {
  id: string;
  user_id: string | null;
  profile_id: string | null;
  name: string;
  status: VideoJobStatus;
  progress: number;
  stage: string | null;
  error: string | null;
  duration_ms: number | null;
  viewport: [number, number] | null;
  pixel_ratio: number | null;
  fps: number | null;
  captured_frames: number | null;
  size_bytes: number | null;
  file_id: string | null;
  poster_file_id: string | null;
  contact_sheet_file_id: string | null;
  steps: number;
  created_at: string | null;
  finished_at: string | null;
}

/** A video a recording or an AI shot appears in (the library lists). */
export interface VideoAssetUse {
  id: string;
  title: string;
}

/** A recording or an AI shot to delete; `force` deletes it even though videos use it. */
export interface VideoMediaDeletion {
  id: string;
  force?: boolean;
}

/** The `detail` of a refused deletion of a medium a video uses (409 `media_in_use`). */
export interface VideoMediaInUse {
  code: "media_in_use";
  message: string;
  /** The videos the caller can open that use it. */
  videos: VideoAssetUse[];
  /** How many other videos use it. */
  others: number;
}

/** The media retention of a recording or an AI shot: `retention.retention_fields`. */
interface VideoRetention {
  /** Exempt from the retention. */
  keep: boolean;
  last_used_at: string | null;
  /** When the retention deleted its media (the row stays). */
  expired_at: string | null;
  /** When the retention will delete them; `null` when it will not (kept, or a video uses it). */
  expires_at: string | null;
  /** The videos it appears in that the reader can open (list reads). */
  used_in?: VideoAssetUse[];
  /** Whether any video uses it, one the reader cannot open included (media library reads). */
  in_use?: boolean;
}

export interface VideoClip extends VideoRetention {
  id: string;
  user_id: string | null;
  /** What the person calls it; `null` reads as its prompt. */
  name: string | null;
  provider: string;
  model: string;
  prompt: string;
  params: Record<string, unknown>;
  status: VideoJobStatus;
  progress: number;
  error: string | null;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  has_audio: boolean;
  file_id: string | null;
  poster_file_id: string | null;
  /** What it opened on: the frame kept from its recording, shot or video file, else its start image. */
  start_frame_file_id: string | null;
  /** Its own first, middle and last frames, kept once it is made. */
  first_frame_file_id?: string | null;
  mid_frame_file_id?: string | null;
  last_frame_file_id?: string | null;
  /** What those frames measure; `null` until it is made. */
  frame_qc?: VideoClipFrameQc | null;
  /** How its drifting camera was locked off, or why not; `null` when it was not asked. */
  stabilisation?: VideoClipStabilisation | null;
  /** Whether its composited logo shows more than once in a frame; `null` when no logo was composited. */
  logo_check?: VideoClipLogoCheck | null;
  /** Its logo drawn onto its subject wherever the take shows it; `null` without the tracked path. */
  logo_tracked?: VideoClipLogoTracked | null;
  /** Its text read against its start picture; `null` until it is checked. */
  text_check?: VideoClipTextCheck | null;
  /** The models it falls back to when a take fails its checks; `null` when it names none. */
  fallback?: VideoClipFallback | null;
  /** A ready shot generated again: being made or failed, its previous take playing meanwhile. */
  regeneration?: VideoClipRegeneration | null;
  /** Waiting in the queue while its provider cools: when it is asked. */
  waiting_for_provider_until?: string | null;
  created_at: string | null;
  finished_at: string | null;
}

/** `regeneration` of an AI shot: its file, poster and frames stay the previous take's meanwhile. */
interface VideoClipRegeneration {
  state: "running" | "failed";
  /** Why the last attempt failed; `null` while one runs. */
  reason: string | null;
  attempts_failed: number;
  previous_take_plays: boolean;
}

/** What a made AI shot's frames measure: SSIM from -1 to 1, the text match from 0 to 1. */
interface VideoClipFrameQc {
  /** Its first frame against the start frame it was asked from. */
  reference_ssim?: number;
  /** The share of the start picture's words its first frame still reads (with the text check). */
  reference_text_match?: number;
  /** Its last frame against its first: low when the camera or the scene moved. */
  motion_ssim?: number;
}

/** `logo_check` of an AI shot: its composited logo matched over sampled frames at its tracked size. */
interface VideoClipLogoCheck {
  status: "passed" | "duplicated";
  frames_sampled: number;
  /** The logo found twice, the frame it first shows so (index and seconds), and how many times. */
  label?: string;
  frame?: number;
  seconds?: number;
  count?: number;
  /** The places it matched, 0-1 boxes `[x0, y0, x1, y1]`. */
  boxes?: number[][];
  /** That frame, a file of the shot. */
  frame_file_id?: string;
}

/** `logo_tracked` of an AI shot: its logo drawn onto its subject in every frame the take shows it in. */
interface VideoClipLogoTracked {
  status: "drawn";
  /** The take's frames, how many got the logo, and that share (0-1). */
  frames: number;
  drawn_frames: number;
  share: number;
  /** How many instances of the subject carry it, one logo each. */
  instances: number;
  /** The frames the detector was shown, and how many of them showed the subject. */
  keyframes_sampled: number;
  keyframes_seen: number;
  labels: string[];
  subjects: string[];
  /** `native` when the model's own take failed the logo check and the logo was drawn onto the subject instead. */
  fell_back_from?: string;
}

/** `text_check` of an AI shot: its words against the ones its start picture reads clearly. */
interface VideoClipTextCheck {
  status: "passed" | "failed" | "skipped";
  /** The start picture's words compared: read above the confidence and height thresholds. */
  words_checked: string[];
  /** The start picture's words left out as unreadable there, with how surely they were read (0-1). */
  excluded: { word: string; confidence: number }[];
  /** Lettering the take shows that the start picture has at no confidence: reported, never a failure. */
  hallucinated: { word: string; frame: string }[];
  /** For a failure: the word misdrawn (as expected, as seen), the frame, and the crop of it (a file of the shot). */
  failure?: { word: string; seen: string; frame: string; crop_file_id?: string } | null;
}

/** `stabilisation` of an AI shot: its camera locked to its first frame, or why the raw take stayed. */
interface VideoClipStabilisation {
  applied: boolean;
  /** Why it was not applied: no drift, a camera that could not be followed, a crop too large, no gain. */
  reason?: string;
  /** `1 - SSIM` of the raw take's first and last frames, and of the stabilised take's. */
  raw_motion?: number;
  residual_motion?: number;
  /** The largest corner displacement corrected, as a share of the frame's diagonal. */
  max_drift?: number;
  /** The share of the frame's side the stabilised take keeps. */
  kept_share?: number;
  /** The frame the camera was lost on. */
  lost_at?: number;
  /** The un-stabilised take is kept beside the stabilised one, a file of the shot. */
  unstabilised_kept: boolean;
}

/** `fallback` of an AI shot: the models tried, in order, when a take fails a check of its frames. */
interface VideoClipFallback {
  models: string[];
  /** The models whose take failed, and why, oldest first. */
  tried: { model: string; provider: string | null; reason: string | null }[];
  /** The model of the take kept; `null` until one is ready. */
  made_by: string | null;
  fell_back: boolean;
}

/** `params.reference_video` of an AI shot: the video whose frame it started from. */
export interface VideoClipStartSource {
  kind: "recording" | "shot" | "file";
  id: string;
  file_id: string;
  name: string;
  /** The moment of the video; `null`: its last frame. */
  at_seconds: number | null;
  /** The recording's step whose result the frame shows. */
  step?: number;
  /** Its middle cut to the shot's aspect ratio. */
  crop?: "center";
  /** The frame kept as a file of the shot's conversation. */
  frame_file_id?: string;
}

/** A page of a library paged by the server: its rows, and how many match in all. */
export interface LibraryPage<T> {
  items: T[];
  total: number;
}

/** A page of a Video Studio library. */
export type VideoLibraryPage<T> = LibraryPage<T>;

/** A row of the media library (`GET /video-studio/assets`): a recording or an AI shot, by kind. */
export type VideoAsset = (VideoTake & { kind: "take" }) | (VideoClip & { kind: "clip" });

/** Which media the library lists. */
export type VideoAssetKind = "all" | "take" | "clip";

/** `used`: some video uses it, anyone's; `unused`: none does. */
export type VideoAssetUsage = "all" | "used" | "unused";

/** The filters of the media library, shared by a page and a bulk deletion by filters. */
export interface VideoAssetFilters {
  kind?: VideoAssetKind;
  /** Every word in the name or the prompt. */
  search?: string;
  usage?: VideoAssetUsage;
  /** Made at or after / at or before these instants (ISO). */
  date_from?: string;
  date_to?: string;
}

/** `GET /video-studio/assets`: the person's recordings and AI shots, a page at a time, newest first. */
export interface VideoAssetsQuery extends VideoAssetFilters {
  /** Only these (a video's own media, whatever their age). */
  ids?: string[];
  /** Leave these out (a library search beside a video's own media). */
  exclude?: string[];
  /** Only the media whose files have not expired: the ones a scene can use. */
  available?: boolean;
  /** The order: when made (default) or by title (the name, else the prompt). */
  sort?: VideoAssetSortKey;
  /** Default `desc`. */
  direction?: "asc" | "desc";
  limit: number;
  offset: number;
}

/** The orders of `GET /video-studio/assets`; each is the key of its column on the Media tab. */
export type VideoAssetSortKey = "created_at" | "title";

/** `POST /video-studio/assets/bulk-delete`: the media named, or every one the filters match. */
export type VideoAssetsBulkDelete =
  | { items: { kind: "take" | "clip"; id: string }[] }
  | ({ all_matching_filters: true } & VideoAssetFilters);

/** What a bulk deletion did: never a medium a video uses, which it keeps and counts. */
export interface VideoAssetsBulkDeleted {
  deleted: number;
  kept_in_use: number;
}

/** `GET /video-studio/projects`: the videos the user can open, a page at a time, the last changed first. */
export interface VideoProjectsQuery {
  /** Every word in the title, the template or the owner's name. */
  search?: string;
  /** The visibility segment (`lib/visibility.ts`); the server's [DEPRECATED] `visibility` alias is not sent. */
  vis?: "all" | "mine" | "shared" | "company";
  limit: number;
  offset: number;
  /** The order of the whole match (the server's default: `updated_at`, last changed first). */
  sort?: VideoProjectSortKey;
  direction?: "asc" | "desc";
  /** With a ready render or not (the server's default: every video). */
  render?: VideoProjectRenderFilter;
}

/** The orders `GET /video-studio/projects?sort=` offers (`_PROJECT_SORT_COLUMNS`). */
export type VideoProjectSortKey = "updated_at" | "created_at" | "title" | "scene_count";

/** The render states `GET /video-studio/projects?render=` filters on (`_PROJECT_RENDER_FILTERS`). */
export type VideoProjectRenderFilter = "rendered" | "not_rendered";

type BrowserMfaKind = "code" | "push_number" | "push";

interface BrowserProfileMfaChallenge {
  challenge_id: string;
  kind: BrowserMfaKind;
  expires_at: string | null;
  screenshot_file_id: string | null;
}

/** Where the start URL landed in the saved session, and why it reads signed in or not. */
interface BrowserProfileProbe {
  authenticated: boolean;
  url: string;
  reason: string;
  checked_at: string;
  /** Set when the application could not answer (a 5xx, a 429): the probe says nothing of the session. */
  http_status?: number | null;
}

/**
 * A browser profile's sign-in selectors and URL patterns by name; `keep_storage_keys` lists the storage keys its
 * session lives in, which a recording's page state reset keeps.
 */
export type BrowserProfileLoginConfig = Record<string, string | string[]>;

export interface BrowserProfile {
  id: string;
  user_id: string | null;
  name: string;
  description: string | null;
  start_url: string;
  login_url: string | null;
  allowed_hosts: string[];
  login_config: BrowserProfileLoginConfig;
  browser_options: Record<string, string>;
  username_hint: string | null;
  has_username: boolean;
  has_password: boolean;
  has_totp: boolean;
  /** Whether the stored TOTP secret can generate codes (base32 or otpauth:// URI); `vault`: read from a vault when used. */
  totp_secret_status: "none" | "valid" | "invalid" | "vault";
  /** The credentials read from a secret vault when used, by name. */
  vault_credentials: ("username" | "password" | "totp_secret")[];
  /** Where those credentials read (`locations.credentials[].entry` names the credential), and their last failed read. */
  vault_secrets: VaultSecretStatus | null;
  /** Whether the platform renews this session unattended (credentials + a valid TOTP secret, or a saved IdP session). */
  renews_by_itself: boolean;
  session_status: BrowserSessionStatus;
  session_saved_at: string | null;
  session_checked_at: string | null;
  last_probe_at: string | null;
  last_probe_result: BrowserProfileProbe | null;
  last_sign_in_message: string | null;
  mfa_challenge: BrowserProfileMfaChallenge | null;
  keepalive_minutes: number;
  sign_in_timeout_seconds: number;
  session_save_timeout_seconds: number;
  mfa_timeout_seconds: number;
  is_company_managed: boolean;
  shared_group_ids: string[];
  shared_user_ids: string[];
  can_manage: boolean;
  created_at: string | null;
  updated_at: string | null;
}

/** A profile write. Credentials are write-only: omit to keep, "" to clear. */
export interface BrowserProfileWrite {
  name: string;
  start_url: string;
  login_url?: string | null;
  allowed_hosts?: string[];
  login_config?: BrowserProfileLoginConfig | null;
  browser_options?: Record<string, string> | null;
  description?: string | null;
  keepalive_minutes?: number;
  sign_in_timeout_seconds?: number;
  session_save_timeout_seconds?: number;
  mfa_timeout_seconds?: number;
  username?: string;
  password?: string;
  totp_secret?: string;
  is_company_managed?: boolean;
}

/** The stage a sign-in failed at, with the last URL the platform's browser was on. */
type BrowserSignInStage =
  | "idp_form"
  | "mfa"
  | "mfa_totp_refused"
  | "redirect_chain"
  | "app_load"
  | "session_save";

/** One decision the sign-in took, for the operator to see where it went (and stalled). */
interface BrowserSignInTraceStep {
  stage: string;
  url: string;
  decision: string;
  at: string;
}

export interface BrowserSignInState {
  /** `unverified`: signed in, but the application did not answer the check of the session (a sign-in keeps it, a dry run saves nothing). */
  status: "running" | "active" | "mfa_required" | "failed" | "unverified";
  message: string;
  challenge_id: string | null;
  kind: BrowserMfaKind | null;
  number: string | null;
  screenshot_file_id: string | null;
  expires_at: string | null;
  profile_id: string | null;
  stage: BrowserSignInStage | null;
  last_url: string | null;
  probe: BrowserProfileProbe | null;
  /** The decisions the sign-in took (newest last), from a dry run or a failed sign-in. */
  trace: BrowserSignInTraceStep[] | null;
}

export type VideoMediaKind = "take" | "clip" | "render" | "music";
/** `start_frame`: what an AI shot opened on. */
export type VideoMediaAsset =
  | "video"
  | "poster"
  | "captions"
  | "contact_sheet"
  | "audio"
  | "start_frame"
  /** An AI shot's own frames, or a scene preview's. */
  | "first_frame"
  | "mid_frame"
  | "last_frame";

/** A built-in track: a royalty-free score the platform composes for each video. */
export interface VideoMusicTrack {
  id: string;
  name: string;
  mood: string;
  genre: string;
  bpm: number;
  description: string;
  /** The kinds of video it suits (the templates' purposes). */
  purposes: string[];
  /** Half a minute of its arc, to listen to. */
  audition_url: string;
}

/** An AI track: composed by a music model, any video of its owner plays it. */
export interface VideoMusic {
  id: string;
  user_id: string | null;
  /** What the person calls it; `null` reads as its prompt. */
  name: string | null;
  provider: string;
  model: string;
  prompt: string;
  params: Record<string, unknown>;
  status: VideoJobStatus;
  progress: number;
  error: string | null;
  duration_ms: number | null;
  file_id: string | null;
  created_at: string | null;
  finished_at: string | null;
  used_in?: VideoAssetUse[];
}

/** An AI track asked from the editor (`POST /video-studio/music`). */
export interface VideoMusicCreate {
  prompt: string;
  name?: string;
  model?: string;
  provider_id?: string;
  duration_seconds?: number;
  instrumental?: boolean;
  negative_prompt?: string;
  /** The video whose music it becomes, composed at its length. */
  project_id?: string;
}

/** What the editor plays as the music (`preview._music`): the bed the render prepares, and its ducking. */
export interface VideoPreviewMusic {
  /** `null` while there is nothing to play yet (an AI track being composed, failed, or no source). */
  url: string | null;
  /** What the bed is, whatever its token: the player keeps its source while it is the same. */
  key?: string | null;
  volume: number;
  /** The ducking envelope, `[seconds, gain]` points, linear between them; `null` without ducking. */
  duck: [number, number][] | null;
  source: "track" | "music" | "file" | null;
  state: { state: "music" | "failed" | "none"; music_id?: string; progress?: number; error?: string } | null;
}

/** One thing the audience would see go wrong: text covering text, past the frame, shrunk, read too fast. */
export interface VideoQualityFinding {
  /** The scene's index in the spec as written; null for a scene the template adds. */
  scene_index: number | null;
  scene_id: string | null;
  scene_type: string | null;
  label: string;
  kind: string;
  severity: "overloaded" | "dense" | "advisory";
  message: string;
  /** `content`: about the pictures and the sound, read from the media; `layout`: measured in the compositor. */
  group: "layout" | "content";
  /** The AI shot a content finding is about: a passing preview of every scene showing it clears it at the render gate. */
  clip_id?: string | null;
}

/** The quality check of a saved revision; `skipped` says why it did not run. */
export interface VideoQualityReport {
  scene_count: number;
  skipped: string | null;
  revision: number | null;
  findings: VideoQualityFinding[];
  /** What the check left out because the video declares it so (an ambient screen insert's text): never a finding. */
  unchecked?: string[];
  /** What the check says of the video as a whole (its pace): never a finding. */
  notes?: string[];
  /** The version of the check that computed it (the project's `quality_version`). */
  version?: number;
}

export interface VideoMediaToken {
  url: string;
  expires_at: number;
  content_type: string;
}

/**
 * What a scene shows in the preview instead of its media (`preview.build_preview`):
 * a slide drawn at render time, a recording or an AI shot being made, or one
 * that failed - with the reason. `backdrop`: the shot behind a statement.
 */
export interface VideoPreviewState {
  state: "slide" | "figure" | "recording" | "clip" | "failed";
  slide?: number;
  /** A figure written in its scene: its block's type. */
  block?: string;
  /** A figure whose still is being drawn for the preview: polled until it plays. */
  drawing?: boolean;
  kind?: "clip" | "recording";
  clip_id?: string;
  error?: string;
  /** A failed one that was deleted since the video named it: the scene needs another one. */
  missing?: boolean;
  progress?: number;
  backdrop?: boolean;
}

/** One planned scene of the preview document: the render's own planner (`timeline.plan`). */
/**
 * A screen insert as the compositor draws it (`timeline._insert_payload`): `src` is its source's key in
 * `preview_media`, `quad` the surface's corners on the scene's footage (0-1, clockwise from the top left),
 * `t0` / `t1` seconds into the scene, and a video's `time_map` scene seconds -> source seconds.
 */
export interface VideoPreviewInsert {
  src: string;
  quad: [number, number][];
  fit: "cover" | "contain";
  glow: number;
  /** How much of the footage's light plays over it, drawn by the render's compositor. */
  light?: number;
  /** A camera push on it: seconds after it comes in, how far, toward which point of the source (0-1). */
  zoom?: { start: number; end: number; scale: number; focus: [number, number] };
  t0: number;
  t1: number;
  kind: "image" | "video";
  time_map?: [number, number][];
  /** Drawn ahead by the render's compositor: the clip `build/<src>` plays from `t0`, keyed and tracked. */
  build?: { fps: number; duration: number; t0: number };
  /** Keyed or tracked and shown without its build: the stage maps the source on the quad, unlike the render. */
  approximate?: boolean;
  [key: string]: unknown;
}

export interface VideoPreviewScene {
  id: string;
  type: VideoSceneSummary["type"];
  start: number;
  end: number;
  transition_in?: { type: string; duration: number };
  /** Output time -> source time of a recording or a clip, `[[t, src], ...]`. */
  time_map?: [number, number][];
  /** Seconds into the scene (`timeline._overlay_payload`). */
  overlays?: { t0: number; t1: number; type: string; text: string }[];
  /** The pictures and videos mapped onto surfaces of the scene's footage, part of its picture. */
  inserts?: VideoPreviewInsert[];
  /** A clip whose own sound plays: its level and the envelope it follows under the narration. */
  preview_audio?: { volume: number; duck: [number, number][] | null };
  /** Whether a clip scene's own sound plays, and what decided it (`spec.clip_sound_choice`, or `silent`). */
  preview_sound?: { plays: boolean; reason: "scene" | "video" | "music" | "auto" | "silent" };
  preview_state?: VideoPreviewState;
  /** The name of the recording or the AI shot the scene plays. */
  preview_name?: string;
  /** Set by the editor before the frame loads it: the words of `preview_state`. */
  preview_label?: string;
  /** A figure scene, however it plays: its still, or its build as a clip. */
  figure?: boolean;
  /** A figure whose build plays: seconds into the scene from which it is built in whole. */
  preview_figure_whole?: number;
  /** A figure's or a screen insert's build being drawn for the preview: polled until it plays. */
  preview_building?: boolean;
  /** A screen insert of the scene the stage cannot draw as the render does (`insert_builds.mark_approximate`). */
  preview_inserts_approximate?: boolean;
  [key: string]: unknown;
}

/** The timeline document the compositor draws (`window.__xtm.load`). */
export interface VideoPreviewDocument {
  width: number;
  height: number;
  fps: number;
  duration: number;
  frames: number;
  scenes: VideoPreviewScene[];
  captions: { enabled: boolean; cues: [number, number, string][] };
  /** Each media key's URL: `clip/<scene>`, `take/<scene>`, `image/<scene>`, `poster/<scene>`, `logo`, ... */
  preview_media?: Record<string, string>;
  /** The file (or figure still) each URL plays: a newer token of the same one loads nothing again. */
  preview_media_ids?: Record<string, string>;
  [key: string]: unknown;
}

/**
 * One narration of a preview: `ready` - spoken, `audio_url` plays it at its measured `seconds`;
 * `speaking` - being spoken for this preview; `failed` - with the `error`; `none` - not spoken yet.
 */
interface VideoPreviewNarration {
  scene_id: string;
  start: number;
  seconds: number;
  text: string;
  state?: "ready" | "speaking" | "failed" | "none";
  audio_url?: string | null;
  /** What was spoken, whatever its token: the player keeps its source while it is the same. */
  key?: string | null;
  error?: string | null;
}

/** `POST /video-studio/projects/{id}/preview`. */
export interface VideoPreview {
  revision: number;
  /** The draft with its template applied: what renders. */
  spec: { title: string; scenes: VideoSceneSummary[]; [key: string]: unknown };
  document: VideoPreviewDocument;
  duration: number;
  /** The narration of each scene: at its spoken length once spoken, paced at the planner's speed until then. */
  narration: VideoPreviewNarration[];
  /** The voice-over: on, off, or no speech model for the reader (and why); its level against the music. */
  voice?: { state: "on" | "off" | "unavailable"; volume: number; error: string | null } | null;
  music: VideoPreviewMusic | null;
  /** Ids of the scenes the template adds (its intro / outro). */
  template_scenes: string[];
  notes: string[];
  /** Changes when a recording, an AI shot or a track the video shows does, without a save. */
  media_key?: string;
  /** The AI shots generated again in place, their previous take playing meanwhile: watched until made. */
  regenerating?: string[];
}

/** An AI shot asked from the editor (`POST /video-studio/clips`). */
export interface VideoClipCreate {
  prompt: string;
  name?: string;
  model?: string;
  provider_id?: string;
  duration_seconds?: number;
  aspect_ratio?: "16:9" | "9:16";
  resolution?: "720p" | "1080p";
  negative_prompt?: string;
  /** The first frame: an image or a video file, a recording, or a shot continued. */
  reference_file_id?: string;
  reference_recording_id?: string;
  reference_clip_id?: string;
  /** The moment of the video; else a recording's step; else its last frame. */
  reference_at_seconds?: number;
  reference_at_step?: number;
  /** `false` asks a silent shot. */
  sound?: boolean;
  /** `false` skips the text check of a shot made from a picture (one meant to change its words). */
  text_check?: boolean;
  /** A video's frame of another aspect ratio than the shot's: its middle, cut to it. */
  crop?: "center";
  /** `false` fails the shot on a content drift seen only up close too (minor drift is kept with a note by default). */
  accept_minor_drift?: boolean;
  /** Lock a drifting camera off once made; unset, when its camera is asked still. */
  stabilise?: boolean;
  /** Models tried in order when a take fails a check of its frames. */
  fallback_models?: string[];
  /** Screen replacement: the real screenshot drawn into the screen of the start picture, its text exact. */
  screen?: VideoClipScreen;
  /** Boxes `[x, y, width, height]` (shares of the start picture) drawn back as they are in every frame. */
  keep_regions?: [number, number, number, number][];
  /** The screens a take shows against its negative prompt: failed (default), flattened, or showing a screenshot. */
  stray_screens?: VideoStrayScreens;
  project_id?: string;
  index?: number;
  new_video?: boolean;
}

/** What a take showing a negative-prompt term only on screens gets. */
type VideoStrayScreens = "fail" | "flatten" | { file_id: string; fit?: "cover" | "contain" | "stretch"; light?: number };

/** The screen of a screen-replacement shot: the screenshot, and the screen's corners in the start picture. */
interface VideoClipScreen {
  file_id: string;
  /** Top left, top right, bottom right, bottom left, in the start picture's pixels; left out, found there (a solid green, blue or magenta screen). */
  quad?: [number, number][];
  fit?: "cover" | "contain" | "stretch";
  /** How much of the shot's light and reflections play over the screenshot (0-1, default 0.6). */
  light?: number;
}

/** An AI shot generated again in place - failed, expired, or ready but wrong (`POST /video-studio/clips/{id}/retry`). */
export interface VideoClipRetry {
  prompt?: string;
  model?: string;
  provider_id?: string;
  duration_seconds?: number;
  aspect_ratio?: "16:9" | "9:16";
  resolution?: "720p" | "1080p";
  negative_prompt?: string;
  sound?: boolean;
  text_check?: boolean;
  content_check?: boolean;
  accept_minor_drift?: boolean;
  /** Replaces the shot's reference images (`[]` drops them); may drop its start frame on a model that takes one or the other. */
  reference_image_file_ids?: string[];
  /** A new start; a moment, a step or `crop` alone moves the frame of the video the shot started from. */
  reference_file_id?: string;
  reference_recording_id?: string;
  reference_clip_id?: string;
  reference_at_seconds?: number;
  reference_at_step?: number;
  crop?: "center";
  /** `false` drops the shot's start frame; `true` keeps it beside reference images. */
  start_frame?: boolean;
  /** The video it is generated again for: under its template's art direction. */
  video_id?: string;
  screen?: VideoClipScreen;
  /** Replaces the regions kept from the start picture (`[]` drops them). */
  keep_regions?: [number, number, number, number][];
  /** Replaces the stray screens' cleanup (`fail` drops it). */
  stray_screens?: VideoStrayScreens;
  /** Replaces whether a drifting take is stabilised. */
  stabilise?: boolean;
  /** Replaces the models tried when a take fails its checks (`[]` drops them). */
  fallback_models?: string[];
}

export interface VideoUploadedFile {
  file_id: string;
  filename: string;
  content_type: string;
  size: number;
}

// --- Platform Health (#8254) ---

/** An agent's or a flow's improvement opt-in, as its viewer may act on it. */
export interface HealthOptIn {
  opt_in: boolean;
  can_manage: boolean;
  /** False for a Platform Health fleet agent: the Platform Health never improves itself. */
  eligible: boolean;
}

/** Caps per cycle and per month; `null` sets no cap. Costs are decimal strings. */
export interface HealthBudgetCaps {
  cycle_tokens: number | null;
  cycle_cost_usd: string | null;
  month_tokens: number | null;
  month_cost_usd: string | null;
}

/** One spend ceiling (#1202): the value in force, its default, what the row holds and where the value comes from. */
interface HealthSpendLimit {
  block: "budget" | "analysis" | "module_analyst";
  key: string;
  value: number | string | null;
  default: number | string | null;
  stored: number | string | null;
  source: "default" | "database";
}

/** `GET /health/settings/spend-limits`: the ceilings are locked at their defaults unless `env_var` is true. */
export interface HealthSpendLimits {
  locked: boolean;
  locked_reason: "environment" | null;
  env_var: string;
  limits: HealthSpendLimit[];
}

/**
 * The analysis layer of Platform Health (#101 S9): off by default, with hard ceilings per day and per
 * month; `null` clears a ceiling, which refuses every analysis. Costs are decimal strings.
 */
export interface HealthAnalysisSettings {
  enabled: boolean;
  day_tokens: number | null;
  day_cost_usd: string | null;
  month_tokens: number | null;
  month_cost_usd: string | null;
}

/** Settings > Platform Health (`services/health/settings.py`); later levels add fields. */
export interface HealthSettings extends HealthTriggerSettings {
  analysis: HealthAnalysisSettings;
  enabled: boolean;
  /** The name it goes by with people; `null` is the platform mode's (`HealthName.default_name`). */
  name: string | null;
  scope_mode: "all" | "selected";
  scope_agent_ids: string[];
  scope_flow_ids: string[];
  budget: HealthBudgetCaps;
  /** The editable thresholds of the OpenCTI and OpenAEV checks (#101 S10b), read on the next pass. */
  opencti: HealthOpenCTIThresholds;
  openaev: HealthOpenAEVThresholds;
  /** The alert emails (#101 S12), off by default. */
  alert_emails: HealthAlertEmailSettings;
  /** The Module Analyst's switch, debounce, exclusions and own ceilings (#101 S14). */
  module_analyst: HealthModuleAnalystSettings;
  /** Whom the module checks read OpenCTI, OpenAEV and OpenCRQ as (dev-docs 12.3); `null`: not set, no module read runs. */
  module_run_as_user_id?: string | null;
}

/**
 * `HealthModuleAnalystSettings` of `services/health/settings.py` (#101 S14, dev-docs 12.5): its own
 * ceilings outside the agentic quota; `null` clears one, which refuses every model call. Costs are decimal strings.
 */
export interface HealthModuleAnalystSettings {
  enabled: boolean;
  change_min_interval_minutes: number;
  excluded_registration_ids: string[];
  cycle_tokens: number | null;
  day_tokens: number | null;
  day_cost_usd: string | null;
  month_tokens: number | null;
  month_cost_usd: string | null;
}

/** `HealthAlertEmailSettings` of `services/health/settings.py`. */
export interface HealthAlertEmailSettings {
  enabled: boolean;
  recipients: string[];
  min_severity: "critical" | "warning" | "info";
  immediate: boolean;
  digest: boolean;
  /** Minutes a critical finding stays open before its immediate email (default 15, 0 to 1440). */
  alert_after_minutes?: number;
}

/** `HealthOpenCTIThresholds` of `services/health/settings.py`. */
export interface HealthOpenCTIThresholds {
  connector_stale_minutes: number;
  connector_errors_max: number;
  queue_depth_max: number;
  search_disk_max_percent: number;
  ingestion_lag_minutes: number;
}

/** `HealthOpenAEVThresholds` of `services/health/settings.py`. */
export interface HealthOpenAEVThresholds {
  integration_silent_minutes: number;
  agents_offline_max_percent: number;
}

/** A pet of the Health companion's library (#8261). */
export interface HealthPet {
  slug: string;
  display_name: string;
  description: string | null;
  sprite_version: 1 | 2;
  builtin: boolean;
  sha256: string;
}

/** `GET /health/me/companion`: the signed-in user's own. */
export interface HealthCompanion {
  enabled: boolean;
  pet: string;
  default_pet: string;
}

/** `GET /health/me/activity`: what the companion shows. */
export interface HealthActivity {
  state: "idle" | "running" | "waiting" | "failed" | "review";
  running: number;
  waiting: number;
  review: number;
  failed_at: string | null;
  /** Open Platform Health findings by severity, every module summed. */
  critical: number;
  warning: number;
  info: number;
}

/** `GET /health/name`: what every signed-in person calls the Platform Health. */
export interface HealthName {
  name: string;
  default_name: string;
}

/** The Platform Health's budget line this month. */
export interface HealthBudget {
  month: string;
  caps: HealthBudgetCaps;
  used: { tokens: number; cost_usd: string; reserved_tokens: number; reserved_cost_usd: string };
  remaining_tokens: number | null;
  remaining_cost_usd: string | null;
  open_cycles: number;
  refusal: string | null;
  refusal_message: string | null;
}

// ── Platform Health cycles, signals and inbox (#8261) ─────────────────────

export type HealthTargetType = "agent" | "flow" | "assignment" | "module";
type HealthCycleStatus = "open" | "completed" | "budget_reached" | "failed" | "cancelled";
/** A digest's status: ``completed`` only for a completed run with nothing skipped. */
type HealthDigestStatus = "completed" | "partial" | "failed" | "budget_reached" | "cancelled";
type HealthSignalKind = "feedback_drop" | "failure_spike" | "cost_drift" | "model_change";

interface HealthDigestSkip {
  what: string;
  reason: string;
  count?: number;
}

export interface HealthDigest {
  cycle_id: string;
  specialist: string;
  trigger: string;
  target_type: HealthTargetType | null;
  target_id: string | null;
  status: HealthDigestStatus;
  opened_at: string;
  closed_at: string | null;
  run_id: string | null;
  run_status: string | null;
  stop_reason: string | null;
  signal: HealthSignalReading | null;
  consolidations: Record<string, number> | null;
  findings: Record<string, number> | null;
  experiments: Record<string, number> | null;
  proposals: { filed: number; applied: number; rolled_back: number; by_verdict: Record<string, number> } | null;
  /** `ledger: "module_analyst"` when the spend counts against the Module Analyst's own ceiling (#101 S14d). */
  budget: { spent_tokens: number; spent_cost_usd: string; cap_tokens: number | null; cap_cost_usd: string | null; ledger?: string };
  dry_run_writes: number | null;
  /** A Module Analyst cycle (#101 S14d): recommendations attached (`attached`, then by confidence). */
  recommendations?: Record<string, number> | null;
  /** A Module Analyst cycle: agentic findings of its registration `opened` and `resolved` in its window. */
  agentic_findings?: Record<string, number> | null;
  /** The weekly digest (#101 S15e): per module, the check rounds of the week and the checks failed most. */
  modules?: Partial<Record<HealthModule, HealthWeeklyModuleSummary>> | null;
  skipped: HealthDigestSkip[];
  unavailable: string[];
  narrative: string | null;
}

/** One module of the weekly digest's Modules part (`check_reports.weekly_module_summary`). */
export interface HealthWeeklyModuleSummary {
  rounds: number;
  registrations: number;
  passed: number;
  warned: number;
  failed: number;
  not_run: number;
  most_failed: { id: string; failed: number; warned: number }[];
}

interface HealthSignalReading {
  kind: HealthSignalKind;
  target_type: HealthTargetType;
  target_id: string;
  value: number | string | null;
  baseline: number | string | null;
  detail: Record<string, unknown>;
}

export interface HealthCycle {
  id: string;
  specialist: string;
  trigger: "schedule" | "signal" | "on_demand";
  target_type: HealthTargetType | null;
  target_id: string | null;
  status: HealthCycleStatus;
  stop_reason: string | null;
  opened_at: string;
  closed_at: string | null;
  spent_tokens: number;
  spent_cost_usd: string;
  run_id: string | null;
  signal: HealthSignalReading | null;
  digest: HealthDigest | null;
  requested_by_me: boolean;
  /** The target's name: a module cycle's registration title, an agent's, flow's or assignment's name; null when it no longer exists. */
  target_name?: string | null;
}

export interface HealthCycleStart {
  cycle_id: string;
  run_id: string;
}

export interface HealthSignal {
  target_type: HealthTargetType;
  target_id: string;
  /** The target's name; null when it no longer exists. */
  target_name?: string | null;
  kind: HealthSignalKind;
  active: boolean;
  last_value: Record<string, unknown> | null;
  fired_at: string | null;
  cycle_id: string | null;
  refusal: string | null;
  evaluated_at: string;
}

/** A module of Platform Health (#101 S8); agents and fleets exist in every mode, the others in XTM One modes. */
export type HealthModule = "agents" | "fleets" | "xtm_one" | "opencti" | "openaev" | "opencrq";
export type HealthSeverity = "critical" | "warning" | "info";
export type HealthFindingStatus = "open" | "resolved" | "dismissed";

export interface HealthModuleCard {
  module: HealthModule;
  score: number;
  critical: number;
  warning: number;
  info: number;
  /** The score a week ago; null when no score was written that day. */
  previous_score: number | null;
  /** OpenCTI, OpenAEV, OpenCRQ for an administrator: the Module Analyst's last cycle, null when none (#101 S14c). */
  last_investigation?: HealthLastInvestigation | null;
}

/** The Module Analyst's latest cycle on one of a module's registrations (dev-docs/platform-health.md section 12.7). */
export interface HealthLastInvestigation {
  cycle_id: string;
  registration_id: string;
  status: "open" | "completed" | "budget_reached" | "failed" | "cancelled";
  trigger: "schedule" | "signal" | "on_demand";
  opened_at: string;
  closed_at: string | null;
}

/** What "Run checks now" did on one registration: started, or the refusal code of `start_cycle`. */
interface HealthModuleInvestigationStart {
  registration_id: string;
  cycle_id: string | null;
  run_id: string | null;
  refusal: string | null;
  open_cycle_id?: string | null;
}

export interface HealthModuleInvestigation {
  module: HealthModule;
  items: HealthModuleInvestigationStart[];
}

/** A module checker's verdict on one check (section 13.6). */
export type HealthCheckVerdict = "pass" | "warn" | "fail" | "not_run";

/** One check of a module check report: the playbook check id, its verdict, and why. */
export interface HealthModuleCheckEntry {
  id: string;
  verdict: HealthCheckVerdict;
  category: string;
  recommendation: string | null;
  reason: string | null;
  evidence: HealthAnalystEvidence[];
  finding_id: string | null;
}

/** The latest check round of one registration of a module (#101 S15c, section 13.6). */
export interface HealthModuleCheckReport {
  report_id: string;
  registration_id: string;
  cycle_id: string;
  started_at: string | null;
  finished_at: string;
  passed: number;
  warned: number;
  failed: number;
  not_run: number;
  checks: HealthModuleCheckEntry[];
}

/** `GET /health/modules/{module}/checks` (administrators): one report per registration. */
export interface HealthModuleCheckReports {
  module: HealthModule;
  items: HealthModuleCheckReport[];
  /** `not_set` or `no_access`: the card shows one call to action instead of the per-check lines. */
  run_as?: "ok" | "not_set" | "no_access";
}

/** One read the Module Analyst made to back a recommendation. */
interface HealthAnalystEvidence {
  tool?: string;
  object_ids?: string[];
  values?: Record<string, unknown>;
  read_at?: string;
}

/** The Module Analyst's newest recommendation on a finding (section 12.7). */
export interface HealthAnalystRecommendation {
  text: string;
  confidence: "high" | "medium" | "low";
  evidence: HealthAnalystEvidence[];
  cycle_id: string;
  created_at: string;
}

/** The latest analysis of one module; `status` is null when it was never analysed. */
export interface HealthModuleAnalysis {
  module: HealthModule;
  status: "completed" | "refused" | "failed" | null;
  /** Why it did not run or failed (`services/health/analysis.py`): disabled, ai_off, ceiling_not_set, ceiling_reached, no_model, no_price, model_error. */
  reason: string | null;
  summary: string;
  model: string | null;
  tokens: number;
  cost_usd: string;
  trigger: "schedule" | "on_demand" | null;
  analysed_at: string | null;
}

export interface HealthAnalysisSpend {
  tokens: number;
  cost_usd: string;
  reserved_tokens: number;
}

/** `GET /health/analysis` (administrators): ceilings, today's and this month's spend, one analysis per module. */
export interface HealthAnalysisOverview {
  enabled: boolean;
  ceilings_set: boolean;
  ceilings: Omit<HealthAnalysisSettings, "enabled">;
  day: HealthAnalysisSpend;
  month: HealthAnalysisSpend;
  modules: HealthModuleAnalysis[];
  /** The Module Analyst row of Spend limits (#101 S14d): its own ceilings and ledger. */
  module_analyst: HealthModuleAnalystSpendRow;
}

interface HealthModuleAnalystSpendRow {
  enabled: boolean;
  ceilings_set: boolean;
  ceilings: Omit<HealthModuleAnalystSettings, "enabled" | "change_min_interval_minutes" | "excluded_registration_ids">;
  day: HealthAnalysisSpend;
  month: HealthAnalysisSpend;
}

export interface HealthOverview {
  modules: HealthModuleCard[];
  overall: number;
  /** "all" for an administrator, "managed" for the findings of the agents the reader manages. */
  scope: "all" | "managed";
}

export interface HealthFinding {
  id: string;
  check_id: string;
  module: HealthModule;
  category: string;
  severity: HealthSeverity;
  status: HealthFindingStatus;
  subject: Record<string, string>;
  subject_name: string | null;
  evidence: Record<string, unknown>;
  recommendation: string;
  doc_link: string;
  first_seen: string;
  last_seen: string;
  resolved_at: string | null;
  dismissal: { reason: string; by_name?: string; at?: string; until?: string | null } | null;
  can_dismiss: boolean;
  /** Discovered by the Module Analyst (check id `<module>.agentic.<slug>`), not by a deterministic check. */
  agentic?: boolean;
  /** The Module Analyst's newest recommendation on this finding, null when none. */
  analyst_recommendation?: HealthAnalystRecommendation | null;
}

export interface HealthFindingList {
  items: HealthFinding[];
  total: number;
}

/** What a Dismiss all or a Restore all changed, and the ids it left as they were. */
export interface HealthFindingBulkResult {
  items: HealthFinding[];
  skipped: string[];
}

export interface HealthHistoryPoint {
  day: string;
  module: HealthModule;
  score: number;
  critical: number;
  warning: number;
  info: number;
}

export interface HealthScheduleMark {
  schedule: string;
  fired_at: string;
  cycle_id: string | null;
  refusal: string | null;
}

export interface ImprovementListFilters {
  /** Several values keep any of them (repeated in the query string). */
  proposal_type?: string[];
  status?: string[];
  target_type?: "agent" | "flow" | "skill" | "assignment";
  target_id?: string;
  agent_id?: string;
  /** Words of the summary. */
  q?: string;
  sort?: "date" | "type" | "status";
  direction?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

export interface ImprovementListItem {
  id: string;
  proposal_type: string;
  target_type: string;
  target_id: string;
  agent_id: string | null;
  status: string;
  summary: string | null;
  experiment_verdict: string | null;
  experiment: Record<string, unknown> | null;
  created_at: string;
  can_decide: boolean;
}

export interface ImprovementListResponse {
  items: ImprovementListItem[];
  total: number;
}

export interface ImprovementEvidenceLink {
  kind: string;
  id: string | null;
  href: string | null;
  hidden: boolean;
}

/** Settings > Platform Health fields level 8 adds to level 1's ``HealthSettings`` (#8261). */
export interface HealthSchedules {
  memory: string;
  quality: string;
  flows: string;
  digest: string;
  memory_enabled: boolean;
  quality_enabled: boolean;
  flows_enabled: boolean;
  digest_enabled: boolean;
  timezone: string;
}

export interface HealthSignalSettings {
  feedback_enabled: boolean;
  feedback_drop_points: number;
  feedback_window_days: number;
  feedback_min_ratings: number;
  failure_enabled: boolean;
  failure_rate_pct: number;
  failure_last_runs: number;
  cost_enabled: boolean;
  cost_increase_pct: number;
  cost_window_days: number;
  cost_min_runs: number;
  model_change_enabled: boolean;
}

interface HealthTriggerSettings {
  schedules: HealthSchedules;
  signals: HealthSignalSettings;
  proposal_watch_days: number;
  flow_shadow_days: number;
  flow_watch_days: number;
  flow_shadow_min_pairs: number;
  /** Level 2's field (#8255): every write of every Platform Health agent logged, not executed. */
  dry_run: boolean;
}
