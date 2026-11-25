/**
 * Unit tests for the Cases live refresh coalescer - `yarn test:unit`.
 *
 * A burst of case frames during a case-opening drain must cost at most one
 * list read per window, a lone frame must still refresh at once, and no case
 * id named by a frame may be lost.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { CASE_REFRESH_WINDOW_MS, createCaseRefreshCoalescer } from "./case-refresh-coalescer.ts";

function fakeTimers() {
  let now = 0;
  let next = 1;
  const due = new Map<number, { at: number; run: () => void }>();
  return {
    timers: {
      set: (run: () => void, delayMs: number) => {
        const handle = next++;
        due.set(handle, { at: now + delayMs, run });
        return handle;
      },
      clear: (handle: unknown) => {
        due.delete(handle as number);
      },
    },
    advance(ms: number) {
      const until = now + ms;
      for (;;) {
        const ready = [...due.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at);
        if (ready.length === 0) break;
        const [handle, t] = ready[0];
        due.delete(handle);
        now = t.at;
        t.run();
      }
      now = until;
    },
    pending: () => due.size,
  };
}

test("a lone case frame refreshes at once with its case id", () => {
  const clock = fakeTimers();
  const calls: string[][] = [];
  const c = createCaseRefreshCoalescer((ids) => calls.push(ids), 5_000, clock.timers);
  c.push("case-1");
  assert.deepEqual(calls, [["case-1"]]);
  clock.advance(5_000);
  assert.deepEqual(calls, [["case-1"]]);
  assert.equal(clock.pending(), 0);
});

test("a burst inside one window costs one refresh at once and one at the window's end", () => {
  const clock = fakeTimers();
  const calls: string[][] = [];
  const c = createCaseRefreshCoalescer((ids) => calls.push(ids), 5_000, clock.timers);
  c.push("a");
  for (let i = 0; i < 50; i++) {
    clock.advance(50);
    c.push(`b${i % 3}`);
  }
  assert.equal(calls.length, 1);
  clock.advance(5_000);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].sort(), ["b0", "b1", "b2"]);
});

test("a drain of 70 frames a minute reads the list at most once per window", () => {
  const clock = fakeTimers();
  let refreshes = 0;
  const c = createCaseRefreshCoalescer(() => refreshes++, CASE_REFRESH_WINDOW_MS, clock.timers);
  for (let i = 0; i < 70; i++) {
    c.push(`case-${i}`);
    clock.advance(Math.floor(60_000 / 70));
  }
  clock.advance(CASE_REFRESH_WINDOW_MS);
  assert.ok(refreshes <= 60_000 / CASE_REFRESH_WINDOW_MS + 2, `${refreshes} refreshes`);
});

test("every case id named during the burst reaches a refresh", () => {
  const clock = fakeTimers();
  const seen = new Set<string>();
  const c = createCaseRefreshCoalescer((ids) => ids.forEach((id) => seen.add(id)), 1_000, clock.timers);
  for (let i = 0; i < 20; i++) {
    c.push(`c${i}`);
    clock.advance(120);
  }
  clock.advance(2_000);
  assert.equal(seen.size, 20);
});

test("a frame without a case id still refreshes the list", () => {
  const clock = fakeTimers();
  const calls: string[][] = [];
  const c = createCaseRefreshCoalescer((ids) => calls.push(ids), 1_000, clock.timers);
  c.push();
  assert.deepEqual(calls, [[]]);
});

test("dispose drops the pending refresh and leaves no timer", () => {
  const clock = fakeTimers();
  const calls: string[][] = [];
  const c = createCaseRefreshCoalescer((ids) => calls.push(ids), 1_000, clock.timers);
  c.push("a");
  c.push("b");
  c.dispose();
  assert.equal(clock.pending(), 0);
  clock.advance(5_000);
  c.push("c");
  assert.deepEqual(calls, [["a"]]);
});
