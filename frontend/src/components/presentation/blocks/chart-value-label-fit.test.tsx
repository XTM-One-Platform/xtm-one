/**
 * How the values past upright bars are fitted (`value_label_fit`) -
 * `yarn test:dom`.
 *
 * Pinned here, on the real Recharts figure drawn in happy-dom: a combo chart
 * of two bar series over seven quarters in one of two equal columns, at
 * `label_size` md, writes every value upright at 22px under `stagger` where
 * `auto` sets them smaller - each in its place, a row higher only where the
 * value beside it or the combo's line stands there; `upright` keeps
 * 22px, lifts a value that one row only where nothing stands there,
 * and counts the values it leaves out. Under both the bars
 * give way to the values first, and no value touches another value, a bar
 * but its own or the line - except a group of upright bars, which stands
 * where `auto` puts it and whose values clear one another only.
 * `auto`, and the field unset, draw exactly what the chart drew before it
 * existed; values that fit as asked, and charts the word does not act on,
 * are drawn as `auto` draws them.
 */
import { describe, expect, test } from "vitest";
import { cleanup, render } from "@testing-library/react";

import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";
import catalog from "./chart-catalog.json";
import { axisChartLayout, LABEL_OFFSET, LINE_WIDTH, monotoneCurve, pastBarKey } from "./chart-layout";

/** One column of a two-column content slide on the 1920px canvas, and a one-third dashboard panel. */
const COLUMN = 836;
const ONE_THIRD = 508;
/** The heights a chart takes in that column: `sm`, a `fill` chart under a heading, `md`. */
const HEIGHTS = [300, 420, 460] as const;
/** One row of `stagger` at md: a 22px value's figures (0.74 em), its 6px halo and 4px of clearance. */
const ROW_MD = 27;
/** The room a value keeps past its halo from a line's stroke (`LABEL_CLEARANCE`). */
const CLEARANCE = 4;

const M = 1_000_000;
const QUARTERS = ["Q1 24", "Q2 24", "Q3 24", "Q4 24", "Q1 25", "Q2 25", "Q3 25", "Q4 25"];

/** The acceptance chart: two bar series and a ratio on the right-hand axis over seven quarters, in currency. */
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
    { name: "Ratio", kind: "line", axis: "secondary", values: [1.12, 1.15, 0.64, 1.66, 1.17, 1.64, 1.55] },
  ],
};

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
/** Three bars a quarter, each narrower than its value. */
const SOURCES = chart(
  [
    { name: "New logos", values: [2.4, 1.8, 2.2, 1.4, 2.0, 1.6, 2.2, 4.4].map((v) => v * M) },
    { name: "Expansion", values: [1.4, 1.2, 1.0, 0.8, 1.2, 1.2, 1.2, 2.2].map((v) => v * M) },
    { name: "Partners", values: [0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 1.0].map((v) => v * M) },
  ],
  QUARTERS,
);
/** A span 2 dashboard panel's canvas on a board of one row of panels. */
const SPAN_2 = [1089, 300] as const;
/** Three bars a quarter, the churn below zero, the values at 16px under md's ticks. */
const PIPELINE = chart(
  [
    { name: "New logos", values: [2.4, 1.8, 2.2, 1.4, 2.0, 1.6, 2.2, 4.4].map((v) => v * M) },
    { name: "Expansion", values: [1.4, 1.2, 1.0, 0.8, 1.2, 1.2, 1.2, 2.2].map((v) => v * M) },
    { name: "Churn", values: [-0.6, -0.8, -0.4, -1.0, -0.6, -0.4, -0.8, -1.2].map((v) => v * M) },
  ],
  QUARTERS,
  { label_size: "md", value_label_size: 16 },
);
/** A half-width dashboard panel's canvas (`panel_columns: 2`) on a board of one row of panels. */
const HALF_PANEL = [798, 300] as const;
/**
 * Two bar series over eight quarters, `€0.6M` to `€3.3M` at 16px, with a
 * churn rate on the right-hand axis running through the bars: in four
 * quarters the expansion stands just under the new business.
 */
const SEASONAL: DeckChartBlock = {
  type: "chart",
  kind: "combo",
  format: "currency",
  unit: "EUR",
  decimals: 1,
  unit_scale: "M",
  secondary_format: "percent",
  label_size: "md",
  value_label_size: 16,
  value_label_fit: "upright",
  categories: QUARTERS,
  series: [
    { name: "New business", kind: "bar", values: [1.2, 1.6, 1.4, 2.9, 1.5, 2.0, 1.8, 3.3].map((v) => v * M) },
    { name: "Expansion", kind: "bar", values: [0.6, 1.5, 0.9, 2.8, 1.0, 1.9, 1.2, 3.2].map((v) => v * M) },
    { name: "Churn", kind: "line", axis: "secondary", show_values: false, values: [6.1, 5.8, 5.5, 5.9, 5.2, 4.9, 4.6, 4.4] },
  ],
};
/** One series of eight quarters as level as a plan, wider than its band in a one-third panel. */
const BOOKINGS = chart([{ name: "Bookings", values: [12.4, 12.9, 13.1, 12.7, 13.4, 13.0, 13.8, 14.2].map((v) => v * M) }], QUARTERS);
/** A stack's totals over eight quarters in a one-third panel. */
const STACK = chart(
  [
    { name: "New", values: [4.1, 4.4, 4.2, 4.6, 4.9, 5.1, 5.0, 5.4].map((v) => v * M) },
    { name: "Expansion", values: [2.2, 2.0, 2.4, 2.3, 2.6, 2.5, 2.8, 2.9].map((v) => v * M) },
  ],
  QUARTERS,
  { kind: "stacked_bar" },
);
/** A margin that crosses zero: values past the ends of bars that run both ways. */
const MARGIN = chart([{ name: "EBITDA", values: [-2.1, -1.4, -0.6, 0.3, 1.1, 1.9, 2.6, 3.2].map((v) => v * M) }], QUARTERS);

const REGIONS = ["EMEA", "North America", "APAC", "LATAM"];
const SEVERITIES = ["Critical", "High", "Medium", "Low"];
/** Four severities' share of each region's alerts, on its side, at md. */
const shares = (rows: number[][], extra: Partial<DeckChartBlock> = {}): DeckChartBlock =>
  chart(
    SEVERITIES.map((name, index) => ({ name, values: rows[index] })),
    REGIONS,
    { kind: "stacked_bar", horizontal: true, format: "percent", unit: undefined, label_size: "md", ...extra },
  );
