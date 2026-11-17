/**
 * Unit tests for the short pause-cause labels - `yarn test:unit`.
 *
 * An assignment the platform paused on its own is shown as auto-paused in the
 * agent's assignment list, with the cause one hover away, instead of the bare
 * "Paused" a user's own toggle gets.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { PAUSE_REASON_SUBTITLES, isAssignmentActive, isAutoPaused } from "./pause-reasons.ts";

test("the platform's own pauses read as auto-paused", () => {
  for (const reason of ["auth_quarantine", "stale_reference", "invalid_schedule"]) {
    assert.equal(isAutoPaused({ enabled: false, paused_reason: reason }), true, reason);
  }
});

test("a person's pause, a copy born off and an unknown cause do not", () => {
  for (const reason of ["user_toggle", "agent_tool", "created_disabled", "system_seed", "typo_reason"]) {
    assert.equal(isAutoPaused({ enabled: false, paused_reason: reason }), false, reason);
  }
  assert.equal(isAutoPaused({ enabled: false, paused_reason: null }), false);
});

test("an enabled assignment is never auto-paused, whatever stamp it carries", () => {
  assert.equal(isAutoPaused({ enabled: true, paused_reason: "stale_reference" }), false);
});

test("every auto-pause cause has a short label", () => {
  assert.equal(PAUSE_REASON_SUBTITLES.stale_reference, "auto-paused: integration resource missing");
  assert.ok(PAUSE_REASON_SUBTITLES.auth_quarantine);
  assert.ok(PAUSE_REASON_SUBTITLES.invalid_schedule);
});

test("an enabled assignment whose owner is deactivated is not active", () => {
  assert.equal(isAssignmentActive({ enabled: true, paused_by_owner_deactivation: true }), false);
  assert.equal(isAssignmentActive({ enabled: true, paused_by_owner_deactivation: false }), true);
  assert.equal(isAssignmentActive({ enabled: false, paused_by_owner_deactivation: false }), false);
  // A payload from before the flag existed reads as it always did.
  assert.equal(isAssignmentActive({ enabled: true }), true);
});
