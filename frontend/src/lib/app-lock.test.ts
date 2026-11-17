import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appLockCode,
  isAppLockEnded,
  isAppLockedFrame,
  pinProblem,
  wrongPinAttemptsLeft,
} from "./app-lock.ts";

test("a 423 names the lock screen to show, and nothing else does", () => {
  assert.equal(appLockCode({ code: "app_locked" }), "app_locked");
  assert.equal(appLockCode({ code: "app_pin_setup_required" }), "app_pin_setup_required");
  assert.equal(appLockCode({ code: "session_expired" }), null);
  assert.equal(appLockCode("Locked"), null);
  assert.equal(appLockCode(null), null);
});

test("only the PIN's own 401 says the session ended", () => {
  assert.equal(isAppLockEnded({ code: "app_lock_ended" }), true);
  assert.equal(isAppLockEnded({ code: "session_expired", reason: "idle" }), false);
  assert.equal(isAppLockEnded("app_lock_ended"), false);
  assert.equal(isAppLockEnded(undefined), false);
});

test("a chat stream's lock frame is told apart from every other frame", () => {
  assert.equal(isAppLockedFrame({ type: "app_locked", turn_id: "t1" }), true);
  assert.equal(isAppLockedFrame({ type: "error", content: "app_locked" }), false);
  assert.equal(isAppLockedFrame({ type: "session_expired" }), false);
  assert.equal(isAppLockedFrame(null), false);
});

test("a wrong PIN says how many tries are left, never below zero", () => {
  assert.equal(wrongPinAttemptsLeft({ code: "app_pin_wrong", attempts_left: 3 }), 3);
  assert.equal(wrongPinAttemptsLeft({ code: "app_pin_wrong", attempts_left: -2 }), 0);
  assert.equal(wrongPinAttemptsLeft({ code: "app_pin_wrong", attempts_left: "3" }), null);
  assert.equal(wrongPinAttemptsLeft({ code: "app_pin_wrong" }), null);
  assert.equal(wrongPinAttemptsLeft({ code: "other", attempts_left: 3 }), null);
});

test("the PIN rules are the server's", () => {
  const cases: [string, ReturnType<typeof pinProblem>][] = [
    ["482915", null],
    ["48291573", null],
    ["12345", "length"],
    ["482915736", "length"],
    ["", "digits"],
    ["48291a", "digits"],
    ["٤٨٢٩١٥", "digits"],
    ["000000", "repeated"],
    ["123456", "sequence"],
    ["98765432", "sequence"],
    ["135790", null],
  ];
  for (const [pin, problem] of cases) assert.equal(pinProblem(pin, 6, 8), problem, pin);
});