/** The acceptance pair: the first holds a 5% and a 3% segment, too short for a 22px value; the second none. */
const SHARES_THIN = shares([
  [8, 5, 12, 3],
  [27, 31, 22, 18],
  [41, 38, 45, 46],
  [24, 26, 21, 33],
]);
const SHARES_EVEN = shares([
  [14, 9, 6, 11],
  [33, 28, 36, 25],
  [30, 42, 35, 40],
  [23, 21, 23, 24],
]);
/** The same severities as grouped bars on their side: four bars a region, each thinner than a 22px value. */
const SHARES_GROUPED = { ...SHARES_THIN, kind: "bar" as const };
/** Grouped bars of nearly equal length: every value stands beside its neighbours'. */
const SHARES_LEVEL = shares(
  [
    [40, 42, 38, 41],
    [39, 40, 41, 37],
    [41, 38, 40, 42],
    [38, 41, 39, 40],
  ],
  { kind: "bar" },
);

const plan = (block: DeckChartBlock, width: number, height: number, hidden: ReadonlySet<number> = new Set()) =>
  axisChartLayout(block, width, height, hidden, "en");

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const touches = (a: Box, b: Box): boolean => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

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

type Point = readonly [number, number];

/** A drawn path's cubics (`[from, control, control, to]`), read off its `d`: a straight run is a cubic too. */
function cubicsOf(d: string): Point[][] {
  const cubics: Point[][] = [];
  let at: Point = [0, 0];
  for (const [, command, args] of d.matchAll(/([MLC])([^MLC]*)/g)) {
    const numbers = (args.match(/-?\d*\.?\d+(?:e[-+]?\d+)?/gi) ?? []).map(Number);
    const points: Point[] = [];
    for (let index = 0; index + 1 < numbers.length; index += 2) points.push([numbers[index], numbers[index + 1]]);
    if (command === "M") at = points[0];
    for (let index = 0; command === "C" && index + 2 < points.length; index += 3) {
      cubics.push([at, points[index], points[index + 1], points[index + 2]]);
      at = points[index + 2];
    }
    for (const to of command === "L" ? points : []) {
      cubics.push([at, at, to, to]);
      at = to;
    }
  }
  return cubics;
}

/** Points along drawn cubics, about a pixel apart. */
function along(cubics: readonly Point[][]): Point[] {
  return cubics.flatMap(([p0, p1, p2, p3]) => {
    const reach = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]) + Math.hypot(p2[0] - p1[0], p2[1] - p1[1]) + Math.hypot(p3[0] - p2[0], p3[1] - p2[1]);
    const steps = Math.max(1, Math.ceil(reach));
    return Array.from({ length: steps + 1 }, (_, step): Point => {
      const t = step / steps;
      const u = 1 - t;
      const [a, b, c, e] = [u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t];
      return [a * p0[0] + b * p1[0] + c * p2[0] + e * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + e * p3[1]];
    });
  });
}

/** Whether a line drawn through `samples` comes within `reach` px of a box, its stroke's half included. */
const nearLine = (box: Box, samples: readonly Point[], reach: number): boolean =>
  samples.some(([x, y]) => touches(box, { left: x - reach, right: x + reach, top: y - reach, bottom: y + reach }));

/** The chart drawn at `width` x `height`: its markup, its values, its bars, its lines and its facts. */
function draw(block: DeckChartBlock, width: number, height: number, hidden: ReadonlySet<number> = new Set()) {
  const { container } = render(
    <ChartCanvas
      block={block}
      width={width}
      height={height}
      hidden={new Set(hidden)}
      animate={false}
      locale="en"
      onSettled={() => {}}
      gapLabel="Unexplained"
      runningLabel="Running total"
    />,
  );
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
    let category = seen.get(series) ?? 0;
    // Recharts draws no rectangle for a zero: the n-th one drawn is the n-th non-zero value.
    while ((block.series[series]?.values[category] ?? 0) === 0 && category < block.categories.length) category += 1;
    seen.set(series, category + 1);
    const x = Number(path.getAttribute("x"));
    const y = Number(path.getAttribute("y"));
    const w = Number(path.getAttribute("width"));
    const h = Number(path.getAttribute("height"));
    return { series, category, width: w, box: { left: x, right: x + w, top: Math.min(y, y + h), bottom: Math.max(y, y + h) } };
  });
  // A line's dots are drawn in a layer of their own, named after its series as its path is.
  const dots = Array.from(container.querySelectorAll<SVGCircleElement>("circle.recharts-line-dot"));
  const lines = Array.from(container.querySelectorAll<SVGPathElement>("path.recharts-line-curve")).map((path) => ({
    cubics: cubicsOf(path.getAttribute("d") ?? ""),
    dots: dots
      .filter((dot) => dot.getAttribute("name") === path.getAttribute("name"))
      .map((dot): Point => [Number(dot.getAttribute("cx")), Number(dot.getAttribute("cy"))]),
  }));
  // Each leg of a leader from a thin segment to its value past the stack, its 2px stroke.
  const leaders = Array.from(container.querySelectorAll<SVGLineElement>("line.deck-chart-leader")).map((line) => {
    const [x1, y1, x2, y2] = ["x1", "y1", "x2", "y2"].map((name) => Number(line.getAttribute(name)));
    return {
      series: Number(line.dataset.series),
      category: Number(line.dataset.category),
      box: { left: Math.min(x1, x2) - 1, right: Math.max(x1, x2) + 1, top: Math.min(y1, y2) - 1, bottom: Math.max(y1, y2) + 1 },
    };
  });
  const facts = { ...(container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset ?? {}) };
  const html = markup(new XMLSerializer().serializeToString(container));
  cleanup();
  return { html, values, bars, lines, leaders, facts };
}

/** The values a drawing writes past its bars, along the category axis: the order they are fitted in. */
const pastBars = (drawn: ReturnType<typeof draw>) => {
  const barSeries = new Set(drawn.bars.map((bar) => bar.series));
  return drawn.values
    .filter((value) => value.role === "value" && barSeries.has(value.series))
    .sort((a, b) => a.x - b.x || a.series - b.series);
};
const keyOf = (value: { series: number; category: number }): string => `${value.series}:${value.category}`;

/** No value of the drawing comes within its clearance of a line's stroke (a quarter pixel spared for the curve's sampling). */
function expectClearOfLines(drawn: ReturnType<typeof draw>): void {
  const samples = drawn.lines.flatMap((line) => along(line.cubics));
  for (const value of pastBars(drawn)) {
    expect(nearLine(value.ink, samples, LINE_WIDTH / 2 + CLEARANCE - 0.25), `${value.text} is on the line`).toBe(false);
  }
}

/**
 * A value past a bar is lifted on demand: it rises when, in its place
 * - just past its bar - it would touch a value written before it, a bar but
 * its own or a line, and only then. The ticks stand outside every column of
 * the charts this is asked of. A line's run is kept clear of by boxes a
 * little wider than its stroke where it is steep, hence the slack in `may`.
 */
