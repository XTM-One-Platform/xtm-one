/**
 * Unit tests for the General Assistant's stand-in rule - `yarn test:unit`.
 *
 * The agent linked to the General Assistant answers in its place only when
 * every user can chat with it, the rule the backend applies in
 * `services/general_assistant_link.py`. The sidebar hid the General Assistant
 * entry for any link, a private agent included, which left users with
 * neither.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canStandInForGeneralAssistant,
  conversationAgentId,
  generalAssistantStandIn,
  standInRefusal,
} from "./general-assistant-stand-in.ts";

const ctem = { id: "ctem", enabled: true, disable_chat: false, is_company_managed: true, access_restricted: false };

test("an enabled company-managed chat agent stands in", () => {
  assert.equal(canStandInForGeneralAssistant(ctem), true);
  assert.equal(generalAssistantStandIn("ctem", [ctem]), ctem);
});

for (const [label, overrides, refusal] of [
  ["a private agent", { is_company_managed: false }, "not_company_managed"],
  ["an access-restricted agent", { access_restricted: true }, "access_restricted"],
  ["a disabled agent", { enabled: false }, "disabled"],
  ["an agent with chat disabled", { disable_chat: true }, "chat_disabled"],
] as const) {
  test(`${label} does not stand in, and says why`, () => {
    const agent = { ...ctem, ...overrides };
    assert.equal(canStandInForGeneralAssistant(agent), false);
    assert.equal(standInRefusal(agent), refusal);
    assert.equal(generalAssistantStandIn("ctem", [agent]), null);
  });
}

test("the reason is the first rule the backend checks", () => {
  assert.equal(standInRefusal({ ...ctem, is_company_managed: false, enabled: false }), "not_company_managed");
  assert.equal(standInRefusal(ctem), null);
});

test("no link, an unknown agent or agents still loading is no stand-in", () => {
  assert.equal(generalAssistantStandIn(null, [ctem]), null);
  assert.equal(generalAssistantStandIn("other", [ctem]), null);
  assert.equal(generalAssistantStandIn("ctem", undefined), null);
});

test("a conversation is answered by its own agent, else by the one the server names", () => {
  // A Slack or Teams conversation the General Assistant owns, continued on the web.
  assert.equal(conversationAgentId({ agent_id: null, answering_agent_id: "ctem" }), "ctem");
  assert.equal(conversationAgentId({ agent_id: "bound", answering_agent_id: "ctem" }), "bound");
  // The General Assistant: no agent, and none standing in for it.
  assert.equal(conversationAgentId({ agent_id: null, answering_agent_id: null }), null);
  assert.equal(conversationAgentId({ agent_id: null }), null);
  assert.equal(conversationAgentId(undefined), null);
});
