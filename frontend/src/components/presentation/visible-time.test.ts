/**
 * Unit tests for visible time - `yarn test:unit`.
 *
 * Pinned here: the clock every bound on a deck figure counts in, and the bound
 * the browser-print path puts on the whole deck. Time counts frame by frame,
 * a frame for at most `FRAME_STEP_MS`, so a hidden page counts nothing; one
 * frame loop serves every wait; a cancelled wait never expires. The page is
 * virtual: frames every `FRAME` ms while it is visible. The figures that give
 * up on it are read in a browser by `tests_e2e/presentation-print-settle.spec.ts`
 * and `tests_e2e/presentation-charts.spec.ts`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { FRAME_STEP_MS, createVisibleClock, settlesWithin, type FrameTimers } from "./visible-time.ts";

const FRAME = 16;

/** A page whose frames the test drives: `advance(ms)` runs every frame due meanwhile, in order. */
function virtualPage() {
  let now = 0;
  let nextId = 1;
  let hidden = false;
  let frameMs = FRAME;
  let nextFrameAt = FRAME;
  let requested = 0;
  const frames = new Map<number, () => void>();
  const timers: FrameTimers = {
    now: () => now,
    requestFrame: (run) => {
      const id = nextId++;
      frames.set(id, run);
      requested += 1;
      return id;
    },
    cancelFrame: (id) => {
      frames.delete(id);
    },
  };
  const advance = (ms: number) => {
    const end = now + ms;
    while (!hidden && nextFrameAt <= end) {
      now = nextFrameAt;
      nextFrameAt = now + frameMs;
      const callbacks = [...frames.values()];
      frames.clear();
      for (const run of callbacks) run();
    }
    now = end;
  };
  return {
    timers,
    advance,
    /** Advances to an absolute time. */
    at: (time: number) => advance(time - now),
    setHidden: (value: boolean) => {
      hidden = value;
      if (!value) nextFrameAt = now + frameMs;
    },
    setFrameMs: (ms: number) => {
      frameMs = ms;
      nextFrameAt = now + ms;
    },
    /** Frame callbacks waiting for the next frame. */
    pendingFrames: () => frames.size,
    /** Frames requested so far. */
    requested: () => requested,
  };
}

function setup() {
  const page = virtualPage();
  const clock = createVisibleClock(page.timers);
  return { page, clock };
}

/** Lets the promise callbacks queued so far run. */
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

test("a wait expires once its visible time has passed, and only once", () => {
  const { page, clock } = setup();
  let expired = 0;
  clock.wait(1000, () => {
    expired += 1;
  });
  page.at(1000 - 2 * FRAME);
  assert.equal(expired, 0);
  page.at(1000 + FRAME);
  assert.equal(expired, 1);
  page.advance(10_000);
  assert.equal(expired, 1);
  assert.equal(page.pendingFrames(), 0, "the frame loop stops with its last wait");
});

test("a hidden page counts no time: the wait resumes where it stood once the page is shown", () => {
  const { page, clock } = setup();
  let expired = 0;
  clock.wait(1000, () => {
    expired += 1;
  });
  page.at(500);
  page.setHidden(true);
  page.advance(60_000);
  assert.equal(expired, 0, "no frame, no visible time");
  page.setHidden(false);
  page.advance(400);
  assert.equal(expired, 0, "the hidden minute counted for one frame at most");
  page.advance(200);
  assert.equal(expired, 1);
});

test("a frame counts for at most FRAME_STEP_MS, however far apart the frames are", () => {
  const { page, clock } = setup();
  page.setFrameMs(1000);
  let expired = 0;
  clock.wait(10 * FRAME_STEP_MS, () => {
    expired += 1;
  });
  page.at(9 * 1000);
  assert.equal(expired, 0, "a stalled or throttled page is not time the figure had");
  page.at(10 * 1000);
  assert.equal(expired, 1);
});

test("a cancelled wait never expires, and the frame loop stops with the last wait", () => {
  const { page, clock } = setup();
  let expired = 0;
  const cancel = clock.wait(500, () => {
    expired += 1;
  });
  page.at(200);
  cancel();
  cancel();
  page.advance(5000);
  assert.equal(expired, 0);
  assert.equal(page.pendingFrames(), 0);
});

test("every wait on the page shares one frame loop", () => {
  const { page, clock } = setup();
  const expired: number[] = [];
  for (let index = 0; index < 200; index += 1) {
    clock.wait(300 + index, () => expired.push(index));
  }
  assert.equal(page.pendingFrames(), 1);
  page.at(10 * FRAME);
  assert.equal(page.requested(), 11, "one frame requested per frame drawn, not one per wait");
  page.advance(1000);
  assert.equal(expired.length, 200);
  assert.deepEqual(
    expired,
    [...expired].sort((a, b) => a - b),
    "each wait expires once its own bound has passed",
  );
});

test("a wait that expires may start the next one", () => {
  const { page, clock } = setup();
  const log: string[] = [];
  clock.wait(200, () => {
    log.push("first");
    clock.wait(200, () => log.push("second"));
  });
  page.at(200 + FRAME);
  assert.deepEqual(log, ["first"]);
  page.advance(200 + FRAME);
  assert.deepEqual(log, ["first", "second"]);
});

test("a wait counts from its own start, not from the frames drawn before it", () => {
  const { page, clock } = setup();
  let expired = 0;
  page.at(5000);
  clock.wait(1000, () => {
    expired += 1;
  });
  page.advance(1000 - 2 * FRAME);
  assert.equal(expired, 0);
  page.advance(3 * FRAME);
  assert.equal(expired, 1);
});

test("work that settles within the bound is in time, rejected work included", async () => {
  const { page, clock } = setup();
  let finish: () => void = () => undefined;
  const work = new Promise<void>((resolve) => {
    finish = resolve;
  });
  const inTime = settlesWithin(work, 1000, clock);
  page.at(900);
  finish();
  assert.equal(await inTime, true);
  page.advance(5000);
  assert.equal(page.pendingFrames(), 0, "the bound goes with the work");

  const failed = settlesWithin(Promise.reject(new Error("boom")), 1000, clock);
  assert.equal(await failed, true);
});

test("work still running once the bound has passed in visible time is not in time", async () => {
  const { page, clock } = setup();
  let answer: boolean | null = null;
  void settlesWithin(new Promise(() => undefined), 1000, clock).then((value) => {
    answer = value;
  });
  page.at(500);
  page.setHidden(true);
  page.advance(30_000);
  await flush();
  assert.equal(answer, null, "a hidden page never gives up on the deck");
  page.setHidden(false);
  page.advance(600);
  await flush();
  assert.equal(answer, false);
});