function expectLiftedOnDemand(drawn: ReturnType<typeof draw>): void {
  const samples = drawn.lines.flatMap((line) => along(line.cubics));
  const values = pastBars(drawn);
  for (const [index, value] of values.entries()) {
    const own = drawn.bars.find((bar) => bar.series === value.series && bar.category === value.category);
    if (!own) continue;
    const place = { ...value.ink, top: value.ink.top + value.lift, bottom: value.ink.bottom + value.lift };
    // `data-lift` is written to two decimals.
    expect(place.bottom - 3).toBeCloseTo(own.box.top - LABEL_OFFSET, 1);
    const crowded =
      values.slice(0, index).some((other) => touches(place, other.ink)) ||
      drawn.bars.some((bar) => bar !== own && touches(place, inset(bar.box)));
    const must = crowded || nearLine(place, samples, LINE_WIDTH / 2 + CLEARANCE - 0.25);
    const may = crowded || nearLine(place, samples, LINE_WIDTH / 2 + CLEARANCE + 3);
    if (must) expect(value.lift, `${value.text} of ${keyOf(value)} touches what stands in its place`).toBeGreaterThan(0);
    if (value.lift > 0) expect(may, `${value.text} of ${keyOf(value)} rose with nothing in its place`).toBe(true);
  }
}

/**
 * Every value past a bar upright at `size`, centred on its own bar, touching
 * no other value - a combo line's included - and no bar but its own. A combo
 * line's values sit on points: they keep clear of the other values only.
 */
function expectClearAndUpright(drawn: ReturnType<typeof draw>, size: number, roles: readonly string[] = ["value"]): void {
  const barSeries = new Set(drawn.bars.map((bar) => bar.series));
  const past = drawn.values.filter((value) => roles.includes(value.role ?? "") && (value.role !== "value" || barSeries.has(value.series)));
  for (const value of past) {
    expect(value.rotated, `${value.text} is turned on its side`).toBe(false);
    expect(value.size, `${value.text} is set at ${value.size}px`).toBe(size);
    if (value.role === "value") {
      const own = drawn.bars.find((bar) => bar.series === value.series && bar.category === value.category);
      expect(own, `${value.text} has no bar`).toBeDefined();
      if (own) expect(value.x).toBeCloseTo((own.box.left + own.box.right) / 2, 0);
    }
  }
  for (const value of drawn.values) {
    for (const other of drawn.values) {
      if (other !== value) expect(touches(value.ink, other.ink), `${value.text} touches ${other.text}`).toBe(false);
    }
  }
  for (const value of past) {
    for (const bar of drawn.bars) {
      const own = value.role === "value" ? bar.series === value.series && bar.category === value.category : bar.category === value.category;
      if (!own) expect(touches(value.ink, inset(bar.box)), `${value.text} touches a bar of ${bar.series}:${bar.category}`).toBe(false);
    }
  }
}

/**
 * A bar's box as Recharts writes it, four decimals to a coordinate: a value
 * planned to end exactly where the bar beside it begins reads as touching it
 * by a fraction of the last decimal.
 */
const inset = (box: Box): Box => ({ left: box.left + 0.001, right: box.right - 0.001, top: box.top + 0.001, bottom: box.bottom - 0.001 });

/**
 * A group of upright bars' values: every value past a bar upright at
 * `size`, centred on its own bar, touching no other value. A bar beside it is
 * not in its way - over a taller neighbour it is written on its fill, in its halo.
 */
function expectGroupedClear(drawn: ReturnType<typeof draw>, size: number): void {
  for (const value of pastBars(drawn)) {
    expect(value.rotated, `${value.text} is turned on its side`).toBe(false);
    expect(value.size, `${value.text} is set at ${value.size}px`).toBe(size);
    const own = drawn.bars.find((bar) => bar.series === value.series && bar.category === value.category);
    expect(own, `${value.text} has no bar`).toBeDefined();
    if (own) expect(value.x).toBeCloseTo((own.box.left + own.box.right) / 2, 0);
  }
  for (const value of drawn.values) {
    for (const other of drawn.values) {
      if (other !== value) expect(touches(value.ink, other.ink), `${value.text} touches ${other.text}`).toBe(false);
    }
  }
}

/**
 * A group of upright bars' values lifted only for a value: along the
 * axis, a value moves to the far row exactly when, in its place past its own
 * end - up past a rise, down past a fall - it would touch a value already
 * written in its place before it. A bar never lifts one.
 */
function expectLiftedForValuesOnly(drawn: ReturnType<typeof draw>, block: DeckChartBlock): void {
  const values = pastBars(drawn);
  const placeOf = (value: (typeof values)[number]): Box => {
    const way = (block.series[value.series].values[value.category] ?? 0) < 0 ? -1 : 1;
    return { ...value.ink, top: value.ink.top + way * value.lift, bottom: value.ink.bottom + way * value.lift };
  };
  for (const [index, value] of values.entries()) {
    const place = placeOf(value);
    const crowded = values.slice(0, index).some((other) => other.lift === 0 && touches(place, other.ink));
    expect(value.lift > 0, `${value.text} of ${keyOf(value)} ${crowded ? "touches a value in its place" : "rose with no value in its place"}`).toBe(crowded);
  }
}

/**
 * Every value of a horizontal chart set at `size`, upright: a
 * segment's figures inside its own segment, every other value touching no
 * bar but its own and no leader but its own, and no value touching another.
 */
function expectHorizontalClear(drawn: ReturnType<typeof draw>, size: number): void {
  for (const value of drawn.values) {
    expect(value.rotated, `${value.text} is turned on its side`).toBe(false);
    expect(value.size, `${value.role} ${value.text} of ${value.series}:${value.category} is set at ${value.size}px`).toBe(size);
  }
  for (const value of drawn.values) {
    for (const other of drawn.values) {
      if (other !== value) expect(touches(value.ink, other.ink), `${value.text} touches ${other.text}`).toBe(false);
    }
  }
  for (const value of drawn.values) {
    const ownBar = (bar: (typeof drawn.bars)[number]): boolean => bar.series === value.series && bar.category === value.category;
    if (value.role === "segment") {
      // A segment's value wears no halo: its figures stay on its own segment.
      const own = drawn.bars.find(ownBar);
      expect(own, `${value.text} has no segment`).toBeDefined();
      if (!own) continue;
      const figures = { left: value.ink.left + 3, right: value.ink.right - 3, top: value.ink.top + 3, bottom: value.ink.bottom - 3 };
      expect(figures.left, `${value.text} starts before its segment`).toBeGreaterThanOrEqual(own.box.left);
      expect(figures.right, `${value.text} runs past its segment`).toBeLessThanOrEqual(own.box.right);
      expect(figures.top).toBeGreaterThanOrEqual(own.box.top);
      expect(figures.bottom).toBeLessThanOrEqual(own.box.bottom);
      continue;
    }
    for (const bar of drawn.bars) {
      if (value.role === "value" && ownBar(bar)) continue;
      expect(touches(value.ink, inset(bar.box)), `${value.text} touches a bar of ${bar.series}:${bar.category}`).toBe(false);
    }
    for (const leg of drawn.leaders) {
      if (value.role === "annotation" && leg.series === value.series && leg.category === value.category) continue;
      expect(touches(value.ink, inset(leg.box)), `${value.text} touches the leader of ${leg.series}:${leg.category}`).toBe(false);
    }
  }
}

