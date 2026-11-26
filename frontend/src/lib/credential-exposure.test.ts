/**
 * Unit tests for how a credential's exposure is said - `yarn test:unit`.
 *
 * Whose credential it is must never be ambiguous (yours, a named colleague's,
 * the company's), and an audience names what reaches it beyond the people it
 * counts: the public link, anonymous channel users, the platform MCP key.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  audienceIsEmpty,
  confirmsAnUnshare,
  credentialOwnerLabel,
  describeAudience,
  lentResourceLabel,
  pickSharingChange,
  toGenericPreviewRequest,
  type AudienceText,
} from "./credential-exposure.ts";

/** Formats like the catalog's English: `{x}` replaced, `{count, plural, ...}` read. */
function t(key: string, values: Record<string, string | number> = {}): string {
  const plural = key.match(/^\{count, plural, one \{(.*)\} other \{(.*)\}\}(.*)$/s);
  let text = key;
  if (plural) {
    const count = Number(values.count);
    text = (count === 1 ? plural[1] : plural[2]).replace("#", String(count)) + plural[3];
  }
  return text.replace(/\{(\w+)\}/g, (_m, name: string) => String(values[name] ?? `{${name}}`));
}

const nobody: AudienceText = {
  everyone: false,
  people_count: 0,
  people: [],
  groups: [],
  public_link: false,
  anonymous_channels: [],
  mcp_key_holders: false,
};

test("whose credential it is reads unambiguously", () => {
  const base = { provider: "GitHub", holderName: "Alice", isYours: false, isCompany: false };
  assert.equal(credentialOwnerLabel(t, { ...base, kind: "api_key", isYours: true }), "Your GitHub token or API key");
  assert.equal(credentialOwnerLabel(t, { ...base, kind: "oauth_account" }), "Alice's GitHub connected account");
  assert.equal(credentialOwnerLabel(t, { ...base, kind: "bot_token" }), "Alice's GitHub bot token");
  assert.equal(credentialOwnerLabel(t, { ...base, kind: "oauth_account", isCompany: true }), "The company's GitHub integration");
  assert.equal(
    credentialOwnerLabel(t, { ...base, kind: "api_key", holderName: null }),
    "Someone else's GitHub token or API key",
  );
});

test("an audience names its groups and people, then what no account names", () => {
  const text = describeAudience(
    t,
    {
      ...nobody,
      people_count: 3,
      groups: [{ name: "Support" }],
      people: [{ name: "Dan" }],
      public_link: true,
      anonymous_channels: ["slack"],
      mcp_key_holders: true,
    },
    (channel) => (channel === "slack" ? "Slack" : channel),
  );
  assert.equal(
    text,
    "3 people (Support, Dan), anyone with its public chat link, anyone who messages it on Slack without an account, anyone holding the platform MCP key",
  );
});

test("everyone is said as such, with the count", () => {
  assert.equal(describeAudience(t, { ...nobody, everyone: true, people_count: 1 }), "everyone on the platform (1 person)");
});

test("a long list of names is cut", () => {
  const people = ["A", "B", "C", "D", "E", "F"].map((name) => ({ name }));
  assert.equal(describeAudience(t, { ...nobody, people_count: 6, people }), "6 people (A, B, C, D, E, ...)");
});

test("nobody is an empty sentence", () => {
  assert.equal(describeAudience(t, nobody), "");
});

