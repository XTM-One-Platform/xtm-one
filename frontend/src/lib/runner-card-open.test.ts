/**
 * Unit tests for the opened state of machine activity cards - `yarn test:unit`.
 *
 * Every command card of a running turn arrived open (the last two, the live
 * one, a failed one), each with its whole command and output: the transcript
 * filled with bodies and the newest card was cut by the composer. Cards now
 * arrive collapsed and open on a click only, for the session.
 */

import { beforeEach, test } from "node:test";
import assert from "node:assert/strict";

import { OPENED_CARDS_LIMIT, isRunnerCardOpen, resetRunnerCardOpen, setRunnerCardOpen } from "./runner-card-open.ts";

beforeEach(() => resetRunnerCardOpen());

test("a card arrives collapsed", () => {
  assert.equal(isRunnerCardOpen("cmd-1"), false);
});

test("a card the reader opened stays open, under the same command id (live card then saved card)", () => {
  setRunnerCardOpen("cmd-1", true);
  assert.equal(isRunnerCardOpen("cmd-1"), true);
  assert.equal(isRunnerCardOpen("cmd-2"), false);
});

test("a card the reader closed is collapsed again", () => {
  setRunnerCardOpen("cmd-1", true);
  setRunnerCardOpen("cmd-1", false);
  assert.equal(isRunnerCardOpen("cmd-1"), false);
});

test("a card without a command id never opens by itself and records nothing", () => {
  setRunnerCardOpen(undefined, true);
  setRunnerCardOpen(null, true);
  setRunnerCardOpen("", true);
  assert.equal(isRunnerCardOpen(undefined), false);
  assert.equal(isRunnerCardOpen(""), false);
});

test("the session forgets the oldest openings past the limit", () => {
  for (let i = 0; i <= OPENED_CARDS_LIMIT; i += 1) setRunnerCardOpen(`cmd-${i}`, true);
  assert.equal(isRunnerCardOpen("cmd-0"), false);
  assert.equal(isRunnerCardOpen("cmd-1"), true);
  assert.equal(isRunnerCardOpen(`cmd-${OPENED_CARDS_LIMIT}`), true);
});

test("opening a card again makes it the newest, so it is forgotten last", () => {
  for (let i = 0; i < OPENED_CARDS_LIMIT; i += 1) setRunnerCardOpen(`cmd-${i}`, true);
  setRunnerCardOpen("cmd-0", true);
  setRunnerCardOpen("cmd-new", true);
  assert.equal(isRunnerCardOpen("cmd-0"), true);
  assert.equal(isRunnerCardOpen("cmd-1"), false);
});
