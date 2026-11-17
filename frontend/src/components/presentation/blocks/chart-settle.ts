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
 * When a chart's figure is complete: the moment its host (`chart.tsx`)
 * may drop `data-deck-pending`, the attribute the export page, `deckSettled()`
 * and the specs wait on.
 *
 * A still is complete two frames after it is drawn: Recharts lays its axes out
 * in a layout effect and paints the settled chart on the next render - a signal
 * on the first frame would hand the export a chart with no left axis.
 *
 * An animated build is complete two frames after its last series has grown in,
 * and never before `BUILD_MS` since the first one started. The growth is the
 * clock, not the mount: the lazy chunk and Recharts' layout passes run first,
 * for as long as the machine is slow, and Recharts keys a bar on its position -
 * it replaces every bar on every frame it moves. Each series reports its growth
 * through Recharts' `onAnimationStart` / `onAnimationEnd` and COUNTS its
 * animations: Recharts reports an end when an animation completes and again
 * when it is torn down, and a series that starts again (the drawing was
 * resized) holds the signal until its last animation has ended, whatever order
 * the reports arrive in. The signal then waits its two frames and is dropped
 * if anything started meanwhile.
 *
 * Those reports are Recharts' to send, and a drawing may send none: Recharts
 * renders a series with no animation at all when it has nothing to draw (an
 * area with no points, a series with no plot to draw in). So a build is also
 * bounded: one that reports nothing for `STALL_MS` - no series starting, none
 * ending - is complete as it stands, and none holds the signal past
 * `MAX_BUILD_MS`, however its series keep restarting. Both count visible time,
 * frame by frame, a frame counting for at most `FRAME_STEP_MS` (the step of
 * every figure bound, `visible-time.ts`): a hidden page draws no frame, so its
 * growth waits for the page, and the signal with it.
 *
 * Plain TypeScript with the timers injected, so the rule is exercised without
 * a page (`chart-settle.test.ts`).
 */
import type { DeckChartBlock } from "@/types";

import { FRAME_STEP_MS } from "../visible-time.ts";

export { FRAME_STEP_MS };

/**
 * How long an animated build runs from the moment its series START to grow:
 * the series grow in (`ANIMATION_MS` in `ChartCanvas.tsx` - Recharts writes the
 * value labels only then) and the stylesheet's enter animations of the target
 * line and the gauge reading (a 450ms delay plus 500ms, `presentation.css`)
 * run their course.
 */
export const BUILD_MS = 1000;
/** Visible time with no growth reported - no series starting, none ending - after which a build is complete as it stands. */
export const STALL_MS = 5 * BUILD_MS;
/** Visible time after which a build is complete, however its series keep restarting. */
export const MAX_BUILD_MS = 10 * BUILD_MS;

/** How a Recharts series reports that it grows in: its `onAnimationStart` / `onAnimationEnd`. */
export interface GrowthHandlers {
  onAnimationStart: () => void;
  onAnimationEnd: () => void;
}

/** The timers the clock runs on: the browser's, or a test's. */
export interface SettleTimers {
  now: () => number;
  setTimeout: (callback: () => void, ms: number) => number;
  clearTimeout: (id: number | undefined) => void;
  requestFrame: (callback: () => void) => number;
  cancelFrame: (id: number) => void;
}

const BROWSER_TIMERS: SettleTimers = {
  now: () => performance.now(),
  setTimeout: (callback, ms) => window.setTimeout(callback, ms),
  clearTimeout: (id) => window.clearTimeout(id),
  requestFrame: (callback) => requestAnimationFrame(callback),
  cancelFrame: (id) => cancelAnimationFrame(id),
};

interface SettleClock {
  /** The handlers of the n-th series (the one animated pie of a slice chart or a gauge is series 0). */
  growth: (series: number) => GrowthHandlers;
  /** Starts listening; a drawing that grows nothing in is complete two frames later. */
  start: (grows: boolean) => void;
  stop: () => void;
}

