/**
 * A chart's values never sit on each other or on a tick, and a value on a
 * point stays next to its point - `yarn test:dom`.
 *
 * Pinned here, each on the real Recharts figure drawn in happy-dom: a value
 * on a point (a combo's line, a line chart) keeps clear of every other value
 * and the value axes' ticks - a value set too near another's is moved just
 * clear of it, a first value that would run into the axis is slid back into
 * the plot - and stays next to its dot whatever bar it falls on, its halo
 * keeping it legible on the bar's fill; a value that touches nothing stays
 * where it always was. The
 * optional `y_min` / `y_max` (and `secondary_` pair) pin an axis' range, and
 * a series' own `show_values` leaves its values to the tooltip.
 *
 * The figures are made up; their shapes are a board deck's: bars up to 83% of
 * their axis with a line at 78% of its own, two bars a quarter with a ratio
 * dipping between them, a line whose first point stands just under a tick.
 */
import { describe, expect, test } from "vitest";
import { render } from "@testing-library/react";

import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";
import { chartExportPayload, dualAxisScales, valueAxisScale } from "./chart-format";
import { axisChartLayout } from "./chart-layout";

const M = 1_000_000;
const QUARTERS = ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8"];

/** Revenue bars and a growth line on the right-hand axis; the last point falls inside the last bar. */
const INSIDE: DeckChartBlock = {
  type: "chart",
  kind: "combo",
  format: "currency",
  unit: "EUR",
  decimals: 1,
  secondary_format: "currency",
  secondary_unit: "EUR",
  secondary_decimals: 1,
  categories: QUARTERS,
  series: [
    { name: "Revenue", kind: "bar", values: [10, 13, 16, 20, 24, 27, 30, 33].map((v) => v * M) },
    { name: "Growth", kind: "line", axis: "secondary", values: [2.2, 2.4, 3.0, 3.6, 3.7, 3.9, 4.6, 3.9].map((v) => v * M) },
  ],
};

/** Two bars a quarter and a ratio on the right-hand axis that dips between them. */
const BETWEEN: DeckChartBlock = {
  type: "chart",
  kind: "combo",
  format: "currency",
  unit: "EUR",
  decimals: 1,
  secondary_format: "number",
  secondary_unit: "x",
  secondary_decimals: 2,
  secondary_target: 1,
  categories: QUARTERS.slice(0, 7),
  series: [
    { name: "Bookings", kind: "bar", values: [2.0, 3.0, 3.6, 3.7, 3.8, 4.6, 3.7].map((v) => v * M) },
    { name: "Spend", kind: "bar", tone: "warning", values: [2.0, 3.1, 1.9, 5.7, 4.0, 7.0, 5.3].map((v) => v * M) },
    { name: "Ratio", kind: "line", axis: "secondary", values: [1.12, 1.15, 0.64, 1.66, 1.17, 1.64, 1.55] },
  ],
};

/** A line whose first point stands just under the 40M tick of its axis. */
const UNDER_A_TICK: DeckChartBlock = {
  type: "chart",
  kind: "line",
  format: "currency",
  unit: "EUR",
  decimals: 1,
  categories: QUARTERS.slice(0, 7),
  series: [{ name: "Cash", values: [36.5, 33.0, 78.0, 75.5, 71.0, 64.0, 59.0].map((v) => v * M) }],
};

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface Value {
  line: string;
  series: number;
  category: number;
  placement: string | undefined;
  x: number;
  y: number;
  /** Its glyphs (0.6 em each, semibold) and figures' height (0.74 em), with the 6px halo. */
  ink: Box;
}

const touches = (a: Box, b: Box): boolean => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

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

