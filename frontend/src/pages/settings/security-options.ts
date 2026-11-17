// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Access and security options: copy and pure helpers of the card.
 *
 * The backend registry (`app/core/security_options.py`) says which options
 * exist; this file says what they are called. `tests/test_security_options.py`
 * fails when a registered option has no label here.
 *
 * The contract the card shows: an option the platform mode fixes is locked
 * with its reason (in XTM One, quotas always fail closed); an option
 * its environment variable sets is locked (read-only, "Set by environment
 * variable X", a write is refused); otherwise the value stored here applies,
 * else the default.
 */
import type { SecurityOption, SecurityOptionValue } from "@/types";

import { categoryLabel, categoryRank } from "./security-audit.ts";

/** The field names, read by `t_i18n(SECURITY_OPTION_LABELS[key])`. @i18n-keys */
export const SECURITY_OPTION_LABELS = {
  trust_proxy_headers: "Trust proxy headers",
  client_ip_strict: "Canonical client addresses",
  credential_transport_strict: "Require HTTPS for runner enrollment and MCP tokens",
  rate_limit_ipv6_prefix: "IPv6 rate-limit prefix length",
  rate_limit_fail_mode: "Rate limits when Redis is unreachable",
  quota_fail_mode: "Quotas when their store is unreachable",
  min_password_length: "Minimum password length",
  password_reset_expire_hours: "Password reset link lifetime (hours)",
  invitation_expire_hours: "Invitation link lifetime (hours)",
  jwt_expire_minutes: "Absolute session lifetime (minutes)",
  session_idle_timeout_minutes: "Idle session timeout (minutes)",
  api_key_max_lifetime_days: "API key maximum lifetime (days)",
  mcp_dcr_enabled: "MCP client self-registration",
  mcp_cimd_enabled: "MCP client metadata documents",
  mcp_outbound_dcr_enabled: "Automatic MCP OAuth client registration",
  oauth_token_url_strict: "Strict OAuth token URL checks",
  oauth_token_dev_trusted_origins: "Loopback token URL origins (development)",
  mcp_server_url_policy: "MCP server URL policy",
  mcp_server_trusted_origins: "Trusted MCP server origins",
  a2a_client_dev_trusted_origins: "Trusted A2A partner origins (development)",
  a2a_allow_insecure_loopback: "Accept plain HTTP from loopback (testing)",
  webhook_require_secret: "Require webhook secrets",
  webhook_exact_event_matching: "Exact event matching for every trigger",
  integration_notifications_require_verification: "Require verified integration notifications",
  public_widget_strict_rendering: "Strict widget rendering",
  prompt_guard_block_public_widget: "Prompt guards block widget messages",
  prompt_guard_block_general_assistant: "Prompt guards block General Assistant messages",
  prompt_guard_model_output: "Prompt guards on model answers",
  agent_tools_strict_admin_access: "Strict admin access for agent tools",
  presentation_public_links_enabled: "Public presentation links",
  video_public_links_enabled: "Public video links",
  dashboard_public_links_enabled: "Published dashboard links",
  browser_private_targets: "Browser profiles on private networks",
  scheduled_work_items_enabled: "Scheduled work items",
  phone_notification_content: "Phone notification content",
  companion_pin_required: "Require a PIN on the mobile companion",
  prompt_guard_scanned_tools: "Tools screened by the prompt guard",
  prompt_guard_block_tool_results: "Withhold tool results a guard rule blocks",
  runner_strict_path_confinement: "Strict runner folder confinement",
  disable_code_interpreter: "Disable the code interpreter",
  disable_custom_tools: "Disable custom tools",
  disable_custom_variables: "Disable custom variables",
  code_interpreter_extra_allowed_modules: "Code interpreter: re-enabled modules",
  custom_tool_extra_allowed_modules: "Custom tools: re-enabled Python modules",
  custom_tool_node_permission: "Node.js permission model for custom tools",
} as const;

/** One sentence per option, read by `t_i18n(SECURITY_OPTION_DESCRIPTIONS[key])`. @i18n-keys
 * @public
 */
export const SECURITY_OPTION_DESCRIPTIONS = {
  trust_proxy_headers:
    "Reads the client address and scheme from X-Forwarded-For / X-Forwarded-Proto sent by any peer. Only behind a proxy that rewrites them: otherwise any caller can spoof its address. FORWARDED_ALLOW_IPS, which trusts only the proxy addresses it names, is the safer setting.",
  client_ip_strict:
    "Key, log and store every client address in canonical form (no port, brackets or IPv6 zone, IPv4-mapped read as IPv4), so one client is one rate-limit key whatever the proxy wrote.",
  credential_transport_strict:
    "Refuse runner enrollment, MCP OAuth client registration and MCP OAuth token requests the platform cannot confirm arrived over HTTPS. Off serves them as before and only logs them. Behind a TLS-terminating proxy, set FORWARDED_ALLOW_IPS first.",
  rate_limit_ipv6_prefix:
    "The network a native IPv6 client is rate-limited on: 128 keys each address, 64 its /64, which every device of that /64 then shares.",
  rate_limit_fail_mode:
    "Open lets requests through while Redis cannot be read; closed refuses them with 503.",
  quota_fail_mode:
    "Open runs agents unmetered while the quota store cannot be read; closed refuses them.",
  min_password_length: "Enforced on registration, password changes and local accounts an administrator creates.",
  password_reset_expire_hours: "How long a password reset link stays valid.",
  invitation_expire_hours: "Default lifetime of an invitation link.",
  jwt_expire_minutes:
    "How long a session lasts after sign-in, even while in use (1440 is a day). Lowering it ends older sessions at their next request; raising it applies from the next sign-in.",
  session_idle_timeout_minutes:
    "Sign a person out after this many minutes without clicking, typing or scrolling; the web app warns a minute before. 0 turns it off. Not enforced while Redis is unreachable.",
  api_key_max_lifetime_days:
    "The longest a personal API key created or given a new expiry may live; 0 sets no maximum. Existing keys keep their expiry, and the Security audit lists those beyond it.",
  mcp_dcr_enabled:
    "Let external MCP clients register themselves with this platform (an unauthenticated endpoint, rate limited).",
  mcp_cimd_enabled:
    "Let an MCP client sign in with its own HTTPS metadata document instead of registering (claude.ai, Claude Code, VS Code). The platform fetches it from a public address, bounded and cached.",
  mcp_outbound_dcr_enabled:
    "Let the platform register itself with a remote MCP server; off requires a client ID for every server.",
  oauth_token_url_strict:
    "Also refuse token URLs on loopback, link-local, unspecified, reserved or multicast addresses, and any scheme but http(s). Off refuses only cloud metadata addresses, so every token URL that worked before keeps working.",
  oauth_token_dev_trusted_origins:
    "Origins whose OAuth token URL may resolve to a loopback address under the strict checks, for local development only.",
  mcp_server_url_policy:
    "Which addresses a remote MCP server may be reached at, checked on save and on every connection. Permissive refuses only cloud metadata addresses; private networks also refuses loopback, link-local and reserved addresses; public only refuses every internal address, so an on-prem server is reached through a relay group.",
  mcp_server_trusted_origins:
    "Origins a remote MCP server may be reached at whatever their address under the stricter URL policies. Cloud metadata addresses stay refused.",
  a2a_client_dev_trusted_origins:
    "Origins an A2A delegation may reach over plain HTTP and at a loopback or private address, for local development only. Cloud metadata addresses stay refused.",
  a2a_allow_insecure_loopback:
    "Count a plain HTTP request from this machine (127.0.0.1, ::1) as encrypted, so the A2A endpoints accept it, for a deployment federated with itself in tests. Never behind a reverse proxy on the same host.",
  webhook_require_secret: "Refuse webhook deliveries to assignments that have no webhook secret.",
  webhook_exact_event_matching:
    "Run triggers saved before exact event matching only on the events they select too (pull_request never on pull_request_review). Triggers created or given new events since already match exactly.",
  integration_notifications_require_verification:
    "Refuse Slack, Microsoft Graph and LinkedIn notifications the platform cannot verify, and limit the admin OAuth app's deliveries to its own integrations. Off keeps accepting them as before, with a warning.",
  public_widget_strict_rendering:
    "Show only the images the platform serves in widget answers, drop their styles, SVG / MathML and links other than http(s), mailto and tel, and open every link in a new tab.",
  prompt_guard_block_public_widget:
    "While a prompt guard is connected, refuse a visitor's message its block rules match. Off, the match is audited and the message answered.",
  prompt_guard_block_general_assistant:
    "While a prompt guard is connected, refuse a General Assistant message its block rules match. Off, the match is audited and the message answered.",
  prompt_guard_model_output:
    "While a prompt guard is connected: log evaluates every answer in the background once delivered and audits a match; block withholds an answer a block rule matches before it is delivered. A streamed answer has already been shown, so it is only audited.",
  agent_tools_strict_admin_access:
    "Keep an admin's agent tools (the agent builder, the work items, the XTM One MCP server) to the admin's own, group-shared and company-managed assignments.",
  presentation_public_links_enabled:
    "Let the people who manage a presentation give it a link anyone can open read-only, without an account. Off, every public link stops working at once and none can be created; turning it back on restores the links that were not turned off.",
  video_public_links_enabled:
    "Let the people who manage a video give it a link anyone can watch, and optionally download, without an account. Off, every public video link stops working at once and none can be created; turning it back on restores the links that were not turned off.",
  dashboard_public_links_enabled:
    "Let the people who manage a custom dashboard publish it as a link anyone can open without an account, showing a snapshot of its data read with the publisher's access. Off by default; off, every published link stops working at once and none can be published, and turning it back on restores them.",
  browser_private_targets:
    "Let a browser profile open an application on a private network, on the hosts it declares: its sign-in, the agents browsing with it and Video Studio recordings. Loopback, link-local and cloud metadata addresses stay refused. Off, browser profiles reach public addresses only, like the agent browser tool.",
  scheduled_work_items_enabled:
    "Let every agent schedule a one-off action from a chat and run it at the time asked, with no assignment. Off, the scheduling tools answer that scheduling is disabled by the administrator and nothing scheduled is run; items that come due meanwhile are marked missed.",
  companion_pin_required:
    "Make everyone unlock the mobile companion with a PIN of their own before it shows or approves anything. Without a PIN yet, a person sets one on their phone first. Off, each person chooses in their profile.",
  phone_notification_content:
    "What a notification shows on a linked phone. Full: its title and text, encrypted to the phone so the push service cannot read them. Minimal: only that something is new, and no Approve or Reject button.",
  prompt_guard_scanned_tools:
    "While a prompt guard (Nova) is connected, it evaluates what these tools return to an agent, the text of their attached files included, and records each rule match in the audit log. Empty screens nothing.",
  prompt_guard_block_tool_results:
    "Withhold a screened tool result that a block rule matches: the agent reads a short refusal instead. Off records the match and leaves the result as it is; on, the agent waits for the verdict on each screened result.",
  runner_strict_path_confinement:
    "Refuse a restricted runner's file command whose path leaves its allowed folders through a link.",
  disable_code_interpreter: "Turn the code interpreter off for every agent.",
  disable_custom_tools: "Turn custom tools off for every agent.",
  disable_custom_variables: "Turn custom variables off platform-wide.",
  code_interpreter_extra_allowed_modules:
    "Blocked modules the code interpreter may import again (for example socket). Grants network access.",
  custom_tool_extra_allowed_modules:
    "Blocked modules custom Python tools may import again (for example requests). Grants network access.",
  custom_tool_node_permission:
    "Deny file system and child process access to JavaScript custom tools that run without Docker.",
} as const;

/** A security audit finding an option decides. */
interface OptionFinding {
  /** The finding's `id` (`security_audit.py`). */
  id: string;
  /** Its recommendation names the option (`where_to_set`): the finding links back to the field. */
  recommends?: true;
}

/**
 * The security audit findings an option decides, the one table both links
 * are derived from: the card links an option to them (the audit opens on the
 * one it reports), and a finding marked `recommends` links to the option's
 * field. `tests/test_security_options.py` holds the ids to the audit and the
 * `recommends` marks to its `where_to_set` calls, both ways.
 */
export const SECURITY_OPTION_FINDINGS: Partial<Record<keyof typeof SECURITY_OPTION_LABELS, readonly OptionFinding[]>> = {
  trust_proxy_headers: [
    { id: "network.forwarded_headers_trusted", recommends: true },
    { id: "network.proxy_trust_missing" },
    { id: "network.proxy_trust_declared" },
    { id: "network.client_addresses_undeclared_proxy" },
    { id: "network.client_addresses_declared_proxy" },
    { id: "network.client_addresses_no_undeclared_proxy" },
    { id: "network.client_addresses_check_skipped" },
  ],
  credential_transport_strict: [
    { id: "network.credential_transport_strict_off", recommends: true },
    { id: "network.credential_transport_strict_on" },
    { id: "network.proxy_trust_missing" },
  ],
  rate_limit_ipv6_prefix: [
    { id: "network.ipv6_rate_limit_per_address", recommends: true },
    { id: "network.ipv6_rate_limit_narrow_network", recommends: true },
    { id: "network.ipv6_rate_limit_per_prefix" },
  ],
  rate_limit_fail_mode: [
    { id: "infrastructure.rate_limit_fails_open", recommends: true },
    { id: "infrastructure.rate_limit_fails_closed" },
  ],
  quota_fail_mode: [
    { id: "infrastructure.quota_fails_open", recommends: true },
    { id: "infrastructure.quota_fails_closed" },
  ],
  min_password_length: [
    { id: "authentication.password_length_short", recommends: true },
    { id: "authentication.password_length_sufficient" },
  ],
  password_reset_expire_hours: [{ id: "authentication.password_reset_link_long", recommends: true }],
  invitation_expire_hours: [{ id: "authentication.invitation_link_long", recommends: true }],
  jwt_expire_minutes: [
    { id: "authentication.session_lifetime_long", recommends: true },
    { id: "authentication.session_lifetime_bounded" },
  ],
  session_idle_timeout_minutes: [
    { id: "authentication.session_idle_timeout_off", recommends: true },
    { id: "authentication.session_idle_timeout_long", recommends: true },
    { id: "authentication.session_idle_timeout_on" },
    { id: "infrastructure.session_idle_timeout_fails_open" },
    { id: "infrastructure.session_idle_timeout_unenforced" },
  ],
  api_key_max_lifetime_days: [
    { id: "authentication.api_keys_without_expiry", recommends: true },
    { id: "authentication.api_keys_beyond_max_lifetime" },
    { id: "authentication.api_keys_expire" },
  ],
  mcp_dcr_enabled: [{ id: "mcp.dynamic_client_registration_on", recommends: true }],
  mcp_cimd_enabled: [{ id: "mcp.client_id_metadata_documents_on", recommends: true }],
  oauth_token_url_strict: [
    { id: "integrations.oauth_token_url_strict_off", recommends: true },
    { id: "integrations.oauth_token_url_strict_on" },
  ],
  oauth_token_dev_trusted_origins: [{ id: "integrations.oauth_token_dev_origins_set", recommends: true }],
  a2a_client_dev_trusted_origins: [
    { id: "mcp.a2a_dev_origins_set", recommends: true },
    { id: "mcp.a2a_partners_over_https" },
  ],
  a2a_allow_insecure_loopback: [
    { id: "mcp.a2a_loopback_counts_as_https", recommends: true },
    { id: "mcp.a2a_requests_over_https" },
  ],
  webhook_require_secret: [
    { id: "assignments.webhook_without_secret", recommends: true },
    { id: "assignments.webhook_without_secret_refused" },
    { id: "assignments.webhook_secrets_configured" },
  ],
  webhook_exact_event_matching: [
    { id: "assignments.legacy_event_matching", recommends: true },
    { id: "assignments.event_matching_exact" },
  ],
  integration_notifications_require_verification: [
    { id: "integrations.slack_integrations_unverified", recommends: true },
    { id: "integrations.slack_events_unverified", recommends: true },
    { id: "integrations.linkedin_notifications_unverified", recommends: true },
    { id: "integrations.admin_app_scope_not_applied", recommends: true },
    { id: "integrations.provider_webhook_unverified", recommends: true },
    { id: "integrations.notifications_verified" },
    { id: "integrations.provider_webhook_verified" },
    { id: "integrations.slack_integrations_polled" },
    { id: "integrations.linkedin_notifications_dropped" },
    { id: "integrations.linkedin_notifications_verified" },
    { id: "integrations.slack_events_verified" },
    { id: "integrations.slack_signing_secret_unusable" },
    { id: "channels.teams_graph_notifications_unverified", recommends: true },
    { id: "channels.teams_graph_notifications_verified" },
  ],
  public_widget_strict_rendering: [
    { id: "web.public_widget_remote_images", recommends: true },
    { id: "web.public_widget_strict" },
  ],
  prompt_guard_block_public_widget: [
    { id: "agents.prompt_guard_public_widget_logs_only", recommends: true },
    { id: "agents.prompt_guard_public_widget_blocks" },
    { id: "web.public_widget_uploads_on", recommends: true },
  ],
  prompt_guard_block_general_assistant: [
    { id: "agents.prompt_guard_general_assistant_logs_only", recommends: true },
    { id: "agents.prompt_guard_general_assistant_blocks" },
  ],
  prompt_guard_model_output: [
    { id: "agents.prompt_guard_model_output_off", recommends: true },
    { id: "agents.prompt_guard_model_output_logs_only", recommends: true },
    { id: "agents.prompt_guard_model_output_blocks" },
  ],
  agent_tools_strict_admin_access: [
    { id: "access.admin_reach_private_assignments", recommends: true },
    { id: "access.admin_reach_strict" },
  ],
  prompt_guard_scanned_tools: [
    { id: "agents.tool_result_guard_unscoped", recommends: true },
    { id: "agents.tool_result_guard_not_connected" },
  ],
  prompt_guard_block_tool_results: [
    { id: "agents.tool_result_guard_log_only", recommends: true },
    { id: "agents.tool_result_guard_blocking" },
  ],
  runner_strict_path_confinement: [
    { id: "tools.runner_path_links", recommends: true },
    { id: "tools.runner_path_confinement_strict" },
  ],
  disable_code_interpreter: [{ id: "tools.code_interpreter_enabled", recommends: true }],
  disable_custom_tools: [
    { id: "tools.custom_tools_disabled" },
    { id: "tools.custom_tool_sandbox_none", recommends: true },
    { id: "tools.custom_tool_sandbox_docker_unavailable" },
    { id: "tools.custom_tool_sandbox_opensandbox_unavailable" },
  ],
  disable_custom_variables: [{ id: "tools.custom_variables_enabled", recommends: true }],
  code_interpreter_extra_allowed_modules: [
    { id: "tools.code_interpreter_modules_reenabled", recommends: true },
    { id: "tools.code_interpreter_modules_blocked" },
  ],
  custom_tool_extra_allowed_modules: [
    { id: "tools.custom_tool_modules_reenabled", recommends: true },
    { id: "tools.custom_tool_modules_blocked" },
  ],
  custom_tool_node_permission: [
    { id: "tools.node_permission_off", recommends: true },
    { id: "tools.node_permission_on" },
    { id: "tools.node_permission_unsupported" },
  ],
};

function findingsOf(key: string): readonly OptionFinding[] {
  return Object.prototype.hasOwnProperty.call(SECURITY_OPTION_FINDINGS, key)
    ? (SECURITY_OPTION_FINDINGS as Record<string, readonly OptionFinding[]>)[key]
    : [];
}

/** The audit address that opens on the findings *keys* name (`?finding=` repeated). */
export function auditFindingPath(key: string): string | null {
  const ids = findingsOf(key).map((finding) => finding.id);
  if (!ids.length) return null;
  return `/settings/security?${ids.map((id) => `finding=${encodeURIComponent(id)}`).join("&")}`;
}

/** The option a finding's recommendation names (a `recommends` entry), or `null`. */
export function recommendedOptionKey(findingId: string | null | undefined): string | null {
  if (!findingId) return null;
  for (const [key, findings] of Object.entries(SECURITY_OPTION_FINDINGS)) {
    if (findings?.some((finding) => finding.id === findingId && finding.recommends)) return key;
  }
  return null;
}

/**
 * The option a finding links to: the one its recommendation names, while the
 * options are known and its environment variable does not lock it. A locked
 * option keeps naming only the variable, which is the thing to change.
 */
export function linkedOptionKey(
  findingId: string | null | undefined,
  options: readonly { key: string; locked: boolean }[] | undefined,
): string | null {
  const key = recommendedOptionKey(findingId);
  if (!key || !options) return null;
  return options.some((option) => option.key === key && !option.locked) ? key : null;
}

/**
 * The option a security control is (a config control's `setting` is
 * its variable), or `null` for a control Security options does not hold.
 */
export function controlOption<T extends { env_var: string }>(
  control: { setting: string; stored_in: string },
  options: readonly T[] | undefined,
): T | null {
  if (control.stored_in !== "config" || !options) return null;
  return options.find((candidate) => candidate.env_var === control.setting) ?? null;
}

/** The option a security control is, while nothing locks it; else `null`. */
export function controlOptionKey(
  control: { setting: string; stored_in: string },
  options: readonly { key: string; env_var: string; locked: boolean }[] | undefined,
): string | null {
  const option = controlOption(control, options);
  return option && !option.locked ? option.key : null;
}

/** Where a control's value comes from, as its details say it: the lock's reason, or its source. */
export function optionSourceMessage(option: SecurityOption, t: Translate): string {
  const locked = lockMessage(option, t);
  if (locked) return locked;
  return t(option.source === "database" ? SAVED_IN_OPTIONS_MESSAGE : DEFAULT_VALUE_MESSAGE);
}

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** The section's route, under the Settings page's `/settings/:tab?/:section?`. */
const SECURITY_OPTIONS_ROUTE = "/settings/security/options";

/** An option's row is `#option-<key>`: other pages and saved links point at it, so it never changes. */
const OPTION_ANCHOR_PREFIX = "option-";

export function optionAnchorId(key: string): string {
  return `${OPTION_ANCHOR_PREFIX}${key}`;
}

/** Where a link to an option's field goes, as a router location (`<Link to>` adds the base path). */
export function optionLocation(key: string): { pathname: string; hash: string } {
  return { pathname: SECURITY_OPTIONS_ROUTE, hash: `#${optionAnchorId(key)}` };
}

/** The option a location hash points at (`#option-<key>`), or `null`. */
export function targetedOptionKey(hash: string): string | null {
  const prefix = `#${OPTION_ANCHOR_PREFIX}`;
  return hash.startsWith(prefix) && hash.length > prefix.length ? hash.slice(prefix.length) : null;
}

/**
 * Choice values, read by `t_i18n(SECURITY_OPTION_CHOICE_LABELS[choice])`. Keys of
 * their own: the catalogs' "Open" is the verb (open a file). @i18n-keys
 */
export const SECURITY_OPTION_CHOICE_LABELS = {
  open: "Open (let through)",
  closed: "Closed (refuse)",
  off: "Off (not evaluated)",
  log: "Log (audit only)",
  block: "Block (withhold)",
  permissive: "Permissive (refuse cloud metadata)",
  private_networks: "Private networks (refuse loopback and link-local)",
  public_only: "Public only (refuse every internal address)",
  full: "Full (title and text)",
  minimal: "Minimal (\"New notification\" only)",
} as const;

export const LOCKED_BY_ENV_MESSAGE = "Set by environment variable {name}";

/** A value an administrator saved in Security options, and one nothing sets. */
export const SAVED_IN_OPTIONS_MESSAGE = "Saved in Security options";
export const DEFAULT_VALUE_MESSAGE = "Default value";

/** The unit of a number option, read by `t_i18n(SECURITY_OPTION_UNITS[key])`. @i18n-keys */
const SECURITY_OPTION_UNITS = {
  rate_limit_ipv6_prefix: "bits",
  min_password_length: "characters",
  password_reset_expire_hours: "hours",
  invitation_expire_hours: "hours",
  jwt_expire_minutes: "minutes",
  session_idle_timeout_minutes: "minutes",
  api_key_max_lifetime_days: "days",
} as const;

/** A list option as an administrator types it: the field's placeholder and the bubble's example. */
export const SECURITY_OPTION_LIST_EXAMPLES: Readonly<Partial<Record<SecurityOption["kind"], string>>> = {
  origins: "http://localhost:8080",
  modules: "requests, urllib",
};

/** What an empty list option does, read by `t_i18n(SECURITY_OPTION_EMPTY_LIST[kind])`. @i18n-keys */
const SECURITY_OPTION_EMPTY_LIST = {
  origins: "Empty: no origin is trusted beyond the policy.",
  modules: "Empty: every blocked module stays blocked.",
  tools: "Empty: no tool is screened.",
} as const;

/**
 * The lines of an option's info bubble beyond its help line: its unit, the
 * values it accepts (and the one that turns it off), an example as typed, and
 * what an empty value does. The environment variable is added by the card.
 */
export function optionInfo(option: SecurityOption, t: Translate): string[] {
  const lines: string[] = [];
  const unit = (SECURITY_OPTION_UNITS as Record<string, string>)[option.key];
  if (unit) lines.push(t("Unit: {unit}", { unit: t(unit) }));
  if (option.kind === "int" && option.minimum !== null && option.maximum !== null) {
    const bounds = { off: option.off_value ?? 0, minimum: option.minimum, maximum: option.maximum };
    lines.push(
      t(option.off_value !== null ? "{off} (off), or between {minimum} and {maximum}" : "Between {minimum} and {maximum}", bounds),
    );
  }
  const example = SECURITY_OPTION_LIST_EXAMPLES[option.kind];
  if (example) lines.push(t("Example: {example}", { example }));
  if (Object.prototype.hasOwnProperty.call(SECURITY_OPTION_EMPTY_LIST, option.kind)) {
    lines.push(t(SECURITY_OPTION_EMPTY_LIST[option.kind as keyof typeof SECURITY_OPTION_EMPTY_LIST]));
  }
  return lines;
}

/**
 * Why the platform mode fixes an option (`pinned_reason` in the registry),
 * read by `t_i18n(SECURITY_OPTION_PINNED_REASONS[key])`.
 * `tests/test_security_options.py` fails on a pinned option with no entry. @i18n-keys
 */
export const SECURITY_OPTION_PINNED_REASONS = {
  quota_fail_mode:
    "Always closed in XTM One: quotas are part of the licence, so agentic work is refused while their store is unreachable.",
} as const;

/** A pinned option this frontend has no reason for (a newer backend). */
export const LOCKED_BY_PLATFORM_MODE_MESSAGE = "Fixed in XTM One: it cannot be changed here.";

/** What a locked option says instead of offering a control, or `null` when it is editable. */
export function lockMessage(option: SecurityOption, t: Translate): string | null {
  if (!option.locked) return null;
  if (option.locked_reason === "platform_mode") {
    return t(
      Object.prototype.hasOwnProperty.call(SECURITY_OPTION_PINNED_REASONS, option.key)
        ? SECURITY_OPTION_PINNED_REASONS[option.key as keyof typeof SECURITY_OPTION_PINNED_REASONS]
        : LOCKED_BY_PLATFORM_MODE_MESSAGE,
    );
  }
  return t(LOCKED_BY_ENV_MESSAGE, { name: option.env_var });
}

export function optionLabel(key: string): string {
  return (SECURITY_OPTION_LABELS as Record<string, string>)[key] ?? key;
}

export function optionDescription(key: string): string | null {
  return (SECURITY_OPTION_DESCRIPTIONS as Record<string, string>)[key] ?? null;
}

/**
 * A section's name. The card's sections are the Security audit's categories
 * (an option's group is the category of its control), named alike.
 */
export function groupLabel(group: string): string {
  return categoryLabel(group);
}

/** Options grouped by section, in the audit's category order, unknown groups last. */
export function groupOptions(options: SecurityOption[]): [string, SecurityOption[]][] {
  const groups = new Map<string, SecurityOption[]>();
  for (const option of options) {
    const list = groups.get(option.group) ?? [];
    list.push(option);
    groups.set(option.group, list);
  }
  return [...groups.entries()].sort(([a], [b]) => categoryRank(a) - categoryRank(b) || a.localeCompare(b));
}

/** The text a number or list field starts from: the value as the admin would type it. */
export function fieldText(option: SecurityOption, value: SecurityOptionValue): string {
  if (Array.isArray(value)) return value.join(", ");
  return option.kind === "int" && typeof value === "number" ? String(value) : "";
}

/** The draft a number field's text stands for: `NaN` until it is a number (`intProblem` says so). */
export function parseInteger(text: string): number {
  return text.trim() === "" ? Number.NaN : Number(text);
}

/** Text a list field is typed in, one entry per comma or line. */
export function parseList(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function sameValue(a: SecurityOptionValue, b: SecurityOptionValue): boolean {
  // ``null`` is a reset, never the same as a value - not even an empty list.
  if (a === null || b === null) return a === b;
  if (Array.isArray(a) || Array.isArray(b)) {
    return JSON.stringify(a) === JSON.stringify(b);
  }
  return a === b;
}

/** The PUT body: only the editable options whose draft differs from their effective value. */
export function changedValues(
  options: SecurityOption[],
  draft: Record<string, SecurityOptionValue>,
): Record<string, SecurityOptionValue> {
  const changes: Record<string, SecurityOptionValue> = {};
  for (const option of options) {
    if (option.locked || !(option.key in draft)) continue;
    const next = draft[option.key];
    if (!sameValue(next, option.value)) changes[option.key] = next;
  }
  return changes;
}

/** An integer draft outside the option's bounds, as the message to show, else `null`. */
export function intProblem(option: SecurityOption, value: SecurityOptionValue): string | null {
  // ``null`` is a reset to the default, not a number typed in.
  if (option.kind !== "int" || value === null) return null;
  if (typeof value !== "number" || !Number.isInteger(value)) return "Enter a whole number";
  if (option.off_value !== null && value === option.off_value) return null;
  if ((option.minimum !== null && value < option.minimum) || (option.maximum !== null && value > option.maximum)) {
    return option.off_value !== null ? "{off} (off), or between {minimum} and {maximum}" : "Between {minimum} and {maximum}";
  }
  return null;
}

/**
 * The entries of a list draft the backend would refuse, as `parse_trusted_origins`
 * and the module check of `security_options.py` read them: an origin needs a
 * scheme and a host, a module name is dotted Python identifiers. Tools are
 * picked from the scanned list, so never typed wrong.
 */
export function invalidListEntries(option: SecurityOption, value: SecurityOptionValue): string[] {
  if (!Array.isArray(value)) return [];
  if (option.kind === "origins") return value.filter((entry) => !isOrigin(entry));
  if (option.kind === "modules") {
    return value.filter((entry) => !entry.split(".").every((part) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(part)));
  }
  return [];
}

function isOrigin(entry: string): boolean {
  try {
    const url = new URL(entry);
    return url.protocol.length > 1 && url.hostname !== "";
  } catch {
    return false;
  }
}

/** A list draft with an entry the backend would refuse, as the message to show, else `null`. */
export function listProblem(option: SecurityOption, value: SecurityOptionValue): string | null {
  if (invalidListEntries(option, value).length === 0) return null;
  return option.kind === "origins"
    ? "Not an origin with a scheme and host: {entries}. Example: {example}"
    : "Not a Python module name: {entries}";
}

/** A value as the read-only view shows it (translation keys for booleans and choices). */
export function displayValue(option: SecurityOption, value: SecurityOptionValue): string {
  if (option.kind === "bool") return value ? "On" : "Off";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  if (value === null || value === "") return "None";
  if (option.kind === "choice") {
    return (SECURITY_OPTION_CHOICE_LABELS as Record<string, string>)[String(value)] ?? String(value);
  }
  return String(value);
}
