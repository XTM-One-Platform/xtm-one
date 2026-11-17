/**
 * Unit tests for the tool selection a chat send carries - `yarn test:unit`.
 *
 * A message sent in an open conversation before its tool selection is
 * resolved (the agents list still loading) must carry no selection, so the
 * server runs the agent's own tools instead of reading `[]` as "no tools".
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { sessionToolSelectionForSend, sessionToolsResolved } from "./session-tool-selection.ts";

const selection = {
  builtin_tool_ids: ["generate_report"],
  custom_tool_ids: [],
  integration_ids: ["i-1"],
  mcp_server_ids: [],
  knowledge_base_ids: [],
};

test("an open conversation whose selection is not resolved sends no selection", () => {
  assert.equal(sessionToolsResolved("c-1", null), false);
  assert.deepEqual(sessionToolSelectionForSend("c-1", null, selection), {});
});

test("a selection resolved for another conversation is not sent in this one", () => {
  assert.equal(sessionToolsResolved("c-2", "c-1"), false);
  assert.deepEqual(sessionToolSelectionForSend("c-2", "c-1", selection), {});
});

test("a selection resolved for the open conversation is sent, an empty one included", () => {
  assert.deepEqual(sessionToolSelectionForSend("c-1", "c-1", selection), selection);
  const none = { ...selection, builtin_tool_ids: [], integration_ids: [] };
  assert.deepEqual(sessionToolSelectionForSend("c-1", "c-1", none), none);
});

test("with no conversation open the session selection is sent as it is", () => {
  assert.equal(sessionToolsResolved(null, null), true);
  assert.deepEqual(sessionToolSelectionForSend(null, null, selection), selection);
  assert.deepEqual(sessionToolSelectionForSend(undefined, "c-1", selection), selection);
});
