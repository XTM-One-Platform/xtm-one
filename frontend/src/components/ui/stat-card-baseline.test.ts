/**
 * The values of a StatCard strip share one baseline -
 * `yarn test:unit`.
 *
 * happy-dom computes no layout, so this pins the structure that gives the
 * baseline instead of measuring it; the UX lab measures the painted result.
 * The label block reserves two caption lines whether the label wraps or not,
 * and the value and subtitle follow it in normal flow. Pinning the value block
 * to the bottom of the card (`mt-auto`) put a value as high as its subtitle
 * was tall: in ja at 1024 a subtitle wrapping to three lines lifted its value
 * 30 px above its neighbours on the User detail strip.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./stat-card.tsx", import.meta.url), "utf8");
const code = source.replace(/\{\/\*[\s\S]*?\*\/\}/g, "");

const openingTag = (testId: string) => {
  const match = code.match(new RegExp(`<div[^>]*data-testid="${testId}"[^>]*>`));
  assert.ok(match, `no element with data-testid="${testId}"`);
  return match[0];
};

test("the label block reserves two caption lines", () => {
  const tag = openingTag("stat-card-label-block");
  assert.match(tag, /\bmin-h-\[2lh\]/, "the label block must reserve two lines");
  assert.match(tag, /\bcontent-caption\b/, "2lh must be measured in the caption line height");
});

test("the value is not pinned to the bottom of the card", () => {
  assert.doesNotMatch(code, /\bmt-auto\b/, "a value pinned to the bottom moves with its subtitle");
});

test("the value follows the label block, the subtitle follows the value", () => {
  const label = code.indexOf('data-testid="stat-card-label-block"');
  const value = code.indexOf('data-testid="stat-card-value"');
  const subtitle = code.indexOf("t_i18n(subtitle)");
  assert.ok(label >= 0 && value > label && subtitle > value, "order must be label, value, subtitle");
});