/** The chart as Recharts drew it: its values, its bars, its value-axis ticks, its line dots and its facts. */
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
  const values: Value[] = Array.from(container.querySelectorAll<SVGTextElement>("text.deck-chart-value")).map((text) => {
    const size = Number.parseFloat(text.style.fontSize);
    const x = Math.round(Number(text.getAttribute("x")));
    const y = Math.round(Number(text.getAttribute("y")));
    const { series, category, placement } = text.dataset;
    return {
      line: `s${series} c${category} ${size}px${placement ? ` ${placement}` : ""} @${x},${y} ${text.textContent}`,
      series: Number(series),
      category: Number(category),
      placement,
      x: Number(text.getAttribute("x")),
      y: Number(text.getAttribute("y")),
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
    const h = Number(path.getAttribute("height"));
    return { series, category, box: { left: x, right: x + Number(path.getAttribute("width")), top: Math.min(y, y + h), bottom: Math.max(y, y + h) } };
  });
  const ticks = Array.from(container.querySelectorAll<SVGTextElement>(".deck-chart-tick-primary, .deck-chart-tick-secondary")).map(
    (text) => {
      const x = Number(text.getAttribute("x"));
      const y = Number(text.getAttribute("y"));
      const size = Number(text.getAttribute("font-size"));
      const length = (text.textContent ?? "").length * size * 0.58;
      const [left, right] = text.getAttribute("text-anchor") === "end" ? [x - length, x] : [x, x + length];
      return { text: text.textContent ?? "", box: { left, right, top: y - size * 0.37, bottom: y + size * 0.37 } };
    },
  );
  const dots = Array.from(container.querySelectorAll<SVGCircleElement>(".recharts-line-dots circle")).map((dot) => ({
    x: Number(dot.getAttribute("cx")),
    y: Number(dot.getAttribute("cy")),
  }));
  const facts = container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset ?? {};
  return { container, values, bars, ticks, dots, facts };
}

/** Every value clear of every other value and of every tick - a bar is no obstacle, the halo is for it. */
function expectNothingTouches(drawn: ReturnType<typeof draw>): void {
  const { values, ticks } = drawn;
  for (const value of values) {
    for (const other of values) {
      if (other !== value) expect(touches(value.ink, other.ink), `${value.line} touches ${other.line}`).toBe(false);
    }
    for (const tick of ticks) expect(touches(value.ink, tick.box), `${value.line} touches the tick ${tick.text}`).toBe(false);
  }
}

/**
 * Every value of the line `series` next to its own dot: across its dot, and
 * no further from it than `POINT_LABEL_OFFSET` (10) plus one label height.
 * `dots` are the line's, in category order.
 */
function expectNextToItsPoint(drawn: ReturnType<typeof draw>, series: number, dots = drawn.dots): void {
  for (const [category, dot] of dots.entries()) {
    const value = of(drawn.values, series, category);
    expect(value, `s${series} c${category} is written`).toBeDefined();
    if (!value) continue;
    const size = Number.parseFloat(value.line.split(" ")[2]);
    const away = Math.max(0, value.ink.top + 3 - dot.y, dot.y - (value.ink.bottom - 3));
    expect(away, `${value.line} is ${away.toFixed(1)}px from its dot`).toBeLessThanOrEqual(10 + size);
    expect(value.ink.left <= dot.x && dot.x <= value.ink.right, `${value.line} runs across its dot`).toBe(true);
  }
}

const of = (values: readonly Value[], series: number, category: number) =>
  values.find((value) => value.series === series && value.category === category);

