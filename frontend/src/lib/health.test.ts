/**
 * Unit tests for the Platform Health labels (#8261) - `yarn test:unit`.
 *
 * An unknown code is shown as itself, never as a known label; a refusal reads
 * by its code, the generic message otherwise; a target links to its own page.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  HEALTH_SIGNAL_LABELS,
  healthReasonLabel,
  healthRefusalCodeKey,
  healthRefusalKey,
  defaultHealthName,
  healthTargetHref,
  labelOf,
} from "./health.ts";

test("a known code reads its label, an unknown one reads as itself", () => {
  assert.equal(labelOf(HEALTH_SIGNAL_LABELS, "cost_drift"), "Cost drift");
  assert.equal(labelOf(HEALTH_SIGNAL_LABELS, "future_signal"), "future_signal");
  assert.equal(labelOf(HEALTH_SIGNAL_LABELS, "toString"), "toString");
  assert.equal(healthReasonLabel("unmeasured"), "unmeasured");
});

test("a refusal reads by its code, the generic message otherwise", () => {
  assert.equal(healthRefusalKey({ detail: { code: "cycle_open" } }), "{name} is already working on this target.");
  assert.equal(healthRefusalKey({ detail: { code: "constructor" } }), "{name} could not start this work.");
  assert.equal(healthRefusalKey(new Error("boom")), "{name} could not start this work.");
  assert.equal(healthRefusalKey({ detail: { code: null } }), "{name} could not start this work.");
});

test("a budget refusal code reads its translated key, an unknown one none (#794)", () => {
  assert.equal(healthRefusalCodeKey("concierge_disabled"), "{name} is not enabled.");
  assert.equal(healthRefusalCodeKey("ai_disabled"), "Agentic AI is turned off on this platform.");
  assert.equal(healthRefusalCodeKey("future_code"), null);
  assert.equal(healthRefusalCodeKey(null), null);
});

test("a target links to its own page", () => {
  assert.deepEqual(
    [
      healthTargetHref("agent", "a1"),
      healthTargetHref("flow", "f1"),
      healthTargetHref("assignment", "s1"),
      healthTargetHref(null, "x"),
      healthTargetHref("agent", null),
    ],
    ["/agents/a1", "/flows/library/f1", "/agents/assignments/s1", null, null],
  );
});

test("the default name is Platform Health in every mode, as the server's", () => {
  assert.equal(defaultHealthName(), "Platform Health");
});
