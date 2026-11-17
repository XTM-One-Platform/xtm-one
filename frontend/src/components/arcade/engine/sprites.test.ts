/**
 * The waiting arcade's pixel art is hand-drawn text: keep it well-formed.
 * `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { SPRITES, frameCount, spriteSize, type SpriteName } from "./sprites.ts";

test("every frame is a rectangle of palette slots, the same size across frames", () => {
  for (const name of Object.keys(SPRITES) as SpriteName[]) {
    const frames = SPRITES[name];
    assert.ok(frames.length > 0, name);
    const { w, h } = spriteSize(name);
    assert.ok(w > 0 && h > 0, name);
    frames.forEach((frame, index) => {
      assert.equal(frame.length, h, `${name}[${index}] height`);
      for (const row of frame) {
        assert.equal(row.length, w, `${name}[${index}] row "${row}"`);
        assert.match(row, /^[0-4]+$/, `${name}[${index}] row "${row}"`);
      }
      assert.ok(frame.some((row) => /[1-4]/.test(row)), `${name}[${index}] is empty`);
    });
    assert.equal(frameCount(name), frames.length);
  }
});

test("the animated invaders have two frames to march with", () => {
  for (const name of ["squid", "crab", "octopus", "bug", "pumpkin", "hallucination", "minion"] as SpriteName[]) {
    assert.equal(frameCount(name), 2, name);
  }
});
