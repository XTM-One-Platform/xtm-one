/**
 * How an axis chart writes its values - `yarn test:dom`.
 *
 * Pinned here: a board deck's grouped bars read like the dashboard card they
 * replace - a small upright value just above each bar - in one of two equal
 * columns of a 16:9 slide (836px on the 1920px canvas). Every size of the
 * chart's text scale (`label_size`) is tried upright before a value is turned
 * on its side, and a value only has to clear the other values and the other
 * bars: the ARR of a quarter stands far above the net new ARR beside it, so
 * the two never touch however near their bars are. Values that still have to
 * be turned say why (`turned`), for the layout check.
 *
 * A stack's segment too thin for its value writes it past the stack, after
 * the total, in its series' colour with a leader: a dashboard panel's
 * stacked chart with a 1-3% series writes every value, nothing touching.
 *
 * vitest rather than `node --test`: `chart-layout.ts` imports its siblings
 * without an extension, as the app does, and the rendered half of each case
 * draws the real Recharts figure in happy-dom.
 */
import { describe, expect, test } from "vitest";
import { render } from "@testing-library/react";

import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";
import { seriesColors } from "./chart-format";
import { axisChartLayout, CHART_TEXT_SCALES, LABEL_OFFSET, LEADER_WIDTH, labelRun, labelWidth } from "./chart-layout";

/** One column of a two-column content slide on the 1920px canvas. */
const COLUMN = 836;
/** The heights a chart takes there: `sm`, a `fill` chart under a heading, `md`. */
const HEIGHTS = [300, 420, 460] as const;

const M = 1_000_000;
const QUARTERS = ["Q1 24", "Q2 24", "Q3 24", "Q4 24", "Q1 25", "Q2 25", "Q3 25", "Q4 25"];

const chart = (series: DeckChartBlock["series"], categories: string[], extra: Partial<DeckChartBlock> = {}): DeckChartBlock => ({
  type: "chart",
  kind: "bar",
  format: "currency",
  unit: "EUR",
  series,
  categories,
  ...extra,
});

/** Slide 2 of the reported deck: ARR and net new ARR over eight quarters, 16 bars. */
const ARR = chart(
  [
    { name: "ARR", values: [42.8, 46.2, 49.8, 52.4, 56.0, 59.2, 63.1, 66.4].map((v) => v * M) },
    { name: "Net new ARR", values: [4.2, 3.4, 3.6, 2.6, 3.6, 3.2, 3.8, 7.6].map((v) => v * M) },
  ],
  QUARTERS,
);
/** Slide 2 too: new logos by quarter, 8 bars. */
const NEW_LOGOS = chart([{ name: "New logos", values: [13, 10, 15, 11, 17, 14, 20, 26] }], QUARTERS, {
  format: "number",
  unit: undefined,
});
/** Slide 3: net new ARR against burn over seven quarters, 14 bars - a burn as tall as the ARR it buys. */
const BURN = chart(
  [
    { name: "Net new ARR", values: [3.6, 3.2, 3.8, 7.6, 4.8, 5.8, 6.8].map((v) => v * M) },
    { name: "Burn", values: [5.2, 4.8, 5.8, 4.4, 5.0, 4.2, 3.8].map((v) => v * M) },
  ],
  QUARTERS.slice(1),
);
/** Three bars a quarter: each bar is narrower than its value, set upright at any size. */
const SOURCES = chart(
  [
    { name: "New logos", values: [2.4, 1.8, 2.2, 1.4, 2.0, 1.6, 2.2, 4.4].map((v) => v * M) },
    { name: "Expansion", values: [1.4, 1.2, 1.0, 0.8, 1.2, 1.2, 1.2, 2.2].map((v) => v * M) },
    { name: "Partners", values: [0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 1.0].map((v) => v * M) },
  ],
  QUARTERS,
);

const plan = (block: DeckChartBlock, width: number, height: number) =>
  axisChartLayout(block, width, height, new Set(), "en");

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface DrawnValue {
  text: string;
  /** What the value writes: a bar's own, its stack's total, or a segment's. */
  role: string | undefined;
  series: number;
  category: number;
  x: number;
  y: number;
  size: number;
  rotated: boolean;
  /** Where a value on a point was moved (`data-placement`): `below`, `lifted`, `shifted`; unset where it was not. */
  placement: string | undefined;
  /** The ink of the value with its 6px halo, as the stylesheet paints it. */
  ink: Box;
  /** The colour a value past its stack is written in (`--deck-annotation`); empty elsewhere. */
  colour: string;
}

/** One leg of a leader from a thin segment to its value past the stack. */
interface DrawnLeg {
  series: number;
  category: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  colour: string | null;
}

interface DrawnBar {
  series: number;
  category: number;
  box: Box;
}

/** A value-axis tick as Recharts wrote it, and a line's dot as it drew it. */
interface DrawnTick {
  text: string;
  box: Box;
}
interface Dot {
  x: number;
  y: number;
}