/** `onSettled` is called once, the first time the figure is complete. */
export function createSettleClock(onSettled: () => void, timers: SettleTimers = BROWSER_TIMERS): SettleClock {
  /** Animations started and not yet ended, per series. */
  const growing = new Map<number, number>();
  const handlers = new Map<number, GrowthHandlers>();
  let startedAt: number | null = null;
  let starts = 0;
  let live = false;
  let settled = false;
  let timer: number | undefined;
  let frame: number | undefined;
  // The bound: visible time since the last report (`quiet`) and since the build began (`spent`).
  let watchFrame: number | undefined;
  let lastTick: number | null = null;
  let quiet = 0;
  let spent = 0;
  const cancel = () => {
    timers.clearTimeout(timer);
    if (frame !== undefined) timers.cancelFrame(frame);
    timer = undefined;
    frame = undefined;
  };
  const stopWatch = () => {
    if (watchFrame !== undefined) timers.cancelFrame(watchFrame);
    watchFrame = undefined;
    lastTick = null;
  };
  const settle = () => {
    cancel();
    stopWatch();
    settled = true;
    onSettled();
  };
  const signal = () => {
    cancel();
    const seen = starts;
    frame = timers.requestFrame(() => {
      frame = timers.requestFrame(() => {
        frame = undefined;
        if (live && !settled && starts === seen && growing.size === 0) settle();
      });
    });
  };
  // Every series has grown in: the signal waits out `BUILD_MS` since the first start.
  const schedule = () => {
    cancel();
    timer = timers.setTimeout(signal, Math.max(0, (startedAt ?? 0) + BUILD_MS - timers.now()));
  };
  const watch = () => {
    watchFrame = undefined;
    if (!live || settled) return;
    const now = timers.now();
    if (lastTick !== null) {
      const step = Math.min(Math.max(now - lastTick, 0), FRAME_STEP_MS);
      quiet += step;
      spent += step;
    }
    lastTick = now;
    if (quiet >= STALL_MS || spent >= MAX_BUILD_MS) settle();
    else watchFrame = timers.requestFrame(watch);
  };
  const startWatch = () => {
    watchFrame ??= timers.requestFrame(watch);
  };
  // One pair per series, created once: Recharts compares a series' props by
  // identity and grows the series in again when a handler changes.
  const growth = (series: number): GrowthHandlers => {
    let entry = handlers.get(series);
    if (!entry) {
      entry = {
        onAnimationStart: () => {
          startedAt ??= timers.now();
          starts += 1;
          growing.set(series, (growing.get(series) ?? 0) + 1);
          quiet = 0;
          if (!live || settled) return;
          cancel();
          startWatch();
        },
        onAnimationEnd: () => {
          const open = (growing.get(series) ?? 0) - 1;
          if (open > 0) growing.set(series, open);
          else growing.delete(series);
          quiet = 0;
          if (live && !settled && startedAt !== null && growing.size === 0) schedule();
        },
      };
      handlers.set(series, entry);
    }
    return entry;
  };
  return {
    growth,
    start(grows) {
      live = true;
      if (settled) return;
      if (!grows && growing.size === 0) {
        signal();
        return;
      }
      startWatch();
      // A drawing restarted after its series had grown in has nothing left to wait for.
      if (startedAt !== null && growing.size === 0) schedule();
    },
    stop() {
      live = false;
      cancel();
      stopWatch();
    },
  };
}

/**
 * Whether a drawing grows anything in: Recharts draws nothing for a hidden
 * series, and no pie at all when its visible slices add up to nothing, so
 * there would be no growth to wait for. An axis chart with every series hidden
 * is a guard: the legend keeps one visible, and an edit that would leave none
 * visible shows them all again (`resolveHidden`).
 */
export function growsIn(block: Pick<DeckChartBlock, "kind" | "series">, hidden: ReadonlySet<number>): boolean {
  if (block.kind === "gauge") return true;
  // A map is drawn whole; the stylesheet fades it in on a slide entry.
  if (block.kind === "map") return false;
  if (block.kind === "pie" || block.kind === "donut") {
    return (block.series[0]?.values ?? []).some((value, index) => value > 0 && !hidden.has(index));
  }
  return block.series.some((_, index) => !hidden.has(index));
}
