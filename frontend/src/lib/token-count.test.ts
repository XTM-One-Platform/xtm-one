/**
 * Unit tests for the context-window field of Settings > AI and models > Models - `yarn test:unit`.
 *
 * The vectors are those of `backend/tests/test_context_window_scan.py`: the
 * field must accept exactly what the save accepts, or a value it shows as
 * valid is refused with a 422.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { MAX_CONTEXT_WINDOW_TOKENS, formatTokenCount, parseTokenCount } from "./token-count.ts";

const VALID: [string, number][] = [
  ["128K", 128_000],
  ["128k", 128_000],
  [" 128 K ", 128_000],
  ["1.05M", 1_050_000],
  ["1.050000M", 1_050_000],
  ["100.0M", MAX_CONTEXT_WINDOW_TOKENS],
  ["1,05M", 1_050_000],
  ["1M", 1_000_000],
  ["128.5K", 128_500],
  ["128.5000K", 128_500],
  ["0.5K", 500],
  ["200000", 200_000],
  ["200,000", 200_000],
  ["200 000", 200_000],
  ["1_000_000", 1_000_000],
  ["1\u202f000\u202f000", 1_000_000],
  ["100M", MAX_CONTEXT_WINDOW_TOKENS],
];

const INVALID = [
  "big",
  "128KB",
  "128 tokens",
  "1.5",
  "1.0000005M",
  "0",
  "0K",
  "-5",
  "-1M",
  "1,00,000",
  "200,000 000",
  "1e6",
  "128\u00a0K",
  "128\tK",
  "0x10",
  "\u0661\u0662\u0668",
  "\u0661\u0662\u0668K",
  "\u0661,\u0662\u0662\u0668",
  "101M",
  String(MAX_CONTEXT_WINDOW_TOKENS + 1),
  "99999999999999999999",
  "1.0000000000000000000000000000001M",
  "100.000000000000000000000000000001M",
  "1.0001K",
  "1".repeat(5000),
  `1.${"0".repeat(5000)}M`,
];

test("a context window is read as typed, shorthand included", () => {
  for (const [typed, tokens] of VALID) {
    assert.deepEqual(parseTokenCount(typed), { kind: "tokens", tokens }, typed);
  }
});

test("anything else is not a context window", () => {
  for (const typed of INVALID) {
    assert.deepEqual(parseTokenCount(typed), { kind: "invalid" }, typed);
  }
});

test("an empty field is no override", () => {
  assert.deepEqual(parseTokenCount(""), { kind: "empty" });
  assert.deepEqual(parseTokenCount("   "), { kind: "empty" });
});

test("a stored window is shown in its shortest shorthand", () => {
  const shown: [number, string][] = [
    [128_000, "128K"],
    [1_050_000, "1.05M"],
    [1_000_000, "1M"],
    [200_000, "200K"],
    [128_500, "128.5K"],
    [1_048_576, "1048576"],
    [131_072, "131072"],
    [1_234_000, "1234K"],
    [8_192, "8192"],
    [999, "999"],
  ];
  for (const [tokens, text] of shown) assert.equal(formatTokenCount(tokens), text, String(tokens));
});

test("what is shown reads back to the same count", () => {
  const counts = [1, 999, 1_000, 8_192, 32_768, 128_000, 128_500, 131_072, 200_000, 272_000,
    922_000, 1_000_000, 1_048_576, 1_050_000, 1_234_567, 2_000_000, MAX_CONTEXT_WINDOW_TOKENS];
  for (const tokens of counts) {
    assert.deepEqual(parseTokenCount(formatTokenCount(tokens)), { kind: "tokens", tokens }, String(tokens));
  }
});
