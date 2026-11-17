/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Visible time: the clock every bound on a deck figure counts in.
 *
 * A figure that waits on something with no end of its own - a chart canvas
 * with no room to draw in, a lazy chunk the network holds, a Mermaid render,
 * the icon catalog - gives up after a bound (`figure-wait.ts`), and the
 * browser-print path gives up on the whole deck after a longer one
 * (`deckSettled()` in `ready.ts`). Both count the time the page was ON SCREEN:
 * a hidden page draws no frame, so time is counted frame by frame, and a frame
 * counts for at most `FRAME_STEP_MS` - a longer gap is a page that was hidden
 * or a thread that stalled, not time the figure had. A background tab never
 * gives up on a figure nobody has seen, and a loaded machine only makes a bound
 * more lenient.
 *
 * One frame loop serves every wait on the page, however many are running: an
 * icon catalog still on its way holds every icon of a deck at once.
 *
 * Plain TypeScript with the frames injected, so the rule is exercised without
 * a page (`visible-time.test.ts`).
 */

/** The most one frame counts for: a longer gap is a page that was hidden or a thread that stalled, not time the figure had. */
export const FRAME_STEP_MS = 100;

/** The frames a clock runs on: the browser's, or a test's. */
export interface FrameTimers {
  now: () => number;
  requestFrame: (callback: () => void) => number;
  cancelFrame: (id: number) => void;
}

interface VisibleClock {
  /** Calls `onExpire` once `ms` of visible time has passed; returns what cancels the wait. */
  wait: (ms: number, onExpire: () => void) => () => void;
}

interface Wait {
  ms: number;
  onExpire: () => void;
  /** Visible time counted so far. */
  spent: number;
  /** When it was last counted: the wait's start, then its last frame. */
  last: number;
}

export function createVisibleClock(timers: FrameTimers): VisibleClock {
  const waits = new Set<Wait>();
  let frame: number | undefined;
  const tick = () => {
    frame = undefined;
    const now = timers.now();
    const expired: Wait[] = [];
    for (const wait of waits) {
      wait.spent += Math.min(Math.max(now - wait.last, 0), FRAME_STEP_MS);
      wait.last = now;
      if (wait.spent >= wait.ms) expired.push(wait);
    }
    for (const wait of expired) waits.delete(wait);
    if (waits.size > 0) frame = timers.requestFrame(tick);
    for (const wait of expired) wait.onExpire();
  };
  return {
    wait(ms, onExpire) {
      const wait: Wait = { ms, onExpire, spent: 0, last: timers.now() };
      waits.add(wait);
      frame ??= timers.requestFrame(tick);
      return () => {
        if (!waits.delete(wait) || waits.size > 0 || frame === undefined) return;
        timers.cancelFrame(frame);
        frame = undefined;
      };
    },
  };
}

const BROWSER_FRAMES: FrameTimers = {
  now: () => performance.now(),
  requestFrame: (callback) => window.requestAnimationFrame(callback),
  cancelFrame: (id) => window.cancelAnimationFrame(id),
};

let pageClock: VisibleClock | null = null;

/** The page's clock, on the browser's frames: one for every figure of every deck on the page. */
export function pageVisibleClock(): VisibleClock {
  pageClock ??= createVisibleClock(BROWSER_FRAMES);
  return pageClock;
}

/**
 * Whether `work` settled - fulfilled or rejected - within `ms` of visible
 * time. Never rejects: the bound is about time, and what the work came to is
 * its caller's.
 */
export function settlesWithin(work: Promise<unknown>, ms: number, clock: VisibleClock = pageVisibleClock()): Promise<boolean> {
  return new Promise((resolve) => {
    const cancel = clock.wait(ms, () => resolve(false));
    const done = () => {
      cancel();
      resolve(true);
    };
    work.then(done, done);
  });
}
