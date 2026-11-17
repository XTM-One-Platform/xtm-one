/**
 * Unit tests for the settle clock of a chart - `yarn test:unit`.
 *
 * Pinned here: when a chart may drop `data-deck-pending`. A still two frames
 * after it is drawn; a build two frames after its last series has grown in,
 * never before `BUILD_MS` since the first start - whatever order Recharts
 * reports the growth of a series that restarts in, the second end it sends
 * when an animation is torn down included; a drawing Recharts draws nothing
 * in as a still; and never later than the bound, counted in visible time only.
 * The page is virtual: timers run at their due time and frames every `FRAME`
 * ms while the page is visible. The live figure is read in a browser by
 * `tests_e2e/presentation-charts.spec.ts`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { DeckChartBlock } from "@/types";

import {
  BUILD_MS,
  FRAME_STEP_MS,
  MAX_BUILD_MS,
  STALL_MS,
  createSettleClock,
  growsIn,
  type SettleTimers,
} from "./chart-settle.ts";

const FRAME = 16;

type Timer = { at: number; run: () => void };

/** A page whose clocks the test drives: `advance(ms)` runs every timer and frame due meanwhile, in order. */
function virtualPage() {
  let now = 0;
  let nextId = 1;
  let hidden = false;
  let frameMs = FRAME;
  let nextFrameAt = FRAME;
  const timeouts = new Map<number, Timer>();
  const frames = new Map<number, () => void>();
  const timers: SettleTimers = {
    now: () => now,
    setTimeout: (run, ms) => {
      const id = nextId++;
      timeouts.set(id, { at: now + Math.max(ms, 0), run });
      return id;
    },
    clearTimeout: (id) => {
      if (id !== undefined) timeouts.delete(id);
    },
    requestFrame: (run) => {
      const id = nextId++;
      frames.set(id, run);
      return id;
    },
    cancelFrame: (id) => {
      frames.delete(id);
    },
  };
  const dueTimeout = (): [number, Timer] | undefined => {
    let due: [number, Timer] | undefined;
    for (const entry of timeouts) if (!due || entry[1].at < due[1].at) due = entry;
    return due;
  };
  const advance = (ms: number) => {
    const end = now + ms;
    for (;;) {
      const timeout = dueTimeout();
      const timeoutAt = timeout ? timeout[1].at : Infinity;
      const frameAt = hidden ? Infinity : nextFrameAt;
      if (Math.min(timeoutAt, frameAt) > end) break;
      if (timeout && timeoutAt <= frameAt) {
        now = timeoutAt;
        timeouts.delete(timeout[0]);
        timeout[1].run();
        continue;
      }
      now = frameAt;
      nextFrameAt = frameAt + frameMs;
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
  };
}

/** A clock on a virtual page, and how many times it said the figure was complete. */
function setup() {
  const page = virtualPage();
  let settledCount = 0;
  const clock = createSettleClock(() => {
    settledCount += 1;
  }, page.timers);
  return { page, clock, settled: () => settledCount };
}

test("a still is complete two frames after it is drawn", () => {
  const { page, clock, settled } = setup();
  clock.start(false);
  page.at(FRAME);
  assert.equal(settled(), 0, "one frame is not enough: the axes are laid out on the next render");
  page.at(2 * FRAME);
  assert.equal(settled(), 1);
});

test("a build is counted from its growth, never from the mount", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  // The lazy chunk and the layout passes can take any time: nothing is
  // complete before a series has grown in.
  page.at(3 * BUILD_MS);
  assert.equal(settled(), 0);
  const bars = clock.growth(0);
  bars.onAnimationStart();
  page.at(4 * BUILD_MS);
  bars.onAnimationEnd();
  page.at(4 * BUILD_MS + 3 * FRAME);
  assert.equal(settled(), 1);
});

test("a build is complete two frames after its last series has grown in, never before BUILD_MS since the first start", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  page.at(300);
  clock.growth(0).onAnimationStart();
  clock.growth(1).onAnimationStart();
  page.at(1000);
  clock.growth(0).onAnimationEnd();
  page.at(1100);
  clock.growth(1).onAnimationEnd();
  page.at(300 + BUILD_MS - 1);
  assert.equal(settled(), 0, "the stylesheet's enter animations run for BUILD_MS since the first start");
  page.at(300 + BUILD_MS + 3 * FRAME);
  assert.equal(settled(), 1);
});

