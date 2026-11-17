/**
 * The shared minute tick behind relative times in memoized lists - `yarn test:unit`.
 *
 * The chat sidebar is memoized, so it re-renders only when its props change and
 * its "5m ago" labels froze at whatever they read last. Each label now reads a
 * store that notifies once a minute, on ONE timer shared by every subscriber.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { MINUTE_MS, createMinuteTick, type TickClock } from "./minute-tick.ts";

function fakeClock(start: number) {
  let now = start;
  let nextId = 1;
  const timers = new Map<number, { at: number; fn: () => void }>();
  const clock: TickClock = {
    now: () => now,
    setTimeout: (fn, ms) => {
      const id = nextId++;
      timers.set(id, { at: now + ms, fn });
      return id;
    },
    clearTimeout: (handle) => {
      timers.delete(handle as number);
    },
  };
  const advance = (ms: number) => {
    const target = now + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]);
      now = due[1].at;
      due[1].fn();
    }
    now = target;
  };
  return { clock, advance, pending: () => timers.size };
}

test("every subscriber is notified at each minute boundary, on one shared timer", () => {
  const { clock, advance, pending } = fakeClock(10 * MINUTE_MS + 15_000);
  const tick = createMinuteTick(clock);
  const calls = { a: 0, b: 0 };

  const offA = tick.subscribe(() => calls.a++);
  const offB = tick.subscribe(() => calls.b++);
  assert.equal(pending(), 1);
  assert.equal(tick.getSnapshot(), 10);

  advance(44_999);
  assert.deepEqual(calls, { a: 0, b: 0 });
  advance(1);
  assert.deepEqual(calls, { a: 1, b: 1 });
  assert.equal(tick.getSnapshot(), 11);

  advance(3 * MINUTE_MS);
  assert.deepEqual(calls, { a: 4, b: 4 });
  assert.equal(pending(), 1);

  offA();
  assert.equal(pending(), 1);
  offB();
  assert.equal(pending(), 0);
  advance(5 * MINUTE_MS);
  assert.deepEqual(calls, { a: 4, b: 4 });
});

test("the snapshot is stable within a minute", () => {
  const { clock, advance } = fakeClock(3 * MINUTE_MS);
  const tick = createMinuteTick(clock);
  const first = tick.getSnapshot();
  advance(MINUTE_MS - 1);
  assert.equal(tick.getSnapshot(), first);
});

test("a resubscription after the last unsubscribe arms the timer again", () => {
  const { clock, advance, pending } = fakeClock(0);
  const tick = createMinuteTick(clock);
  let calls = 0;
  tick.subscribe(() => calls++)();
  assert.equal(pending(), 0);
  tick.subscribe(() => calls++);
  assert.equal(pending(), 1);
  advance(MINUTE_MS);
  assert.equal(calls, 1);
});

test("the memoized conversation sidebar draws its relative times through the minute tick", () => {
  const source = readFileSync(new URL("../pages/chat/ConversationSidebar.tsx", import.meta.url), "utf8");
  assert.match(source, /export const ConversationSidebar = memo\(/);
  assert.match(source, /function UpdatedAgo\(\{ iso \}: \{ iso: string \}\) \{\s*useMinuteTick\(\);/);
  assert.match(source, /<UpdatedAgo iso=\{conv\.updated_at\} \/>/);
  assert.doesNotMatch(source, /\{timeAgo\(conv\.updated_at\)\}/);
});