describe("a line point inside a bar", () => {
  test("writes its value next to its point, on the bar with its halo, clear of the bar's own - the figure as drawn", () => {
    const drawn = draw(INSIDE, 900, 420);
    // The case holds: the last point is inside the last bar, a bar narrower than the value.
    const last = drawn.bars.find((bar) => bar.series === 0 && bar.category === 7);
    expect(drawn.dots[7].y).toBeGreaterThan(last?.box.top ?? Infinity);
    const line = of(drawn.values, 1, 7);
    expect(line && last && line.ink.right - line.ink.left > last.box.right - last.box.left).toBe(true);
    // The bar's value holds the room over the point: the line's goes under it, on the bar.
    expect(line?.placement).toBe("below");
    expectNextToItsPoint(drawn, 1);
    expectNothingTouches(drawn);
    expect(drawn.facts).toMatchObject({ valuesAsked: "16", valuesWritten: "16" });
    expect(drawn.facts.valuesReason).toBeUndefined();
    expect(drawn.values.map((value) => value.line)).toMatchSnapshot();
  });

  test("moves that one value only: the others, and the plot, are where they were", () => {
    const drawn = draw(INSIDE, 900, 420);
    expect(drawn.values.filter((value) => value.placement).map((value) => value.line.split(" ").slice(0, 2).join(" "))).toEqual(["s1 c7"]);
    // Over its point, `POINT_LABEL_OFFSET` above the dot.
    for (const [category, dot] of drawn.dots.entries()) {
      if (category === 7) continue;
      expect(of(drawn.values, 1, category)?.y).toBeCloseTo(dot.y - 10, 0);
    }
    expect(axisChartLayout(INSIDE, 900, 420, new Set(), "en").margin.top).toBe(16);
  });

  test("a line well inside the bars keeps every value over its point", () => {
    // Each value sits on its bar's fill, its halo keeping it legible, and reads
    // next to its point - nothing moves.
    const inside: DeckChartBlock = {
      type: "chart",
      kind: "combo",
      format: "currency",
      unit: "EUR",
      categories: ["Q1", "Q2", "Q3", "Q4"],
      series: [
        { name: "Pipeline", kind: "bar", values: [300_000, 400_000, 460_000, 520_000] },
        { name: "Closed", kind: "line", values: [150_000, 200_000, 250_000, 300_000] },
      ],
    };
    const drawn = draw(inside, 900, 400);
    expect(drawn.values.some((value) => value.placement)).toBe(false);
    for (const [category, dot] of drawn.dots.entries()) {
      const value = of(drawn.values, 1, category);
      const bar = drawn.bars.find((entry) => entry.series === 0 && entry.category === category);
      expect(value?.y).toBeCloseTo(dot.y - 10, 0);
      expect(value && bar && value.ink.left >= bar.box.left && value.ink.right <= bar.box.right).toBe(true);
    }
  });

  test("a pinned range parts the line from the bars on purpose, and its value goes back over its point", () => {
    const parted: DeckChartBlock = { ...INSIDE, y_max: 60 * M };
    const drawn = draw(parted, 900, 420);
    expect(drawn.facts).toMatchObject({ min: "0", max: String(60 * M) });
    const last = drawn.bars.find((bar) => bar.series === 0 && bar.category === 7);
    expect(drawn.dots[7].y).toBeLessThan(last?.box.top ?? -Infinity);
    expect(drawn.values.some((value) => value.placement)).toBe(false);
    expectNothingTouches(drawn);
  });
});

describe("a ratio line between two bars a quarter", () => {
  test("writes every ratio next to its point, clear of the bars' values, upright at the chart's size", () => {
    const drawn = draw(BETWEEN, 960, 440);
    expectNothingTouches(drawn);
    // The dip between the two bars of its quarter stays at its point, on the bars.
    expectNextToItsPoint(drawn, 2);
    expect(drawn.facts).toMatchObject({ valuesAsked: "21", valuesWritten: "21" });
    expect(drawn.facts.labelRotate).toBeUndefined();
  });

  test("keeps the bars' values where they were", () => {
    const drawn = draw(BETWEEN, 960, 440);
    expect(drawn.values.filter((value) => value.series < 2 && value.placement)).toEqual([]);
    for (const bar of drawn.bars) expect(of(drawn.values, bar.series, bar.category)?.y).toBeCloseTo(bar.box.top - 6, 0);
  });
});

