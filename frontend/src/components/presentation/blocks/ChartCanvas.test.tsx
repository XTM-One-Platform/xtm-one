/**
 * The values a bar chart writes - `yarn test:dom`.
 *
 * Pinned here: `show_values` puts a value on every bar of a grouped bar chart
 * and on every stack of a stacked one, at the room a board deck gives them -
 * a `sm` chart (300px) in one column of a 16:9 slide (836px) - set smaller
 * upright, or turned on its side when even the smallest size would touch a
 * neighbour (`chart-layout.test.tsx`), never dropped; a stack writes
 * its segments' values inside them when they fit, past the stack in the
 * series' colour when they do not, and its total above it.
 * What truly cannot be written is counted on the figure's facts, which the
 * layout check reports as "values hidden on chart N".
 *
 * The snapshots list every label as drawn: its role, series and category,
 * size, whether it reads along the bar, where it is anchored and its text.
 * The live figure is read in a browser by `tests_e2e/presentation-charts.spec.ts`.
 */
import { describe, expect, test } from "vitest";
import { render } from "@testing-library/react";

import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";

/** One column of a two-column content slide on the 1920px canvas, at the `sm` height. */
const COLUMN = { width: 836, height: 300 } as const;

const QUARTERS = ["Q1 24", "Q2 24", "Q3 24", "Q4 24", "Q1 25", "Q2 25", "Q3 25", "Q4 25"];
const M = 1_000_000;

const ARR: DeckChartBlock = {
  type: "chart",
  kind: "bar",
  format: "currency",
  unit: "EUR",
  show_values: true,
  size: "sm",
  series: [
    { name: "ARR", values: [42.8, 46.2, 49.8, 52.4, 56.0, 59.2, 63.1, 66.4].map((v) => v * M) },
    { name: "Net new ARR", values: [4.2, 3.4, 3.6, 2.6, 3.6, 3.2, 3.8, 7.6].map((v) => v * M) },
  ],
  categories: QUARTERS,
};

const SOURCES: DeckChartBlock = {
  type: "chart",
  kind: "bar",
  format: "currency",
  unit: "EUR",
  show_values: true,
  size: "sm",
  series: [
    { name: "New logos", values: [2.4, 1.8, 2.2, 1.4, 2.0, 1.6, 2.2, 4.4].map((v) => v * M) },
    { name: "Expansion", values: [1.4, 1.2, 1.0, 0.8, 1.2, 1.2, 1.2, 2.2].map((v) => v * M) },
    { name: "Partners", values: [0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 1.0].map((v) => v * M) },
  ],
  categories: QUARTERS,
};

const BY_SEGMENT: DeckChartBlock = {
  type: "chart",
  kind: "stacked_bar",
  format: "currency",
  unit: "EUR",
  show_values: true,
  size: "sm",
  series: [
    { name: "Enterprise", values: [25.6, 27.8, 30.2, 32.0, 34.4, 36.6, 39.2, 41.8].map((v) => v * M) },
    { name: "Mid-market", values: [12.2, 13.2, 14.0, 14.6, 15.4, 16.2, 17.2, 18.0].map((v) => v * M) },
    { name: "SMB", values: [5.0, 5.2, 5.6, 5.8, 6.2, 6.4, 6.6, 6.6].map((v) => v * M) },
  ],
  categories: QUARTERS,
};

interface Drawn {
  labels: string[];
  facts: DOMStringMap;
}

/** The chart drawn at `width` x `height`, read as its value labels and its published facts. */
function draw(block: DeckChartBlock, width: number = COLUMN.width, height: number = COLUMN.height): Drawn {
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
  const labels = Array.from(container.querySelectorAll<SVGTextElement>("text.deck-chart-value")).map((text) => {
    const { role, series, category, rotate, placement } = text.dataset;
    const x = Math.round(Number(text.getAttribute("x")));
    const y = Math.round(Number(text.getAttribute("y")));
    const size = text.style.fontSize;
    const where = `${rotate ? " rotated" : ""}${placement ? ` ${placement}` : ""}`;
    return `${role} s${series} c${category} ${size}${where} @${x},${y} ${text.textContent}`;
  });
  const facts = container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset ?? {};
  return { labels, facts };
}

/** Every `series:category` pair a label of `role` covers. */
const covered = (labels: readonly string[], role: string): string[] =>
  labels.filter((line) => line.startsWith(`${role} `)).map((line) => line.split(" ").slice(1, 3).join(":"));

const pairs = (series: number, categories: number): string[] =>
  Array.from({ length: series }, (_, s) => Array.from({ length: categories }, (_, c) => `s${s}:c${c}`)).flat();

