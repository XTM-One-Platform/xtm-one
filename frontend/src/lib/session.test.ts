/**
 * Unit tests for the web session rules - `yarn test:unit`.
 *
 * An ended session is told apart from every other refusal, the warning and the
 * check are timed on the server's clock, and real input is signalled often
 * enough to keep an active person signed in without flooding the server.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ACTIVITY_SIGNAL_MAX_INTERVAL_MS,
  ACTIVITY_SIGNAL_MIN_INTERVAL_MS,
  ACTIVITY_SIGNAL_WITHOUT_IDLE_TIMEOUT_MS,
  MAX_TIMER_DELAY_MS,
  activitySignalIntervalMs,
  idleWarningLeadMs,
  namedSessionEndReason,
  readSessionEndedParam,
  safeReturnPath,
  secondsLeft,
  sessionEndReason,
  sessionEndedLoginPath,
  sessionTiming,
  signedInUrlSwitchesSession,
  streamSessionEndReason,
  timerDelay,
} from "./session.ts";

test("a 401 says the session ended only with its code", () => {
  assert.equal(sessionEndReason({ code: "session_expired", reason: "idle" }), "idle");
  assert.equal(sessionEndReason({ code: "session_expired", reason: "lifetime" }), "lifetime");
  assert.equal(sessionEndReason({ code: "session_expired", reason: "other" }), "lifetime");
  assert.equal(sessionEndReason("Session expired, please sign in again"), null);
  assert.equal(sessionEndReason("Invalid token"), null);
  assert.equal(sessionEndReason({ code: "ai_disabled" }), null);
  assert.equal(sessionEndReason(null), null);
});

test("the sign-in page reads why the session ended, the PIN lock included", () => {
  assert.equal(readSessionEndedParam("?session_ended=pin"), "pin");
  assert.equal(readSessionEndedParam("?session_ended=idle"), "idle");
  assert.equal(readSessionEndedParam("?session_ended=other"), null);
  // A server signal never says "pin": only the PIN lock's own 401 does.
  assert.equal(sessionEndReason({ code: "session_expired", reason: "pin" }), "lifetime");
});

test("the activity stream's event names its reason", () => {
  assert.equal(streamSessionEndReason('{"reason": "idle"}'), "idle");
  assert.equal(streamSessionEndReason("not json"), "lifetime");
});

test("an ended session's named reason is read, its lifetime when it names none", () => {
  assert.equal(namedSessionEndReason("idle"), "idle");
  assert.equal(namedSessionEndReason("lifetime"), "lifetime");
  assert.equal(namedSessionEndReason(undefined), "lifetime");
  assert.equal(namedSessionEndReason("other"), "lifetime");
  assert.equal(streamSessionEndReason("null"), "lifetime");
});

test("the sign-in page is told why, and reads only the reasons there are", () => {
  assert.equal(sessionEndedLoginPath("idle"), "/login?session_ended=idle");
  assert.equal(readSessionEndedParam("?session_ended=idle"), "idle");
  assert.equal(readSessionEndedParam("?session_ended=toString"), null);
  assert.equal(readSessionEndedParam(""), null);
});

test("only a path on this origin is a page to come back to", () => {
  assert.equal(safeReturnPath("/agents/123?tab=memory"), "/agents/123?tab=memory");
  assert.equal(safeReturnPath("/chat#latest"), "/chat#latest");
  for (const raw of ["//evil.example/x", "/\\evil.example", "https://evil.example", "/login", "/login?x=1", "", null]) {
    assert.equal(safeReturnPath(raw), null, String(raw));
  }
  assert.equal(safeReturnPath("/a\nb"), null);
});

test("a signed-in URL switches the session only over another stored token", () => {
  assert.equal(signedInUrlSwitchesSession("admin-jwt", "peer-jwt"), true);
  assert.equal(signedInUrlSwitchesSession(null, "peer-jwt"), false);
  assert.equal(signedInUrlSwitchesSession("", "peer-jwt"), false);
  assert.equal(signedInUrlSwitchesSession("peer-jwt", "peer-jwt"), false);
});

test("real input is signalled a quarter of the idle timeout apart, within bounds", () => {
  assert.equal(activitySignalIntervalMs(30 * 60), ACTIVITY_SIGNAL_MAX_INTERVAL_MS);
  assert.equal(activitySignalIntervalMs(120), 30_000);
  assert.equal(activitySignalIntervalMs(30), ACTIVITY_SIGNAL_MIN_INTERVAL_MS);
  assert.equal(activitySignalIntervalMs(0), ACTIVITY_SIGNAL_WITHOUT_IDLE_TIMEOUT_MS);
  // The shortest idle timeout the server accepts (5 minutes) still sees several signals.
  assert.ok(ACTIVITY_SIGNAL_WITHOUT_IDLE_TIMEOUT_MS * 4 <= 5 * 60 * 1000);
});

test("the warning comes a minute before, or a quarter of a short timeout", () => {
  assert.equal(idleWarningLeadMs(30 * 60), 60_000);
  assert.equal(idleWarningLeadMs(60), 15_000);
  assert.equal(idleWarningLeadMs(0), 0);
});

const SERVER_NOW = Date.parse("2026-10-02T10:00:00Z");

function status(overrides: Partial<Parameters<typeof sessionTiming>[0]> = {}) {
  return {
    kind: "session",
    server_time: new Date(SERVER_NOW).toISOString(),
    expires_at: new Date(SERVER_NOW + 8 * 3600_000).toISOString(),
    idle_timeout_seconds: 1800,
    idle_expires_at: new Date(SERVER_NOW + 1800_000).toISOString(),
    ...overrides,
  };
}

test("the deadlines are read on the server's clock", () => {
  // The browser's clock runs five minutes behind the server's.
  const receivedAt = SERVER_NOW - 5 * 60_000;
  const timing = sessionTiming(status(), receivedAt);
  assert.ok(timing);
  assert.equal(timing.idleEndsAt, receivedAt + 1800_000);
  assert.equal(timing.endsAt, receivedAt + 1800_000);
  assert.equal(timing.warnAt, receivedAt + 1800_000 - 60_000);
});

test("no idle warning when the lifetime ends the session first, or without an idle timeout", () => {
  const lifetimeFirst = sessionTiming(
    status({ expires_at: new Date(SERVER_NOW + 600_000).toISOString() }),
    SERVER_NOW,
  );
  assert.equal(lifetimeFirst?.warnAt, null);
  assert.equal(lifetimeFirst?.endsAt, SERVER_NOW + 600_000);
  const noIdle = sessionTiming(status({ idle_timeout_seconds: 0, idle_expires_at: null }), SERVER_NOW);
  assert.deepEqual(noIdle, { endsAt: SERVER_NOW + 8 * 3600_000, idleEndsAt: null, warnAt: null });
});

test("a credential that is not a web session has nothing to time", () => {
  assert.equal(sessionTiming(status({ kind: "other" }), SERVER_NOW), null);
});

test("timers never exceed what setTimeout takes, nor go negative", () => {
  assert.equal(timerDelay(1_000, 400), 600);
  assert.equal(timerDelay(400, 1_000), 0);
  assert.equal(timerDelay(Number.MAX_SAFE_INTEGER, 0), MAX_TIMER_DELAY_MS);
  assert.equal(secondsLeft(10_400, 10_000), 1);
  assert.equal(secondsLeft(9_000, 10_000), 0);
});
