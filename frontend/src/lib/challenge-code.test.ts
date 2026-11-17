/**
 * Unit tests for the emergency stop's typed challenge code.
 *
 * Run with `yarn test:unit` — Node's built-in runner, stripping the types
 * natively, so this needs no test framework in `package.json`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CHALLENGE_ALPHABET,
  CHALLENGE_LENGTH,
  challengeMatches,
  generateChallengeCode,
} from "./challenge-code.ts";

test("generates a code of the expected length from the unambiguous alphabet", () => {
  for (let i = 0; i < 50; i += 1) {
    const code = generateChallengeCode();
    assert.equal(code.length, CHALLENGE_LENGTH);
    for (const ch of code) assert.ok(CHALLENGE_ALPHABET.includes(ch), `unexpected char ${ch}`);
  }
  // The alphabet is what makes the code retypable off a screen.
  for (const ambiguous of ["0", "O", "1", "I"]) assert.ok(!CHALLENGE_ALPHABET.includes(ambiguous));
});

test("two consecutive codes are (overwhelmingly) different", () => {
  const codes = new Set(Array.from({ length: 20 }, () => generateChallengeCode()));
  assert.ok(codes.size > 1);
});

test("matches ignoring case and surrounding whitespace, and nothing else", () => {
  assert.equal(challengeMatches("K7MN2P", "K7MN2P"), true);
  assert.equal(challengeMatches("K7MN2P", "  k7mn2p "), true);
  assert.equal(challengeMatches("K7MN2P", "K7MN2"), false);
  assert.equal(challengeMatches("K7MN2P", "K7MN2PX"), false);
  assert.equal(challengeMatches("K7MN2P", ""), false);
  // An empty code must never enable the button.
  assert.equal(challengeMatches("", ""), false);
});
