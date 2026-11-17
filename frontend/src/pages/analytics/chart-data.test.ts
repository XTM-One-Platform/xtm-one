/** `yarn test:unit`: the series LLM Analytics draws. */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  OTHER_KEY,
  TOTAL_KEY,
  bucketRows,
  bucketStart,
  defaultBucket,
  foldStack,
  labelKey,
  longestFigure,
  pivotCostSeries,
  placeValueLabel,
  stackTotals,
  valueLabelLayout,
  windowDays,
} from "./chart-data.ts";

test("a week starts on Monday and a month on its first day, in UTC", () => {
  assert.equal(bucketStart("2026-10-23", "week"), "2026-10-19"); // a Friday
  assert.equal(bucketStart("2026-10-19", "week"), "2026-10-19"); // the Monday itself
  assert.equal(bucketStart("2026-10-25", "week"), "2026-10-19"); // the Sunday after
  assert.equal(bucketStart("2026-10-23", "month"), "2026-10-01");
  assert.equal(bucketStart("2026-10-23", "day"), "2026-10-23");
});

test("rows are summed per bucket, every key kept", () => {
  const rows = pivotCostSeries([
    { date: "2026-10-19", model: "a", cost: 1 },
    { date: "2026-10-20", model: "b", cost: 2 },
    { date: "2026-10-27", model: "a", cost: 4 },
  ]);
  assert.deepEqual(bucketRows(rows, "week"), [
    { date: "2026-10-19", a: 1, b: 2 },
    { date: "2026-10-26", a: 4, b: 0 },
  ]);
});

test("the smallest keys fold into Other and each row carries its total", () => {
  const rows = [
    { date: "d1", big: 10, mid: 5, small: 1, tiny: 0.5 },
    { date: "d2", big: 2, mid: 1, small: 3, tiny: 0 },
  ];
  const folded = foldStack(rows, 2);
  assert.deepEqual(folded.keys, ["big", "mid", OTHER_KEY]);
  assert.equal(folded.rows[0][OTHER_KEY], 1.5);
  assert.equal(folded.rows[0][TOTAL_KEY], 16.5);
  assert.equal(folded.rows[1][TOTAL_KEY], 6);
  // Nothing to fold: no Other series.
  assert.deepEqual(foldStack(rows, 10).keys, ["big", "mid", "small", "tiny"]);
});

test("the opening bucket keeps a figure per bar readable", () => {
  assert.equal(defaultBucket(windowDays("30d")), "day");
  assert.equal(defaultBucket(windowDays("90d")), "week");
  assert.equal(defaultBucket(windowDays("180d")), "week");
  assert.equal(defaultBucket(windowDays("1y")), "month");
  assert.equal(windowDays("custom", "2026-01-01", "2026-03-02"), 60);
  assert.equal(windowDays("custom", "", ""), 30);
});

test("a stack's total rides on its topmost segment with a value", () => {
  const keys = ["big", "mid", OTHER_KEY];
  assert.equal(labelKey({ date: "d", big: 3, mid: 2, [OTHER_KEY]: 1 }, keys), OTHER_KEY);
  // The top series is at zero for that bucket: the total moves down, never vanishes.
  assert.equal(labelKey({ date: "d", big: 3, mid: 2, [OTHER_KEY]: 0 }, keys), "mid");
  assert.equal(labelKey({ date: "d", big: 3, mid: 0, [OTHER_KEY]: 0 }, keys), "big");
  // An empty bucket draws no bar and so no figure.
  assert.equal(labelKey({ date: "d", big: 0, mid: 0, [OTHER_KEY]: 0 }, keys), undefined);
  // The Total view draws one bar.
  assert.equal(labelKey({ date: "d", [TOTAL_KEY]: 4 }, [TOTAL_KEY]), TOTAL_KEY);
});