test("a growth that outlasts BUILD_MS is complete two frames after its end", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  clock.growth(0).onAnimationStart();
  page.at(1500);
  assert.equal(settled(), 0);
  clock.growth(0).onAnimationEnd();
  page.at(1500 + FRAME);
  assert.equal(settled(), 0);
  page.at(1500 + 3 * FRAME);
  assert.equal(settled(), 1);
});

test("a series that starts again before its previous growth reported an end holds the signal until the new growth ends", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  const series = clock.growth(0);
  series.onAnimationStart();
  page.at(600);
  series.onAnimationStart();
  page.at(700);
  series.onAnimationEnd();
  page.at(1299);
  assert.equal(settled(), 0, "the end of the first growth must not end the one that replaced it");
  series.onAnimationEnd();
  page.at(1299 + 3 * FRAME);
  assert.equal(settled(), 1);
});

test("the second end Recharts sends when a finished growth is torn down never releases a series that started again", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  const bars = clock.growth(0);
  const line = clock.growth(1);
  bars.onAnimationStart();
  line.onAnimationStart();
  page.at(700);
  bars.onAnimationEnd();
  line.onAnimationEnd();
  page.at(900);
  // The drawing was resized: Recharts tears each finished animation down (an
  // end) and mounts the next one (a start), in that order.
  for (const series of [bars, line]) {
    series.onAnimationEnd();
    series.onAnimationStart();
  }
  page.at(1300);
  line.onAnimationEnd();
  page.at(1599);
  assert.equal(settled(), 0, "the bars are still growing");
  bars.onAnimationEnd();
  page.at(1599 + 3 * FRAME);
  assert.equal(settled(), 1);
});

test("a series that starts while the signal waits its two frames drops the signal", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  const series = clock.growth(0);
  series.onAnimationStart();
  page.at(BUILD_MS);
  series.onAnimationEnd();
  page.at(BUILD_MS + FRAME);
  series.onAnimationStart();
  page.at(BUILD_MS + 700 - 1);
  assert.equal(settled(), 0);
  series.onAnimationEnd();
  page.at(BUILD_MS + 700 + 3 * FRAME);
  assert.equal(settled(), 1);
});

test("a growth reported before the drawing went live completes it once it is", () => {
  const { page, clock, settled } = setup();
  // Recharts' effects run before the drawing's own: a series may start - and
  // report an end, when an effect runs twice - before the clock is live.
  clock.growth(0).onAnimationStart();
  clock.growth(0).onAnimationEnd();
  clock.growth(0).onAnimationStart();
  clock.start(true);
  page.at(700);
  clock.growth(0).onAnimationEnd();
  page.at(BUILD_MS + 3 * FRAME);
  assert.equal(settled(), 1);
});

test("a build whose series never start is complete after STALL_MS of visible time", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  page.at(STALL_MS - 5 * FRAME);
  assert.equal(settled(), 0);
  page.at(STALL_MS + 5 * FRAME);
  assert.equal(settled(), 1);
});

test("a series that starts and never reports its end is complete STALL_MS after its start", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  page.at(2000);
  clock.growth(0).onAnimationStart();
  page.at(2000 + STALL_MS - 5 * FRAME);
  assert.equal(settled(), 0, "a report restarts the stall");
  page.at(2000 + STALL_MS + 5 * FRAME);
  assert.equal(settled(), 1);
});

test("a hidden page counts no time: its growth waits for the page, and the signal with it", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  clock.growth(0).onAnimationStart();
  page.at(100);
  page.setHidden(true);
  page.at(10 * STALL_MS);
  assert.equal(settled(), 0, "no frame, no visible time");
  page.setHidden(false);
  page.advance(600);
  assert.equal(settled(), 0);
  clock.growth(0).onAnimationEnd();
  page.advance(3 * FRAME);
  assert.equal(settled(), 1);
});

test("a frame counts for at most FRAME_STEP_MS, however far apart the frames are", () => {
  const { page, clock, settled } = setup();
  page.setFrameMs(1000);
  clock.start(true);
  page.at((STALL_MS / FRAME_STEP_MS) * 1000 - 2000);
  assert.equal(settled(), 0, "a stalled or throttled page is not a stalled growth");
  page.at((STALL_MS / FRAME_STEP_MS) * 1000 + 3000);
  assert.equal(settled(), 1);
});

