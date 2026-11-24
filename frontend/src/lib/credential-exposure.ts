// SPDX-License-Identifier: Apache-2.0

/**
 * How a stored credential's exposure is said: whose it is and who can
 * act with it. Shared by the sharing confirmation, the integration overview
 * and the list cards.
 *
 * Dependency-free on purpose (display names are passed in): `yarn test:unit`
 * runs it directly.
 */

export type Translate = (key: string, values?: Record<string, string | number>) => string;

type CredentialKindName = "none" | "oauth_account" | "api_key" | "bot_token" | "platform_managed";

export interface AudienceText {
  everyone: boolean;
  people_count: number;
  people: { name: string }[];
  groups: { name: string }[];
  public_link: boolean;
  anonymous_channels: string[];
  mcp_key_holders: boolean;
  a2a_partners?: boolean;
  webhook_callers?: boolean;
}

/** "Your GitHub token or API key", "Alice's Jira connected account", "The company's Slack integration". */
export function credentialOwnerLabel(
  t: Translate,
  input: {
    provider: string;
    kind: CredentialKindName;
    isCompany: boolean;
    isYours: boolean;
    holderName: string | null;
  },
): string {
  const provider = input.provider;
  if (input.isCompany) return t("The company's {provider} integration", { provider });
  if (input.kind === "oauth_account") {
    if (input.isYours) return t("Your {provider} connected account", { provider });
    return input.holderName
      ? t("{name}'s {provider} connected account", { name: input.holderName, provider })
      : t("Someone else's {provider} connected account", { provider });
  }
  if (input.kind === "bot_token") {
    if (input.isYours) return t("Your {provider} bot token", { provider });
    return input.holderName
      ? t("{name}'s {provider} bot token", { name: input.holderName, provider })
      : t("Someone else's {provider} bot token", { provider });
  }
  if (input.isYours) return t("Your {provider} token or API key", { provider });
  return input.holderName
    ? t("{name}'s {provider} token or API key", { name: input.holderName, provider })
    : t("Someone else's {provider} token or API key", { provider });
}

/** At most this many groups and people are named before "...". */
const NAMED_AUDIENCE_LIMIT = 5;

/** "12 people (Support, Alice), anyone with its public chat link" - who an audience is. */
export function describeAudience(
  t: Translate,
  audience: AudienceText,
  channelLabel: (channel: string) => string = (channel) => channel,
): string {
  const parts: string[] = [];
  if (audience.everyone) {
    parts.push(t("{count, plural, one {everyone on the platform (# person)} other {everyone on the platform (# people)}}", { count: audience.people_count }));
  } else if (audience.people_count > 0) {
    const named = [...audience.groups.map((g) => g.name), ...audience.people.map((p) => p.name)];
    parts.push(
      named.length
        ? t("{count, plural, one {# person} other {# people}} ({names})", {
            count: audience.people_count,
            names: named.slice(0, NAMED_AUDIENCE_LIMIT).join(", ") + (named.length > NAMED_AUDIENCE_LIMIT ? ", ..." : ""),
          })
        : t("{count, plural, one {# person} other {# people}}", { count: audience.people_count }),
    );
  }
  if (audience.public_link) parts.push(t("anyone with its public chat link"));
  if (audience.anonymous_channels.length) {
    parts.push(
      t("anyone who messages it on {channels} without an account", {
        channels: audience.anonymous_channels.map(channelLabel).join(", "),
      }),
    );
  }
  if (audience.mcp_key_holders) parts.push(t("anyone holding the platform MCP key"));
  if (audience.a2a_partners) parts.push(t("its A2A partners"));
  if (audience.webhook_callers) parts.push(t("anyone holding one of its webhook URLs"));
  return parts.join(", ");
}

/** Whether an audience reaches anybody at all. */
export function audienceIsEmpty(audience: AudienceText): boolean {
  return !(
    audience.everyone
    || audience.people_count > 0
    || audience.public_link
    || audience.anonymous_channels.length > 0
    || audience.mcp_key_holders
    || audience.a2a_partners
    || audience.webhook_callers
  );
}

/** Whether a confirmation takes access away: what it lists says so, and a
 *  preview that lists nothing (a cut one) is what its caller asked for. */
export function confirmsAnUnshare(
  items: { gained: AudienceText }[],
  mode: "share" | "unshare" | "delete",
): boolean {
  return items.length ? items.every((i) => audienceIsEmpty(i.gained)) : mode === "unshare";
}

export type LentKindName =
  | "integration"
  | "mcp_server"
  | "custom_tool"
  | "channel_bot"
  | "variable"
  | "knowledge_base"
  | "skill"
  | "prompt";

