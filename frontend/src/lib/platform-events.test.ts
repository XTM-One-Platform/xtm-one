/**
 * Unit tests for the platform event trigger helpers - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  platformEventConfig,
  platformEventDraft,
  platformEventFamily,
  platformFilterFromRows,
  platformFilterRows,
  platformWatchState,
  platformDeadlineSavable,
  platformDeadlineState,
  platformTriggerEnded,
  platformEventScope,
} from "./platform-events.ts";

test("an event's family is its name up to the first dot", () => {
  assert.equal(platformEventFamily("work_item.note_added"), "work_item");
  assert.equal(platformEventFamily("video.render.finished"), "video");
  assert.equal(platformEventFamily("plain"), "plain");
});

test("a stored filter reads back as one row per field", () => {
  assert.deepEqual(platformFilterRows({ status: "done", "data.count": 3 }), [
    { key: "status", value: "done" },
    { key: "data.count", value: "3" },
  ]);
  assert.deepEqual(platformFilterRows(undefined), []);
});

test("rows save as a filter without the blank ones", () => {
  assert.deepEqual(
    platformFilterFromRows([
      { key: " correlation_key ", value: " dev:x " },
      { key: "status", value: "" },
      { key: "", value: "orphan" },
    ]),
    { correlation_key: "dev:x" },
  );
});

test("no row left saves no filter: every event of the kind in scope", () => {
  assert.equal(platformFilterFromRows([{ key: "status", value: " " }]), undefined);
  assert.equal(platformFilterFromRows([]), undefined);
});

test("a one-off watch is armed until its first event, then fired", () => {
  assert.equal(platformWatchState({ once: true }), "armed");
  assert.equal(platformWatchState({ once: true, fired_event_id: "e1" }), "fired");
  assert.equal(platformWatchState({ fired_event_id: "e1" }), null);
  assert.equal(platformWatchState(undefined), null);
});

test("a one-off watch past its deadline before any event is expired, not armed", () => {
  const now = new Date("2026-11-02T12:00:00Z");
  assert.equal(platformWatchState({ once: true, expires_at: "2026-11-02T09:00:00Z" }, now), "expired");
  assert.equal(platformWatchState({ once: true, expires_at: "2026-11-03T09:00:00Z" }, now), "armed");
  assert.equal(platformWatchState({ once: true, expires_at: "2026-11-02T09:00:00Z", fired_event_id: "e1" }, now), "fired");
  // The platform never delivers to a deadline that does not parse: expired, as it reads it.
  assert.equal(platformWatchState({ once: true, expires_at: "not a date" }, now), "expired");
});

test("a standing trigger's deadline reads active, then ended; a past one cannot be saved", () => {
  const now = new Date("2026-11-02T12:00:00Z");
  assert.equal(platformDeadlineState(undefined, now), null);
  assert.equal(platformDeadlineState("2026-11-03T09:00:00Z", now), "active");
  assert.equal(platformDeadlineState("2026-11-02T09:00:00Z", now), "ended");
  assert.equal(platformDeadlineState("not a date", now), "ended");
  assert.equal(platformDeadlineSavable(undefined, now), true);
  assert.equal(platformDeadlineSavable("2026-11-03T09:00:00+01:00", now), true);
  assert.equal(platformDeadlineSavable("2026-11-02T09:00:00Z", now), false);
});

test("a platform event trigger past its deadline has ended, whatever its kind", () => {
  const now = new Date("2026-11-02T12:00:00Z");
  const past = "2026-11-02T09:00:00Z";
  const ahead = "2026-11-03T09:00:00Z";
  assert.equal(platformTriggerEnded("platform_event", { once: true, expires_at: past }, now), true);
  assert.equal(platformTriggerEnded("platform_event", { expires_at: past }, now), true);
  assert.equal(platformTriggerEnded("platform_event", { once: true, expires_at: ahead }, now), false);
  assert.equal(platformTriggerEnded("platform_event", { expires_at: ahead }, now), false);
  assert.equal(platformTriggerEnded("platform_event", {}, now), false);
  assert.equal(platformTriggerEnded("platform_event", null, now), false);
  // Only a platform event trigger reads its deadline.
  assert.equal(platformTriggerEnded("schedule", { expires_at: past }, now), false);
});

test("a one-off watch that fired has ended, with or without a deadline", () => {
  const now = new Date("2026-11-02T12:00:00Z");
  assert.equal(platformTriggerEnded("platform_event", { once: true, fired_event_id: "e1" }, now), true);
  assert.equal(platformTriggerEnded("platform_event", { once: true, fired_event_id: "e1", expires_at: "2026-11-03T09:00:00Z" }, now), true);
  // A standing trigger keeps listening after an event: not ended by it.
  assert.equal(platformTriggerEnded("platform_event", { fired_event_id: "e1" }, now), false);
  assert.equal(platformTriggerEnded("platform_event", { once: true }, now), false);
});

test("a save from the editor keeps a deadline set through the API", () => {
  const stored = { event: "work_item.completed", once: true, expires_at: "2026-11-02T09:00:00Z", fired_event_id: "e1" };
  assert.deepEqual(platformEventConfig(platformEventDraft(stored)), {
    event: "work_item.completed",
    once: true,
    expires_at: "2026-11-02T09:00:00Z",
  });
  assert.equal("expires_at" in platformEventConfig(platformEventDraft({ event: "run.failed" })), false);
});

test("a fleet scope reads back as fleet and saves unchanged; own stays the default", () => {
  const stored = { event: "work_item.past_due", scope: "fleet" as const };
  const draft = platformEventDraft(stored);
  assert.equal(draft.scope, "fleet");
  assert.deepEqual(platformEventConfig(draft), { event: "work_item.past_due", scope: "fleet" });
  assert.deepEqual(platformEventConfig(platformEventDraft({ event: "run.failed", scope: "all" })), { event: "run.failed", scope: "all" });
  assert.equal("scope" in platformEventConfig(platformEventDraft({ event: "run.failed", scope: "own" })), false);
  assert.equal(platformEventScope("anything"), "own");
  assert.equal(platformEventScope(undefined), "own");
});