/** The ink of a value: its glyphs (0.6 em each, set semibold) and the height of its figures (0.74 em). */
function inkOf(text: SVGTextElement, size: number): Box {
  const x = Number(text.getAttribute("x"));
  const y = Number(text.getAttribute("y"));
  const length = (text.textContent ?? "").length * size * 0.6;
  const height = size * 0.74;
  const anchor = text.getAttribute("text-anchor");
  const baseline = text.getAttribute("dominant-baseline");
  const halo = 3;
  if (text.dataset.rotate === "true") {
    const [top, bottom] = anchor === "start" ? [y - length, y] : [y, y + length];
    return { left: x - height / 2 - halo, right: x + height / 2 + halo, top: top - halo, bottom: bottom + halo };
  }
  const [left, right] = anchor === "middle" ? [x - length / 2, x + length / 2] : anchor === "start" ? [x, x + length] : [x - length, x];
  const [top, bottom] =
    baseline === "hanging" ? [y, y + height] : baseline === "central" ? [y - height / 2, y + height / 2] : [y - height, y];
  return { left: left - halo, right: right + halo, top: top - halo, bottom: bottom + halo };
}

const touches = (a: Box, b: Box): boolean => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

/** The chart drawn at `width` x `height`: its values, its bars, its value-axis ticks, its line dots and its leaders, as Recharts placed them. */
function draw(
  block: DeckChartBlock,
  width: number,
  height: number,
): { values: DrawnValue[]; bars: DrawnBar[]; ticks: DrawnTick[]; dots: Dot[]; leaders: DrawnLeg[]; facts: DOMStringMap } {
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
      placement: text.dataset.placement,
      ink: inkOf(text, size),
      colour: text.style.getPropertyValue("--deck-annotation"),
    };
  });
  const leaders = Array.from(container.querySelectorAll<SVGLineElement>("line.deck-chart-leader")).map((line) => ({
    series: Number(line.dataset.series),
    category: Number(line.dataset.category),
    x1: Number(line.getAttribute("x1")),
    y1: Number(line.getAttribute("y1")),
    x2: Number(line.getAttribute("x2")),
    y2: Number(line.getAttribute("y2")),
    colour: line.getAttribute("stroke"),
  }));
  // One rectangle per non-zero value, series after series in category order.
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
  // The value axes' ticks (0.58 em a glyph, their figures 0.74 em high, centred on their level).
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
  return { values, bars, ticks, dots, leaders, facts };
}

/** Every value upright, centred just above its own bar, touching no other value and no other bar. */
function expectReadLikeADashboard(block: DeckChartBlock, width: number, height: number): DrawnValue[] {
  const { values, bars, facts } = draw(block, width, height);
  const count = block.series.reduce((total, entry) => total + entry.values.filter((value) => value !== 0).length, 0);
  expect(values).toHaveLength(count);
  expect(facts).toMatchObject({ valuesAsked: String(count), valuesWritten: String(count) });
  expect(facts.labelRotate).toBeUndefined();
  for (const value of values) {
    expect(value.rotated, `${value.text} is turned on its side`).toBe(false);
    const own = bars.find((bar) => bar.series === value.series && bar.category === value.category);
    expect(own, `${value.text} has no bar`).toBeDefined();
    if (!own) continue;
    expect(value.x).toBeCloseTo((own.box.left + own.box.right) / 2, 0);
    expect(value.y).toBeCloseTo(own.box.top - LABEL_OFFSET, 0);
    for (const other of values) {
      if (other !== value) expect(touches(value.ink, other.ink), `${value.text} touches ${other.text}`).toBe(false);
    }
    for (const bar of bars) {
      if (bar !== own) expect(touches(value.ink, bar.box), `${value.text} touches a bar`).toBe(false);
    }
  }
  return values;
}

describe("two series over eight quarters in one of two columns", () => {
  test.each(HEIGHTS)("are written upright at %ipx high, down to the smallest size before any is turned", (height) => {
    const { values, tickSize, labelOffset } = plan(ARR, COLUMN, height);
    expect(tickSize).toBe(22);
    expect(labelOffset).toBe(LABEL_OFFSET);
    expect(values.outer).toEqual({ size: 12, rotate: false });
    expect(values).toMatchObject({ asked: 16, written: 16, reason: null, turned: null });
  });

  test.each(HEIGHTS)("each sits just above its own bar and touches nothing else, at %ipx high", (height) => {
    const values = expectReadLikeADashboard(ARR, COLUMN, height);
    expect(values.map((value) => value.text)).toContain("€66.4M");
    expect(values.map((value) => value.text)).toContain("€7.6M");
  });

  test("two values at the same height are not set upright when they would overprint each other", () => {
    // The same 16 bars with the second series as tall as the first: two
    // `€42.8M` side by side need more than the 37px between their bars.
    const twins = chart(
      [ARR.series[0], { name: "Plan", values: ARR.series[0].values.map((value) => value + 0.1 * M) }],
      QUARTERS,
    );
    const { values } = plan(twins, COLUMN, 420);
    expect(values.outer?.rotate).toBe(true);
    expect(values.written).toBe(16);
    expect(values.turned).toEqual({ size: 12, room: 37, need: 50 });
  });

  test("a full-width row writes them larger", () => {
    const { values } = plan(ARR, 1720, 420);
    expect(values.outer?.rotate).toBe(false);
    expect(values.outer?.size).toBeGreaterThan(12);
    expectReadLikeADashboard(ARR, 1720, 420);
  });
});

