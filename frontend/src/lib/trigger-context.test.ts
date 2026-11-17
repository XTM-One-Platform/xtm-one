/**
 * Unit tests for the run detail page's trigger metadata tiles - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { isTriggerMetadataKey } from "./trigger-context.ts";

test("what the trigger says is shown as a tile", () => {
  for (const key of ["cron", "scheduled_at", "triggered_at", "user_email", "provider", "event"]) {
    assert.equal(isTriggerMetadataKey(key), true, key);
  }
});

test("the badge, the payload dialog and internal fields are not tiles", () => {
  for (const key of ["type", "body", "payload", "headers", "dispatch_via_worker", "dedup_key", "_first_run"]) {
    assert.equal(isTriggerMetadataKey(key), false, key);
  }
});

test("the hold marker is not a tile: the Held badge says it while the run waits", () => {
  // It stays on a run that then ended, where a raw JSON tile would outlive the hold.
  assert.equal(isTriggerMetadataKey("ai_hold"), false);
});
