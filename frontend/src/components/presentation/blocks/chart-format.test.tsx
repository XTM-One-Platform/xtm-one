/**
 * How a chart writes its numbers - `yarn test:dom`.
 *
 * `chart-format.ts` is pure, but it reads the deck theme through a bare
 * import that `node --test` cannot resolve, so its rules are exercised here,
 * and the figure `ChartCanvas` draws from them is read off its SVG the way
 * the PPTX extractor reads it. The table of every format rule is
 * `tests_unit/chart-format.test.ts`; the PPTX number formats that mirror these
 * decimals are pinned in `backend/tests/test_chart_export.py`.
 */
import { describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";
import {
  MAX_AUTO_DECIMALS,
  axisView,
  chartExportPayload,
  chartValueWriter,
  formatChartValue,
  pinnedTier,
  secondaryTargetLegendName,
  targetLegendName,
  waterfallLabel,
  waterfallSteps,
  writtenValues,
} from "./chart-format";
import { axisTick, seriesLabels } from "./chart-layout";

const LOCALE = "en-US";

/** The burn multiple of slide 3 of the Q3 2026 quarterly review; its KPI tile reads 1.27x. */
const BURN_MULTIPLE = [0.96, 1.04, 0.47, 1.57, 1.18, 1.56, 1.27];
const AS_THE_TILE_READS = ["0.96x", "1.04x", "0.47x", "1.57x", "1.18x", "1.56x", "1.27x"];

/** Slide 3: net new ARR and burn as euro bars, the burn multiple as a line against the right-hand axis. */
const SLIDE_3: DeckChartBlock = {
  type: "chart",
  kind: "combo",
  format: "currency",
  unit: "EUR",
  categories: ["Q1 25", "Q2 25", "Q3 25", "Q4 25", "Q1 26", "Q2 26", "Q3 26"],
  series: [
    { name: "Net new ARR", kind: "bar", tone: "success", values: [1_400_000, 1_950_000, 5_200_000, 1_600_000, 2_800_000, 1_500_000, 2_200_000] },
    { name: "Burn", kind: "bar", tone: "error", values: [1_300_000, 2_000_000, 1_700_000, 3_000_000, 3_000_000, 2_800_000, 3_500_000] },
    { name: "Burn multiple", kind: "line", axis: "secondary", values: BURN_MULTIPLE },
  ],
  secondary_format: "number",
  secondary_unit: "x",
  secondary_decimals: 2,
};

const burnLabels = (block: DeckChartBlock) => BURN_MULTIPLE.map(seriesLabels(block, LOCALE)[2]);

describe("the decimals a chart sets", () => {
  it("writes the burn multiple of slide 3 the way its KPI tile does", () => {
    expect(burnLabels(SLIDE_3)).toEqual(AS_THE_TILE_READS);
    // Every tick of the right-hand axis takes them, a round one included.
    expect([0, 0.5, 1, 1.5, 2].map(axisTick(SLIDE_3, "secondary", LOCALE, [0, 0.5, 1, 1.5, 2]))).toEqual([
      "0.00x",
      "0.50x",
      "1.00x",
      "1.50x",
      "2.00x",
    ]);
    // The left-hand axis keeps its own reading.
    const euros = [0, 2_000_000, 4_000_000, 6_000_000];
    expect(euros.map(axisTick(SLIDE_3, "primary", LOCALE, euros))).toEqual(["€0", "€2M", "€4M", "€6M"]);
    expect(seriesLabels(SLIDE_3, LOCALE)[0](2_400_000)).toBe("€2.4M");
  });

  it("writes every value with exactly that many, of the compact amount for a currency", () => {
    const euros = { format: "currency", unit: "EUR", decimals: 2 } as const;
    expect(formatChartValue(19_140_000, euros, LOCALE)).toBe("€19.14M");
    expect(formatChartValue(527_100, euros, LOCALE)).toBe("€527.10k");
    expect(formatChartValue(-400_000, euros, LOCALE)).toBe("-€400.00k");
    expect(formatChartValue(19_140_000, { ...euros, decimals: 0 }, LOCALE)).toBe("€19M");
    expect(formatChartValue(86, { format: "percent", decimals: 1 }, LOCALE)).toBe("86.0%");
    expect(formatChartValue(4, { unit: "h", decimals: 3 }, LOCALE)).toBe("4.000h");
    expect(formatChartValue(2_200_000, { unit: "users", decimals: 2 }, LOCALE, "label_compact")).toBe("2.20M users");
    // A run never overrides what the block sets.
    expect(chartValueWriter({ decimals: 1 }, [0.96, 0.47], LOCALE)(0.96)).toBe("1.0");
  });

  it("reads the decimals of each axis apart", () => {
    const both = { ...SLIDE_3, decimals: 1 as const };
    expect([axisView(both, "primary").decimals, axisView(both, "secondary").decimals]).toEqual([1, 2]);
    expect(seriesLabels(both, LOCALE)[0](2_400_000)).toBe("€2.4M");
    expect(seriesLabels(both, LOCALE)[0](5_000_000)).toBe("€5.0M");
    expect(burnLabels(both)).toEqual(AS_THE_TILE_READS);
  });

  it("names a target in the legend in the decimals of the axis it is read against", () => {
    expect(secondaryTargetLegendName({ ...SLIDE_3, secondary_target: 1 }, LOCALE)).toBe("1.00x");
    expect(targetLegendName({ ...SLIDE_3, target: 3_000_000, decimals: 1 }, LOCALE)).toBe("€3.0M");
  });

  it("publishes the decimals of both axes for the PPTX writer", () => {
    const payload = JSON.parse(chartExportPayload(SLIDE_3, LOCALE, "Unexplained"));
    expect([payload.decimals, payload.secondary_decimals]).toEqual([null, 2]);
    const oneAxis: DeckChartBlock = { ...SLIDE_3, kind: "bar", series: [{ name: "ARR", values: [1, 2, 3, 4, 5, 6, 7] }], decimals: 1 };
    const plain = JSON.parse(chartExportPayload(oneAxis, LOCALE, "Unexplained"));
    expect([plain.decimals, plain.secondary_decimals]).toEqual([1, null]);
  });
});

describe("the decimals a chart writes on its own", () => {
  it("keeps two significant digits under 10, and one decimal at most above", () => {
    const one = (value: number) => formatChartValue(value, {}, LOCALE);
    expect([0.96, 0.47, 0.053, 1.27, 9.96, 12.345, 1204.5].map(one)).toEqual([
      "0.96",
      "0.47",
      "0.053",
      "1.3",
      "10",
      "12.3",
      "1,204.5",
    ]);
    expect(formatChartValue(1_250, { format: "currency", unit: "GBP" }, LOCALE)).toBe("£1.3k");
  });

  it("reads the burn multiple of slide 3 to the hundredth with no decimals set", () => {
    // 0.47 needs two decimals for two significant digits: the whole line takes them.
    expect(burnLabels({ ...SLIDE_3, secondary_decimals: undefined })).toEqual(AS_THE_TILE_READS);
  });

  it("adds a decimal while two different values would read the same, and only then", () => {
    const run = (values: number[], block: Pick<DeckChartBlock, "format" | "unit"> = {}) =>
      values.map(chartValueWriter(block, values, LOCALE));
    expect(run([1.57, 1.56, 1.27])).toEqual(["1.57", "1.56", "1.27"]);
    expect(run([1.4, 1.6, 2])).toEqual(["1.4", "1.6", "2"]);
    expect(run([12.31, 12.34])).toEqual(["12.31", "12.34"]);
    expect(run([16_510_000, 16_540_000], { format: "currency", unit: "EUR" })).toEqual(["€16.51M", "€16.54M"]);
    // Float noise is no difference: 0.1 + 0.2 reads 0.3 like 0.3 does.
    expect(run([0.3, 0.1 + 0.2])).toEqual(["0.3", "0.3"]);
    // Past MAX_AUTO_DECIMALS two values are one figure.
    expect(MAX_AUTO_DECIMALS).toBe(6);
    expect(run([1.00000001, 1.00000002])).toEqual(["1", "1"]);
  });

  it("rounds a tie on the decimal it reads, as chart_export does", () => {
    const two = { decimals: 2 } as const;
    expect([1.005, 2.675, 0.125, 1.015].map((value) => formatChartValue(value, two, LOCALE))).toEqual([
      "1.01",
      "2.68",
      "0.13",
      "1.02",
    ]);
    // 1.005 and 1.01 read the same at two decimals: the run takes a third.
    expect([1.005, 1.01].map(chartValueWriter({}, [1.005, 1.01], LOCALE))).toEqual(["1.005", "1.01"]);
  });

  it("decides once for an axis, so its bars and its line read alike", () => {
    const combo: DeckChartBlock = {
      type: "chart",
      kind: "combo",
      categories: ["Q1", "Q2"],
      series: [
        { name: "Deals", kind: "bar", values: [12.25, 13.5] },
        { name: "Win rate", kind: "line", values: [0.53, 0.8] },
      ],
    };
    const [deals, rate] = seriesLabels(combo, LOCALE);
    expect([deals(12.25), deals(13.5), rate(0.53)]).toEqual(["12.25", "13.5", "0.53"]);
  });

  it("counts every value a chart writes in its run", () => {
    const stack: DeckChartBlock = {
      type: "chart",
      kind: "stacked_bar",
      categories: ["Q1", "Q2", "Q3"],
      target: 7,
      series: [
        { name: "A", values: [1, -2, 3] },
        { name: "B", values: [4, 5, -6] },
      ],
    };
    expect(writtenValues(stack)).toEqual([1, -2, 3, 4, 5, -6, 5, 0, 5, -2, 3, -6, 7]);
    const donut: DeckChartBlock = { type: "chart", kind: "donut", categories: ["CE", "EE"], series: [{ name: "S", values: [64, 36] }] };
    expect(writtenValues(donut)).toEqual([64, 36, 100]);
    const gauge: DeckChartBlock = { type: "chart", kind: "gauge", categories: ["NRR"], series: [{ name: "NRR", values: [86] }], format: "percent" };
    expect(writtenValues(gauge)).toEqual([86, 100]);
    const bridge: DeckChartBlock = {
      type: "chart",
      kind: "waterfall",
      categories: ["Q2", "New", "Q3"],
      totals: [0, 2],
      series: [{ name: "ARR", values: [16_500_000, 1_800_000, 18_300_000] }],
    };
    expect(writtenValues(bridge)).toEqual([16_500_000, 16_500_000, 1_800_000, 18_300_000, 18_300_000, 18_300_000]);
  });
});

/** Slide 3's two units as a line chart, which writes a value on every point of both lines. */
const LINES: DeckChartBlock = {
  ...SLIDE_3,
  kind: "line",
  series: SLIDE_3.series.filter((entry) => entry.name !== "Burn").map((entry) => ({ ...entry, kind: undefined })),
};

async function drawn(block: DeckChartBlock) {
  const onSettled = vi.fn();
  const { container } = render(
    <ChartCanvas
      block={block}
      width={1400}
      height={600}
      hidden={new Set()}
      animate={false}
      locale="en"
      onSettled={onSettled}
      gapLabel="Unexplained"
      runningLabel="Running total"
    />,
  );
  await waitFor(() => expect(onSettled).toHaveBeenCalled());
  const texts = (selector: string) =>
    Array.from(container.querySelectorAll(selector)).map((node) => (node.textContent ?? "").trim());
  return { texts };
}

describe("the figure", () => {
  it("writes slide 3's burn multiple on its combo line, and its right-hand ticks, in its decimals", async () => {
    const { texts } = await drawn(SLIDE_3);
    await waitFor(() => expect(texts(".deck-chart-tick-secondary")).toEqual(["0.00x", "0.50x", "1.00x", "1.50x", "2.00x"]));
    expect(texts(".deck-chart-tick-primary")).toContain("€6M");
    expect(texts('.deck-chart-value[data-series="2"]')).toEqual(AS_THE_TILE_READS);
  });

  it("writes every point of the burn multiple as the KPI tile reads it", async () => {
    const { texts } = await drawn(LINES);
    await waitFor(() => expect(texts('.deck-chart-value[data-series="1"]')).toEqual(AS_THE_TILE_READS));
    expect(texts('.deck-chart-value[data-series="0"]')).toContain("€1.4M");
  });
});

/** Two rises, a fall under a million, the ending. */
const BRIDGE: DeckChartBlock = {
  type: "chart",
  kind: "waterfall",
  format: "currency",
  unit: "EUR",
  decimals: 2,
  categories: ["Licences", "Services", "Churn", "Ending"],
  series: [{ name: "ARR", values: [2_500_000, 1_250_000, -250_000, 3_500_000] }],
  totals: [3],
};

/** What a waterfall writes on its bars: each step through the bridge's own writer (`ChartCanvas`). */
const bridgeLabels = (block: DeckChartBlock) =>
  waterfallSteps(block, "Unexplained").map((step) => waterfallLabel(step, seriesLabels(block, LOCALE)[0]));

/** A stack of services under licences, 480k and 1.2M. */
const STACK: DeckChartBlock = {
  type: "chart",
  kind: "stacked_bar",
  format: "currency",
  unit: "EUR",
  decimals: 1,
  categories: ["Q3"],
  series: [
    { name: "Services", values: [480_000] },
    { name: "Licences", values: [1_200_000] },
  ],
};

describe("a pinned unit scale", () => {
  it("writes the bridge in millions only, the fall under a million too", () => {
    // Unpinned, the fall takes its own letter, mixing k and M.
    expect(bridgeLabels(BRIDGE)).toEqual(["+€2.50M", "+€1.25M", "-€250.00k", "€3.50M"]);
    expect(bridgeLabels({ ...BRIDGE, unit_scale: "M" })).toEqual(["+€2.50M", "+€1.25M", "-€0.25M", "€3.50M"]);
  });

  it("writes a stack's segments and total in the chart's one letter", () => {
    const [services, licences] = seriesLabels({ ...STACK, unit_scale: "M" }, LOCALE);
    expect([services(480_000), licences(1_200_000), licences(1_680_000)]).toEqual(["€0.5M", "€1.2M", "€1.7M"]);
    expect(seriesLabels(STACK, LOCALE)[0](480_000)).toBe("€480.0k");
  });

  it("writes every tick of the axis in that letter, with the chart's decimals", () => {
    const ticks = [0, 1_000_000, 2_000_000, 3_000_000, 4_000_000];
    expect(ticks.map(axisTick({ ...BRIDGE, unit_scale: "M" }, "primary", LOCALE, ticks))).toEqual([
      "€0.00M",
      "€1.00M",
      "€2.00M",
      "€3.00M",
      "€4.00M",
    ]);
    // Unpinned and with no decimals, the ticks keep today's own letters.
    const halves = [0, 500_000, 1_000_000];
    expect(halves.map(axisTick({ ...STACK, decimals: undefined }, "primary", LOCALE, halves))).toEqual(["€0", "€500k", "€1M"]);
    expect(halves.map(axisTick({ ...STACK, decimals: undefined, unit_scale: "M" }, "primary", LOCALE, halves))).toEqual([
      "€0M",
      "€0.5M",
      "€1M",
    ]);
  });

  it("writes an amount that rounds to zero in that letter with the chart's decimals, never drops it", () => {
    const pinned = { format: "currency", unit: "EUR", decimals: 2, unit_scale: "M" } as const;
    expect(formatChartValue(1_000, pinned, LOCALE)).toBe("€0.00M");
    expect(formatChartValue(0, pinned, LOCALE)).toBe("€0.00M");
    expect(formatChartValue(40_000_000, { ...pinned, decimals: 1, unit_scale: "B" }, LOCALE)).toBe("€0.0B");
    expect(formatChartValue(1_250, { ...pinned, decimals: 0, unit_scale: "k" }, LOCALE)).toBe("€1k");
  });

  it("reads a run with no decimals at the pinned letter", () => {
    const values = [480_000, 1_200_000];
    const write = chartValueWriter({ format: "currency", unit: "EUR", unit_scale: "M" }, values, LOCALE);
    // 0.48 needs two decimals for two significant digits; trailing zeros dropped.
    expect(values.map(write)).toEqual(["€0.48M", "€1.2M"]);
  });

  it("names a target in the letter of the axis it is read against", () => {
    const planned = { ...STACK, kind: "bar" as const, stacked: undefined, target: 750_000 };
    expect(targetLegendName(planned, LOCALE)).toBe("€750.0k");
    expect(targetLegendName({ ...planned, unit_scale: "M" }, LOCALE)).toBe("€0.8M");
  });

  it("reads the right-hand axis in its own scale and decimals", () => {
    const two: DeckChartBlock = {
      ...SLIDE_3,
      unit_scale: "M",
      decimals: 1,
      series: [
        SLIDE_3.series[0],
        { name: "ASP", kind: "line", axis: "secondary", values: [1_250_000, 98_000] },
      ],
      categories: ["Q1", "Q2"],
      secondary_format: "currency",
      secondary_unit: "USD",
      secondary_decimals: 0,
      secondary_unit_scale: "k",
      secondary_target: 500_000,
    };
    expect([axisView(two, "primary").unit_scale, axisView(two, "secondary").unit_scale]).toEqual(["M", "k"]);
    const [arr, asp] = seriesLabels(two, LOCALE);
    expect([arr(480_000), asp(1_250_000), asp(98_000)]).toEqual(["€0.5M", "$1,250k", "$98k"]);
    expect(secondaryTargetLegendName(two, LOCALE)).toBe("$500k");
  });

  it("leaves a pie, a donut, a gauge, a number and a percent to their own rule", () => {
    for (const kind of ["pie", "donut", "gauge"] as const) {
      const reading = { format: "currency", unit: "EUR", unit_scale: "M", kind } as const;
      expect(pinnedTier(reading)).toBeNull();
      expect(formatChartValue(480_000, reading, LOCALE)).toBe("€480k");
    }
    expect(formatChartValue(480_000, { format: "number", unit_scale: "M" }, LOCALE, "label_compact")).toBe("480k");
    expect(formatChartValue(86, { format: "percent", unit_scale: "M" }, LOCALE)).toBe("86%");
  });

  it("writes auto exactly as unset, and publishes a scale only when one is pinned", () => {
    for (const block of [BRIDGE, STACK, SLIDE_3]) {
      const auto = { ...block, unit_scale: "auto" as const, secondary_unit_scale: "auto" as const };
      expect(bridgeOrSeries(auto)).toEqual(bridgeOrSeries(block));
      expect(chartExportPayload(auto, LOCALE, "Unexplained")).toBe(chartExportPayload(block, LOCALE, "Unexplained"));
      expect(JSON.parse(chartExportPayload(block, LOCALE, "Unexplained"))).not.toHaveProperty("unit_scale");
    }
    const pinned = JSON.parse(chartExportPayload({ ...BRIDGE, unit_scale: "M" }, LOCALE, "Unexplained"));
    expect(pinned.unit_scale).toBe("M");
    const pie: DeckChartBlock = { ...STACK, kind: "pie", series: [STACK.series[0]], unit_scale: "M" };
    expect(JSON.parse(chartExportPayload(pie, LOCALE, "Unexplained"))).not.toHaveProperty("unit_scale");
  });
});

/** Every label a block writes from its own values, for comparing two readings of it. */
function bridgeOrSeries(block: DeckChartBlock): string[] {
  if (block.kind === "waterfall") return bridgeLabels(block);
  const writers = seriesLabels(block, LOCALE);
  return block.series.flatMap((entry, index) => entry.values.map(writers[index]));
}