test("each row carries its stack total for the figure over its top", () => {
  const rows = stackTotals(
    [
      { date: "d1", a: 1.5, b: 2 },
      { date: "d2", a: 0, b: 0 },
    ],
    ["a", "b"],
  );
  assert.deepEqual(
    rows.map((row) => row[TOTAL_KEY]),
    [3.5, 0],
  );
  assert.equal(rows[0].a, 1.5, "the series are kept");
});

test("every bar keeps a label slot, at 31 and at 365 buckets", () => {
  for (const count of [1, 7, 12, 13, 31, 53, 90, 365]) {
    const rows = Array.from({ length: count }, (_, index) => ({ date: `d${index}`, a: index % 3, b: 1 }));
    const labelled = rows.filter((row) => labelKey(row, ["a", "b"]) !== undefined);
    assert.equal(labelled.length, count, `${count} buckets`);
    const layout = valueLabelLayout(count);
    if (count > 12) assert.ok(layout.minWidth >= count * layout.fontSize, `${count} buckets keep a floor`);
    else assert.equal(layout.minWidth, 0);
    assert.ok(layout.fontSize >= 9);
  }
});

/** The plot width of Usage over time on a wide card (xl two columns at 1440) and at 1024. */
const WIDE = 1180;
const NARROW = 860;

test("the figure over a bar is never vertical, whatever the buckets and the card", () => {
  for (const count of [1, 7, 12, 13, 31, 53, 90, 180, 365]) {
    for (const plotWidth of [0, 320, NARROW, WIDE, 4800]) {
      for (const longest of [1, 4, 6, 8]) {
        const layout = valueLabelLayout(count, { plotWidth, longest });
        assert.ok(["flat", "stagger", "diagonal", "hidden"].includes(layout.mode), `${count}/${plotWidth}/${longest}`);
        assert.equal("rotated" in layout, false);
      }
    }
  }
});

test("7 and 12 buckets write flat at the normal size", () => {
  for (const count of [7, 12]) {
    for (const plotWidth of [NARROW, WIDE]) {
      const layout = valueLabelLayout(count, { plotWidth, longest: longestFigure(["$12.50", "$0.40"]) });
      assert.equal(layout.mode, "flat", `${count} at ${plotWidth}`);
      assert.equal(layout.fontSize, 10);
    }
  }
});

test("31 daily buckets on a wide card stay flat, a size smaller or staggered, never diagonal first", () => {
  // About 38 px a bucket: a short figure fits flat at the normal size.
  assert.deepEqual(
    [valueLabelLayout(31, { plotWidth: WIDE, longest: 4 }).mode, valueLabelLayout(31, { plotWidth: WIDE, longest: 4 }).fontSize],
    ["flat", 10],
  );
  // The decision steps down one rung at a time as the figure grows.
  assert.equal(valueLabelLayout(31, { plotWidth: WIDE, longest: 6 }).fontSize, 9);
  assert.equal(valueLabelLayout(31, { plotWidth: WIDE, longest: 6 }).mode, "flat");
  assert.equal(valueLabelLayout(31, { plotWidth: WIDE, longest: 8 }).mode, "stagger");
  // The same 31 buckets on the scroll floor (17 px a bucket) with long figures: diagonal, still not vertical.
  assert.equal(valueLabelLayout(31, { plotWidth: 31 * 17, longest: 8 }).mode, "diagonal");
});

test("90 buckets write diagonal or hide, never vertical", () => {
  for (const plotWidth of [NARROW, WIDE]) {
    const mode = valueLabelLayout(90, { plotWidth, longest: 6 }).mode;
    assert.ok(mode === "diagonal" || mode === "hidden", `${plotWidth}: ${mode}`);
  }
});

