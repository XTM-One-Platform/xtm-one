/**
 * Unit tests for the waiting arcade's secrets - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { isAchievementId } from "./achievements.ts";
import {
  KONAMI_CODE,
  PAUSE_TIPS,
  RARE_MESSAGE_CHANCE,
  RARE_WAITING_MESSAGES,
  SecretCodeListener,
  WAITING_MESSAGES,
  isEditableTarget,
  normalizeKey,
  playTimeAchievements,
  seasonalTheme,
  waitingMessage,
} from "./easter-eggs.ts";

const KONAMI_KEYS = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"];

function feed(listener: SecretCodeListener, keys: string[]): (string | null)[] {
  return keys.map((key) => listener.push(key));
}

test("the Konami code completes on its last key, whatever came before", () => {
  const listener = new SecretCodeListener();
  const results = feed(listener, ["x", "Shift", "ArrowUp", ...KONAMI_KEYS]);
  assert.equal(results[results.length - 1], "konami");
  assert.deepEqual(results.slice(0, -1).filter(Boolean), []);
  assert.equal(KONAMI_CODE.length, KONAMI_KEYS.length);
});

test("typed codes are case-insensitive and reset once matched", () => {
  const listener = new SecretCodeListener();
  assert.equal(feed(listener, ["I", "D", "D", "Q", "D"]).pop(), "iddqd");
  // The buffer was cleared: the tail of the previous code does not leak into the next.
  assert.equal(feed(listener, ["d"]).pop(), null);
  assert.equal(feed(listener, ["i", "d", "k", "f", "a"]).pop(), "idkfa");
  assert.equal(feed(listener, ["4", "2"]).pop(), "answer");
  assert.equal(feed(listener, ["h", "e", "l", "l", "o"]).pop(), "hello");
  assert.equal(feed(listener, ["x", "y", "z", "z", "y"]).pop(), "xyzzy");
  assert.equal(feed(listener, ["s", "u", "d", "o"]).pop(), "sudo");
});

test("modifier and function keys are ignored, not recorded", () => {
  assert.equal(normalizeKey("Shift"), null);
  assert.equal(normalizeKey("F5"), null);
  assert.equal(normalizeKey(""), null);
  assert.equal(normalizeKey("ArrowLeft"), "arrowleft");
  assert.equal(normalizeKey("Q"), "q");
  const listener = new SecretCodeListener();
  assert.equal(feed(listener, ["i", "Shift", "d", "Control", "d", "q", "d"]).pop(), "iddqd");
});

test("a text field is never a place for global shortcuts", () => {
  assert.equal(isEditableTarget({ tagName: "TEXTAREA" }), true);
  assert.equal(isEditableTarget({ tagName: "input", type: "text" }), true);
  assert.equal(isEditableTarget({ tagName: "INPUT" }), true);
  assert.equal(isEditableTarget({ tagName: "INPUT", type: "checkbox" }), false);
  assert.equal(isEditableTarget({ tagName: "SELECT" }), true);
  assert.equal(isEditableTarget({ tagName: "DIV", isContentEditable: true }), true);
  assert.equal(isEditableTarget({ tagName: "DIV", getAttribute: (n: string) => (n === "role" ? "textbox" : null) }), true);
  assert.equal(isEditableTarget({ tagName: "BUTTON" }), false);
  assert.equal(isEditableTarget(null), false);
  assert.equal(isEditableTarget("div"), false);
});

test("the calendar knows its days", () => {
  assert.equal(seasonalTheme(new Date(2026, 9, 31)), "halloween");
  assert.equal(seasonalTheme(new Date(2026, 9, 23)), null);
  assert.equal(seasonalTheme(new Date(2026, 11, 24)), "christmas");
  assert.equal(seasonalTheme(new Date(2027, 3, 1)), "aprilFools");
  assert.equal(seasonalTheme(new Date(2027, 2, 14)), "piDay");
  assert.equal(seasonalTheme(new Date(2027, 4, 4)), "mayFourth");
  assert.equal(seasonalTheme(new Date(2026, 8, 26)), null);
});

test("playing at odd hours unlocks the calendar achievements", () => {
  assert.deepEqual(playTimeAchievements(new Date(2026, 8, 26, 3, 0)), ["night_owl"]);
  assert.deepEqual(playTimeAchievements(new Date(2026, 8, 26, 6, 30)), ["early_bird"]);
  assert.deepEqual(playTimeAchievements(new Date(2026, 8, 26, 12, 0)), []);
  // 2026-09-25 is a Friday.
  assert.deepEqual(playTimeAchievements(new Date(2026, 8, 25, 18, 0)), ["friday_deploy"]);
  assert.deepEqual(playTimeAchievements(new Date(2026, 9, 31, 12, 0)), ["trick_or_treat"]);
  for (const id of playTimeAchievements(new Date(2026, 11, 25, 2, 0))) assert.ok(isAchievementId(id), id);
});

test("waiting messages cycle, with a rare one now and then", () => {
  assert.equal(waitingMessage(0, 0.5), WAITING_MESSAGES[0]);
  assert.equal(waitingMessage(WAITING_MESSAGES.length + 2, 0.9), WAITING_MESSAGES[2]);
  assert.equal(waitingMessage(-1, 0.9), WAITING_MESSAGES[WAITING_MESSAGES.length - 1]);
  const rare = new Set<string>();
  for (let i = 0; i < 1000; i += 1) rare.add(waitingMessage(0, (i / 1000) * RARE_MESSAGE_CHANCE));
  assert.deepEqual([...rare].sort(), [...RARE_WAITING_MESSAGES].sort());
});

test("texts are ICU-safe i18n ids", () => {
  for (const text of [...WAITING_MESSAGES, ...RARE_WAITING_MESSAGES, ...PAUSE_TIPS]) {
    assert.ok(text.trim().length > 0);
    assert.ok(!/[{}]/.test(text), text);
  }
});