describe("a line chart's values against its axis and each other", () => {
  test("slides a first value that would run into a tick back into the plot, and moves no other", () => {
    const drawn = draw(UNDER_A_TICK, 920, 430);
    const first = of(drawn.values, 0, 0);
    expect(first?.placement).toBe("shifted");
    expect(first && first.x).toBeGreaterThan(drawn.dots[0].x);
    expectNothingTouches(drawn);
    for (const [category, dot] of drawn.dots.entries()) {
      expect(of(drawn.values, 0, category)?.y).toBeCloseTo(dot.y - 10, 0);
      if (category > 0) expect(of(drawn.values, 0, category)?.placement).toBeUndefined();
    }
    expect(drawn.facts).toMatchObject({ valuesAsked: "7", valuesWritten: "7" });
  });

  test("writes two lines' values apart where the lines meet", () => {
    const meeting: DeckChartBlock = {
      type: "chart",
      kind: "line",
      categories: ["Q1", "Q2", "Q3", "Q4"],
      series: [
        { name: "Plan", values: [70, 50, 60, 40] },
        { name: "Actual", values: [68, 30, 61, 45] },
      ],
    };
    const drawn = draw(meeting, 1200, 460);
    expectNothingTouches(drawn);
    expect(drawn.values.filter((value) => value.series === 0 && value.placement)).toEqual([]);
    expect(drawn.values.filter((value) => value.series === 1 && value.placement).length).toBeGreaterThan(0);
    expect(drawn.facts).toMatchObject({ valuesAsked: "8", valuesWritten: "8" });
  });
});

describe("a series' own show_values", () => {
  test("leaves the bars' values to the tooltip and still writes the line's clear of the bars", () => {
    const quiet: DeckChartBlock = { ...INSIDE, series: [{ ...INSIDE.series[0], show_values: false }, INSIDE.series[1]] };
    const drawn = draw(quiet, 900, 420);
    expect(drawn.values.every((value) => value.series === 1)).toBe(true);
    expect(drawn.values).toHaveLength(8);
    expectNothingTouches(drawn);
    expectNextToItsPoint(drawn, 1);
    expect(drawn.facts).toMatchObject({ values: "false", valuesAsked: "8", valuesWritten: "8", pointSize: "22" });
    const payload = JSON.parse(chartExportPayload(quiet, "en", "Unexplained"));
    expect(payload.series.map((entry: { show_values?: boolean }) => entry.show_values)).toEqual([false, undefined]);
  });

  test("writes one series of a chart whose own show_values is off", () => {
    const one: DeckChartBlock = {
      type: "chart",
      kind: "bar",
      show_values: false,
      categories: ["Q1", "Q2", "Q3"],
      series: [
        { name: "A", values: [10, 20, 30] },
        { name: "B", values: [12, 18, 31], show_values: true },
      ],
    };
    const drawn = draw(one, 900, 420);
    expect(drawn.values.map((value) => value.series)).toEqual([1, 1, 1]);
    expectNothingTouches(drawn);
    expect(drawn.facts).toMatchObject({ values: "true", valuesAsked: "3", valuesWritten: "3" });
  });

  test("a chart with every series following it publishes nothing new", () => {
    const payload = JSON.parse(chartExportPayload(INSIDE, "en", "Unexplained"));
    expect(payload.series.some((entry: object) => "show_values" in entry)).toBe(false);
    const off = JSON.parse(chartExportPayload({ ...INSIDE, show_values: false }, "en", "Unexplained"));
    expect(off.series.some((entry: object) => "show_values" in entry)).toBe(false);
  });
});

