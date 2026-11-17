/**
 * The chart catalog - `yarn test:dom`.
 *
 * `chart-catalog.json` holds the chart examples the `generate_presentation`
 * tool teaches (`presentation_guidance.py`, pinned to it by
 * `tests/test_presentation_guidance.py`); each one is drawn here the way the
 * deck draws it, so an example the renderer cannot draw fails the build. Next
 * to them, one block of every `ChartKind`, and the waterfall's own reading -
 * the ARR bridge of a board slide, quarter over quarter, whose bars must add up -
 * and the two-unit example read against two value axes. The
 * butterfly's own reading is `chart-butterfly.test.tsx`.
 * A map is drawn by its own drawing, as `chart.tsx` draws it.
 *
 * Drawn without motion, as the export page and a preview draw them; the
 * figures are read off the SVG Recharts renders (the bars' rectangles, the
 * value labels, the axis ticks, the connectors), the way the PPTX extractor
 * and the layout audit read them.
 */
import { describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

import type { DeckChartBlock, DeckChartKind } from "@/types";

import { CHART_KINDS } from "../block-schema";
import ChartCanvas from "./ChartCanvas";
import MapCanvas from "./MapCanvas";
import catalog from "./chart-catalog.json";
import {
  DEFAULT_WATERFALL_TOLERANCE,
  bridgeMisses,
  chartExportPayload,
  valueAxisScale,
  waterfallSteps,
} from "./chart-format";

const WIDTH = 1200;
const HEIGHT = 560;
const GAP_LABEL = "Unexplained";

/** The bridge of the Q3 2026 quarterly review: opening ARR, the changes, ending ARR. */
const ARR_BRIDGE: DeckChartBlock = {
  type: "chart",
  kind: "waterfall",
  categories: ["Opening ARR", "New logos", "Expansion", "Churn", "Closing ARR"],
  series: [{ name: "ARR", values: [16_500_000, 1_800_000, 1_300_000, -400_000, 19_200_000] }],
  totals: [0, 4],
  format: "currency",
};

/** The same bridge with an ending 0.06M short of its steps, drawn with `allow_gap`. */
const SHORT_BRIDGE: DeckChartBlock = {
  ...ARR_BRIDGE,
  series: [{ name: "ARR", values: [16_500_000, 1_800_000, 1_300_000, -400_000, 19_140_000] }],
  allow_gap: true,
};

const QUARTERS = ["Q1", "Q2", "Q3", "Q4"];

/** One block per kind; `satisfies` fails the build on a kind with no entry. */
const CATALOG = {
  bar: { type: "chart", kind: "bar", categories: QUARTERS, series: [{ name: "Trials", values: [64, 78, 91, 112] }] },
  stacked_bar: {
    type: "chart",
    kind: "stacked_bar",
    categories: QUARTERS,
    series: [
      { name: "6.x", values: [1400, 1900, 2400, 2900] },
      { name: "5.12", values: [2600, 2500, 2500, 2300] },
    ],
  },
  line: {
    type: "chart",
    kind: "line",
    categories: QUARTERS,
    series: [{ name: "ARR", values: [11_200_000, 12_900_000, 14_600_000, 16_500_000] }],
    format: "currency",
    target: 18_000_000,
    goal_label: "Plan",
  },
  area: { type: "chart", kind: "area", categories: QUARTERS, series: [{ name: "Users", values: [120, 340, 560, 910] }] },
  pie: { type: "chart", kind: "pie", categories: ["CE", "EE"], series: [{ name: "Share", values: [64, 36] }], format: "percent" },
  donut: { type: "chart", kind: "donut", categories: ["CE", "EE"], series: [{ name: "Share", values: [64, 36] }] },
  gauge: { type: "chart", kind: "gauge", categories: ["Coverage"], series: [{ name: "MITRE", values: [86] }], format: "percent" },
  combo: {
    type: "chart",
    kind: "combo",
    categories: QUARTERS,
    series: [
      { name: "Revenue", values: [10, 12, 15, 18], kind: "bar" },
      { name: "Margin", values: [8, 9, 11, 12], kind: "line" },
    ],
  },
  waterfall: ARR_BRIDGE,
  butterfly: {
    type: "chart",
    kind: "butterfly",
    categories: ["Executive", "Director", "Manager", "Lead"],
    series: [
      { name: "Contributors", values: [0, 2, 10, 40] },
      { name: "Managers", values: [5, 20, 18, 6] },
    ],
  },
  map: { type: "chart", kind: "map", categories: ["FR", "US", "BR"], series: [{ name: "Customers", values: [31, 42, 7] }] },
} satisfies Record<DeckChartKind, DeckChartBlock>;

function draw(block: DeckChartBlock) {
  const onSettled = vi.fn();
  const view = render(
    <div style={{ width: WIDTH, height: HEIGHT }}>
      {block.kind === "map" ? (
        <MapCanvas block={block} width={WIDTH} height={HEIGHT} locale="en" onSettled={onSettled} noDataLabel="No data" />
      ) : (
        <ChartCanvas
          block={block}
          width={WIDTH}
          height={HEIGHT}
          hidden={new Set()}
          animate={false}
          locale="en"
          onSettled={onSettled}
          gapLabel={GAP_LABEL}
          runningLabel="Running total"
        />
      )}
    </div>,
  );
  return { ...view, onSettled };
}

/** The drawn bars, left to right: their box and fill as Recharts set them. */
function bars(container: HTMLElement) {
  return Array.from(container.querySelectorAll<SVGPathElement>(".recharts-bar-rectangle path.recharts-rectangle"))
    .map((path) => ({
      x: Number(path.getAttribute("x")),
      y: Number(path.getAttribute("y")),
      width: Number(path.getAttribute("width")),
      height: Number(path.getAttribute("height")),
      fill: path.getAttribute("fill"),
    }))
    .sort((a, b) => a.x - b.x);
}

const texts = (container: HTMLElement, selector: string) =>
  Array.from(container.querySelectorAll(selector)).map((node) => (node.textContent ?? "").trim()).filter(Boolean);

describe("the chart catalog", () => {
  it.each(Object.entries(catalog))("draws the %s example the tool teaches", async (_name, example) => {
    const block = example as DeckChartBlock;
    const { container, onSettled } = draw(block);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    if (block.kind === "map") {
      // Every country the example names is drawn, in a colour of its own.
      const drawn = Array.from(container.querySelectorAll<SVGElement>(".deck-chart-map [data-code][data-category]"));
      expect(new Set(drawn.map((country) => country.dataset.code))).toEqual(new Set(block.categories));
      return;
    }
    expect(container.querySelector(".recharts-surface")).not.toBeNull();
    // Every category label carries the category ticks' class, a butterfly's on its spine included.
    expect(texts(container, ".deck-chart-tick-category")).toEqual(expect.arrayContaining(block.categories));
  });

  it("teaches the ARR bridge as the waterfall example, and it adds up", () => {
    const bridge = catalog.waterfall_chart as DeckChartBlock;
    expect(bridge.kind).toBe("waterfall");
    expect(waterfallSteps(bridge, GAP_LABEL).some((step) => step.role === "gap")).toBe(false);
    expect(waterfallSteps(bridge, GAP_LABEL).map((step) => step.category)).toEqual(bridge.categories);
  });

  it("has one block of every kind the spec accepts", () => {
    expect(Object.keys(CATALOG).sort()).toEqual([...CHART_KINDS].sort());
  });

  it.each(Object.entries(CATALOG))("draws a %s chart and settles", async (_kind, block) => {
    const { container, onSettled } = draw(block);
    expect(container.querySelector("svg")).not.toBeNull();
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    // What the PPTX extractor reads beside the payload; a gauge and a map are exported as a picture.
    const facts = container.querySelector<HTMLElement>(".deck-chart-facts");
    if (block.kind === "gauge") expect(facts).toBeNull();
    else if (block.kind === "map") expect(facts?.dataset.region).toBe("world");
    else expect(facts?.dataset.values).toBe("true");
  });
});

describe("the waterfall", () => {
  it("reads the quarter-over-quarter ARR bridge as one running sum", () => {
    const steps = waterfallSteps(ARR_BRIDGE, GAP_LABEL);
    expect(steps.map(({ category, role }) => [category, role])).toEqual([
      ["Opening ARR", "total"],
      ["New logos", "increase"],
      ["Expansion", "increase"],
      ["Churn", "decrease"],
      ["Closing ARR", "total"],
    ]);
    expect(steps.map(({ start, end }) => [start, end])).toEqual([
      [0, 16_500_000],
      [16_500_000, 18_300_000],
      [18_300_000, 19_600_000],
      [19_600_000, 19_200_000],
      [0, 19_200_000],
    ]);
    // The axis reaches the highest level the bridge passes through, not the ending.
    expect(valueAxisScale(ARR_BRIDGE, 5)).toMatchObject({ min: 0, max: 20_000_000, step: 5_000_000 });
  });

  it("floats each step from the level the bar before it left, in its role's tone", async () => {
    const { container, onSettled } = draw(ARR_BRIDGE);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    const drawn = bars(container);
    expect(drawn).toHaveLength(5);
    expect(drawn.map((bar) => bar.fill)).toEqual([
      "var(--deck-accent)",
      "var(--deck-success)",
      "var(--deck-success)",
      "var(--deck-error)",
      "var(--deck-accent)",
    ]);
    const [opening, logos, expansion, churn, ending] = drawn;
    const top = (bar: (typeof drawn)[number]) => bar.y;
    const bottom = (bar: (typeof drawn)[number]) => bar.y + bar.height;
    // Real boxes: the opening is tall, a change a sliver of it, side by side.
    expect(opening.height).toBeGreaterThan(200);
    expect(logos.height).toBeGreaterThan(5);
    expect(logos.height).toBeLessThan(opening.height / 5);
    expect(logos.x).toBeGreaterThan(opening.x + opening.width);
    expect(churn.height).toBeLessThan(logos.height);
    // A rise starts where the bar before it ends; a fall hangs from the top it starts at.
    expect(bottom(logos)).toBeCloseTo(top(opening), 0);
    expect(bottom(expansion)).toBeCloseTo(top(logos), 0);
    expect(top(churn)).toBeCloseTo(top(expansion), 0);
    // The ending stands on the axis, at the level the churn brought the bridge to.
    expect(bottom(ending)).toBeCloseTo(bottom(opening), 0);
    expect(top(ending)).toBeCloseTo(bottom(churn), 0);
  });

  it("writes every change with its sign and every total as its level, the categories on the axis", async () => {
    const { container, onSettled } = draw(ARR_BRIDGE);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    await waitFor(() =>
      expect(texts(container, ".deck-chart-value").sort()).toEqual(
        ["€16.5M", "+€1.8M", "+€1.3M", "-€400k", "€19.2M"].sort(),
      ),
    );
    expect(texts(container, ".recharts-cartesian-axis-tick-value")).toEqual(
      expect.arrayContaining(["Opening ARR", "New logos", "Expansion", "Churn", "Closing ARR"]),
    );
  });

  it("joins the bars with a connector at each running level, in the gap between two bars", async () => {
    const { container, onSettled } = draw(ARR_BRIDGE);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    const drawn = bars(container);
    const connectors = Array.from(container.querySelectorAll<SVGLineElement>("line.deck-chart-connector")).map((line) => ({
      x1: Number(line.getAttribute("x1")),
      x2: Number(line.getAttribute("x2")),
      y1: Number(line.getAttribute("y1")),
      y2: Number(line.getAttribute("y2")),
    }));
    expect(connectors).toHaveLength(4);
    connectors.forEach((connector, index) => {
      expect(connector.y1).toBe(connector.y2);
      expect(connector.x2 - connector.x1).toBeGreaterThan(10);
      // From the right edge of one bar to the left edge of the next, never along a bar.
      expect(connector.x1).toBeCloseTo(drawn[index].x + drawn[index].width, 0);
      expect(connector.x2).toBeCloseTo(drawn[index + 1].x, 0);
    });
    // The first runs at the opening level, the last at the level the ending stands at.
    expect(connectors[0].y1).toBeCloseTo(drawn[0].y, 0);
    expect(connectors[3].y1).toBeCloseTo(drawn[4].y, 0);
  });

  it("draws the miss of a bridge that does not add up as an Unexplained step, with allow_gap", async () => {
    const steps = waterfallSteps(SHORT_BRIDGE, GAP_LABEL);
    expect(steps).toHaveLength(6);
    expect(steps[4]).toMatchObject({ category: GAP_LABEL, role: "gap", start: 19_200_000, end: 19_140_000 });
    expect(steps[4].value).toBeCloseTo(-60_000, 6);
    // Without allow_gap the same figures draw no gap: the validator refuses them before.
    expect(waterfallSteps({ ...SHORT_BRIDGE, allow_gap: false }, GAP_LABEL)).toHaveLength(5);

    const { container, onSettled } = draw(SHORT_BRIDGE);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    const drawn = bars(container);
    expect(drawn).toHaveLength(6);
    expect(drawn[4].fill).toBe("var(--deck-warning)");
    await waitFor(() => expect(texts(container, ".deck-chart-value")).toContain("-€60k"));
    expect(texts(container, ".recharts-cartesian-axis-tick-value")).toContain(GAP_LABEL);
  });

  it("writes a step of zero over its empty band, the level running on through it", async () => {
    const flat: DeckChartBlock = {
      ...ARR_BRIDGE,
      categories: ["Opening ARR", "New logos", "Contraction", "Churn", "Closing ARR"],
      series: [{ name: "ARR", values: [16_500_000, 1_800_000, 0, -400_000, 17_900_000] }],
    };
    const { container, onSettled } = draw(flat);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    // In bar order: the zero's value is drawn from the axis scales, apart from the bars' labels.
    const byBar = () =>
      Array.from(container.querySelectorAll<SVGTextElement>("text.deck-chart-value"))
        .sort((a, b) => Number(a.dataset.category) - Number(b.dataset.category))
        .map((text) => text.textContent);
    await waitFor(() => expect(byBar()).toEqual(["€16.5M", "+€1.8M", "€0", "-€400k", "€17.9M"]));
    // The connectors still carry the level across the band that has no bar.
    expect(container.querySelectorAll("line.deck-chart-connector")).toHaveLength(4);
  });

  it("holds a total to the steps before it the way the validator does", () => {
    // 0.06M on the 3.5M the steps moved is over 0.5% of them; 0.5% of the
    // 19M level would have let it through.
    expect(bridgeMisses(19_140_000, 19_200_000, 3_500_000, DEFAULT_WATERFALL_TOLERANCE)).toBe(true);
    expect(bridgeMisses(19_200_000, 16_500_000 + 1_800_000 + 1_300_000 - 400_000, 3_500_000, 0.5)).toBe(false);
    expect(bridgeMisses(19_215_000, 19_200_000, 3_500_000, 0.5)).toBe(false);
    // Within the tolerance a total stands at its own level, no gap drawn.
    const rounded = { ...SHORT_BRIDGE, series: [{ name: "ARR", values: [16_500_000, 1_800_000, 1_300_000, -400_000, 19_215_000] }] };
    expect(waterfallSteps(rounded, GAP_LABEL).some((step) => step.role === "gap")).toBe(false);
  });

  it("gives a toned series' tone to every bar but a gap", () => {
    const toned = { ...SHORT_BRIDGE, series: [{ ...SHORT_BRIDGE.series[0], tone: "info" as const }] };
    const payload = JSON.parse(chartExportPayload(toned, "en", GAP_LABEL));
    expect(payload.steps.map((step: { color: string }) => step.color)).toEqual([
      "var(--deck-info)",
      "var(--deck-info)",
      "var(--deck-info)",
      "var(--deck-info)",
      "var(--deck-warning)",
      "var(--deck-info)",
    ]);
  });

  it("publishes the bars it drew for the PPTX writer, which never sums the bridge itself", () => {
    const payload = JSON.parse(chartExportPayload(SHORT_BRIDGE, "en", GAP_LABEL));
    expect(payload.kind).toBe("waterfall");
    expect(payload.categories).toEqual(["Opening ARR", "New logos", "Expansion", "Churn", GAP_LABEL, "Closing ARR"]);
    expect(payload.steps.map((step: { role: string }) => step.role)).toEqual([
      "total",
      "increase",
      "increase",
      "decrease",
      "gap",
      "total",
    ]);
    expect(payload.steps[1]).toMatchObject({ start: 16_500_000, end: 18_300_000, color: "var(--deck-success)" });
    expect(payload.series).toHaveLength(1);
    expect(payload.series[0].values).toHaveLength(payload.categories.length);
    expect(payload.stacked).toBe(false);
    // A one-series bridge needs no legend.
    expect(payload.legend).toBe(false);
  });
});

const ticksOf = (container: HTMLElement, axis: "category" | "primary" | "secondary") =>
  texts(container, `.deck-chart-tick-${axis}`);

describe("two units on one figure", () => {
  it("reads the two-unit example against two labelled value axes on one grid", async () => {
    const block = catalog.dual_axis_chart as DeckChartBlock;
    const { container, onSettled } = draw(block);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());

    // The count reads on the left, the price on the right: each axis writes
    // its own unit, both cut into the same steps so they share the gridlines.
    await waitFor(() => expect(ticksOf(container, "secondary")).toEqual(["€0", "€200k", "€400k", "€600k"]));
    expect(ticksOf(container, "primary")).toEqual(["0", "20", "40", "60"]);
    expect(ticksOf(container, "category")).toEqual(block.categories);

    // Each axis wears the ink of the one series it reads.
    const ink = (axis: string) => container.querySelector(`.deck-chart-tick-${axis}`)?.getAttribute("fill");
    expect(ink("primary")).toBe("var(--deck-chart-1)");
    expect(ink("secondary")).toBe("var(--deck-chart-2)");

    // The bars are labelled as counts, the combo's line in euros at each of its points.
    expect(texts(container, '.deck-chart-value[data-series="0"]')).toEqual(["24", "34", "42", "56"]);
    expect(texts(container, '.deck-chart-value[data-series="1"]')).toEqual(["€240k", "€330k", "€420k", "€540k"]);

    // The price line is plotted against the right-hand axis: read against
    // the left-hand one (0-60), 240,000 would land far above the plot.
    const gridLines = Array.from(container.querySelectorAll(".recharts-cartesian-grid-horizontal line")).map((line) =>
      Number(line.getAttribute("y1")),
    );
    const top = Math.min(...gridLines);
    const bottom = Math.max(...gridLines);
    const dots = Array.from(container.querySelectorAll(".recharts-line-dots circle")).map((dot) =>
      Number(dot.getAttribute("cy")),
    );
    expect(dots).toHaveLength(4);
    for (const cy of dots) {
      expect(cy).toBeGreaterThanOrEqual(top - 0.5);
      expect(cy).toBeLessThanOrEqual(bottom + 0.5);
    }
    // 540k of a 600k axis sits a tenth of the plot below its top.
    expect(dots[3]).toBeCloseTo(top + (bottom - top) * 0.1, 0);

    // What the drawing publishes for the PPTX writer: both axes it settled on.
    expect(container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset).toMatchObject({
      min: "0",
      max: "60",
      step: "20",
      secondaryMin: "0",
      secondaryMax: "600000",
      secondaryStep: "200000",
    });
  });
});