test("however its series keep restarting, a build is complete after MAX_BUILD_MS of visible time", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  const series = clock.growth(0);
  series.onAnimationStart();
  for (let time = 400; time < MAX_BUILD_MS - 5 * FRAME; time += 400) {
    page.at(time);
    series.onAnimationEnd();
    series.onAnimationStart();
  }
  assert.equal(settled(), 0);
  page.at(MAX_BUILD_MS + 5 * FRAME);
  assert.equal(settled(), 1);
});

test("a stopped clock never signals, a late end included, and completes once restarted", () => {
  const { page, clock, settled } = setup();
  clock.start(false);
  clock.stop();
  page.at(3 * STALL_MS);
  assert.equal(settled(), 0, "a still's signal goes with its drawing");
  clock.start(true);
  const bars = clock.growth(0);
  bars.onAnimationStart();
  page.advance(700);
  bars.onAnimationEnd();
  clock.stop();
  bars.onAnimationEnd();
  page.advance(2 * MAX_BUILD_MS);
  assert.equal(settled(), 0, "a build's signal and its bound go with its drawing");
  clock.start(true);
  page.advance(BUILD_MS + 3 * FRAME);
  assert.equal(settled(), 1, "the growth it already saw completes the restarted drawing");
});

test("a figure is complete once, and nothing reported afterwards changes that", () => {
  const { page, clock, settled } = setup();
  clock.start(true);
  const series = clock.growth(0);
  series.onAnimationStart();
  page.at(700);
  series.onAnimationEnd();
  page.at(BUILD_MS + 3 * FRAME);
  assert.equal(settled(), 1);
  series.onAnimationStart();
  page.advance(700);
  series.onAnimationEnd();
  clock.stop();
  clock.start(false);
  page.advance(2 * MAX_BUILD_MS);
  assert.equal(settled(), 1);
});

test("each series keeps one pair of handlers: Recharts grows a series in again when a handler changes", () => {
  const { clock } = setup();
  assert.equal(clock.growth(0), clock.growth(0));
  assert.equal(clock.growth(0).onAnimationStart, clock.growth(0).onAnimationStart);
  assert.notEqual(clock.growth(0), clock.growth(1));
});

const chart = (kind: DeckChartBlock["kind"], ...series: number[][]): Pick<DeckChartBlock, "kind" | "series"> => ({
  kind,
  series: series.map((values, index) => ({ name: `Series ${index + 1}`, values })),
});

test("a pie or donut whose visible slices add up to nothing grows nothing in", () => {
  // Recharts draws no pie at all then, so no growth would ever be reported.
  const none = new Set<number>();
  assert.equal(growsIn(chart("pie", [0, 0, 0]), none), false);
  assert.equal(growsIn(chart("donut", [0, 4, 0]), new Set([1])), false, "a slice the legend switched off is not drawn");
  assert.equal(growsIn(chart("pie", [-4, 0]), none), false, "a slice is never below zero");
  assert.equal(growsIn(chart("donut", [0, 4, 0]), new Set([0])), true);
  assert.equal(growsIn(chart("pie", [46, 28]), none), true);
});

test("an axis chart whose series are all hidden grows nothing in", () => {
  // A guard - the legend keeps one series visible, and an edit that would leave
  // none visible shows them all again - since Recharts draws nothing for a
  // hidden series.
  assert.equal(growsIn(chart("bar", [1, 2], [3, 4]), new Set([0, 1])), false);
  assert.equal(growsIn(chart("combo", [1, 2], [3, 4]), new Set([0, 1])), false);
  assert.equal(growsIn(chart("line", [1, 2], [3, 4]), new Set([0])), true);
  assert.equal(growsIn(chart("stacked_bar", [1, 2], [3, 4]), new Set()), true);
});

test("a visible series grows in from the baseline even at zero, and a gauge always grows", () => {
  const none = new Set<number>();
  for (const kind of ["bar", "stacked_bar", "line", "area", "combo"] as const) {
    assert.equal(growsIn(chart(kind, [0, 0]), none), true, kind);
  }
  assert.equal(growsIn(chart("gauge", [0]), none), true);
});