describe("the reported combo chart: two bar series over seven quarters in one of two columns, at md", () => {
  test.each(HEIGHTS)("auto, today's rule, sets the values under the size asked at %ipx high", (height) => {
    const { values } = plan(REPORTED, COLUMN, height);
    expect(values.fit).toBe("auto");
    expect(values.outer?.size).toBeLessThan(22);
  });

  test.each(HEIGHTS)("stagger writes every value upright at 22px, clear of the values, the bars and the line, at %ipx high", (height) => {
    const layout = plan({ ...REPORTED, value_label_fit: "stagger" }, COLUMN, height);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, fit: "stagger", asked: 21, written: 21, reason: null, turned: null });
    const drawn = draw({ ...REPORTED, value_label_fit: "stagger" }, COLUMN, height);
    expect(drawn.facts).toMatchObject({ valuesFit: "stagger", valuesAsked: "21", valuesWritten: "21", labelSize: "22" });
    expect(drawn.facts.labelRotate).toBeUndefined();
    expect(drawn.facts.valuesReason).toBeUndefined();
    expectClearAndUpright(drawn, 22);
    expectClearOfLines(drawn);
    expectLiftedOnDemand(drawn);
    expect(pastBars(drawn)).toHaveLength(14);
    // The tallest bar's value, €7.8, needs no lift: the plot gives up no more
    // height than it does for values kept on one row.
    expect(layout.values.lifts?.get(pastBarKey("value", 1, 1, 5))).toBe(0);
    expect(layout.plot.top).toBe(plan({ ...REPORTED, value_label_fit: "upright" }, COLUMN, height).plot.top);
  });

  test.each(HEIGHTS)("with the line hidden, only a value beside one at its height rises, one row, at %ipx high", (height) => {
    const block: DeckChartBlock = { ...REPORTED, value_label_fit: "stagger" };
    const line = new Set([2]);
    const drawn = draw(block, COLUMN, height, line);
    expectClearAndUpright(drawn, 22);
    expectLiftedOnDemand(drawn);
    // €2.6 beside €2.6, €3.7 beside €3.6 and €4.6 beside €4.4: the second of
    // each pair moves. €2.5 beside €4.2, €6.3 beside €4.3, €7.8 beside €5.2
    // and €5.9 beside €4.3 stand clear in their place.
    const lifted = pastBars(drawn).filter((value) => value.lift > 0);
    expect(lifted.map((value) => `${keyOf(value)} ${value.text}`)).toEqual(["1:0 €2.6", "1:1 €3.7", "1:4 €4.6"]);
    for (const value of lifted) expect(value.lift).toBe(ROW_MD);
    expect(drawn.html.match(/data-lift=/g)).toHaveLength(3);
    expect(plan(block, COLUMN, height, line).values).toMatchObject({ asked: 14, written: 14, reason: null });
  });

  test("the line the values keep clear of is the one drawn: its monotone curve through its points", () => {
    const drawn = draw({ ...REPORTED, value_label_fit: "stagger" }, COLUMN, 420);
    expect(drawn.lines).toHaveLength(1);
    const [{ cubics, dots }] = drawn.lines;
    expect(dots).toHaveLength(7);
    const planned = monotoneCurve(dots);
    expect(planned).toHaveLength(cubics.length);
    planned.forEach((cubic, index) =>
      cubic.forEach(([x, y], corner) => {
        expect(x).toBeCloseTo(cubics[index][corner][0], 6);
        expect(y).toBeCloseTo(cubics[index][corner][1], 6);
      }),
    );
  });

  test("a value past a bar the line runs through rises past it, centred on its bar", () => {
    // In its place, €4.3 of Q4 24 sits on the line's climb to its peak.
    const drawn = draw({ ...REPORTED, value_label_fit: "stagger" }, COLUMN, 420);
    const samples = drawn.lines.flatMap((line) => along(line.cubics));
    const value = pastBars(drawn).find((entry) => keyOf(entry) === "0:3");
    const own = drawn.bars.find((bar) => bar.series === 0 && bar.category === 3);
    expect(value).toBeDefined();
    expect(own).toBeDefined();
    if (!value || !own) return;
    const place = { ...value.ink, top: value.ink.top + value.lift, bottom: value.ink.bottom + value.lift };
    expect(nearLine(place, samples, LINE_WIDTH / 2)).toBe(true);
    expect(value.lift).toBeGreaterThan(ROW_MD);
    expect(nearLine(value.ink, samples, LINE_WIDTH / 2 + CLEARANCE - 0.25)).toBe(false);
    expect(value.x).toBeCloseTo((own.box.left + own.box.right) / 2, 0);
  });

  test("the bars give way: they narrow and part their quarters so no value touches the next quarter's", () => {
    const layout = plan({ ...REPORTED, value_label_fit: "stagger" }, COLUMN, 420);
    const before = plan(REPORTED, COLUMN, 420);
    expect(layout.gaveWay).not.toBeNull();
    expect(layout.gaveWay?.width).toBeLessThan(before.barSize);
    expect(layout.gaveWay?.width).toBeGreaterThanOrEqual(Math.ceil(before.barSize * 0.4));
    expect(before.gaveWay).toBeNull();
    const drawn = draw({ ...REPORTED, value_label_fit: "stagger" }, COLUMN, 420);
    for (const bar of drawn.bars) expect(bar.width).toBe(layout.gaveWay?.width);
    expect(Number(drawn.facts.barWidth)).toBe(layout.gaveWay?.width);
    expect(Number(drawn.facts.barGap)).toBeCloseTo(layout.gaveWay?.gap ?? 0, 2);
    expect(Number(drawn.facts.categoryGap)).toBeCloseTo(layout.gaveWay?.categoryGap ?? 0, 2);
  });

  test.each(HEIGHTS)("upright keeps 22px, a value one row out at most, and counts the values that still touch another or the line, at %ipx high", (height) => {
    const layout = plan({ ...REPORTED, value_label_fit: "upright" }, COLUMN, height);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, fit: "upright", asked: 21, reason: "upright" });
    expect(layout.values.written).toBeLessThan(21);
    expect(layout.values.need).toBeGreaterThan(layout.values.room);
    const drawn = draw({ ...REPORTED, value_label_fit: "upright" }, COLUMN, height);
    expect(drawn.facts).toMatchObject({ valuesFit: "upright", valuesReason: "upright", valuesWritten: String(layout.values.written) });
    expectClearAndUpright(drawn, 22);
    expectClearOfLines(drawn);
    // Upright writes in their place exactly the values stagger writes there;
    // of those stagger lifts, it writes the ones that are clear one
    // row out, and leaves out the ones the line or another value holds there.
    const staggered = pastBars(draw({ ...REPORTED, value_label_fit: "stagger" }, COLUMN, height));
    const kept = pastBars(drawn);
    expect(kept.filter((value) => value.lift === 0).map(keyOf)).toEqual(staggered.filter((value) => value.lift === 0).map(keyOf));
    const lifted = kept.filter((value) => value.lift > 0);
    const staggeredLifted = new Set(staggered.filter((value) => value.lift > 0).map(keyOf));
    expect(lifted.length).toBeGreaterThan(0);
    for (const value of lifted) {
      expect(staggeredLifted.has(keyOf(value)), `${value.text} of ${keyOf(value)} rose where stagger keeps it in place`).toBe(true);
      expect(value.lift).toBe(ROW_MD);
    }
  });

  test.each(HEIGHTS)("with the line hidden, upright lifts a value beside one at its height one row, as stagger does, at %ipx high", (height) => {
    const block: DeckChartBlock = { ...REPORTED, value_label_fit: "upright" };
    const line = new Set([2]);
    expect(plan(block, COLUMN, height, line).values).toMatchObject({ asked: 14, written: 14, reason: null });
    const drawn = draw(block, COLUMN, height, line);
    expectClearAndUpright(drawn, 22);
    expectLiftedOnDemand(drawn);
    const lifted = pastBars(drawn).filter((value) => value.lift > 0);
    expect(lifted.map((value) => `${keyOf(value)} ${value.text}`)).toEqual(["1:0 €2.6", "1:1 €3.7", "1:4 €4.6"]);
    for (const value of lifted) expect(value.lift).toBe(ROW_MD);
    const staggered = draw({ ...block, value_label_fit: "stagger" }, COLUMN, height, line);
    expect(drawn.html.replace('data-values-fit="upright"', "")).toBe(staggered.html.replace('data-values-fit="stagger"', ""));
  });
});

