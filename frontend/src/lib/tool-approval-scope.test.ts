/**
 * Unit tests for where tool approvals are shown and changed - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  approvalsShown,
  effectiveApproval,
  onPersonalList,
  PERSONAL_APPROVALS_PATH,
  PLATFORM_APPROVALS_PATH,
  toolApprovalLink,
  toolApprovalsHref,
} from "./tool-approval-scope.ts";

test("nothing is shown before the answer is known", () => {
  assert.equal(approvalsShown(undefined), false);
  assert.equal(approvalsShown(undefined, { approveToolCalls: true }), false);
});

test("the platform shows approvals only while they are in use", () => {
  assert.equal(approvalsShown({ approvals_in_use: false, tool_approvals_enabled: true }), false);
  assert.equal(approvalsShown({ approvals_in_use: true, tool_approvals_enabled: true }), true);
  // A response without the field (an older server) shows nothing rather than guessing.
  assert.equal(approvalsShown({ tool_approvals_enabled: true, approve_tool_calls_default: true }), false);
});

test("an agent shows approvals only while it asks", () => {
  const off = { tool_approvals_enabled: true, approve_tool_calls_default: false, approvals_in_use: true };
  assert.equal(approvalsShown(off, { approveToolCalls: null }), false);
  assert.equal(approvalsShown(off, { approveToolCalls: false }), false);
  assert.equal(approvalsShown(off, { approveToolCalls: true }), true);
  const on = { ...off, approve_tool_calls_default: true };
  assert.equal(approvalsShown(on, { approveToolCalls: null }), true);
  assert.equal(approvalsShown(on, { approveToolCalls: false }), false);
});

test("a disabled agent asks nothing, whatever it is set to", () => {
  const on = { tool_approvals_enabled: true, approve_tool_calls_default: true, approvals_in_use: true };
  assert.equal(approvalsShown(on, { approveToolCalls: true, enabled: false }), false);
  assert.equal(approvalsShown(on, { approveToolCalls: null, enabled: false }), false);
  assert.equal(approvalsShown(on, { approveToolCalls: true, enabled: true }), true);
});

test("suspended approvals show nothing, whatever an agent is set to", () => {
  const suspended = { tool_approvals_enabled: false, approve_tool_calls_default: true, approvals_in_use: false };
  assert.equal(approvalsShown(suspended), false);
  assert.equal(approvalsShown(suspended, { approveToolCalls: true }), false);
});

test("an agent's own setting wins over the platform default", () => {
  assert.equal(effectiveApproval(true, false), true);
  assert.equal(effectiveApproval(false, true), false);
  assert.equal(effectiveApproval(null, true), true);
  assert.equal(effectiveApproval(undefined, undefined), undefined);
});

test("a tool's approval is changed in Settings by an admin, in the profile by anyone else", () => {
  assert.equal(toolApprovalsHref(undefined, true), PLATFORM_APPROVALS_PATH);
  assert.equal(toolApprovalsHref(undefined, false), PERSONAL_APPROVALS_PATH);
  assert.equal(
    toolApprovalsHref("delete_issue", true),
    "/settings/security/approvals?tool=delete_issue",
  );
  assert.equal(toolApprovalsHref("a b&c", false), "/profile/approvals?tool=a%20b%26c");
});

test("the personal list holds the tools that ask first and the ones the reader always allows", () => {
  const none = new Set<string>();
  assert.equal(onPersonalList({ tool_key: "delete_issue", gates: true }, none), true);
  assert.equal(onPersonalList({ tool_key: "web_search", gates: false }, none), false);
  assert.equal(onPersonalList({ tool_key: "web_search", gates: false }, new Set(["web_search"])), true);
});

test("a tool that runs without asking links nowhere for a non-admin with nothing to change", () => {
  const none = new Set<string>();
  const open = { tool_key: "web_search", gates: false };
  assert.equal(toolApprovalLink(open, false, none), null);
  assert.equal(toolApprovalLink(open, true, none), "/settings/security/approvals?tool=web_search");
  assert.equal(toolApprovalLink(open, false, new Set(["web_search"])), "/profile/approvals?tool=web_search");
  assert.equal(
    toolApprovalLink({ tool_key: "delete_issue", gates: true }, false, none),
    "/profile/approvals?tool=delete_issue",
  );
});