describe("grouped bars", () => {
  test("two series over eight quarters write a compact value above each of the 16 bars, upright", () => {
    const { labels, facts } = draw(ARR);
    expect(covered(labels, "value").sort()).toEqual(pairs(2, 8).sort());
    expect(labels.some((line) => line.endsWith(" €66.4M"))).toBe(true);
    expect(labels.some((line) => line.endsWith(" €7.6M"))).toBe(true);
    expect(labels.some((line) => line.includes(" rotated "))).toBe(false);
    expect(facts).toMatchObject({ values: "true", valuesAsked: "16", valuesWritten: "16" });
    expect(facts.valuesReason).toBeUndefined();
    expect(facts.labelRotate).toBeUndefined();
    expect(labels).toMatchSnapshot();
  });

  test("three series over eight quarters write a value on each of the 24 bars, turned on its side", () => {
    const { labels, facts } = draw(SOURCES);
    expect(covered(labels, "value").sort()).toEqual(pairs(3, 8).sort());
    // A 3-bar group leaves each bar narrower than `€2.4M` set upright, even at 12px.
    expect(labels.every((line) => line.includes(" rotated "))).toBe(true);
    expect(facts).toMatchObject({ values: "true", valuesAsked: "24", valuesWritten: "24", labelRotate: "true" });
    expect(labels).toMatchSnapshot();
  });

  test("a large number chart writes its labels compact, with the unit", () => {
    const block: DeckChartBlock = {
      ...ARR,
      format: "number",
      unit: "users",
      series: [{ name: "Users", values: [1.2, 1.9, 2.6, 2.2].map((v) => v * M) }],
      categories: ["Q1", "Q2", "Q3", "Q4"],
    };
    const { labels } = draw(block);
    expect(labels.map((line) => line.split(" ").slice(-2).join(" "))).toEqual([
      "1.2M users",
      "1.9M users",
      "2.6M users",
      "2.2M users",
    ]);
  });

  test("values that truly cannot fit are counted and the reason published, never silently dropped", () => {
    const crowded: DeckChartBlock = {
      ...SOURCES,
      series: Array.from({ length: 6 }, (_, s) => ({ name: `S${s}`, values: Array.from({ length: 16 }, (_, c) => (s + c + 1) * M) })),
      categories: Array.from({ length: 16 }, (_, c) => `M${c + 1}`),
    };
    const { labels, facts } = draw(crowded, 600, 300);
    expect(labels).toEqual([]);
    expect(facts).toMatchObject({ values: "false", valuesAsked: "96", valuesWritten: "0", valuesReason: "narrow" });
    expect(Number(facts.valuesRoom)).toBeLessThan(Number(facts.valuesNeed));
  });

  test("the deck's own room - a fill chart under a heading in one of two columns - writes every value too", () => {
    // Slides 1 and 3 of the reported deck draw their charts 396-442px high.
    for (const [block, count] of [
      [ARR, 16],
      [SOURCES, 24],
    ] as const) {
      const { labels, facts } = draw(block, COLUMN.width, 420);
      expect(labels).toHaveLength(count);
      expect(facts).toMatchObject({ valuesAsked: String(count), valuesWritten: String(count) });
    }
    const { labels } = draw(BY_SEGMENT, COLUMN.width, 420);
    expect(covered(labels, "total")).toHaveLength(8);
    expect(covered(labels, "segment").length).toBeGreaterThanOrEqual(16);
  });

  test("show_values false asks for nothing", () => {
    const { labels, facts } = draw({ ...ARR, show_values: false });
    expect(labels).toEqual([]);
    expect(facts).toMatchObject({ values: "false", valuesAsked: "0", valuesWritten: "0" });
  });
});

