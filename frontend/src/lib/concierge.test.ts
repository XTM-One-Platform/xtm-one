/**
 * Unit tests for the Concierge labels (#8261) - `yarn test:unit`.
 *
 * An unknown code is shown as itself, never as a known label; a refusal reads
 * by its code, the generic message otherwise; a target links to its own page.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CONCIERGE_SIGNAL_LABELS,
  conciergeReasonLabel,
  conciergeRefusalKey,
  defaultConciergeName,
  conciergeTargetHref,
  labelOf,
} from "./concierge.ts";

test("a known code reads its label, an unknown one reads as itself", () => {
  assert.equal(labelOf(CONCIERGE_SIGNAL_LABELS, "cost_drift"), "Cost drift");
  assert.equal(labelOf(CONCIERGE_SIGNAL_LABELS, "future_signal"), "future_signal");
  assert.equal(labelOf(CONCIERGE_SIGNAL_LABELS, "toString"), "toString");
  assert.equal(conciergeReasonLabel("unmeasured"), "unmeasured");
});

test("a refusal reads by its code, the generic message otherwise", () => {
  assert.equal(conciergeRefusalKey({ detail: { code: "cycle_open" } }), "{name} is already working on this target.");
  assert.equal(conciergeRefusalKey({ detail: { code: "constructor" } }), "{name} could not start this work.");
  assert.equal(conciergeRefusalKey(new Error("boom")), "{name} could not start this work.");
  assert.equal(conciergeRefusalKey({ detail: { code: null } }), "{name} could not start this work.");
});

test("a target links to its own page", () => {
  assert.deepEqual(
    [
      conciergeTargetHref("agent", "a1"),
      conciergeTargetHref("flow", "f1"),
      conciergeTargetHref("assignment", "s1"),
      conciergeTargetHref(null, "x"),
      conciergeTargetHref("agent", null),
    ],
    ["/agents/a1", "/flows/f1/builder", "/assignments/s1", null, null],
  );
});

test("the default name follows the platform mode, as the server's", () => {
  assert.equal(defaultConciergeName("copilot"), "Alfred");
  assert.equal(defaultConciergeName("xtm_one"), "Concierge");
  assert.equal(defaultConciergeName("dev"), "Concierge");
  assert.equal(defaultConciergeName(undefined), "Concierge");
});
