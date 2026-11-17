/**
 * Unit tests for the one-click approval of an escalated action - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { approvalCodesIn, approvalReply } from "./hitl-approval.ts";

// The question an agent relays for an escalated OpenCTI merge
// (`merge_opencti_entities`): the entities, then the reply to send.
const MERGE_QUESTION = [
  "I found duplicate intrusion sets and need your approval to merge them.",
  "",
  "Keep: 'APT29' (Intrusion-Set, 5c1a...)",
  "Delete, merging into the above:",
  "  - 'Cozy Bear' (Intrusion-Set, 9d2e...)",
  "",
  "Your reply must START with `APPROVE M-3F2A9C10` to confirm.",
].join("\n");

test("the code a question asks to approve is found", () => {
  assert.deepEqual(approvalCodesIn(MERGE_QUESTION), ["M-3F2A9C10"]);
});

test("the approval is found however the agent quotes it", () => {
  for (const question of [
    "Reply APPROVE M-3F2A9C10 to merge.",
    "Reply **APPROVE M-3F2A9C10** to merge.",
    "Reply `APPROVE` `M-3F2A9C10` to merge.",
    "Reply APPROVE: M-3F2A9C10",
    "Reply \"APPROVE M-3F2A9C10\".",
    "reply approve m-3f2a9c10",
  ]) {
    assert.deepEqual(approvalCodesIn(question), ["M-3F2A9C10"], question);
  }
});

test("several merges asked at once are all found, once each", () => {
  const question =
    "Merge 1: reply APPROVE M-3F2A9C10.\nMerge 2: reply APPROVE M-7D41E0B2.\n" +
    "Or both at once: APPROVE M-3F2A9C10, APPROVE M-7D41E0B2";
  assert.deepEqual(approvalCodesIn(question), ["M-3F2A9C10", "M-7D41E0B2"]);
});

test("a question that asks for no approval code offers nothing", () => {
  for (const question of [
    null,
    undefined,
    "",
    "Which of the two reports should I publish?",
    "The merge M-3F2A9C10 was approved yesterday.",
    "Should I DISAPPROVE M-3F2A9C10?",
    "Reply APPROVE M-3F2A9C10X to merge.",
    "Reply APPROVE M-3F2A9C1 to merge.",
  ]) {
    assert.deepEqual(approvalCodesIn(question), [], String(question));
  }
});

test("the reply is exactly what the person would type", () => {
  assert.equal(approvalReply(["M-3F2A9C10"]), "APPROVE M-3F2A9C10");
  // Two approvals back to back, each with its own keyword: the form the
  // backend parser approves both codes for (tests/test_hitl_one_click_approval.py).
  assert.equal(approvalReply(["M-3F2A9C10", "M-7D41E0B2"]), "APPROVE M-3F2A9C10, APPROVE M-7D41E0B2");
});
