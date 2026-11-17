/**
 * The size the values on an axis chart are tried at first (`value_label_size`)
 * - `yarn test:dom`.
 *
 * Pinned here: the ladder a text scale or a number of canvas px names; the
 * reported combo - two bar series and a ratio on the right-hand axis over seven
 * quarters in one of two equal columns, `label_size` md - with
 * `value_label_size` 20 under `stagger`, drawn on the real Recharts figure in
 * happy-dom: its ticks at 22px, every value upright at 20px, a value lifted
 * a 20px row out only where something stands in its place; a text
 * scale sets the values in its ladder and leaves the
 * ticks alone; `value_label_fit` keeps the first size of the ladder; the
 * export payload carries the field only where it acts; and a chart that
 * names no value size, or names one where it does not act, draws what it drew.
 */
import { describe, expect, test } from "vitest";
import { cleanup, render } from "@testing-library/react";

import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";
import catalog from "./chart-catalog.json";
import { chartExportPayload } from "./chart-format";
import { axisChartLayout, CHART_TEXT_SCALES, chartTextScale, chartValueScale, LABEL_OFFSET } from "./chart-layout";

/** One column of a two-column content slide on the 1920px canvas, and a one-third dashboard panel. */
const COLUMN = 836;
const ONE_THIRD = 508;
/** The heights a chart takes in that column: `sm`, a `fill` chart under a heading, `md`. */
const HEIGHTS = [300, 420, 460] as const;
/** One row of `stagger` at 20px: the figures (0.74 em), the 6px halo and 4px of clearance. */
const ROW_20 = Math.ceil(20 * 0.74 + 6 + 4);

const M = 1_000_000;
const QUARTERS = ["Q1 24", "Q2 24", "Q3 24", "Q4 24", "Q1 25", "Q2 25", "Q3 25", "Q4 25"];

/** The acceptance chart. */
const REPORTED: DeckChartBlock = {
  type: "chart",
  kind: "combo",
  format: "currency",
  secondary_format: "number",
  secondary_unit: "x",
  label_size: "md",
  categories: QUARTERS.slice(0, 7),
  series: [
    { name: "A", kind: "bar", values: [2.6, 3.6, 4.2, 4.3, 4.4, 5.2, 4.3] },
    { name: "B", kind: "bar", values: [2.6, 3.7, 2.5, 6.3, 4.6, 7.8, 5.9] },
    { name: "Ratio", kind: "line", axis: "secondary", values: [1.10, 1.15, 0.65, 1.65, 1.17, 1.64, 1.54] },
  ],
};
const ASKED: DeckChartBlock = { ...REPORTED, value_label_size: 20, value_label_fit: "stagger" };

const chart = (series: DeckChartBlock["series"], categories: string[], extra: Partial<DeckChartBlock> = {}): DeckChartBlock => ({
  type: "chart",
  kind: "bar",
  format: "currency",
  unit: "EUR",
  series,
  categories,
  ...extra,
});
/** Two series over eight quarters, the second far shorter than the first. */
const ARR = chart(
  [
    { name: "ARR", values: [42.8, 46.2, 49.8, 52.4, 56.0, 59.2, 63.1, 66.4].map((v) => v * M) },
    { name: "Net new ARR", values: [4.2, 3.4, 3.6, 2.6, 3.6, 3.2, 3.8, 7.6].map((v) => v * M) },
  ],
  QUARTERS,
);
/** New logos by quarter: eight bars whose values fit their bands at every size. */
const LOGOS = chart([{ name: "New logos", values: [13, 10, 15, 11, 17, 14, 20, 26] }], QUARTERS, { format: "number", unit: undefined });
/** A stack whose segments write their values inside. */
const STACK = chart(
  [
    { name: "New", values: [4.1, 4.4, 4.2, 4.6, 4.9, 5.1, 5.0, 5.4].map((v) => v * M) },
    { name: "Expansion", values: [2.2, 2.0, 2.4, 2.3, 2.6, 2.5, 2.8, 2.9].map((v) => v * M) },
  ],
  QUARTERS,
  { kind: "stacked_bar" },
);
const LINE = chart([{ name: "ASP", values: [246_800, 277_800, 305_400, 342_600, 379_600, 429_200, 475_000, 536_400] }], QUARTERS, {
  kind: "line",
});
const DONUT = chart([{ name: "Share", values: [40, 30, 20, 10] }], ["A", "B", "C", "D"], { kind: "donut", format: "percent", unit: undefined });