test("the five rungs of the decision: flat, small, stagger, diagonal, hidden", () => {
  // 10 buckets, a 5 character figure: 34 px at 10 px, 31 px at 9 px.
  assert.deepEqual(pick(valueLabelLayout(10, { plotWidth: 400, longest: 5 })), ["flat", 10]);
  assert.deepEqual(pick(valueLabelLayout(10, { plotWidth: 320, longest: 5 })), ["flat", 9]);
  assert.deepEqual(pick(valueLabelLayout(10, { plotWidth: 200, longest: 5 })), ["stagger", 9]);
  assert.deepEqual(pick(valueLabelLayout(10, { plotWidth: 170, longest: 5 })), ["diagonal", 9]);
  assert.deepEqual(pick(valueLabelLayout(10, { plotWidth: 100, longest: 5 })), ["hidden", 9]);
  // A diagonal figure takes room above the bars and leans past the last one; a hidden one takes none.
  const diagonal = valueLabelLayout(10, { plotWidth: 170, longest: 5 });
  assert.ok(diagonal.topMargin > 20 && diagonal.rightMargin > 0);
  assert.equal(valueLabelLayout(10, { plotWidth: 100, longest: 5 }).rightMargin, 0);
  // Staggered figures need a second row above the bars.
  assert.ok(valueLabelLayout(10, { plotWidth: 200, longest: 5 }).topMargin > 20);
});

/** The box a figure of *text* takes once placed, at 0.6 em a character (the layout's own estimate). */
function figureBox(layout: ReturnType<typeof valueLabelLayout>, x: number, y: number, index: number, text: string) {
  const place = placeValueLabel(layout, { x, y, width: layout.slot * 0.8, index, text });
  assert.ok(place);
  const width = text.length * layout.fontSize * 0.6;
  const left = place.anchor === "start" ? place.x : place.anchor === "end" ? place.x - width : place.x - width / 2;
  return { place, left, right: left + width, top: place.y - layout.fontSize, bottom: place.y };
}

/** Bars of uneven heights side by side: a short one, a tall one, a short one. */
const UNEVEN = [180, 40, 175, 60, 190, 45, 170, 50, 185, 42];
const FIGURES = ["$85.41", "$162", "$98.11", "$172", "$140", "$342", "$170", "$12.50", "$9.99", "$222"];

test("staggered figures over uneven bars never touch, and stay inside the plot", () => {
  const layout = valueLabelLayout(10, { plotWidth: 230, longest: 6 });
  assert.equal(layout.mode, "stagger");
  const boxes = UNEVEN.map((top, index) => figureBox(layout, index * layout.slot + layout.slot * 0.1, top, index, FIGURES[index]));
  for (let index = 0; index < boxes.length; index += 1) {
    const box = boxes[index];
    // Every figure sits above the tallest bar, in the room the layout keeps over the plot.
    assert.ok(box.bottom < layout.topMargin && box.top >= 0, `${index} above the plot`);
    for (let other = index + 1; other < boxes.length; other += 1) {
      const next = boxes[other];
      const apart = box.right <= next.left || next.right <= box.left || box.bottom <= next.top || next.bottom <= box.top;
      assert.ok(apart, `${FIGURES[index]} and ${FIGURES[other]} overlap`);
    }
  }
  // The first and last figures stay inside the plot.
  assert.ok(boxes[0].left >= 0);
  assert.ok(boxes[boxes.length - 1].right <= 10 * layout.slot);
});

test("diagonal figures over uneven bars share one baseline above the tallest bar", () => {
  const layout = valueLabelLayout(10, { plotWidth: 170, longest: 6 });
  assert.equal(layout.mode, "diagonal");
  const places = UNEVEN.map((top, index) => figureBox(layout, index * layout.slot, top, index, FIGURES[index]).place);
  // One baseline for all, whatever the bar heights: two parallel 45 degree figures one bucket apart
  // are slot x sin 45 apart, at least the line height, so they never cross.
  assert.equal(new Set(places.map((place) => place.y)).size, 1);
  assert.ok(places[0].y < layout.topMargin);
  assert.ok(layout.slot * Math.SQRT1_2 >= LINE_BOX);
  // The rise of the longest figure stays inside the room kept above the plot, so it crosses no bar.
  const rise = (6 * layout.fontSize * 0.6) * Math.SQRT1_2;
  assert.ok(places[0].y - rise >= 0);
  for (const place of places) assert.equal(place.angle, -45);
});

