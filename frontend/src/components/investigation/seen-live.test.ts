/** Unit tests for the runs a tab watched live - `yarn test:unit`. */
import { test } from "node:test";
import assert from "node:assert/strict";

import { markSeenLive, wasSeenLive } from "./seen-live.ts";

test("a run watched live is remembered across the block's remount, others are not", () => {
  assert.equal(wasSeenLive("run-1"), false);
  markSeenLive("run-1");
  // A new block for the same run (the saved message replacing the streamed one).
  assert.equal(wasSeenLive("run-1"), true);
  assert.equal(wasSeenLive("run-2"), false, "a run met already finished stays collapsed");
  markSeenLive(undefined);
  assert.equal(wasSeenLive(undefined), false);
});