const plan = (block: DeckChartBlock, width: number, height: number) => axisChartLayout(block, width, height, new Set(), "en");

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const touches = (a: Box, b: Box): boolean => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
/** A bar's box as Recharts writes it, four decimals to a coordinate: a value ending where the bar begins reads as touching it. */
const inset = (box: Box): Box => ({ left: box.left + 0.001, right: box.right - 0.001, top: box.top + 0.001, bottom: box.bottom - 0.001 });

/** The ink of a value: its glyphs (0.6 em each, set semibold) and the height of its figures (0.74 em), with its 6px halo. */
function inkOf(text: SVGTextElement, size: number): Box {
  const x = Number(text.getAttribute("x"));
  const y = Number(text.getAttribute("y"));
  const length = (text.textContent ?? "").length * size * 0.6;
  const height = size * 0.74;
  const anchor = text.getAttribute("text-anchor");
  const baseline = text.getAttribute("dominant-baseline");
  const [left, right] = anchor === "middle" ? [x - length / 2, x + length / 2] : anchor === "start" ? [x, x + length] : [x - length, x];
  const [top, bottom] = baseline === "hanging" ? [y, y + height] : baseline === "central" ? [y - height / 2, y + height / 2] : [y - height, y];
  return { left: left - 3, right: right + 3, top: top - 3, bottom: bottom + 3 };
}

/** Recharts' generated ids count every chart drawn before; the figure is otherwise the same markup. */
const markup = (html: string): string => html.replace(/recharts\d+-clip/g, "recharts#-clip").replace(/_r_[0-9a-z]+_/g, "_r_#_");

/** The chart drawn at `width` x `height`: its markup, its ticks, its values, its bars and its facts. */
function draw(block: DeckChartBlock, width: number, height: number) {
  const { container } = render(
    <ChartCanvas
      block={block}
      width={width}
      height={height}
      hidden={new Set()}
      animate={false}
      locale="en"
      onSettled={() => {}}
      gapLabel="Unexplained"
      runningLabel="Running total"
    />,
  );
  const ticks = Array.from(
    container.querySelectorAll<SVGTextElement>(".deck-chart-tick-category, .deck-chart-tick-primary, .deck-chart-tick-secondary"),
  ).map((text) => Number(text.getAttribute("font-size")));
  const values = Array.from(container.querySelectorAll<SVGTextElement>("text.deck-chart-value")).map((text) => {
    const size = Number.parseFloat(text.style.fontSize);
    return {
      text: text.textContent ?? "",
      role: text.dataset.role,
      series: Number(text.dataset.series),
      category: Number(text.dataset.category),
      x: Number(text.getAttribute("x")),
      y: Number(text.getAttribute("y")),
      size,
      rotated: text.dataset.rotate === "true",
      lift: Number(text.dataset.lift ?? 0),
      ink: inkOf(text, size),
    };
  });
  const names = block.series.map((entry) => entry.name);
  const seen = new Map<number, number>();
  const bars = Array.from(container.querySelectorAll<SVGPathElement>("path.recharts-rectangle")).map((path) => {
    const series = names.indexOf(path.getAttribute("name") ?? "");
    const category = seen.get(series) ?? 0;
    seen.set(series, category + 1);
    const x = Number(path.getAttribute("x"));
    const y = Number(path.getAttribute("y"));
    const w = Number(path.getAttribute("width"));
    const h = Number(path.getAttribute("height"));
    return { series, category, box: { left: x, right: x + w, top: Math.min(y, y + h), bottom: Math.max(y, y + h) } };
  });
  const facts = { ...(container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset ?? {}) };
  const html = markup(new XMLSerializer().serializeToString(container));
  cleanup();
  return { html, ticks, values, bars, facts };
}

