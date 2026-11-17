/**
 * Unit tests for when the toast provider drops a toast - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { DEFAULT_TOAST_MS, forcedRemovalDelay } from "./toast-timing.ts";

test("a toast without an action is dropped after its duration, 5 s by default", () => {
  assert.equal(DEFAULT_TOAST_MS, 5000);
  assert.equal(forcedRemovalDelay({}), DEFAULT_TOAST_MS);
  assert.equal(forcedRemovalDelay({ durationMs: 15_000 }), 15_000);
});

test("a toast with an action is left to Radix, whose timer pauses while it is hovered or focused", () => {
  const action = { label: "Reload", onClick: () => undefined };
  assert.equal(forcedRemovalDelay({ durationMs: 15_000, action }), null);
  assert.equal(forcedRemovalDelay({ action }), null);
});