/** The rendered line box of a 9 px figure: 9 x 1.2, rounded up. */
const LINE_BOX = 11;

test("the diagonal slot holds the rendered 11 px line box, with air between neighbours", () => {
  // ceil(11 x sqrt 2) + 1 = 17 px a bucket: two parallel figures one bucket apart are 17 x sin 45 = 12 px apart.
  const at17 = valueLabelLayout(10, { plotWidth: 170, longest: 6 });
  assert.equal(at17.mode, "diagonal");
  assert.ok(at17.slot * Math.SQRT1_2 - LINE_BOX >= 1, "at least a pixel of air between two line boxes");
  // A bucket narrower than that (the former 14 px slot sized on a 9 px line) no longer writes diagonal.
  for (const plotWidth of [140, 160]) assert.equal(valueLabelLayout(10, { plotWidth, longest: 6 }).mode, "hidden", `${plotWidth}`);
  // Past a dozen buckets the scroll floor gives every bucket the diagonal slot.
  for (const count of [13, 31, 53, 90, 365]) {
    const floor = valueLabelLayout(count);
    assert.ok(floor.minWidth >= count * 17, `${count} buckets`);
    assert.ok((floor.minWidth - 64) / count >= 17);
  }
});

test("the first and last staggered figures are clamped inside the drawing box", () => {
  // 30 daily buckets on a narrow card: the first bar's centre sits nearer the left edge than half its figure.
  const layout = valueLabelLayout(30, { plotWidth: 30 * 23, longest: 6, boxWidth: 30 * 23 });
  assert.equal(layout.mode, "stagger");
  const width = 6 * layout.fontSize * 0.6;
  for (const x of [-0.53, 0, 0.4, 1.2]) {
    const first = placeValueLabel(layout, { x, y: 100, width: 8, index: 0, text: "$85.41" });
    assert.ok(first);
    const left = first.anchor === "start" ? first.x : first.anchor === "end" ? first.x - width : first.x - width / 2;
    assert.ok(left >= 0, `first figure at bar x ${x} starts at ${left}`);
  }
  // The mirror: the last figure never ends past the drawing box's right edge.
  for (const shift of [-1.2, 0, 0.53, 2]) {
    const x = layout.plotRight - 8 + shift;
    const last = placeValueLabel(layout, { x, y: 100, width: 8, index: 29, text: "$85.41" });
    assert.ok(last);
    const right = last.anchor === "end" ? last.x : last.anchor === "start" ? last.x + width : last.x + width / 2;
    assert.ok(right <= layout.plotRight, `last figure at bar x ${x} ends at ${right}`);
  }
  // A short edge figure that fits its own bucket stays centred over its bar.
  assert.equal(placeValueLabel(layout, { x: 40, y: 100, width: 8, index: 0, text: "$1" })?.anchor, "middle");
});

test("a flat figure sits over its own bar, a hidden one is not written", () => {
  const flat = valueLabelLayout(7, { plotWidth: 800, longest: 4 });
  assert.deepEqual(placeValueLabel(flat, { x: 10, y: 120, width: 20, index: 3, text: "$12" }), { x: 20, y: 116, anchor: "middle", angle: 0 });
  const hidden = valueLabelLayout(10, { plotWidth: 100, longest: 5 });
  assert.equal(placeValueLabel(hidden, { x: 10, y: 120, width: 8, index: 0, text: "$12" }), null);
});

test("the longest figure is the widest formatted value", () => {
  assert.equal(longestFigure(["$1", "$12.50", "$0.4"]), 6);
  assert.equal(longestFigure([]), 0);
});

function pick(layout: ReturnType<typeof valueLabelLayout>): [string, number] {
  return [layout.mode, layout.fontSize];
}