describe("the other charts of the reported deck", () => {
  test.each(HEIGHTS)("new logos by quarter keep the tick size upright at %ipx high", (height) => {
    expect(plan(NEW_LOGOS, COLUMN, height).values.outer).toEqual({ size: 22, rotate: false });
    expectReadLikeADashboard(NEW_LOGOS, COLUMN, height);
  });

  test.each(HEIGHTS)("net new ARR against burn, 14 bars, is written upright at %ipx high", (height) => {
    expect(plan(BURN, COLUMN, height).values.outer?.rotate).toBe(false);
    expectReadLikeADashboard(BURN, COLUMN, height);
  });

  test("three bars a quarter are turned on their side and say what they lacked upright", () => {
    const { values } = plan(SOURCES, COLUMN, 420);
    expect(values.outer?.rotate).toBe(true);
    expect(values.written).toBe(24);
    expect(values.turned).toEqual({ size: 12, room: 25, need: 42 });
    const { facts } = draw(SOURCES, COLUMN, 420);
    expect(facts).toMatchObject({ labelRotate: "true", uprightSize: "12", uprightRoom: "25", uprightNeed: "42" });
  });
});

describe("label_size", () => {
  test("each size sets the ticks and the largest value, and is mirrored by the stylesheet", () => {
    expect(Object.fromEntries(Object.entries(CHART_TEXT_SCALES).map(([key, scale]) => [key, scale.tick]))).toEqual({
      sm: 18,
      md: 22,
      lg: 26,
    });
    for (const scale of Object.values(CHART_TEXT_SCALES)) {
      expect(scale.values[0]).toBe(scale.tick);
      expect([...scale.values].sort((a, b) => b - a)).toEqual([...scale.values]);
      expect(scale.values).toContain(scale.segmentFloor);
    }
    const md = CHART_TEXT_SCALES.md.values;
    expect(md[md.length - 1]).toBe(12);
  });

  test("sm sets the values and the ticks one step down, as on a dashboard card", () => {
    const md = plan(ARR, COLUMN, 420);
    const sm = plan({ ...ARR, label_size: "sm" }, COLUMN, 420);
    expect(sm.tickSize).toBe(18);
    expect(sm.readingAxisWidth).toBeLessThan(md.readingAxisWidth);
    expect(sm.values.outer?.rotate).toBe(false);
    expect(sm.values.outer?.size).toBeLessThanOrEqual(12);
    expectReadLikeADashboard({ ...NEW_LOGOS, label_size: "sm" }, COLUMN, 420).forEach((value) => {
      expect(value.size).toBe(18);
    });
  });

  test("lg reads from the back of the room", () => {
    const lg = plan({ ...NEW_LOGOS, label_size: "lg" }, COLUMN, 420);
    expect(lg.tickSize).toBe(26);
    expect(lg.values.outer).toEqual({ size: 26, rotate: false });
  });

  test("the drawing sets its ticks in the chart's size", () => {
    const { container } = render(
      <ChartCanvas
        block={{ ...ARR, label_size: "sm" }}
        width={COLUMN}
        height={420}
        hidden={new Set()}
        animate={false}
        locale="en"
        onSettled={() => {}}
        gapLabel="Unexplained"
        runningLabel="Running total"
      />,
    );
    const ticks = Array.from(container.querySelectorAll(".deck-chart-tick-category, .deck-chart-tick-primary"));
    expect(ticks.length).toBeGreaterThan(0);
    for (const tick of ticks) expect(tick.getAttribute("font-size")).toBe("18");
  });
});