const MCP_SECRET_WORDING: Record<string, string> = {
  mcp_oauth_token: "MCP server {name} and its connected account",
  mcp_headers: "MCP server {name} and its headers",
  mcp_env: "MCP server {name} and its environment variables",
};

/**
 * Whose lent resource it is, for every kind: "Your MCP server Tickets
 * and its headers", "Alice's secret variable token", "The company's Slack bot
 * Support". Integrations keep `credentialOwnerLabel`.
 */
export function lentResourceLabel(
  t: Translate,
  input: {
    kind: LentKindName;
    secretKind: string;
    name: string;
    provider: string;
    isCompany: boolean;
    isYours: boolean;
    holderName: string | null;
  },
): string {
  if (input.kind === "integration") {
    const known = ["none", "oauth_account", "api_key", "bot_token", "platform_managed"];
    return credentialOwnerLabel(t, {
      provider: input.provider,
      kind: (known.includes(input.secretKind) ? input.secretKind : "api_key") as CredentialKindName,
      isCompany: input.isCompany,
      isYours: input.isYours,
      holderName: input.holderName,
    });
  }
  const name = input.name;
  if (input.kind === "channel_bot") return t("The company's {provider} bot {name}", { provider: input.provider, name });
  let thing: string;
  if (input.kind === "mcp_server") thing = t(MCP_SECRET_WORDING[input.secretKind] ?? "MCP server {name}", { name });
  else if (input.kind === "custom_tool") thing = t("custom tool {name} and its code", { name });
  else if (input.kind === "knowledge_base") thing = t("knowledge base {name} and its documents", { name });
  else if (input.kind === "skill") thing = t("skill {name} and its instructions", { name });
  else if (input.kind === "prompt") thing = t("prompt {name}", { name });
  else thing = input.secretKind === "secret_value" ? t("secret variable {name}", { name }) : t("variable {name}", { name });
  if (input.isCompany) return t("The company's {thing}", { thing });
  if (input.isYours) return t("Your {thing}", { thing });
  return input.holderName
    ? t("{holder}'s {thing}", { holder: input.holderName, thing })
    : t("Someone else's {thing}", { thing });
}

const SHARING_FIELDS = [
  "is_company_managed",
  "access_restricted",
  "shared_group_ids",
  "shared_user_ids",
  "shared_group_permissions",
  "shared_user_permissions",
] as const;

/** The sharing part of an entity's update payload, as a preview `change`. */
export function pickSharingChange(payload: Record<string, unknown>): Record<string, unknown> {
  const change: Record<string, unknown> = {};
  for (const field of SHARING_FIELDS) {
    if (field in payload && payload[field] !== undefined) change[field] = payload[field];
  }
  return change;
}

/** The integration-only preview request (`integration_ids`, flat sharing fields). */
export interface FlatPreviewRequest {
  entity_type: string;
  entity_id?: string | null;
  integration_ids?: string[];
  graph?: Record<string, unknown>;
  is_company_managed?: boolean;
  access_restricted?: boolean;
  shared_group_ids?: string[] | null;
  shared_user_ids?: string[] | null;
  web_enabled?: boolean;
  new_owner_id?: string;
  withhold_connected_account?: boolean;
}

/** `POST /exposure/preview`'s body. */
export interface GenericPreviewRequest {
  entity_type: string;
  entity_id?: string | null;
  parent_id?: string | null;
  change: Record<string, unknown>;
}

const FLAT_FIELDS = [
  "graph",
  "is_company_managed",
  "access_restricted",
  "shared_group_ids",
  "shared_user_ids",
  "web_enabled",
  "new_owner_id",
] as const;

/**
 * One preview request for every surface: a generic one passes through; an
 * integration-only one is read as the change it describes, so a
 * surface that asks about integrations is also told about every other
 * resource the same change lends or takes back.
 */
export function toGenericPreviewRequest(request: FlatPreviewRequest | GenericPreviewRequest): GenericPreviewRequest {
  if ("change" in request && request.change) return request as GenericPreviewRequest;
  const flat = request as FlatPreviewRequest;
  const change: Record<string, unknown> = {};
  for (const field of FLAT_FIELDS) {
    if (field in flat) change[field] = flat[field];
  }
  if (flat.withhold_connected_account !== undefined) {
    change.keep_oauth_credentials_on_share = !flat.withhold_connected_account;
  }
  if (flat.integration_ids) {
    const ids = flat.integration_ids;
    if (flat.entity_type === "knowledge_base") change.sync_source_integration_ids = ids;
    else if (flat.entity_type === "skill") change.source_integration_id = ids[0] ?? null;
    else if (flat.entity_type !== "integration") change.integration_bindings = { preview: ids };
  }
  return { entity_type: flat.entity_type, entity_id: flat.entity_id ?? null, change };
}