describe("auto is today's rule", () => {
  const SHAPES: [string, DeckChartBlock, number, number][] = [
    ["the reported combo", REPORTED, COLUMN, 420],
    ["two series over eight quarters", ARR, COLUMN, 420],
    ["three bars a quarter", SOURCES, COLUMN, 300],
    ["three bars a quarter across zero in a span 2 panel", PIPELINE, ...SPAN_2],
    ["a stack in a one-third panel", STACK, ONE_THIRD, 420],
    ["a margin crossing zero", MARGIN, ONE_THIRD, 300],
    ["a waterfall", catalog.waterfall_chart as DeckChartBlock, COLUMN, 420],
    ["a two-unit combo", catalog.dual_axis_chart as DeckChartBlock, COLUMN, 420],
    // On their side: today's sizes - the thin stack's segments at 16px,
    // the grouped bars' values hidden or set smaller - whatever the word.
    ["a horizontal stack with thin segments", SHARES_THIN, COLUMN, 300],
    ["a horizontal stack", SHARES_EVEN, COLUMN, 460],
    ["four horizontal bars a region", SHARES_GROUPED, COLUMN, 300],
    ["four level horizontal bars a region", SHARES_LEVEL, COLUMN, 460],
  ];

  test.each(SHAPES)("%s draws the same figure with the field unset and set to auto", (_name, block, width, height) => {
    const unset = draw(block, width, height);
    const auto = draw({ ...block, value_label_fit: "auto" }, width, height);
    expect(auto.html).toBe(unset.html);
    expect(unset.facts.valuesFit).toBeUndefined();
    expect(unset.facts.barWidth).toBeUndefined();
    expect(unset.html).not.toContain("data-lift");
    expect(plan({ ...block, value_label_fit: "auto" }, width, height)).toEqual(plan(block, width, height));
  });
});

describe("where the word changes nothing", () => {
  test.each([
    // New logos by quarter: eight bars whose 22px values fit their bands.
    ["eight upright bars", chart([{ name: "New logos", values: [13, 10, 15, 11, 17, 14, 20, 26] }], QUARTERS, { format: "number", unit: undefined }), COLUMN],
    // Four rows on their side, each value past its bar in its own row.
    ["four horizontal bars", chart([{ name: "Hours", values: [12, 26, 31, 48] }], ["P1", "P2", "P3", "P4"], { horizontal: true, format: "number", unit: "h" }), ONE_THIRD],
    // A horizontal stack whose every segment holds its 22px value.
    ["a horizontal stack", SHARES_EVEN, COLUMN],
  ])("%s whose values fit at the size asked are written as auto writes them", (_name, block, width) => {
    const auto = draw(block, width, 420);
    expect(plan(block, width, 420).values.outer).toEqual({ size: 22, rotate: false });
    for (const fit of ["stagger", "upright"] as const) {
      expect(plan({ ...block, value_label_fit: fit }, width, 420).values.fit).toBe("auto");
      expect(draw({ ...block, value_label_fit: fit }, width, 420).html).toBe(auto.html);
    }
  });

  test.each([
    ["a line", chart([{ name: "ASP", values: [246_800, 277_800, 305_400, 342_600, 379_600, 429_200, 475_000, 536_400] }], QUARTERS, { kind: "line" })],
    ["an area", chart([{ name: "NRR", values: [1.21, 1.22, 1.21, 1.23, 1.22, 1.21, 1.22, 1.23].map((v) => v * M) }], QUARTERS, { kind: "area" })],
    ["a waterfall", catalog.waterfall_chart as DeckChartBlock],
  ])("%s is drawn as auto draws it", (_name, block) => {
    const auto = draw(block, ONE_THIRD, 420);
    for (const fit of ["stagger", "upright"] as const) {
      expect(draw({ ...block, value_label_fit: fit }, ONE_THIRD, 420).html).toBe(auto.html);
    }
  });
});

