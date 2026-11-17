/**
 * Unit tests for a run's held state - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { evaluationRunPollTier, heldCause, runHeldReason } from "./held.ts";

test("what a hold is waiting for is worded per platform mode", () => {
  assert.equal(heldCause("ai_disabled", true), "ai_disabled");
  assert.equal(heldCause("ai_disabled", false), "ai_disabled");
  assert.equal(heldCause("license_required", true), "community_edition");
  // Copilot mode has no Community Edition: the license is what is missing.
  assert.equal(heldCause("license_required", false), "license");
});

test("a hold on the quota store is never worded as missing AI", () => {
  assert.equal(heldCause("quota_unavailable", true), "quota_store");
  assert.equal(heldCause("quota_unavailable", false), "quota_store");
});

const marker = {
  ai_hold: {
    reason: "license_required",
    message: "Held: AI is not available on this platform (Community Edition).",
    since: "2026-09-01T08:00:00+00:00",
  },
  quota_metered: false,
};

test("a run still waiting reads as held, with the refusal code", () => {
  for (const status of ["pending", "delayed", "awaiting_human"]) {
    assert.equal(runHeldReason({ status, trigger_context: marker }), "license_required");
  }
});

test("a run that ran or ended is never held, whatever marker it kept", () => {
  // The marker stays on a run failed at the hold's bound, cancelled or
  // dismissed while held, or dead-lettered: it is history, not state.
  for (const status of ["running", "completed", "failed", "cancelled", "skipped", "queued"]) {
    assert.equal(runHeldReason({ status, trigger_context: marker }), null, status);
  }
});

test("an open evaluation run refreshes until it ends, slowly while held", () => {
  // A held run resumes by itself: the page must notice without a reload.
  assert.equal(evaluationRunPollTier({ status: "pending", held_reason: "license_required" }), "standard");
  assert.equal(evaluationRunPollTier({ status: "pending", held_reason: null }), "fast");
  assert.equal(evaluationRunPollTier({ status: "running" }), "fast");
  for (const status of ["completed", "failed", "cancelled"]) {
    assert.equal(evaluationRunPollTier({ status, held_reason: "ai_disabled" }), null, status);
  }
  assert.equal(evaluationRunPollTier(undefined), null);
});

test("a run without a usable marker is not held", () => {
  for (const trigger_context of [null, undefined, {}, { ai_hold: null }, { ai_hold: "x" }, { ai_hold: { reason: "" } }]) {
    assert.equal(runHeldReason({ status: "pending", trigger_context }), null);
  }
});