describe("totals past the end of a horizontal stack at the top of its axis", () => {
  /** The reported chart: five verticals' share of each region, every stack a whole. */
  const SHARE: DeckChartBlock = {
    type: "chart",
    kind: "stacked_bar",
    horizontal: true,
    format: "percent",
    show_values: true,
    series: [
      { name: "Finance", values: [40, 35, 30, 25] },
      { name: "Public", values: [25, 25, 25, 25] },
      { name: "Energy", values: [15, 20, 20, 20] },
      { name: "Health", values: [12, 12, 15, 18] },
      { name: "Retail", values: [8, 8, 10, 12] },
    ],
    categories: ["EMEA", "Americas", "APAC", "LATAM"],
  };
  /**
   * `100%` set semibold in IBM Plex Sans measures 2.76 em in Chromium - three
   * digits of 0.6 em and a `%` of 0.96 - where the glyph-count estimate reads
   * 2.4 em: the run every total takes on screen, which the frame used to cut.
   */
  const PERCENT_TOTAL_EM = 2.76;
  /** Where the chart was reported cut: a content slide's fill, a dashboard panel of span 3 and one of span 1. */
  const ROOMS = [
    ["a content slide", 1670, 650],
    ["a span 3 dashboard panel", 1670, 235],
    ["a span 1 dashboard panel", 508, 198],
  ] as const;
  /** The plot's right margin before any value claims room past the bars (`PLOT_MARGIN_RIGHT`). */
  const MARGIN_RIGHT = 40;

  test("the run a value keeps toward the canvas edge is its estimate and one wide glyph's excess", () => {
    expect(labelRun("100%", 22)).toBeCloseTo(PERCENT_TOTAL_EM * 22, 1);
    for (const size of [18, 22, 26]) {
      expect(labelRun("100%", size) - labelWidth("100%", size)).toBeCloseTo(0.36 * size, 6);
    }
  });

  test.each(ROOMS)("in %s every total stands whole inside the canvas, at every label size", (_where, width, height) => {
    for (const label_size of ["sm", "md", "lg"] as const) {
      const block = { ...SHARE, label_size };
      const tick = CHART_TEXT_SCALES[label_size].tick;
      const { values, margin } = plan(block, width, height);
      // The totals are written at the tick size; the room is given up by the
      // plot, never bought by dropping or shrinking them. (A span 1 panel's
      // thinnest segments still hold no value inside, as before: `segments`.)
      expect(values.outer, label_size).toEqual({ size: tick, rotate: false });
      expect(["narrow", "short"], label_size).not.toContain(values.reason);
      // The right margin holds a total's offset and its whole run past a bar at the axis top.
      expect(margin.right, label_size).toBeGreaterThanOrEqual(LABEL_OFFSET + PERCENT_TOTAL_EM * tick);
      const { values: drawn, bars } = draw(block, width, height);
      const totals = drawn.filter((value) => value.role === "total");
      expect(totals, label_size).toHaveLength(4);
      for (const total of totals) {
        expect(total.text).toBe("100%");
        expect(total.rotated).toBe(false);
        expect(total.size).toBe(tick);
        // Just past the end of its stack, which reaches the plot's right edge...
        const end = Math.max(...bars.filter((bar) => bar.category === total.category).map((bar) => bar.box.right));
        expect(total.x).toBeCloseTo(end + LABEL_OFFSET, 0);
        // ...with its whole run, as the browser sets it, inside the canvas.
        expect(total.x + PERCENT_TOTAL_EM * total.size, `${label_size} ${_where}`).toBeLessThanOrEqual(width);
      }
    }
  });

  test("a horizontal chart whose bars stop short of the axis top claims no room past the margin", () => {
    // The e2e fixture: hours to first response on an axis topping out at 60.
    const hours = chart([{ name: "Time to first response", values: [12, 26, 31, 48] }], ["P1", "P2", "P3", "P4"], {
      horizontal: true,
      format: "number",
      unit: "h",
      target: 40,
    });
    const { margin, values } = plan(hours, 1670, 650);
    expect(margin.right).toBe(MARGIN_RIGHT);
    expect(values.outer).toEqual({ size: 22, rotate: false });
    // And one that writes no values keeps its margin too.
    expect(plan({ ...SHARE, show_values: false }, 1670, 650).margin.right).toBe(MARGIN_RIGHT);
  });

  test("an upright stack keeps the room it had: a label's height above its bar, not its run", () => {
    // 100% at the top of the axis: the label's size (22) past its offset (6),
    // less the 14px the top margin already holds past the edge pad.
    const upright = plan({ ...SHARE, horizontal: false }, COLUMN, 420);
    expect(upright.margin.top).toBe(30);
    expect(upright.margin.right).toBe(MARGIN_RIGHT);
    expect(upright.values.outer).toEqual({ size: 22, rotate: false });
  });
});