describe("the ladder a value size names", () => {
  test("unset, the values take label_size's text scale", () => {
    for (const label_size of [undefined, "sm", "md", "lg"] as const) {
      const block = { ...LOGOS, label_size };
      expect(chartValueScale(block), String(label_size)).toBe(chartTextScale(block));
    }
  });

  test("a text scale's name gives its values and its segments' floor, whatever label_size says", () => {
    for (const label_size of ["sm", "md", "lg"] as const) {
      for (const value_label_size of ["sm", "md", "lg"] as const) {
        const block: DeckChartBlock = { ...LOGOS, label_size, value_label_size };
        expect(chartValueScale(block)).toBe(CHART_TEXT_SCALES[value_label_size]);
        expect(chartTextScale(block).tick).toBe(CHART_TEXT_SCALES[label_size].tick);
      }
    }
  });

  test.each([
    [20, [20, 18, 16, 14, 12], 14],
    [22, [22, 20, 18, 16, 14], 16],
    [32, [32, 30, 28, 26, 24], 26],
    [21, [21, 19, 17, 15, 13], 15],
    [17, [17, 15, 13, 11], 11],
    [15, [15, 13, 11], 10],
    [12, [12, 10], 10],
    [11, [11], 10],
    [10, [10], 10],
  ])("%ipx tries %j, its segments never under %ipx", (size, values, segmentFloor) => {
    expect(chartValueScale({ ...LOGOS, value_label_size: size })).toEqual({ values, segmentFloor });
  });

  test("a size outside the domain, or on a kind that is not an axis chart, leaves the values on label_size", () => {
    for (const value_label_size of [9, 33, 0, 20.5, Number.NaN, "xl"]) {
      const block = { ...LOGOS, label_size: "sm", value_label_size } as DeckChartBlock;
      expect(chartValueScale(block), String(value_label_size)).toBe(CHART_TEXT_SCALES.sm);
    }
    for (const kind of ["pie", "donut", "gauge"] as const) {
      expect(chartValueScale({ ...DONUT, kind, value_label_size: 20 }), kind).toBe(CHART_TEXT_SCALES.md);
    }
  });
});

