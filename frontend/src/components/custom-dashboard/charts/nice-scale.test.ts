import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { niceScale, valueBounds } from "./nice-scale.ts";

describe("the value axis ApexCharts lays out", () => {
  it("reads a series of zeros 0 to 2, by halves (OpenCTI's empty line and bars)", () => {
    assert.deepEqual(niceScale({ min: 0, max: 0, length: 380, horizontal: false }), [0, 0.5, 1, 1.5, 2]);
  });

  it("reads one interval up to the next nice value on OpenCTI's horizontal bars (tickAmount 1)", () => {
    assert.deepEqual(
      niceScale({
        min: 0,
        max: 4,
        length: 488,
        horizontal: true,
        tickAmount: 1,
      }),
      [0, 5],
    );
    assert.deepEqual(
      niceScale({
        min: 0,
        max: 1,
        length: 488,
        horizontal: true,
        tickAmount: 1,
      }),
      [0, 1],
    );
    assert.deepEqual(
      niceScale({
        min: 0,
        max: 31,
        length: 488,
        horizontal: true,
        tickAmount: 1,
      }),
      [0, 50],
    );
  });

  it("steps by a nice value its room allows, and fewer when the axis is short", () => {
    assert.deepEqual(niceScale({ min: 0, max: 40, length: 380, horizontal: false }), [0, 10, 20, 30, 40]);
    assert.deepEqual(niceScale({ min: 0, max: 7, length: 380, horizontal: false }), [0, 2, 4, 6, 8]);
    const short = niceScale({
      min: 0,
      max: 100,
      length: 160,
      horizontal: false,
    });
    assert.equal(short[0], 0);
    assert.ok(short.length <= 4 && short[short.length - 1] >= 100);
  });

  it("snaps a least value close to zero onto it, and spans negative values", () => {
    assert.equal(niceScale({ min: 2, max: 40, length: 380, horizontal: false })[0], 0);
    assert.deepEqual(niceScale({ min: -10, max: 10, length: 380, horizontal: false }), [-10, -5, 0, 5, 10]);
  });
});

describe("the bounds a value axis shows", () => {
  it("reads each series apart, and a stack by its totals, positive and negative apart", () => {
    const rows = [
      { s0: 3, s1: 4, s2: 10 },
      { s0: -2, s1: -5, s2: null },
    ];
    assert.deepEqual(
      valueBounds(rows, 3, (i) => i < 2),
      { min: -7, max: 10 },
    );
    assert.deepEqual(
      valueBounds(rows, 2, () => false),
      { min: -5, max: 4 },
    );
  });

  it("reads zero when nothing is drawn", () => {
    assert.deepEqual(
      valueBounds([{ s0: null }], 1, () => false),
      { min: 0, max: 0 },
    );
  });
});