describe("a line's values alternate over and under their points when one row has no room", () => {
  /** A one-third dashboard panel's canvas on the 1920px canvas (span 1), and a full-width one's (span 3). */
  const ONE_THIRD = 508;
  const FULL_WIDTH = 1670;
  /** The heights a panel's canvas takes on a one-row board, the shortest a dashboard gives a line its values at. */
  const PANEL_HEIGHTS = [300, 420, 520] as const;
  /** The acceptance chart: one series over eight quarters, 240k to 540k, in currency (`€246.8k`). */
  const ASP = chart([{ name: "ASP", values: [246_800, 277_800, 305_400, 342_600, 379_600, 429_200, 475_000, 536_400] }], QUARTERS, {
    kind: "line",
  });
  /** A line as flat as a retention rate: its neighbours never stand in each other's way. */
  const FLAT = chart([{ name: "NRR", values: [1.21, 1.22, 1.21, 1.23, 1.22, 1.21, 1.22, 1.23].map((v) => v * M) }], QUARTERS, {
    kind: "line",
  });
  /** The distance between two neighbouring points on the plot the layout drew. */
  const pitchOf = (layout: ReturnType<typeof plan>, categories: number): number =>
    (layout.plot.right - layout.plot.left - 2 * layout.pointPadding) / (categories - 1);

  /** Every value clear of every other value and of every tick, and next to its own dot (across it, within its offset and height). */
  function expectWrittenNextToTheirPoints(drawn: ReturnType<typeof draw>, count: number): void {
    const { values, ticks, dots, facts } = drawn;
    expect(values).toHaveLength(count);
    expect(facts).toMatchObject({ valuesAsked: String(count), valuesWritten: String(count) });
    expect(facts.valuesReason).toBeUndefined();
    for (const value of values) {
      for (const other of values) {
        if (other !== value) expect(touches(value.ink, other.ink), `${value.text} touches ${other.text}`).toBe(false);
      }
      for (const tick of ticks) expect(touches(value.ink, tick.box), `${value.text} touches the tick ${tick.text}`).toBe(false);
      const dot = dots[value.category];
      const away = Math.max(0, value.ink.top + 3 - dot.y, dot.y - (value.ink.bottom - 3));
      expect(away, `${value.text} is ${away.toFixed(1)}px from its dot`).toBeLessThanOrEqual(10 + value.size);
      expect(value.ink.left <= dot.x && dot.x <= value.ink.right, `${value.text} runs across its dot`).toBe(true);
    }
  }

  test.each(PANEL_HEIGHTS)("writes all eight values of a one-third panel alternating, at the largest size two points' room takes, at %ipx high", (height) => {
    const layout = plan(ASP, ONE_THIRD, height);
    // One row has no room for them, not even at the smallest size (every
    // value was hidden, `narrow`); two points' room takes them, at the
    // largest size it does - the one the scale reaches first.
    const pitch = pitchOf(layout, 8);
    expect(labelWidth("€246.8k", 12) + 12).toBeGreaterThan(pitch);
    const size = CHART_TEXT_SCALES.md.values.find((candidate) => labelWidth("€246.8k", candidate) + 12 <= 2 * pitch);
    expect(size).toBeGreaterThanOrEqual(18);
    expect(layout.values).toMatchObject({ outer: { size, rotate: false }, alternate: true, asked: 8, written: 8, reason: null });
    const drawn = draw(ASP, ONE_THIRD, height);
    expectWrittenNextToTheirPoints(drawn, 8);
    expect(drawn.facts.valuesAlternate).toBe("true");
    for (const value of drawn.values) expect(value.size).toBe(size);
  });

  test("the same chart at full width is one row over the points at the largest size, as before", () => {
    const layout = plan(ASP, FULL_WIDTH, 520);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, alternate: false, written: 8, reason: null });
    for (const place of layout.values.placed.values()) expect(place).toEqual({ under: false, rise: 0, shift: 0 });
    const drawn = draw(ASP, FULL_WIDTH, 520);
    expectWrittenNextToTheirPoints(drawn, 8);
    expect(drawn.facts.valuesAlternate).toBeUndefined();
    for (const value of drawn.values) {
      expect(value.placement).toBeUndefined();
      expect(value.y).toBeCloseTo(drawn.dots[value.category].y - 10, 0);
    }
  });

  test("the even points' values sit over them and the odd points' under them", () => {
    const layout = plan(FLAT, ONE_THIRD, 420);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, alternate: true, written: 8, reason: null });
    const drawn = draw(FLAT, ONE_THIRD, 420);
    expectWrittenNextToTheirPoints(drawn, 8);
    for (const value of drawn.values) {
      const dot = drawn.dots[value.category];
      if (value.category % 2 === 1) {
        expect(value.placement, value.text).toBe("below");
        expect(value.y).toBeCloseTo(dot.y + 10, 0);
      } else {
        expect(value.placement, value.text).toBeUndefined();
        expect(value.y).toBeCloseTo(dot.y - 10, 0);
      }
    }
    // An area's values sit on points too, and alternate the same way.
    expect(plan({ ...FLAT, kind: "area" }, ONE_THIRD, 420).values).toMatchObject({ outer: { size: 22, rotate: false }, alternate: true, written: 8 });
  });

  test("alternating at a size comes before one row at the next size down", () => {
    // In one of two columns, one `€241.1k` is too wide for a row at 22 or 20
    // and fits one at 18: the values alternate at 22 instead.
    const wide = chart([{ name: "ASP", values: [241_100, 276_000, 310_000, 342_000, 380_000, 424_000, 476_000, 540_000] }], QUARTERS, {
      kind: "line",
    });
    const layout = plan(wide, COLUMN, 420);
    const pitch = pitchOf(layout, 8);
    expect(labelWidth("€241.1k", 20) + 12).toBeGreaterThan(pitch);
    expect(labelWidth("€241.1k", 18) + 12).toBeLessThanOrEqual(pitch);
    expect(layout.values).toMatchObject({ outer: { size: 22, rotate: false }, alternate: true, written: 8, reason: null });
    const drawn = draw(wide, COLUMN, 420);
    expectWrittenNextToTheirPoints(drawn, 8);
    expect(drawn.values.filter((value) => value.placement === "below").map((value) => value.category)).toEqual([1, 3, 5, 7]);
  });

  test("still counts the values hidden when even alternating at the smallest size has no room", () => {
    // Sixteen quarters in a one-third panel: two points' room is under the smallest label.
    const sixteen = chart([{ name: "ASP", values: ASP.series[0].values.flatMap((value) => [value, value + 500]) }], [...QUARTERS, ...QUARTERS], {
      kind: "line",
    });
    const layout = plan(sixteen, ONE_THIRD, 420);
    expect(labelWidth("€241.1k", 12) + 12).toBeGreaterThan(2 * pitchOf(layout, 16));
    expect(layout.values).toMatchObject({ outer: null, alternate: false, asked: 16, written: 0, reason: "narrow" });
    expect(layout.values.need).toBeGreaterThan(layout.values.room);
    const { values, facts } = draw(sixteen, ONE_THIRD, 420);
    expect(values).toEqual([]);
    expect(facts).toMatchObject({ valuesAsked: "16", valuesWritten: "0", valuesReason: "narrow" });
    expect(facts.valuesAlternate).toBeUndefined();
    // A value under an odd point at the axis floor would run into the category axis: no alternating either.
    const grounded = chart([{ name: "ASP", values: [241_100, 0, 310_000, 342_000, 380_000, 424_000, 476_000, 540_000] }], QUARTERS, {
      kind: "line",
    });
    expect(plan(grounded, ONE_THIRD, 420).values).toMatchObject({ outer: null, alternate: false, written: 0, reason: "narrow" });
    // A canvas too short for a line's values stays so.
    expect(plan(ASP, ONE_THIRD, 198).values).toMatchObject({ outer: null, alternate: false, written: 0, reason: "short" });
  });
});