describe("the reported combo at md, values at 20px, staggered", () => {
  test.each(HEIGHTS)("ticks at 22px and every value upright at 20px, a 20px row out where something stands in its place, at %ipx high", (height) => {
    // A value is lifted only where a value, a bar or the line stands in its
    // place, and then by a row of the values' own size at least.
    const layout = plan(ASKED, COLUMN, height);
    expect(layout.tickSize).toBe(22);
    expect(layout.values).toMatchObject({ outer: { size: 20, rotate: false }, fit: "stagger", asked: 21, written: 21, reason: null, turned: null });
    expect(layout.values.points?.size).toBeLessThanOrEqual(20);

    const drawn = draw(ASKED, COLUMN, height);
    expect(drawn.facts).toMatchObject({ valuesFit: "stagger", valuesAsked: "21", valuesWritten: "21", labelSize: "20" });
    expect(drawn.facts.labelRotate).toBeUndefined();
    expect(drawn.ticks.length).toBeGreaterThan(0);
    for (const size of drawn.ticks) expect(size).toBe(22);

    const past = drawn.values.filter((value) => value.role === "value" && value.series < 2);
    expect(past).toHaveLength(14);
    for (const value of past) {
      expect(value.size, value.text).toBe(20);
      expect(value.rotated, value.text).toBe(false);
      const own = drawn.bars.find((bar) => bar.series === value.series && bar.category === value.category);
      expect(own, `${value.text} has no bar`).toBeDefined();
      if (!own) continue;
      if (value.lift > 0) expect(value.lift, `${value.text} of series ${value.series}`).toBeGreaterThanOrEqual(ROW_20);
      expect(value.y).toBeCloseTo(own.box.top - LABEL_OFFSET - value.lift, 0);
      for (const bar of drawn.bars) {
        if (bar !== own) expect(touches(value.ink, inset(bar.box)), `${value.text} touches a bar of ${bar.series}:${bar.category}`).toBe(false);
      }
    }
    // Of two values at one height, €2.1 beside €2.1, the second rises one 20px row.
    const pair = past.filter((value) => value.category === 0).map((value) => value.lift);
    expect(pair).toEqual([0, ROW_20]);
    for (const value of drawn.values) {
      expect(value.size, value.text).toBeLessThanOrEqual(20);
      for (const other of drawn.values) {
        if (other !== value) expect(touches(value.ink, other.ink), `${value.text} touches ${other.text}`).toBe(false);
      }
    }
  });

  test("the axes keep the room label_size gives them: the values change, the ticks do not", () => {
    const sized = plan(ASKED, COLUMN, 420);
    const md = plan({ ...REPORTED, value_label_fit: "stagger" }, COLUMN, 420);
    expect(sized.readingAxisWidth).toBe(md.readingAxisWidth);
    expect(sized.secondaryAxisWidth).toBe(md.secondaryAxisWidth);
    expect(md.values.outer?.size).toBe(22);
  });

  test("upright keeps 20px on one row and counts what still touches its neighbour", () => {
    const layout = plan({ ...ASKED, value_label_fit: "upright" }, COLUMN, 420);
    expect(layout.tickSize).toBe(22);
    expect(layout.values.outer).toEqual({ size: 20, rotate: false });
    expect(layout.values.fit).toBe("upright");
    if (layout.values.reason === "upright") expect(layout.values.need).toBeGreaterThan(layout.values.room);
    const drawn = draw({ ...ASKED, value_label_fit: "upright" }, COLUMN, 420);
    for (const value of drawn.values.filter((each) => each.role === "value" && each.series < 2)) expect(value.size).toBe(20);
  });

  test("auto tries 20px first and never sets a value larger, writing what md writes once md is under 20px", () => {
    const md = plan(REPORTED, COLUMN, 420);
    const sized = plan({ ...REPORTED, value_label_size: 20 }, COLUMN, 420);
    expect(sized.values.fit).toBe("auto");
    expect(sized.tickSize).toBe(22);
    expect(md.values.outer?.size).toBeLessThan(20);
    // From 20px down, the ladder IS md's: the same plot, the same first size that fits.
    expect(sized.values.outer).toEqual(md.values.outer);
  });
});