describe("the other shapes of bars", () => {
  test("a lone series wider than its bands alternates its values along the axis", () => {
    const layout = plan({ ...BOOKINGS, value_label_fit: "stagger" }, ONE_THIRD, 420);
    // Auto turns them on their side: no size of the scale fits them upright.
    expect(plan(BOOKINGS, ONE_THIRD, 420).values.outer?.rotate).toBe(true);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, written: 8, reason: null });
    const drawn = draw({ ...BOOKINGS, value_label_fit: "stagger" }, ONE_THIRD, 420);
    expectClearAndUpright(drawn, 22);
    // Each touches the one before it in its place, so every other one waits for the far row.
    expectLiftedOnDemand(drawn);
    expect(drawn.values.map((value) => value.lift > 0)).toEqual([false, true, false, true, false, true, false, true]);
  });

  test("a stack's totals alternate, its segments written inside as before", () => {
    const layout = plan({ ...STACK, value_label_fit: "stagger" }, ONE_THIRD, 420);
    expect(plan(STACK, ONE_THIRD, 420).values.outer?.size).toBeLessThan(22);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, fit: "stagger" });
    const drawn = draw({ ...STACK, value_label_fit: "stagger" }, ONE_THIRD, 420);
    const totals = drawn.values.filter((value) => value.role === "total");
    expect(totals).toHaveLength(8);
    expect(totals.map((total) => total.lift > 0)).toEqual([false, true, false, true, false, true, false, true]);
    expectClearAndUpright(drawn, 22, ["total"]);
  });

  test("three bars a quarter write every value upright at the size asked, touching no other value", () => {
    const block: DeckChartBlock = { ...SOURCES, value_label_fit: "stagger" };
    const layout = plan(block, COLUMN, 420);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, asked: 24, written: 24, reason: null });
    expect(layout.gaveWay).toBeNull();
    const drawn = draw(block, COLUMN, 420);
    expectGroupedClear(drawn, 22);
    expectLiftedForValuesOnly(drawn, block);
    // Each value stands at its own height, clear of the others, over the
    // taller bar beside it where it is wider than its own: none rises, and
    // `upright` leaves none out - the bars beside a value are not in its way.
    expect(drawn.html).not.toContain("data-lift");
    const upright: DeckChartBlock = { ...SOURCES, value_label_fit: "upright" };
    expect(plan(upright, COLUMN, 420).values).toMatchObject({ outer: { size: 22, rotate: false }, written: 24, reason: null });
    const kept = draw(upright, COLUMN, 420);
    expectGroupedClear(kept, 22);
    expect(pastBars(kept).map(keyOf)).toEqual(pastBars(drawn).map(keyOf));
  });

  test("bars running both ways keep each value past its own end, touching nothing", () => {
    for (const fit of ["stagger", "upright"] as const) {
      const block = { ...MARGIN, value_label_fit: fit };
      const layout = plan(block, ONE_THIRD, 300);
      expect(layout.values.outer).toEqual({ size: 22, rotate: false });
      expect(layout.values.written).toBe(fit === "stagger" ? 8 : layout.values.written);
      expectClearAndUpright(draw(block, ONE_THIRD, 300), 22);
    }
  });

  test("a value with no room above the plot is counted, never set smaller", () => {
    // A 198px dashboard panel of span 1: the far row has no room left above.
    const layout = plan({ ...BOOKINGS, value_label_fit: "stagger" }, ONE_THIRD, 198);
    expect(layout.values.outer).toEqual({ size: 22, rotate: false });
    if (layout.values.written < layout.values.asked) expect(layout.values.reason).toBe("short");
    expectClearAndUpright(draw({ ...BOOKINGS, value_label_fit: "stagger" }, ONE_THIRD, 198), 22);
  });

  test("ARR over eight quarters: the net new ARR, far under the ARR, needs no second row", () => {
    // Every value already clears the others at its own height upright at the
    // size asked, the net new ARR's over the ARR bar beside it:
    // `stagger` writes 16 at 22px and lifts none of them, the plot
    // giving up nothing for a far row.
    const block: DeckChartBlock = { ...ARR, value_label_fit: "stagger" };
    const layout = plan(block, COLUMN, 420);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, written: 16, reason: null });
    expect([...(layout.values.lifts?.values() ?? [])]).toEqual(Array(16).fill(0));
    const upright = plan({ ...ARR, value_label_fit: "upright" }, COLUMN, 420);
    expect(upright.values).toMatchObject({ written: 16, reason: null });
    expect(layout.plot).toEqual(upright.plot);
    const drawn = draw(block, COLUMN, 420);
    expectGroupedClear(drawn, 22);
    expectLiftedForValuesOnly(drawn, block);
    expect(drawn.html).not.toContain("data-lift");
  });
});

describe("a group of upright bars keeps its group under a fit", () => {
  const FITS = ["stagger", "upright"] as const;
  /** The room between two bars of a quarter, and between two quarters, read off the drawn boxes. */
  const gapsOf = (drawn: ReturnType<typeof draw>) => {
    const quarters = PIPELINE.categories.map((_, category) =>
      drawn.bars.filter((bar) => bar.category === category).sort((a, b) => a.box.left - b.box.left),
    );
    const inner = quarters.flatMap((bars) => bars.slice(1).map((bar, index) => bar.box.left - bars[index].box.right));
    const between = quarters.slice(1).map((bars, index) => bars[0].box.left - quarters[index][quarters[index].length - 1].box.right);
    return { inner, between };
  };

  test.each(FITS)("%s draws the bars where auto draws them: a tight group a quarter, the quarters apart", (fit) => {
    const block: DeckChartBlock = { ...PIPELINE, value_label_fit: fit };
    const layout = plan(block, ...SPAN_2);
    expect(layout.values.fit).toBe(fit);
    expect(layout.gaveWay).toBeNull();
    const auto = draw(PIPELINE, ...SPAN_2);
    const drawn = draw(block, ...SPAN_2);
    const across = (bars: typeof drawn.bars) => bars.map(({ series, category, box }) => [series, category, box.left, box.right]);
    expect(across(drawn.bars)).toEqual(across(auto.bars));
    const { inner, between } = gapsOf(drawn);
    expect(inner).toHaveLength(16);
    for (const gap of inner) expect(gap).toBeLessThanOrEqual(6.01);
    for (const gap of between) expect(gap).toBeGreaterThanOrEqual(2 * Math.max(...inner));
    // The facts say how they stand, so the PPTX writer spaces PowerPoint's bars alike.
    expect(Number(drawn.facts.barWidth)).toBeCloseTo(drawn.bars[0].width, 2);
    expect(Number(drawn.facts.barGap)).toBeCloseTo(inner[0], 1);
    expect(Number(drawn.facts.categoryGap)).toBeCloseTo(between[0], 1);
    expect(auto.facts.barWidth).toBeUndefined();
  });

  test("stagger writes every value upright at 16px, a value lifted only where it would touch another", () => {
    const block: DeckChartBlock = { ...PIPELINE, value_label_fit: "stagger" };
    const layout = plan(block, ...SPAN_2);
    expect(layout.values).toMatchObject({ outer: { size: 16, rotate: false }, fit: "stagger", asked: 24, written: 24, reason: null, turned: null });
    const drawn = draw(block, ...SPAN_2);
    expect(drawn.facts).toMatchObject({ valuesFit: "stagger", valuesAsked: "24", valuesWritten: "24", labelSize: "16" });
    expect(pastBars(drawn)).toHaveLength(24);
    expectGroupedClear(drawn, 16);
    expectLiftedForValuesOnly(drawn, block);
    expect(pastBars(drawn).some((value) => value.lift > 0)).toBe(true);
    // An expansion wider than its bar stands over the taller new logos beside it, on its fill.
    const overBars = pastBars(drawn).filter((value) =>
      drawn.bars.some((bar) => (bar.series !== value.series || bar.category !== value.category) && touches(value.ink, inset(bar.box))),
    );
    expect(overBars.length).toBeGreaterThan(0);
    // A fall's value stays under its bar.
    for (const value of pastBars(drawn).filter((entry) => entry.series === 2)) {
      const own = drawn.bars.find((bar) => bar.series === 2 && bar.category === value.category);
      if (own) expect(value.ink.top).toBeGreaterThanOrEqual(own.box.bottom);
    }
  });

  test("upright lifts the one value that would touch another a row past it, as stagger does, and leaves none out", () => {
    const block: DeckChartBlock = { ...PIPELINE, value_label_fit: "upright" };
    const staggered = pastBars(draw({ ...PIPELINE, value_label_fit: "stagger" }, ...SPAN_2));
    const layout = plan(block, ...SPAN_2);
    expect(layout.values).toMatchObject({ outer: { size: 16, rotate: false }, fit: "upright", asked: 24, written: 24, reason: null });
    const drawn = draw(block, ...SPAN_2);
    expect(drawn.facts).toMatchObject({ valuesFit: "upright", valuesWritten: "24" });
    expect(drawn.facts.valuesReason).toBeUndefined();
    expectGroupedClear(drawn, 16);
    expectLiftedForValuesOnly(drawn, block);
    expect(pastBars(drawn).filter((value) => value.lift > 0)).toHaveLength(1);
    expect(pastBars(drawn).map((value) => [keyOf(value), value.lift])).toEqual(staggered.map((value) => [keyOf(value), value.lift]));
  });
});

