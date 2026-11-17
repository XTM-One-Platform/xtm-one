/**
 * Unit tests for the activity stream's retry policy - `yarn test:unit`.
 *
 * A connection refused for a dead session must be followed by a session check
 * and a growing wait, never by the same request 5 seconds later forever, and
 * tabs dropped together must not come back together.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  STREAM_MAX_RETRY_DELAY_MS,
  STREAM_RECONNECT_DELAY_MS,
  streamRetry,
} from "./activity-stream-retry.ts";

const nominal = () => 0.5;

test("a stream that was open and ended reconnects after the usual delay, unchecked", () => {
  assert.deepEqual(streamRetry(0, nominal), {
    delayMs: STREAM_RECONNECT_DELAY_MS,
    checkSession: false,
  });
});

test("a connection that failed before it opened checks the session first", () => {
  for (const failures of [1, 2, 7, 50]) {
    assert.equal(streamRetry(failures, nominal).checkSession, true);
  }
});

test("failed connections back off exponentially from the usual delay", () => {
  assert.equal(streamRetry(1, nominal).delayMs, STREAM_RECONNECT_DELAY_MS);
  assert.equal(streamRetry(2, nominal).delayMs, STREAM_RECONNECT_DELAY_MS * 2);
  assert.equal(streamRetry(3, nominal).delayMs, STREAM_RECONNECT_DELAY_MS * 4);
  assert.equal(streamRetry(4, nominal).delayMs, STREAM_RECONNECT_DELAY_MS * 8);
});

test("the backoff is capped, however long the failures last", () => {
  assert.equal(streamRetry(7, nominal).delayMs, STREAM_MAX_RETRY_DELAY_MS);
  assert.equal(streamRetry(10_000, nominal).delayMs, STREAM_MAX_RETRY_DELAY_MS);
});

test("every wait is spread between half and one and a half times its nominal value", () => {
  for (const failures of [0, 1, 3, 10_000]) {
    const middle = streamRetry(failures, nominal).delayMs;
    assert.equal(streamRetry(failures, () => 0).delayMs, middle / 2);
    assert.ok(streamRetry(failures, () => 0.999_999).delayMs <= middle * 1.5);
  }
});

test("tabs dropped at the same moment come back spread over seconds", () => {
  let seed = 7;
  const random = () => {
    seed = (seed * 16_807) % 2_147_483_647;
    return seed / 2_147_483_647;
  };
  const delays = Array.from({ length: 60 }, () => streamRetry(0, random).delayMs);
  assert.ok(Math.max(...delays) - Math.min(...delays) >= STREAM_RECONNECT_DELAY_MS / 2);
});

test("a stream that keeps failing costs a handful of attempts an hour, not 720", () => {
  // The loop the access log showed: one refused request every 5 s.
  let failures = 0;
  let elapsed = 0;
  let attempts = 0;
  while (elapsed < 60 * 60_000) {
    failures += 1;
    attempts += 1;
    elapsed += streamRetry(failures, () => 0).delayMs;
  }
  assert.ok(attempts < 40, `${attempts} attempts in the first hour`);
});