test("every lent resource says whose it is and what it holds", () => {
  const base = { name: "Tickets", provider: "", isCompany: false, isYours: false, holderName: "Alice" };
  assert.equal(lentResourceLabel(t, { ...base, kind: "mcp_server", secretKind: "mcp_headers", isYours: true }), "Your MCP server Tickets and its headers");
  assert.equal(lentResourceLabel(t, { ...base, kind: "mcp_server", secretKind: "mcp_oauth_token" }), "Alice's MCP server Tickets and its connected account");
  assert.equal(lentResourceLabel(t, { ...base, kind: "custom_tool", secretKind: "code", holderName: null }), "Someone else's custom tool Tickets and its code");
  assert.equal(lentResourceLabel(t, { ...base, kind: "variable", secretKind: "secret_value", isCompany: true }), "The company's secret variable Tickets");
  assert.equal(lentResourceLabel(t, { ...base, kind: "variable", secretKind: "plain_value" }), "Alice's variable Tickets");
  assert.equal(lentResourceLabel(t, { ...base, kind: "channel_bot", secretKind: "bot_token", provider: "Slack", name: "Support" }), "The company's Slack bot Support");
  assert.equal(lentResourceLabel(t, { ...base, kind: "integration", secretKind: "oauth_account", provider: "GitHub" }), "Alice's GitHub connected account");
});

test("an attached knowledge base, skill or prompt says whose it is", () => {
  const base = { name: "Playbooks", provider: "", isCompany: false, isYours: false, holderName: "Alice" };
  assert.equal(lentResourceLabel(t, { ...base, kind: "knowledge_base", secretKind: "documents", isYours: true }), "Your knowledge base Playbooks and its documents");
  assert.equal(lentResourceLabel(t, { ...base, kind: "skill", secretKind: "instructions" }), "Alice's skill Playbooks and its instructions");
  assert.equal(lentResourceLabel(t, { ...base, kind: "prompt", secretKind: "instructions", holderName: null }), "Someone else's prompt Playbooks");
});

test("A2A partners and webhook callers are named, and count as someone", () => {
  assert.equal(describeAudience(t, { ...nobody, a2a_partners: true, webhook_callers: true }), "its A2A partners, anyone holding one of its webhook URLs");
  assert.equal(audienceIsEmpty(nobody), true);
  assert.equal(audienceIsEmpty({ ...nobody, webhook_callers: true }), false);
});

test("a confirmation that lists nothing (a cut preview) unshares only when asked to", () => {
  assert.equal(confirmsAnUnshare([], "unshare"), true);
  assert.equal(confirmsAnUnshare([], "share"), false);
  assert.equal(confirmsAnUnshare([], "delete"), false);
  // What it lists decides otherwise, whatever the mode.
  assert.equal(confirmsAnUnshare([{ gained: nobody }], "share"), true);
  assert.equal(confirmsAnUnshare([{ gained: { ...nobody, people_count: 2 } }], "unshare"), false);
});

test("an integration-only preview request is read as the change it describes", () => {
  assert.deepEqual(
    toGenericPreviewRequest({ entity_type: "agent", entity_id: "a", integration_ids: ["i"], web_enabled: true }),
    { entity_type: "agent", entity_id: "a", change: { web_enabled: true, integration_bindings: { preview: ["i"] } } },
  );
  assert.deepEqual(
    toGenericPreviewRequest({ entity_type: "knowledge_base", entity_id: "k", integration_ids: ["i"] }).change,
    { sync_source_integration_ids: ["i"] },
  );
  assert.deepEqual(
    toGenericPreviewRequest({ entity_type: "skill", entity_id: "s", integration_ids: [] }).change,
    { source_integration_id: null },
  );
  assert.deepEqual(
    toGenericPreviewRequest({ entity_type: "integration", entity_id: "i", shared_group_ids: ["g"], withhold_connected_account: true }).change,
    { shared_group_ids: ["g"], keep_oauth_credentials_on_share: false },
  );
  const generic = { entity_type: "variable", entity_id: "v", change: { delete: true } };
  assert.equal(toGenericPreviewRequest(generic), generic);
});

test("only the sharing part of a payload is previewed", () => {
  assert.deepEqual(
    pickSharingChange({ name: "x", is_company_managed: false, shared_group_ids: null, shared_user_ids: undefined }),
    { is_company_managed: false, shared_group_ids: null },
  );
});