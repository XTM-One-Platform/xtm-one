/** The Health companion's pet (#8261): the atlas, the frame clock and the state shown - `yarn test:unit`. */
import { test } from "node:test";
import assert from "node:assert/strict";

import { PET_ROW_FOR, PET_STATES, companionState, frameAt, framePosition } from "./health-pet.ts";

test("the states are the format's rows, with its frame counts", () => {
  assert.deepEqual(
    Object.entries(PET_STATES).map(([state, { row, durations }]) => [state, row, durations.length]),
    [
      ["idle", 0, 6],
      ["running-right", 1, 8],
      ["running-left", 2, 8],
      ["waving", 3, 4],
      ["jumping", 4, 5],
      ["failed", 5, 8],
      ["waiting", 6, 6],
      ["running", 7, 6],
      ["review", 8, 6],
    ],
  );
  assert.deepEqual(PET_STATES.idle.durations, [280, 110, 110, 140, 140, 320]);
});

test("a failure wins, then a wait, a review, work, and idle last", () => {
  assert.equal(companionState("idle", "idle"), "idle");
  assert.equal(companionState(undefined, "idle"), "idle");
  assert.equal(companionState("idle", "running"), "running");
  assert.equal(companionState("review", "running"), "review");
  assert.equal(companionState("waiting", "running"), "waiting");
  assert.equal(companionState("failed", "running"), "failed");
});

test("every companion state plays a row of its own", () => {
  assert.deepEqual(PET_ROW_FOR, { idle: "idle", running: "running", waiting: "waiting", failed: "failed", review: "review" });
});

test("the frame clock follows each frame's duration and loops", () => {
  assert.equal(frameAt("idle", 0), 0);
  assert.equal(frameAt("idle", 279), 0);
  assert.equal(frameAt("idle", 280), 1);
  assert.equal(frameAt("idle", 280 + 110), 2);
  assert.equal(frameAt("idle", 1099), 5);
  assert.equal(frameAt("idle", 1100), 0);
  assert.equal(frameAt("idle", -1), 5);
});

test("a frame's position is its column and its state's row, scaled", () => {
  assert.equal(framePosition("idle", 0, 1), "0px 0px");
  assert.equal(framePosition("review", 2, 0.5), "-192px -832px");
});