describe("stacked bars", () => {
  test("three segments over eight quarters write the segment values inside and the total above each stack", () => {
    const { labels, facts } = draw(BY_SEGMENT);
    expect(covered(labels, "total").sort()).toEqual(pairs(1, 8).map((pair) => pair.replace("s0", "s2")).sort());
    // The two lower segments hold their value at this size; the top one,
    // too thin here, writes it past its stack, over the total.
    expect(covered(labels, "segment").sort()).toEqual(pairs(2, 8).sort());
    expect(covered(labels, "annotation").sort()).toEqual(pairs(3, 8).filter((pair) => pair.startsWith("s2")).sort());
    expect(labels.some((line) => line.startsWith("total ") && line.endsWith(" €66.4M"))).toBe(true);
    expect(facts).toMatchObject({ valuesAsked: String(8 + 24), valuesWritten: String(labels.length), valuesAnnotated: "8" });
    expect(facts.valuesReason).toBeUndefined();
    expect(labels).toMatchSnapshot();
  });

  test("a full-width stack writes every segment value", () => {
    const { labels, facts } = draw(BY_SEGMENT, 1720, 460);
    expect(covered(labels, "segment").sort()).toEqual(pairs(3, 8).sort());
    expect(facts).toMatchObject({ valuesAsked: "32", valuesWritten: "32" });
    expect(facts.valuesReason).toBeUndefined();
    expect(labels).toMatchSnapshot();
  });

  test("a segment alone in its stack is its total, written once", () => {
    const block: DeckChartBlock = {
      ...BY_SEGMENT,
      series: [
        { name: "A", values: [4 * M, 3 * M] },
        { name: "B", values: [0, 2 * M] },
      ],
      categories: ["Q1", "Q2"],
    };
    const { labels } = draw(block);
    expect(covered(labels, "segment").sort()).toEqual(["s0:c1", "s1:c1"]);
    expect(covered(labels, "total").sort()).toEqual(["s0:c0", "s1:c1"]);
  });
});

/** Deals as bars and a line through their tops, on the same axis: each line value meets a bar's. */
const THROUGH_THE_BARS: DeckChartBlock = {
  type: "chart",
  kind: "combo",
  show_values: true,
  categories: ["Q1", "Q2", "Q3", "Q4"],
  series: [
    { name: "Deals", kind: "bar", values: [10, 20, 30, 40] },
    { name: "Forecast", kind: "line", values: [10, 20, 30, 40] },
  ],
};

const lineLabels = (labels: readonly string[], series: number): string[] =>
  labels.filter((line) => line.startsWith(`value s${series} `));

describe("a combo's line", () => {
  test("writes each value over its point when the bars leave the room free", () => {
    const clear: DeckChartBlock = {
      ...THROUGH_THE_BARS,
      series: [THROUGH_THE_BARS.series[0], { name: "Target", kind: "line", values: [60, 60, 60, 60] }],
    };
    const { labels, facts } = draw(clear, 1200, 560);
    expect(lineLabels(labels, 1).map((line) => line.split(" ").pop())).toEqual(["60", "60", "60", "60"]);
    expect(lineLabels(labels, 1).some((line) => line.includes(" below "))).toBe(false);
    expect(facts).toMatchObject({ valuesAsked: "8", valuesWritten: "8", pointSize: "22" });
  });

  test("writes a value under its point where a bar's value holds the room over it", () => {
    const { labels, facts } = draw(THROUGH_THE_BARS, 1200, 560);
    const line = lineLabels(labels, 1);
    expect(line).toHaveLength(4);
    expect(line.every((label) => label.includes(" below "))).toBe(true);
    // The bars keep their values over them.
    expect(lineLabels(labels, 0).some((label) => label.includes(" below "))).toBe(false);
    expect(facts).toMatchObject({ valuesAsked: "8", valuesWritten: "8" });
    expect(facts.valuesReason).toBeUndefined();
  });

  test("raises a value over the values in its way, and counts one the canvas has no room for", () => {
    // A second line through the bars' tops: over its point the bar's value,
    // under it the first line's - raised clear of the bar's value instead.
    // At the top of the axis the plot gives up the room the raised one needs;
    // on a canvas too short to give it, that one is counted.
    const twice: DeckChartBlock = {
      ...THROUGH_THE_BARS,
      series: [...THROUGH_THE_BARS.series, { name: "Plan", kind: "line", values: [10, 20, 30, 40] }],
    };
    const { labels, facts } = draw(twice, 1200, 560);
    expect(lineLabels(labels, 1).every((label) => label.includes(" below "))).toBe(true);
    const plan = lineLabels(labels, 2);
    expect(covered(plan, "value")).toEqual(["s2:c0", "s2:c1", "s2:c2", "s2:c3"]);
    expect(plan.every((label) => label.includes(" lifted "))).toBe(true);
    const at = (label: string) => Number(/@-?\d+,(-?\d+) /.exec(label)?.[1]);
    const bar = (category: number) => labels.find((label) => label.startsWith(`value s0 c${category} `)) ?? "";
    plan.forEach((label, category) => expect(at(label)).toBeLessThan(at(bar(category))));
    expect(facts).toMatchObject({ valuesAsked: "12", valuesWritten: "12" });
    const short = draw(twice, 1200, 160);
    expect(short.facts).toMatchObject({ valuesAsked: "12", valuesReason: "overlap" });
    expect(Number(short.facts.valuesWritten)).toBeLessThan(12);
  });
});
