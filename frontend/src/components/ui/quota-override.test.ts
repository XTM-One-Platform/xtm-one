/**
 * Unit tests for the quota override editor's empty / Unlimited rules -
 * `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  isUnlimitedOverride,
  normalizeQuotaOverride,
  overrideHasLimit,
  setUnlimitedOverride,
} from "./quota-override.ts";

test("an emptied limit field is saved as absent, so the override inherits", () => {
  assert.equal(normalizeQuotaOverride({ default: null, period: "daily" }), null);
  assert.equal(normalizeQuotaOverride({ default: null, per_model: {}, period: "monthly" }), null);
  assert.equal(normalizeQuotaOverride(null), null);
  assert.equal(normalizeQuotaOverride(undefined), null);
});

test("a number, including a 0 hard stop, is kept", () => {
  assert.deepEqual(normalizeQuotaOverride({ default: 0, period: "daily" }), { default: 0, period: "daily" });
  assert.deepEqual(normalizeQuotaOverride({ default: 25, period: "monthly", unlimited: false }), {
    default: 25,
    period: "monthly",
  });
  assert.deepEqual(normalizeQuotaOverride({ default: null, per_model: { "gpt-5": 10 } }), {
    default: null,
    per_model: { "gpt-5": 10 },
  });
});

test("the Unlimited switch stores the explicit marker and clears the number", () => {
  const on = setUnlimitedOverride({ default: 40, period: "yearly" }, true);
  assert.deepEqual(on, { default: null, period: "yearly", unlimited: true });
  assert.equal(isUnlimitedOverride(on), true);
  assert.equal(overrideHasLimit(on), false);
  assert.deepEqual(normalizeQuotaOverride(on), { default: null, period: "yearly", unlimited: true });
});

test("the Unlimited switch clears per-model caps, which would still be enforced", () => {
  const on = setUnlimitedOverride({ default: 40, per_model: { "gpt-5": 10 }, period: "monthly" }, true);
  assert.deepEqual(on, { default: null, period: "monthly", unlimited: true });
  assert.deepEqual(normalizeQuotaOverride({ unlimited: true, default: null, per_model: { "gpt-5": 10 } }), {
    unlimited: true,
    default: null,
  });
});

test("the Unlimited switch works on an empty override", () => {
  const on = setUnlimitedOverride(null, true);
  assert.deepEqual(on, { period: "monthly", unlimited: true, default: null });
});

test("turning Unlimited off with nothing typed falls back to inherit", () => {
  const off = setUnlimitedOverride({ default: null, period: "daily", unlimited: true }, false);
  assert.equal(isUnlimitedOverride(off), false);
  assert.equal("unlimited" in off, false);
  assert.equal(normalizeQuotaOverride(off), null);
});

test("a legacy empty field arrives from the API already marked Unlimited", () => {
  // The backend reads a stored {default: null} as the explicit marker, so the
  // editor shows the switch on and the next save persists the explicit form.
  const legacyAsServed = { default: null, period: "monthly" as const, unlimited: true };
  assert.equal(isUnlimitedOverride(legacyAsServed), true);
  assert.deepEqual(normalizeQuotaOverride(legacyAsServed), legacyAsServed);
});