describe("a pinned value axis", () => {
  const BARS: DeckChartBlock = {
    type: "chart",
    kind: "bar",
    format: "currency",
    categories: ["Q1", "Q2"],
    series: [{ name: "A", values: [12 * M, 33 * M] }],
  };

  test("is the chart's own until an end is set", () => {
    expect(valueAxisScale(BARS, 5)).toEqual({ min: 0, max: 40 * M, step: 10 * M, ticks: [0, 10, 20, 30, 40].map((v) => v * M) });
  });

  test("runs from zero to the pinned top in round steps, the count nearest the chart's", () => {
    expect(valueAxisScale({ ...BARS, y_max: 60 * M }, 5).ticks).toEqual([0, 20, 40, 60].map((v) => v * M));
    expect(valueAxisScale({ ...BARS, y_max: 50 * M }, 5).ticks).toEqual([0, 10, 20, 30, 40, 50].map((v) => v * M));
    // No round step cuts 0-45M: four equal ones, the ends kept.
    expect(valueAxisScale({ ...BARS, y_max: 45 * M }, 5)).toMatchObject({ min: 0, max: 45 * M, step: 11.25 * M });
  });

  test("starts at a pinned floor and climbs in round steps past the values", () => {
    expect(valueAxisScale({ ...BARS, y_min: -10 * M }, 5)).toEqual({
      min: -10 * M,
      max: 50 * M,
      step: 20 * M,
      ticks: [-10, 10, 30, 50].map((v) => v * M),
    });
  });

  test("keeps one grid on two axes, the pinned one held to its ends", () => {
    const lifted: DeckChartBlock = { ...INSIDE, secondary_y_min: -5 * M };
    const { primary, secondary } = dualAxisScales(lifted, 5);
    expect(secondary.min).toBe(-5 * M);
    expect(secondary.ticks).toHaveLength(primary.ticks.length);
    expect(secondary.max).toBeGreaterThanOrEqual(4.6 * M);
    expect(dualAxisScales(INSIDE, 5)).toEqual(dualAxisScales({ ...INSIDE, y_min: undefined, secondary_y_max: undefined }, 5));
    const drawn = draw(lifted, 900, 420);
    expect(drawn.facts).toMatchObject({ secondaryMin: String(-5 * M), secondaryMax: String(secondary.max) });
    expectNothingTouches(drawn);
  });
});

describe("a value raised exactly to its clearance", () => {
  // Lines over bars whose values are raised far over each other: a raise lands
  // a value at its clearance, and rounding left a sliver of overlap smaller
  // than the precision of the rise, which then never moved - the layout looped
  // forever and froze the tab that drew the chart.
  const SLIVERS: [DeckChartBlock, number, number][] = [
    [
      {
        type: "chart",
        kind: "combo",
        format: "currency",
        categories: ["Q1", "Q2", "Q3", "Q4", "Q5"],
        series: [
          { name: "Bars", kind: "bar", values: [85.4, 3169.9, 1.1, 1.1, 0.3] },
          { name: "First line", kind: "line", values: [3702.1, 0.2, 4, 39.1, 0.1] },
          { name: "Second line", kind: "line", values: [435.9, 1.3, 131.3, 2092.2, 0] },
        ],
        show_values: true,
      },
      420,
      160,
    ],
    [
      {
        type: "chart",
        kind: "combo",
        format: "currency",
        categories: ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
        series: [
          { name: "Bars", kind: "bar", values: [5.5, 461.2, 0.3, 2.3, 3587.2, 293.7, 2.7] },
          { name: "First line", kind: "line", values: [9.4, 0.5, 0.2, 174.5, 0.3, 3.7, 0.1] },
          { name: "Second line", kind: "line", values: [244.2, 1.3, 1.9, 2840.4, 0.3, 46, 0] },
        ],
        show_values: true,
      },
      560,
      220,
    ],
  ];

  test.each(SLIVERS)("is laid out and drawn, every value it writes clear of the others (%#)", (block, width, height) => {
    expect(axisChartLayout(block, width, height, new Set(), "en").values).toBeDefined();
    const drawn = draw(block, width, height);
    expect(drawn.values.length).toBeGreaterThan(0);
    expectNothingTouches(drawn);
  });
});