describe("upright lifts a value one row before it leaves it out", () => {
  const r2 = (value: number): number => Math.round(value * 100) / 100;
  /** The figure as a reviewer reads it: each bar's box, then each value's size, place and lift. */
  const figure = (drawn: ReturnType<typeof draw>): string[] => [
    ...drawn.bars.map(
      ({ series, category, box }) => `bar s${series} c${category} @${r2(box.left)},${r2(box.top)} ${r2(box.right - box.left)}x${r2(box.bottom - box.top)}`,
    ),
    ...drawn.values.map(
      (value) => `${value.role} s${value.series} c${value.category} ${value.size}px lift ${value.lift} @${r2(value.x)},${r2(value.y)} ${value.text}`,
    ),
  ];

  test("a combo in a half-width panel writes all 16 values, its bars where they stood", () => {
    const layout = plan(SEASONAL, ...HALF_PANEL);
    expect(layout.values).toMatchObject({ outer: { size: 16, rotate: false }, fit: "upright", asked: 16, written: 16, reason: null, turned: null });
    const drawn = draw(SEASONAL, ...HALF_PANEL);
    expect(drawn.facts).toMatchObject({ valuesFit: "upright", valuesAsked: "16", valuesWritten: "16", labelSize: "16" });
    expect(drawn.facts.valuesReason).toBeUndefined();
    expect(pastBars(drawn).map((value) => `${keyOf(value)} ${value.text}`).sort()).toEqual(
      SEASONAL.series
        .slice(0, 2)
        .flatMap((entry, series) => entry.values.map((value, category) => `${series}:${category} €${(value / M).toFixed(1)}M`))
        .sort(),
    );
    expectClearAndUpright(drawn, 16);
    expectClearOfLines(drawn);
    expectLiftedOnDemand(drawn);
    // In four quarters the expansion stands just under the new business: it
    // rises one row past it, exactly where stagger writes it - the plot gives
    // up no height for it and no bar moves.
    expect(pastBars(drawn).filter((value) => value.lift > 0).map(keyOf)).toEqual(["1:1", "1:3", "1:5", "1:7"]);
    const staggered = draw({ ...SEASONAL, value_label_fit: "stagger" }, ...HALF_PANEL);
    expect(drawn.html.replace('data-values-fit="upright"', "")).toBe(staggered.html.replace('data-values-fit="stagger"', ""));
    expect(figure(drawn)).toMatchSnapshot();
  });
});

describe("two horizontal share stacks in one column of a content slide, at md", () => {
  test.each(HEIGHTS)("auto, today's rule, sets the thin stack's segments under the size asked at %ipx high", (height) => {
    const thin = plan(SHARES_THIN, COLUMN, height).values;
    expect(thin.fit).toBe("auto");
    expect(thin.inner?.size).toBeLessThan(22);
    expect(thin.annotation?.size).toBeLessThan(22);
    // The stack beside it, with no thin segment, keeps 22px: one slide, two sizes.
    expect(plan(SHARES_EVEN, COLUMN, height).values.inner?.size).toBe(22);
  });

  test.each(HEIGHTS)("stagger writes every value of both charts at 22px, none shrunk, nothing touching, at %ipx high", (height) => {
    for (const block of [SHARES_THIN, SHARES_EVEN]) {
      const fitted = { ...block, value_label_fit: "stagger" as const };
      const { values } = plan(fitted, COLUMN, height);
      expect(values).toMatchObject({ outer: { size: 22, rotate: false }, inner: { size: 22, rotate: false }, asked: 20, written: 20, reason: null });
      const drawn = draw(fitted, COLUMN, height);
      expect(drawn.values).toHaveLength(20);
      expectHorizontalClear(drawn, 22);
      expect(drawn.facts).toMatchObject({ valuesAsked: "20", valuesWritten: "20", labelSize: "22", segmentSize: "22" });
      expect(drawn.facts.valuesReason).toBeUndefined();
      if (block === SHARES_THIN) {
        // Its 5% and 3% segments are written past their stacks, after the
        // total, at the size asked, with their leaders - the export reads it.
        expect(values.fit).toBe("stagger");
        expect(values.annotation).toEqual({ size: 22, rotate: false });
        expect(drawn.facts).toMatchObject({ valuesFit: "stagger", annotationSize: "22", valuesAnnotated: "2" });
        const past = drawn.values.filter((value) => value.role === "annotation");
        expect(past.map((value) => value.text).sort()).toEqual(["3%", "5%"]);
        for (const value of past) {
          const total = drawn.values.find((other) => other.role === "total" && other.category === value.category);
          expect(total, `the total of ${value.category}`).toBeDefined();
          if (total) expect(value.ink.left).toBeGreaterThanOrEqual(total.ink.right);
          expect(drawn.leaders.some((leg) => leg.series === value.series && leg.category === value.category)).toBe(true);
        }
      } else {
        // Every value already fits as asked: drawn as auto draws it.
        expect(values.fit).toBe("auto");
        expect(drawn.html).toBe(draw(block, COLUMN, height).html);
      }
    }
  });

  test.each(HEIGHTS)("upright keeps 22px and leaves out, counted, a segment too short for its value, at %ipx high", (height) => {
    const fitted = { ...SHARES_THIN, value_label_fit: "upright" as const };
    const { values } = plan(fitted, COLUMN, height);
    expect(values).toMatchObject({ fit: "upright", outer: { size: 22, rotate: false }, inner: { size: 22, rotate: false }, annotation: null });
    expect(values).toMatchObject({ asked: 20, written: 18, reason: "segments" });
    expect(values.need).toBeGreaterThan(values.room);
    const drawn = draw(fitted, COLUMN, height);
    expect(drawn.values).toHaveLength(18);
    expectHorizontalClear(drawn, 22);
    expect(drawn.leaders).toHaveLength(0);
    expect(drawn.facts).toMatchObject({ valuesFit: "upright", valuesReason: "segments", valuesWritten: "18", segmentSize: "22" });
    expect(drawn.facts.annotationSize).toBeUndefined();
    const left = SEVERITIES.flatMap((_, series) =>
      REGIONS.flatMap((__, category) =>
        drawn.values.some((value) => value.role === "segment" && value.series === series && value.category === category) ? [] : [`${series}:${category}`],
      ),
    );
    // The Critical 5% of North America and 3% of LATAM.
    expect(left).toEqual(["0:1", "0:3"]);
  });
});