describe("a text scale named for the values", () => {
  test("sm at label_size md: sm's value ladder, md's ticks", () => {
    const block = { ...ARR, label_size: "md" as const, value_label_size: "sm" as const };
    const layout = plan(block, COLUMN, 420);
    expect(layout.tickSize).toBe(22);
    expect(layout.readingAxisWidth).toBe(plan({ ...ARR, label_size: "md" }, COLUMN, 420).readingAxisWidth);
    expect(layout.readingAxisWidth).toBeGreaterThan(plan({ ...ARR, label_size: "sm" }, COLUMN, 420).readingAxisWidth);
    expect(CHART_TEXT_SCALES.sm.values).toContain(layout.values.outer?.size);
    const drawn = draw(block, COLUMN, 420);
    for (const size of drawn.ticks) expect(size).toBe(22);
    for (const value of drawn.values) expect(value.size).toBe(layout.values.outer?.size);
  });

  test("values larger than the ticks: lg values over sm ticks", () => {
    const layout = plan({ ...LOGOS, label_size: "sm", value_label_size: "lg" }, COLUMN, 420);
    expect(layout.tickSize).toBe(18);
    expect(layout.values.outer).toEqual({ size: 26, rotate: false });
    const drawn = draw({ ...LOGOS, label_size: "sm", value_label_size: "lg" }, COLUMN, 420);
    for (const size of drawn.ticks) expect(size).toBe(18);
    for (const value of drawn.values) expect(value.size).toBe(26);
  });

  test("a stack's segments read from the ladder, never above its totals", () => {
    const layout = plan({ ...STACK, value_label_size: 16 }, COLUMN, 420);
    expect(layout.tickSize).toBe(22);
    expect(layout.values.outer?.size).toBeLessThanOrEqual(16);
    expect(layout.values.inner?.size).toBeLessThanOrEqual(layout.values.outer?.size ?? 0);
    expect([16, 14, 12, 10]).toContain(layout.values.inner?.size);
  });

  test("a line's values and a waterfall's labels take it too", () => {
    expect(plan({ ...LINE, value_label_size: 14 }, COLUMN, 420).values.outer).toEqual({ size: 14, rotate: false });
    const waterfall = catalog.waterfall_chart as DeckChartBlock;
    const sized = plan({ ...waterfall, value_label_size: 16 }, COLUMN, 420);
    expect(sized.tickSize).toBe(chartTextScale(waterfall).tick);
    expect(sized.values.outer?.size).toBeLessThanOrEqual(16);
  });
});

describe("the export payload", () => {
  const payload = (block: DeckChartBlock) => JSON.parse(chartExportPayload(block, "en", "Unexplained")) as Record<string, unknown>;

  test("a chart that names no value size publishes what it always did", () => {
    for (const block of [REPORTED, ARR, LOGOS, STACK, LINE, DONUT]) expect(payload(block)).not.toHaveProperty("value_label_size");
  });

  test("a value size is published as written, beside label_size, and only that key changes", () => {
    for (const value_label_size of [20, 10, 32, "sm", "lg"] as const) {
      const sized = payload({ ...REPORTED, value_label_size });
      expect(sized).toMatchObject({ label_size: "md", value_label_size });
      expect(sized).toEqual({ ...payload(REPORTED), value_label_size });
    }
  });

  test("where it does not act, or outside its domain, nothing is published", () => {
    expect(payload({ ...DONUT, value_label_size: 20 })).toEqual(payload(DONUT));
    for (const value_label_size of [9, 33, 20.5, "xl"]) {
      expect(payload({ ...REPORTED, value_label_size } as DeckChartBlock)).toEqual(payload(REPORTED));
    }
  });
});

describe("the figure a value size leaves alone", () => {
  const SHAPES: [string, DeckChartBlock, number, number][] = [
    ["the reported combo", REPORTED, COLUMN, 420],
    ["two series over eight quarters, staggered", { ...ARR, value_label_fit: "stagger" }, COLUMN, 300],
    ["a stack in a one-third panel", STACK, ONE_THIRD, 420],
    ["a line at sm", { ...LINE, label_size: "sm" }, ONE_THIRD, 420],
    ["a waterfall at lg", { ...(catalog.waterfall_chart as DeckChartBlock), label_size: "lg" }, COLUMN, 420],
    ["a two-unit combo", catalog.dual_axis_chart as DeckChartBlock, COLUMN, 420],
  ];

  test.each(SHAPES)("%s: naming label_size's own scale draws the figure label_size alone draws", (_name, block, width, height) => {
    const named = draw({ ...block, value_label_size: block.label_size ?? "md" }, width, height);
    expect(named.html).toBe(draw(block, width, height).html);
  });

  test("a donut draws the same figure whatever value size it names", () => {
    const plain = draw(DONUT, ONE_THIRD, 420);
    for (const value_label_size of [10, 32, "lg"] as const) expect(draw({ ...DONUT, value_label_size }, ONE_THIRD, 420).html).toBe(plain.html);
  });
});
