/**
 * Unit tests for the wait card's watched items - `yarn test:unit`.
 * A frame's items are read defensively, and a running item counts its own
 * time from when it started.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { readWaitItems, shortWaitItemId, waitItemRunningSeconds, waitItemStatusKey } from "./wait-items.ts";

const RUN = {
  id: "0749c9a3-1111-4111-8111-111111111111",
  kind: "run",
  label: "Daily product brief",
  status: "running",
  started_at: "2026-10-20T19:53:00+00:00",
};

test("a frame's items are read as sent", () => {
  assert.deepEqual(readWaitItems([RUN]), [RUN]);
});

test("an item with no id or no status is dropped, and a frame without items carries none", () => {
  assert.equal(readWaitItems(undefined), undefined);
  assert.equal(readWaitItems(null), undefined);
  assert.equal(readWaitItems([{ id: "x" }, { status: "running" }, "x", null]), undefined);
});

test("a Video Studio job is read as one, named or not", () => {
  const job = { id: "8699213e-2222-4222-8222-222222222222", kind: "job", label: null, status: "queued", started_at: null };
  assert.deepEqual(readWaitItems([job]), [job]);
});

test("a partner task the agent delegated is read as one, with the partner's name", () => {
  const a2a = {
    id: "88888888-8888-4888-8888-888888888888",
    kind: "a2a",
    label: "Partner TI",
    status: "running",
    started_at: "2026-11-15T13:00:00+00:00",
  };
  assert.deepEqual(readWaitItems([a2a]), [a2a]);
});

test("an unknown kind, a blank label and a missing start are read as nothing", () => {
  assert.deepEqual(readWaitItems([{ id: "x", kind: "flow", label: "  ", status: "unknown" }]), [
    { id: "x", kind: null, label: null, status: "unknown", started_at: null },
  ]);
});

test("a running item counts from its start; any other state counts nothing", () => {
  const [item] = readWaitItems([RUN])!;
  assert.equal(waitItemRunningSeconds(item, Date.parse("2026-10-20T19:57:06+00:00")), 246);
  assert.equal(waitItemRunningSeconds({ ...item, status: "completed" }, Date.now()), null);
  assert.equal(waitItemRunningSeconds({ ...item, started_at: null }, Date.now()), null);
});

test("a state is worded through the table, and one it does not know is shown as sent", () => {
  assert.equal(waitItemStatusKey("awaiting_human"), "Waiting for an answer");
  assert.equal(waitItemStatusKey("unknown"), "Not found");
  assert.equal(waitItemStatusKey("paused"), "paused");
});

test("an unnamed item shows the start of its id", () => {
  assert.equal(shortWaitItemId(RUN.id), "0749c9a3");
});
