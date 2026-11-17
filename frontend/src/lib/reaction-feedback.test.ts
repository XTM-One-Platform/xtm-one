/**
 * Unit tests for the reaction setting editor - `yarn test:unit`.
 *
 * The editor spells emojis as the server stores them and never builds a setting
 * the server would refuse (`backend/tests/test_reaction_feedback.py`).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_REACTION_FEEDBACK,
  MAX_EMOJIS_PER_SIGN,
  REACTION_PALETTE,
  nextSign,
  normalizeEmoji,
  paletteFor,
  signOf,
  withSign,
} from "./reaction-feedback.ts";

const UP = "\u{1F44D}";
const DOWN = "\u{1F44E}";
const HEART = "❤️";

test("an emoji is spelled without skin tone nor presentation selector", () => {
  assert.equal(normalizeEmoji(`${UP}\u{1F3FD}`), UP);
  assert.equal(normalizeEmoji(HEART), "❤");
  assert.equal(normalizeEmoji(` ${DOWN} `), DOWN);
});

test("the sign of an emoji is read whatever its spelling", () => {
  const setting = { enabled: true, positive: ["❤"], negative: [DOWN] };
  assert.equal(signOf(setting, HEART), "positive");
  assert.equal(signOf(setting, `${DOWN}\u{1F3FF}`), "negative");
  assert.equal(signOf(setting, UP), null);
});

test("a click cycles none, positive, negative, none", () => {
  assert.equal(nextSign(null), "positive");
  assert.equal(nextSign("positive"), "negative");
  assert.equal(nextSign("negative"), null);
});

test("an emoji moves between signs and is never in both", () => {
  let setting = withSign(DEFAULT_REACTION_FEEDBACK, HEART, "positive");
  assert.deepEqual(setting.positive, [UP, "❤"]);
  setting = withSign(setting, HEART, "negative");
  assert.deepEqual([setting.positive, setting.negative], [[UP], [DOWN, "❤"]]);
  setting = withSign(setting, HEART, null);
  assert.deepEqual([setting.positive, setting.negative], [[UP], [DOWN]]);
  // The default is never changed in place.
  assert.deepEqual(DEFAULT_REACTION_FEEDBACK.positive, [UP]);
});

test("a full sign takes no more emojis", () => {
  const full = { enabled: true, positive: REACTION_PALETTE.slice(2, 2 + MAX_EMOJIS_PER_SIGN).map(normalizeEmoji), negative: [] };
  const setting = withSign(full, UP, "positive");
  assert.equal(setting.positive.length, MAX_EMOJIS_PER_SIGN);
  assert.equal(signOf(setting, UP), null);
});

test("the palette shows the emojis of the setting it does not offer", () => {
  const setting = { enabled: true, positive: ["\u{1F680}"], negative: [DOWN] };
  const palette = paletteFor(setting);
  assert.equal(palette.length, REACTION_PALETTE.length + 1);
  assert.equal(palette[palette.length - 1], "\u{1F680}");
});
