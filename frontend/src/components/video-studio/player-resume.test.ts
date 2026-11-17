/**
 * Where a viewer left a video - `yarn test:unit`.
 *
 * A position worth coming back to is remembered per video, forgotten at the
 * start and the end, bounded in count and age, and a broken or missing store
 * simply starts the video over.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  RESUME_MAX_ENTRIES,
  RESUME_TTL_MS,
  isResumable,
  readResumePosition,
  writeResumePosition,
} from "./player-resume.ts";

function memory() {
  const store = new Map<string, string>();
  return {
    store,
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
  };
}

test("a position worth coming back to is picked up, per video", () => {
  const storage = memory();
  writeResumePosition(storage, "render:a", 42.37, 120, 1000);
  writeResumePosition(storage, "render:b", 61, 120, 1000);
  assert.equal(readResumePosition(storage, "render:a", 120, 2000), 42.3);
  assert.equal(readResumePosition(storage, "render:b", 120, 2000), 61);
  assert.equal(readResumePosition(storage, "render:c", 120, 2000), null);
});

test("the start and the end of a video start it over", () => {
  assert.equal(isResumable(2, 120), false);
  assert.equal(isResumable(115, 120), false);
  assert.equal(isResumable(60, 120), true);
  assert.equal(isResumable(60, 0), false);
  const storage = memory();
  writeResumePosition(storage, "render:a", 60, 120, 1000);
  // Watched to the end: forgotten.
  writeResumePosition(storage, "render:a", 118, 120, 1500);
  assert.equal(readResumePosition(storage, "render:a", 120, 2000), null);
  // A video that got shorter than the position starts over too.
  writeResumePosition(storage, "render:b", 60, 120, 1000);
  assert.equal(readResumePosition(storage, "render:b", 50, 2000), null);
});

test("positions are bounded in count, newest kept, and in age", () => {
  const storage = memory();
  for (let i = 0; i < RESUME_MAX_ENTRIES + 5; i += 1) writeResumePosition(storage, `render:${i}`, 30, 120, 1000 + i);
  assert.equal(Object.keys(JSON.parse(storage.store.get("xtm-video-player-resume") ?? "{}")).length, RESUME_MAX_ENTRIES);
  assert.equal(readResumePosition(storage, "render:0", 120, 2000), null);
  assert.equal(readResumePosition(storage, `render:${RESUME_MAX_ENTRIES + 4}`, 120, 2000), 30);
  assert.equal(readResumePosition(storage, `render:${RESUME_MAX_ENTRIES + 4}`, 120, 1000 + RESUME_TTL_MS + 100), null);
});

test("a broken or missing store starts the video over and is never thrown at", () => {
  const storage = memory();
  storage.store.set("xtm-video-player-resume", "{not json");
  assert.equal(readResumePosition(storage, "render:a", 120), null);
  writeResumePosition(storage, "render:a", 40, 120, 1000);
  assert.equal(readResumePosition(storage, "render:a", 120, 2000), 40);
  assert.equal(readResumePosition(undefined, "render:a", 120), null);
  const refusing = { getItem: () => null, setItem: () => { throw new Error("QuotaExceededError"); } };
  assert.doesNotThrow(() => writeResumePosition(refusing, "render:a", 40, 120));
});