describe("a thin segment's value is written past its stack", () => {
  const ENTERPRISE = [25.6, 27.8, 30.2, 32.0, 34.4, 36.6, 39.2, 41.8].map((v) => v * M);
  const MID_MARKET = [12.2, 13.2, 14.0, 14.6, 15.4, 16.2, 17.2, 18.0].map((v) => v * M);
  /** Each quarter's share of its stack the third series takes: 1 to 3%. */
  const SHARES = [0.01, 0.015, 0.02, 0.025, 0.03, 0.012, 0.018, 0.028];
  const PARTNERS = ENTERPRISE.map((value, quarter) => {
    const rest = value + MID_MARKET[quarter];
    return Math.round((rest * SHARES[quarter]) / (1 - SHARES[quarter]) / 1000) * 1000;
  });
  /** The acceptance chart: 3 series x 8 quarters, the third 1 to 3% of each stack, in currency at label_size sm. */
  const ACCEPTANCE = chart(
    [
      { name: "Enterprise", values: ENTERPRISE },
      { name: "Mid-market", values: MID_MARKET },
      { name: "Partners", values: PARTNERS },
    ],
    QUARTERS,
    { kind: "stacked_bar", label_size: "sm" },
  );
  /** Five series, three of them thin: three values to write past each stack. */
  const FIVE = chart(
    [
      { name: "Enterprise", values: ENTERPRISE },
      { name: "Marketplace", values: PARTNERS.map((value) => Math.round(value * 0.8)) },
      { name: "Mid-market", values: MID_MARKET },
      { name: "Partners", values: PARTNERS },
      { name: "Resellers", values: PARTNERS.map((value) => Math.round(value * 0.6)) },
    ],
    QUARTERS,
    { kind: "stacked_bar", label_size: "sm" },
  );
  /** A dashboard panel's canvas at span 2 and span 3, on a board of one row of panels and of two. */
  const ROOMS = [
    ["span 2", 1089, 300],
    ["span 2 on two rows", 1089, 235],
    ["span 3", 1670, 300],
    ["span 3 on two rows", 1670, 235],
  ] as const;
  const key = (series: number, category: number): string => `s${series}:c${category}`;
  /** A leg's stroke, its caps left out: the first one starts on its segment's side. */
  const strokeOf = (leg: DrawnLeg): Box => {
    const half = LEADER_WIDTH / 2;
    return leg.y1 === leg.y2
      ? { left: Math.min(leg.x1, leg.x2), right: Math.max(leg.x1, leg.x2), top: leg.y1 - half, bottom: leg.y1 + half }
      : { left: leg.x1 - half, right: leg.x1 + half, top: Math.min(leg.y1, leg.y2), bottom: Math.max(leg.y1, leg.y2) };
  };
  const on = (value: number, low: number, high: number): boolean => value >= low - 0.5 && value <= high + 0.5;
  /** Whether two legs' centre lines meet: two leaders of one stack never do. */
  const meet = (a: DrawnLeg, b: DrawnLeg): boolean => {
    const box = (leg: DrawnLeg) => ({
      left: Math.min(leg.x1, leg.x2),
      right: Math.max(leg.x1, leg.x2),
      top: Math.min(leg.y1, leg.y2),
      bottom: Math.max(leg.y1, leg.y2),
    });
    const [p, q] = [box(a), box(b)];
    return p.left <= q.right && q.left <= p.right && p.top <= q.bottom && q.top <= p.bottom;
  };

  /**
   * Every value of the chart written - inside its segment or past its stack -
   * and nothing touching: no two values, no value past a stack and a bar, no
   * leader and a bar or another leader or a value other than its own. Each
   * value past a stack wears its series' colour, comes after the stack's
   * total, and its leader runs from its own segment's side to it.
   */
  function expectEveryValueWritten(block: DeckChartBlock, width: number, height: number): ReturnType<typeof draw> {
    const drawn = draw(block, width, height);
    const { values, bars, leaders, facts } = drawn;
    const horizontal = block.horizontal === true;
    const segments = block.series.length * block.categories.length;
    expect(facts).toMatchObject({ valuesAsked: String(segments + block.categories.length), valuesWritten: String(segments + block.categories.length) });
    expect(facts.valuesReason).toBeUndefined();
    const inside = values.filter((value) => value.role === "segment");
    const outside = values.filter((value) => value.role === "annotation");
    expect([...inside, ...outside].map((value) => key(value.series, value.category)).sort()).toEqual(
      block.series.flatMap((_, series) => block.categories.map((__, category) => key(series, category))).sort(),
    );
    expect(facts.valuesAnnotated).toBe(String(outside.length));
    const colours = seriesColors(block.series);
    for (const value of values) {
      for (const other of values) {
        if (other !== value) expect(touches(value.ink, other.ink), `${value.text} touches ${other.text}`).toBe(false);
      }
    }
    for (const value of outside) {
      expect(value.colour, value.text).toBe(colours[value.series]);
      for (const bar of bars) expect(touches(value.ink, bar.box), `${value.text} touches a bar`).toBe(false);
      const total = values.find((other) => other.role === "total" && other.category === value.category);
      expect(total, `${value.text} has no total`).toBeDefined();
      if (!total) continue;
      if (horizontal) expect(value.ink.left).toBeGreaterThanOrEqual(total.ink.right);
      else expect(value.ink.bottom).toBeLessThanOrEqual(total.ink.top);
      // Its leader: in its series' colour, from its own segment's side to its halo.
      const legs = leaders.filter((leg) => leg.series === value.series && leg.category === value.category);
      expect(legs.length, `${value.text} has no leader`).toBeGreaterThanOrEqual(2);
      for (const leg of legs) expect(leg.colour).toBe(colours[value.series]);
      const own = bars.find((bar) => bar.series === value.series && bar.category === value.category);
      expect(own).toBeDefined();
      if (!own) continue;
      const [first, last] = [legs[0], legs[legs.length - 1]];
      const startsOnSide = horizontal
        ? on(first.x1, own.box.left, own.box.right) && (on(first.y1, own.box.top, own.box.top) || on(first.y1, own.box.bottom, own.box.bottom))
        : on(first.y1, own.box.top, own.box.bottom) && (on(first.x1, own.box.left, own.box.left) || on(first.x1, own.box.right, own.box.right));
      expect(startsOnSide, `${value.text}'s leader starts off its segment`).toBe(true);
      const ends = on(last.x2, value.ink.left, value.ink.right) && on(last.y2, value.ink.top, value.ink.bottom);
      expect(ends, `${value.text}'s leader ends off it`).toBe(true);
    }
    for (const leg of leaders) {
      const stroke = strokeOf(leg);
      for (const bar of bars) expect(touches(stroke, bar.box), `a leader of s${leg.series} c${leg.category} crosses a bar`).toBe(false);
      for (const value of values) {
        if (value.role === "annotation" && value.series === leg.series && value.category === leg.category) continue;
        expect(touches(stroke, value.ink), `a leader of s${leg.series} c${leg.category} crosses ${value.text}`).toBe(false);
      }
      // Another stack's leaders keep clear of it; one stack's own never cross
      // (two that leave segments thinner than a stroke may touch there).
      for (const other of leaders) {
        if (other.series === leg.series && other.category === leg.category) continue;
        if (other.category === leg.category) expect(meet(leg, other), `two leaders cross at c${leg.category}`).toBe(false);
        else expect(touches(stroke, strokeOf(other)), `leaders of c${leg.category} and c${other.category} touch`).toBe(false);
      }
    }
    return drawn;
  }

  test.each(ROOMS)("the acceptance chart writes every value at %s, the small ones past their stacks, upright", (_room, width, height) => {
    const { values } = expectEveryValueWritten(ACCEPTANCE, width, height);
    const outside = values.filter((value) => value.role === "annotation");
    expect(outside.map((value) => key(value.series, value.category)).sort()).toEqual(QUARTERS.map((_, category) => key(2, category)).sort());
    // Small: under the totals, never over the scale's segment floor.
    const { values: layout } = plan(ACCEPTANCE, width, height);
    expect(layout.annotation?.size).toBeLessThanOrEqual(CHART_TEXT_SCALES.sm.segmentFloor);
    expect(layout.annotation?.size).toBeLessThan(layout.outer?.size ?? 0);
  });

  test.each(ROOMS)("and so does it on its side at %s, right of each total", (_room, width, height) => {
    const horizontal = { ...ACCEPTANCE, horizontal: true };
    const { values } = expectEveryValueWritten(horizontal, width, height);
    expect(values.filter((value) => value.role === "annotation")).toHaveLength(8);
  });

  test("the small values hold inside their segments at no height, so they are written past the stacks instead", () => {
    // The 1-3% series is at most 21px long even on a 640px canvas (size lg),
    // where its value needs 24px inside it: no height writes it inside.
    const tallest = plan(ACCEPTANCE, 1089, 640);
    const length = tallest.plot.bottom - tallest.plot.top;
    expect((Math.max(...PARTNERS) / (tallest.axis.max - tallest.axis.min)) * length).toBeLessThan(CHART_TEXT_SCALES.sm.values[0] + 6);
    for (const height of [300, 450, 640]) {
      const { values } = plan(ACCEPTANCE, 1089, height);
      expect(values.segments.size).toBe(16);
      expect(values.annotations.size).toBe(8);
    }
  });

  test("several values past one stack are stacked outward, the segment nearest the end first, their leaders nested", () => {
    const { values, leaders } = expectEveryValueWritten(FIVE, 1089, 300);
    for (const [category] of QUARTERS.entries()) {
      const here = values.filter((value) => value.role === "annotation" && value.category === category);
      // Nearest the total first: Resellers (on top of the stack), then Partners, then Marketplace.
      expect([...here].sort((a, b) => b.ink.bottom - a.ink.bottom).map((value) => value.series)).toEqual([4, 3, 1]);
      // Each one's lane (the leg running beside the stack) a step further out.
      const lanes = [4, 3, 1].map((series) => leaders.find((leg) => leg.series === series && leg.category === category && leg.x1 === leg.x2)?.x1 ?? 0);
      expect(lanes[0]).toBeLessThan(lanes[1]);
      expect(lanes[1]).toBeLessThan(lanes[2]);
    }
  });

  test("a chart whose segments all hold their value writes nothing past them", () => {
    const fits = chart([{ name: "Enterprise", values: ENTERPRISE }, { name: "Mid-market", values: MID_MARKET }], QUARTERS, { kind: "stacked_bar" });
    const { values } = plan(fits, 1720, 460);
    expect(values).toMatchObject({ annotation: null, thin: null, reason: null, written: 24 });
    expect(values.annotations.size).toBe(0);
    const drawn = draw(fits, 1720, 460);
    expect(drawn.leaders).toEqual([]);
    expect(drawn.facts.annotationSize).toBeUndefined();
  });

  test("a value is hidden only when the room past its stack is gone too, and the facts say what more room would do", () => {
    // Five series on their side: each row's gap holds two leaders, the third value of a row has no lane.
    const crowded = { ...FIVE, horizontal: true };
    const { values } = plan(crowded, 1089, 300);
    expect(values).toMatchObject({ asked: 48, reason: "segments" });
    expect(values.written).toBeLessThan(48);
    expect(values.thin).toMatchObject({ taller: true, wider: false, series: [1] });
    // The facts are the planner's own answer at that size.
    expect(plan(crowded, 1089, 640).values.written).toBe(48);
    expect(plan(crowded, 1670, 300).values.written).toBeLessThan(48);
    const { facts } = draw(crowded, 1089, 300);
    expect(facts).toMatchObject({ valuesReason: "segments", valuesTaller: "true", valuesWider: "false", valuesSeries: "1" });
    expect(Number(facts.valuesRoom)).toBeLessThan(Number(facts.valuesNeed));
  });

  test("more height is never said to help when the chart at the tallest canvas still hides them", () => {
    const months = (count: number) => Array.from({ length: count }, (_, month) => `M${month + 1}`);
    const dense = (count: number): DeckChartBlock =>
      chart(
        [
          { name: "Enterprise", values: months(count).map((_, month) => (12 + month * 0.4) * M) },
          { name: "Mid-market", values: months(count).map((_, month) => (6 + month * 0.15) * M) },
          { name: "Partners", values: months(count).map((_, month) => (0.2 + (month % 5) * 0.12) * M) },
        ],
        months(count),
        { kind: "stacked_bar", label_size: "sm" },
      );
    // Sixteen months in one column: the columns are too narrow, more width helps, more height does not.
    const column = plan(dense(16), COLUMN, 300).values;
    expect(column).toMatchObject({ reason: "segments", thin: { taller: false, wider: true } });
    expect(plan(dense(16), COLUMN, 640).values.written).toBeLessThan(column.asked);
    expect(plan(dense(16), 1670, 300).values.written).toBe(column.asked);
    // Twenty-four months across a full row: neither, and the thinnest is short of its value at 640px too.
    const full = plan(dense(24), 1670, 300).values;
    expect(full).toMatchObject({ reason: "segments", thin: { taller: false, wider: false, series: [2] } });
    expect(full.thin?.reach).toBeLessThan(full.need);
  });

  test("a series' own show_values false leaves its segments to the tooltip, its stack's total still counting it", () => {
    const quiet = { ...ACCEPTANCE, series: ACCEPTANCE.series.map((entry, index) => (index === 2 ? { ...entry, show_values: false } : entry)) };
    const { values } = plan(quiet, 1089, 300);
    expect(values).toMatchObject({ asked: 8 + 16, written: 8 + 16, reason: null, annotation: null });
    const drawn = draw(quiet, 1089, 300);
    expect(drawn.values.some((value) => value.series === 2 && value.role !== "total")).toBe(false);
    expect(drawn.values.find((value) => value.role === "total" && value.category === 0)?.text).toBe("€38.2M");
  });
});
