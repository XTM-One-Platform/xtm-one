/**
 * The live room's heartbeat - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_HEARTBEAT_SECONDS,
  FIRST_HEARTBEAT_MS,
  MAX_HEARTBEAT_SECONDS,
  MIN_HEARTBEAT_SECONDS,
  heartbeatIntervalMs,
  retryAfterMs,
  startHeartbeat,
  type HeartbeatTimers,
} from "./heartbeat.ts";

/** Timers on a clock the test moves by hand. */
function manualClock() {
  let now = 0;
  let nextId = 1;
  const pending = new Map<number, { at: number; callback: () => void }>();
  const timers: HeartbeatTimers = {
    set: (callback, ms) => {
      const id = nextId++;
      pending.set(id, { at: now + ms, callback });
      return id;
    },
    clear: (id) => {
      pending.delete(id);
    },
    now: () => now,
  };
  const advance = (ms: number) => {
    const end = now + ms;
    for (;;) {
      const due = [...pending.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      pending.delete(due[0]);
      now = due[1].at;
      due[1].callback();
    }
    now = end;
  };
  return { timers, advance, now: () => now, pending: () => pending.size };
}

/** Let the answers the test resolved reach their handlers. */
const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

test("the cadence is the server's", () => {
  assert.equal(heartbeatIntervalMs(12), 12_000);
  assert.equal(heartbeatIntervalMs(7.5), 7_500);
});

test("a cadence out of range is clamped", () => {
  assert.equal(heartbeatIntervalMs(1), MIN_HEARTBEAT_SECONDS * 1000);
  assert.equal(heartbeatIntervalMs(0.01), MIN_HEARTBEAT_SECONDS * 1000);
  assert.equal(heartbeatIntervalMs(3600), MAX_HEARTBEAT_SECONDS * 1000);
});

test("without a cadence from the server, the client beats at the server's default", () => {
  for (const value of [undefined, null, 0, -10, Number.NaN, Number.POSITIVE_INFINITY, "20" as unknown as number]) {
    assert.equal(heartbeatIntervalMs(value), DEFAULT_HEARTBEAT_SECONDS * 1000, String(value));
  }
  assert.ok(MIN_HEARTBEAT_SECONDS <= DEFAULT_HEARTBEAT_SECONDS && DEFAULT_HEARTBEAT_SECONDS <= MAX_HEARTBEAT_SECONDS);
});

test("the loop beats first soon, then at the cadence it last heard", () => {
  const clock = manualClock();
  const beats: number[] = [];
  let serverSeconds: number | null = null;
  const stop = startHeartbeat(
    () => {
      beats.push(clock.now());
      return undefined;
    },
    () => heartbeatIntervalMs(serverSeconds),
    clock.timers,
  );
  // The session answers before the first beat: 12 seconds.
  serverSeconds = 12;
  clock.advance(FIRST_HEARTBEAT_MS);
  clock.advance(12_000);
  assert.deepEqual(beats, [1_500, 13_500]);
  serverSeconds = 20;
  clock.advance(12_000);
  clock.advance(20_000);
  assert.deepEqual(beats, [1_500, 13_500, 25_500, 45_500]);
  stop();
  assert.equal(clock.pending(), 0);
  clock.advance(60_000);
  assert.equal(beats.length, 4);
});

test("a beat's answer that changes the cadence paces the very next beat, counted from the beat", async () => {
  const clock = manualClock();
  const beats: number[] = [];
  const answers: Array<() => void> = [];
  let serverSeconds = 10;
  const stop = startHeartbeat(
    () => {
      beats.push(clock.now());
      return new Promise<void>((resolve) => answers.push(resolve));
    },
    () => heartbeatIntervalMs(serverSeconds),
    clock.timers,
  );
  clock.advance(FIRST_HEARTBEAT_MS);
  // The answer comes 300 ms after the beat and says 20 seconds.
  clock.advance(300);
  serverSeconds = 20;
  answers.shift()?.();
  await settle();
  // The 10 s wait the beat left with is gone...
  clock.advance(10_000);
  assert.deepEqual(beats, [1_500]);
  // ...the next beat comes 20 s after the first one.
  clock.advance(1_500 + 20_000 - clock.now());
  assert.deepEqual(beats, [1_500, 21_500]);
  stop();
});

test("an answer that arrives after the next beat left changes nothing", async () => {
  const clock = manualClock();
  const beats: number[] = [];
  const answers: Array<() => void> = [];
  let serverSeconds = 10;
  const stop = startHeartbeat(
    () => {
      beats.push(clock.now());
      return new Promise<void>((resolve) => answers.push(resolve));
    },
    () => heartbeatIntervalMs(serverSeconds),
    clock.timers,
  );
  clock.advance(FIRST_HEARTBEAT_MS + 10_000);
  assert.deepEqual(beats, [1_500, 11_500]);
  // The first beat's answer, 12 s late, says 20 seconds: the second beat's wait stands.
  serverSeconds = 20;
  answers.shift()?.();
  await settle();
  clock.advance(10_000);
  assert.deepEqual(beats, [1_500, 11_500, 21_500]);
  // The cadence it said is still the one the loop follows from there.
  clock.advance(20_000);
  assert.deepEqual(beats, [1_500, 11_500, 21_500, 41_500]);
  stop();
});

test("an answer shorter than the time already waited beats at once", async () => {
  const clock = manualClock();
  const beats: number[] = [];
  const answers: Array<() => void> = [];
  let serverSeconds = 30;
  const stop = startHeartbeat(
    () => {
      beats.push(clock.now());
      return new Promise<void>((resolve) => answers.push(resolve));
    },
    () => heartbeatIntervalMs(serverSeconds),
    clock.timers,
  );
  clock.advance(FIRST_HEARTBEAT_MS);
  clock.advance(10_000);
  serverSeconds = MIN_HEARTBEAT_SECONDS;
  answers.shift()?.();
  await settle();
  clock.advance(0);
  assert.deepEqual(beats, [1_500, 11_500]);
  stop();
});

test("a request that never answers never stalls the loop", () => {
  const clock = manualClock();
  let beats = 0;
  const stop = startHeartbeat(
    () => {
      beats += 1;
      return new Promise<void>(() => undefined);
    },
    () => heartbeatIntervalMs(undefined),
    clock.timers,
  );
  clock.advance(FIRST_HEARTBEAT_MS + 3 * DEFAULT_HEARTBEAT_SECONDS * 1000);
  assert.equal(beats, 4);
  stop();
});

test("a refused beat's Retry-After holds the next beat, never longer than the slowest cadence", async () => {
  const clock = manualClock();
  const beats: number[] = [];
  const answers: Array<(value: unknown) => void> = [];
  const stop = startHeartbeat(
    () => {
      beats.push(clock.now());
      return new Promise((resolve) => answers.push(resolve));
    },
    () => heartbeatIntervalMs(10),
    clock.timers,
  );
  clock.advance(FIRST_HEARTBEAT_MS);
  // Refused 200 ms after the beat: wait 25 s, counted from the answer.
  clock.advance(200);
  answers.shift()?.({ retryAfterSeconds: 25 });
  await settle();
  clock.advance(10_000);
  assert.deepEqual(beats, [1_500]);
  clock.advance(15_000);
  assert.deepEqual(beats, [1_500, 26_700]);
  // A hostile Retry-After cannot stall the loop past the slowest cadence.
  answers.shift()?.({ retryAfterSeconds: 86_400 });
  await settle();
  clock.advance(MAX_HEARTBEAT_SECONDS * 1000);
  assert.deepEqual(beats, [1_500, 26_700, 26_700 + MAX_HEARTBEAT_SECONDS * 1000]);
  stop();
});

test("only a positive, finite Retry-After counts", () => {
  for (const answer of [undefined, null, 3, "5", {}, { retryAfterSeconds: 0 }, { retryAfterSeconds: -4 }, { retryAfterSeconds: Number.NaN }]) {
    assert.equal(retryAfterMs(answer), 0, JSON.stringify(answer));
  }
  assert.equal(retryAfterMs({ retryAfterSeconds: 7 }), 7_000);
  assert.equal(retryAfterMs({ retryAfterSeconds: 1e9 }), MAX_HEARTBEAT_SECONDS * 1000);
});

test("a beat that throws is a missed beat, never the end of the loop", () => {
  const clock = manualClock();
  let beats = 0;
  const stop = startHeartbeat(
    () => {
      beats += 1;
      if (beats === 1) throw new Error("boom");
      return undefined;
    },
    () => heartbeatIntervalMs(undefined),
    clock.timers,
  );
  clock.advance(FIRST_HEARTBEAT_MS + 2 * DEFAULT_HEARTBEAT_SECONDS * 1000);
  assert.equal(beats, 3);
  stop();
  assert.equal(clock.pending(), 0);
});

test("stopping schedules nothing more, from inside a beat or while an answer is on its way", async () => {
  const clock = manualClock();
  let beats = 0;
  const stop: () => void = startHeartbeat(
    () => {
      beats += 1;
      stop();
      return undefined;
    },
    () => 5_000,
    clock.timers,
  );
  clock.advance(60_000);
  assert.equal(beats, 1);
  assert.equal(clock.pending(), 0);

  const other = manualClock();
  const answers: Array<() => void> = [];
  let seconds = 10;
  const stopOther = startHeartbeat(
    () => new Promise<void>((resolve) => answers.push(resolve)),
    () => heartbeatIntervalMs(seconds),
    other.timers,
  );
  other.advance(FIRST_HEARTBEAT_MS);
  stopOther();
  seconds = 20;
  answers.shift()?.();
  await settle();
  assert.equal(other.pending(), 0);
});