describe("grouped bars on their side", () => {
  test("auto, today's rule, hides every value of four bars a region at 300px high", () => {
    expect(plan(SHARES_GROUPED, COLUMN, 300).values).toMatchObject({ fit: "auto", outer: null, written: 0, reason: "narrow" });
  });

  test.each(HEIGHTS)("stagger writes every value at 22px, a value in the way of another moved along its row, at %ipx high", (height) => {
    for (const block of [SHARES_GROUPED, SHARES_LEVEL]) {
      const fitted = { ...block, value_label_fit: "stagger" as const };
      const layout = plan(fitted, COLUMN, height);
      expect(layout.values).toMatchObject({ fit: "stagger", outer: { size: 22, rotate: false }, asked: 16, written: 16, reason: null });
      const drawn = draw(fitted, COLUMN, height);
      expect(drawn.values).toHaveLength(16);
      expectHorizontalClear(drawn, 22);
      expect(drawn.facts).toMatchObject({ valuesFit: "stagger", valuesWritten: "16", labelSize: "22" });
      // Each value starts just past its bar's end, further along its row by
      // its lift where a value written before stood in its way.
      for (const value of drawn.values) {
        const own = drawn.bars.find((bar) => bar.series === value.series && bar.category === value.category);
        expect(own, `${value.text} has no bar`).toBeDefined();
        if (own) {
          expect(value.x).toBeCloseTo(own.box.right + LABEL_OFFSET + value.lift, 0);
          expect(value.y).toBeCloseTo((own.box.top + own.box.bottom) / 2, 0);
        }
      }
      if (block === SHARES_LEVEL) expect(drawn.values.some((value) => value.lift > 0)).toBe(true);
      if (layout.gaveWay) {
        // Recharts draws the bars as thick as the bars that gave way.
        for (const bar of drawn.bars) expect(bar.box.bottom - bar.box.top).toBeCloseTo(layout.gaveWay.width, 0);
        expect(Number(drawn.facts.barWidth)).toBe(layout.gaveWay.width);
      }
    }
  });

  test("the bars give way: thinner rows part so a value clears the bars beside it", () => {
    const layout = plan({ ...SHARES_GROUPED, value_label_fit: "stagger" }, COLUMN, 300);
    const before = plan(SHARES_GROUPED, COLUMN, 300);
    expect(before.gaveWay).toBeNull();
    expect(layout.gaveWay).not.toBeNull();
    expect(layout.gaveWay?.width).toBeLessThan(before.barSize);
    expect(layout.gaveWay?.width).toBeGreaterThanOrEqual(Math.ceil(before.barSize * 0.4));
  });

  test.each(HEIGHTS)("upright keeps 22px in place and counts the values that would touch another, at %ipx high", (height) => {
    const fitted = { ...SHARES_LEVEL, value_label_fit: "upright" as const };
    const layout = plan(fitted, COLUMN, height);
    expect(layout.values).toMatchObject({ fit: "upright", outer: { size: 22, rotate: false }, asked: 16, reason: "upright" });
    expect(layout.values.written).toBeLessThan(16);
    expect(layout.values.written).toBeGreaterThanOrEqual(8);
    // The room is the pitch of two rows, the need a 22px value's height with its halo.
    expect(layout.values.need).toBe(Math.ceil(22 * 0.74 + 6));
    expect(layout.values.need).toBeGreaterThan(layout.values.room);
    const drawn = draw(fitted, COLUMN, height);
    expect(drawn.facts).toMatchObject({ valuesFit: "upright", valuesReason: "upright", valuesWritten: String(layout.values.written) });
    expectHorizontalClear(drawn, 22);
    for (const value of drawn.values) {
      expect(value.lift).toBe(0);
      const own = drawn.bars.find((bar) => bar.series === value.series && bar.category === value.category);
      if (own) expect(value.x).toBeCloseTo(own.box.right + LABEL_OFFSET, 0);
    }
  });

  test("a value with no room left of the canvas' right edge has the plot give up its width, never a smaller size", () => {
    // One series of eight quarters in a one-third panel: every value stands
    // beside its neighbours' and the long bars leave little room past them.
    const spend = chart([{ name: "Spend", values: [12.4, 12.9, 13.1, 12.7, 13.4, 13.0, 13.8, 14.2].map((v) => v * M) }], QUARTERS, {
      horizontal: true,
    });
    const fitted = { ...spend, value_label_fit: "stagger" as const };
    const fittedPlan = plan(fitted, ONE_THIRD, 198);
    expect(fittedPlan.values.outer).toEqual({ size: 22, rotate: false });
    expect(fittedPlan.plot.right).toBeLessThanOrEqual(plan(spend, ONE_THIRD, 198).plot.right);
    if (fittedPlan.values.written < fittedPlan.values.asked) expect(fittedPlan.values.reason).toBe("short");
    const drawn = draw(fitted, ONE_THIRD, 198);
    expectHorizontalClear(drawn, 22);
    for (const value of drawn.values) expect(value.ink.right - 3 + 22 * 0.36).toBeLessThanOrEqual(ONE_THIRD - 2 + 0.5);
  });
});
