/**
 * The butterfly - `yarn test:dom`.
 *
 * Two series mirrored about a central spine: the issue's pyramid of
 * individual contributors against managers per seniority level, drawn
 * through the real `ChartCanvas` at the canvas sizes a content slide and a
 * dashboard panel give a chart, and read off the SVG Recharts renders - the
 * bars' boxes, the values, the ticks, the rungs' names - the way the PPTX
 * extractor and the layout audit read them. The figure around it (the
 * legend over the two sides, the side totals) is rendered with the real
 * stylesheet, as `chart-legend-position.test.tsx` does.
 */
import { readFileSync } from "node:fs";
import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";
import { butterflyLayout } from "./chart-butterfly";
import { chartExportPayload, writtenValues } from "./chart-format";
import { labelRun } from "./chart-layout";
import { Block, BlockList } from "./index";

/** The issue's acceptance block, word for word. */
const PYRAMID: DeckChartBlock = {
  type: "chart",
  kind: "butterfly",
  categories: ["Executive", "Director", "Manager", "Lead", "Senior", "Mid", "Entry"],
  series: [
    { name: "Contributors", values: [0, 2, 10, 40, 80, 30, 15] },
    { name: "Managers", values: [5, 20, 18, 6, 0, 0, 0] },
  ],
  show_values: true,
};

/**
 * The canvases a chart is handed (canvas px): a content slide's full row at
 * `size: md`, a span 1 panel of a dashboard with one panel row, and the same
 * panel on a two-row dashboard, where a panel is about 220px tall.
 */
const SLIDE = { width: 1670, height: 460 };
const PANEL = { width: 540, height: 330 };
const SHORT_PANEL = { width: 540, height: 220 };

function draw(block: DeckChartBlock, { width, height } = SLIDE, hidden: ReadonlySet<number> = new Set()) {
  const onSettled = vi.fn();
  const view = render(
    <div style={{ width, height }}>
      <ChartCanvas
        block={block}
        width={width}
        height={height}
        hidden={hidden}
        animate={false}
        locale="en"
        onSettled={onSettled}
        gapLabel="Unexplained"
        runningLabel="Running total"
      />
    </div>,
  );
  return { ...view, onSettled };
}

interface DrawnBar {
  /** Where the bar starts (the spine's edge) and where it ends, along the value axis. */
  from: number;
  to: number;
  middle: number;
  fill: string | null;
}

/** The visible bars, top to bottom then left to right: a spacer of the spine's column is transparent. */
function barsOf(container: HTMLElement): DrawnBar[] {
  return Array.from(container.querySelectorAll<SVGPathElement>(".recharts-bar-rectangle path.recharts-rectangle"))
    .filter((path) => path.getAttribute("fill") !== "transparent")
    .map((path) => {
      const x = Number(path.getAttribute("x"));
      const width = Number(path.getAttribute("width"));
      return {
        from: x,
        to: x + width,
        middle: Number(path.getAttribute("y")) + Number(path.getAttribute("height")) / 2,
        fill: path.getAttribute("fill"),
      };
    })
    .sort((a, b) => a.middle - b.middle || a.from - b.from);
}

interface Written {
  text: string;
  x: number;
  y: number;
  anchor: string | null;
  series: number;
  category: number;
}

function valuesOf(container: HTMLElement): Written[] {
  return Array.from(container.querySelectorAll<SVGTextElement>("text.deck-chart-value")).map((node) => ({
    text: (node.textContent ?? "").trim(),
    x: Number(node.getAttribute("x")),
    y: Number(node.getAttribute("y")),
    anchor: node.getAttribute("text-anchor"),
    series: Number(node.dataset.series),
    category: Number(node.dataset.category),
  }));
}

const texts = (container: HTMLElement, selector: string) =>
  Array.from(container.querySelectorAll(selector)).map((node) => (node.textContent ?? "").trim());

/** One side's values in category order. */
const sideTexts = (written: readonly Written[], series: number) =>
  written.filter((value) => value.series === series).sort((a, b) => a.category - b.category).map((value) => value.text);

describe("the butterfly's room", () => {
  it("draws both sides to one extent on one scale, the spine between them, and writes every tick as a size", () => {
    const layout = butterflyLayout(PYRAMID, SLIDE.width, SLIDE.height, new Set(), "en");
    expect(layout.spine).toBe("center");
    expect(layout.left.max).toBe(80);
    expect(layout.right.max).toBe(80);
    // The two sides are as wide as each other, the spine's column between them.
    expect(layout.spineLeft - layout.plot.left).toBeCloseTo(layout.plot.right - layout.spineRight, 6);
    expect(layout.spineRight - layout.spineLeft).toBeGreaterThan(0);
    // The axis the PPTX writer sets: the left side's extent below zero, one step.
    expect(layout.axis).toMatchObject({ min: -80, max: 80, step: layout.step });
    // Every tick a size: no sign anywhere, a zero on each edge of the spine's column.
    const labels = [...layout.tickLabels.values()];
    expect(labels.some((label) => label.includes("-"))).toBe(false);
    expect(labels.filter((label) => label === "0")).toHaveLength(2);
    expect(layout.values).toMatchObject({ asked: 10, written: 10, reason: null });
  });

  it("plans no value for a zero, on either side: a rung that side lacks", () => {
    const layout = butterflyLayout({ ...PYRAMID, show_totals: true }, SLIDE.width, SLIDE.height, new Set(), "en");
    const planned = (side: number) => layout.marks.filter((mark) => mark.side === side).map((mark) => [mark.category, mark.text]);
    // Executive has no contributor; Senior, Mid and Entry have no manager.
    expect(planned(0)).toEqual([[1, "2"], [2, "10"], [3, "40"], [4, "80"], [5, "30"], [6, "15"]]);
    expect(planned(1)).toEqual([[0, "5"], [1, "20"], [2, "18"], [3, "6"]]);
    expect(layout.marks.some((mark) => mark.text === "0")).toBe(false);
    // The zeros were never asked: nothing is reported hidden for them.
    expect(layout.values).toMatchObject({ asked: 10, written: 10, reason: null });
    // A side of zeros still has its axis, and both sides their zero tick.
    const empty = { ...PYRAMID, series: [PYRAMID.series[0], { ...PYRAMID.series[1], values: [0, 0, 0, 0, 0, 0, 0] }] };
    const lopsided = butterflyLayout(empty, SLIDE.width, SLIDE.height, new Set(), "en");
    expect(lopsided.marks.every((mark) => mark.side === 0)).toBe(true);
    expect([...lopsided.tickLabels.values()].filter((label) => label === "0")).toHaveLength(2);
  });

  it("ends each side at its own largest value when symmetric is off, on the same scale", () => {
    const symmetric = butterflyLayout(PYRAMID, SLIDE.width, SLIDE.height, new Set(), "en");
    const own = butterflyLayout({ ...PYRAMID, symmetric: false }, SLIDE.width, SLIDE.height, new Set(), "en");
    expect(own.left.max).toBe(80);
    expect(own.right.max).toBe(20);
    expect(own.step).toBe(symmetric.step);
    expect(own.axis).toMatchObject({ min: -80, max: 20 });
    // One scale: a step is as long on either side.
    const stepLeft = (own.spineLeft - own.plot.left) / (own.left.max / own.step);
    const stepRight = (own.plot.right - own.spineRight) / (own.right.max / own.step);
    expect(stepLeft).toBeCloseTo(stepRight, 6);
    // The spine moves toward the smaller side.
    expect(own.spineLeft - own.plot.left).toBeGreaterThan(own.plot.right - own.spineRight);
  });

  it("writes the rungs on the left axis with spine left, the two sides meeting at the spine", () => {
    const layout = butterflyLayout({ ...PYRAMID, spine: "left" }, SLIDE.width, SLIDE.height, new Set(), "en");
    expect(layout.gap).toBe(0);
    expect(layout.spineLeft).toBe(layout.spineRight);
    expect(layout.categoryAnchor).toBe("end");
    expect(layout.categoryX).toBeLessThan(layout.plot.left);
    // The sides share their zero: one tick there.
    expect([...layout.tickLabels.entries()].filter(([, label]) => label === "0")).toEqual([[0, "0"]]);
  });

  it("sets the rungs' names smaller before it leaves one out, in a short panel", () => {
    const layout = butterflyLayout(PYRAMID, SHORT_PANEL.width, SHORT_PANEL.height, new Set(), "en");
    expect(layout.categorySize).toBeLessThan(layout.tickSize);
    expect(layout.categoryStep).toBe(1);
    expect(layout.values.written).toBe(layout.values.asked);
  });

  it("counts the values it cannot write, with the reason the layout check names", () => {
    // Seven rows in 120px: a value is taller than its row at every size.
    const crowded = butterflyLayout(PYRAMID, SLIDE.width, 120, new Set(), "en");
    expect(crowded.values).toMatchObject({ outer: null, asked: 10, written: 0, reason: "narrow" });
    expect(crowded.values.need).toBeGreaterThan(crowded.values.room);
    // Amounts like €16.8M past the bars of a 240px canvas leave the sides no room, even at 12px.
    const amounts = {
      ...PYRAMID,
      format: "currency" as const,
      series: PYRAMID.series.map((entry) => ({ ...entry, values: entry.values.map((value) => value * 210_000) })),
    };
    const narrow = butterflyLayout(amounts, 240, SLIDE.height, new Set(), "en");
    expect(narrow.values).toMatchObject({ outer: null, written: 0, reason: "short" });
    expect(narrow.values.need).toBeGreaterThan(narrow.values.room);
    // The same figures across a slide's row are written.
    expect(butterflyLayout(amounts, SLIDE.width, SLIDE.height, new Set(), "en").values.written).toBe(10);
  });

  it("asks only for the values of a visible side that writes them", () => {
    expect(butterflyLayout({ ...PYRAMID, show_values: false }, SLIDE.width, SLIDE.height, new Set(), "en").values.asked).toBe(0);
    const quiet = { ...PYRAMID, series: [PYRAMID.series[0], { ...PYRAMID.series[1], show_values: false }] };
    expect(butterflyLayout(quiet, SLIDE.width, SLIDE.height, new Set(), "en").values.asked).toBe(6);
    expect(butterflyLayout(PYRAMID, SLIDE.width, SLIDE.height, new Set([0]), "en").values.asked).toBe(4);
  });
});

describe("the butterfly's drawing", () => {
  it.each([
    ["a content slide", SLIDE],
    ["a dashboard panel", PANEL],
    ["a short dashboard panel", SHORT_PANEL],
  ])("draws the issue's pyramid mirrored on %s, every value unsigned", async (_where, size) => {
    const { container, onSettled } = draw(PYRAMID, size);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    const layout = butterflyLayout(PYRAMID, size.width, size.height, new Set(), "en");

    // The first series runs leftwards from the spine's left edge, the second
    // rightwards from its right edge, both sides of a rung on one row. A zero
    // draws no bar and writes no value.
    const bars = barsOf(container);
    const left = bars.filter((bar) => bar.fill === "var(--deck-chart-1)");
    const right = bars.filter((bar) => bar.fill === "var(--deck-chart-2)");
    expect(left).toHaveLength(6);
    expect(right).toHaveLength(4);
    for (const bar of left) {
      expect(bar.from).toBeCloseTo(layout.spineLeft, 0);
      expect(bar.to).toBeLessThan(bar.from);
    }
    for (const bar of right) {
      expect(bar.from).toBeCloseTo(layout.spineRight, 0);
      expect(bar.to).toBeGreaterThan(bar.from);
    }
    const length = (bar: DrawnBar) => Math.abs(bar.to - bar.from);
    // One scale: Lead's 40 contributors are twice Director's 20 managers, and the rows line up.
    const leadLeft = left.find((bar) => Math.abs(bar.middle - left[2].middle) < 1) as DrawnBar;
    const directorRight = right[1];
    expect(length(leadLeft) / length(directorRight)).toBeCloseTo(40 / 20, 1);
    expect(left[0].middle).toBeCloseTo(right[1].middle, 0);

    // Every value written as the size it was given, top to bottom - but a
    // zero: no "0" against the spine on Executive's left nor on Senior's,
    // Mid's and Entry's right.
    await waitFor(() => expect(container.querySelectorAll("text.deck-chart-value")).toHaveLength(10));
    const written = valuesOf(container);
    expect(sideTexts(written, 0)).toEqual(["2", "10", "40", "80", "30", "15"]);
    expect(sideTexts(written, 1)).toEqual(["5", "20", "18", "6"]);
    expect(written.filter((value) => value.series === 0).map((value) => value.category)).not.toContain(0);
    expect(written.filter((value) => value.series === 1).map((value) => value.category).sort()).toEqual([0, 1, 2, 3]);
    expect(written.some((value) => value.text === "0" || value.text.includes("-"))).toBe(false);
    // Past the outer end of its bar, reading away from the spine, inside the canvas.
    const outerValue = layout.values.outer?.size ?? 0;
    for (const value of written) {
      if (value.series === 0) {
        expect(value.anchor).toBe("end");
        expect(value.x).toBeLessThan(layout.spineLeft);
        expect(value.x - labelRun(value.text, outerValue)).toBeGreaterThanOrEqual(0);
      } else {
        expect(value.anchor).toBe("start");
        expect(value.x).toBeGreaterThan(layout.spineRight);
        expect(value.x + labelRun(value.text, outerValue)).toBeLessThanOrEqual(size.width);
      }
    }

    // The rungs' names on the spine, in the order given, top to bottom.
    const names = Array.from(container.querySelectorAll<SVGTextElement>("text.deck-chart-tick-category"));
    expect(names.map((node) => node.textContent)).toEqual(PYRAMID.categories);
    for (const node of names) expect(Number(node.getAttribute("x"))).toBeCloseTo(layout.spineCenter, 6);
    const rows = names.map((node) => Number(node.getAttribute("y")));
    expect([...rows].sort((a, b) => a - b)).toEqual(rows);

    // The ticks: sizes on both sides, never a sign.
    const ticks = texts(container, ".deck-chart-tick-primary");
    expect(ticks.length).toBeGreaterThanOrEqual(4);
    expect(ticks.some((tick) => tick.includes("-"))).toBe(false);
    expect(ticks.filter((tick) => tick === "80")).toHaveLength(2);
  });

  it("publishes the axis and the bars it settled on for the PPTX writer", async () => {
    const { container, onSettled } = draw(PYRAMID);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    const facts = container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset;
    expect(facts).toMatchObject({ min: "-80", max: "80", values: "true", valuesAsked: "10", valuesWritten: "10", barGap: "0" });
    expect(Number(facts?.barWidth)).toBeGreaterThan(0);
    // The payload carries the sizes as given; the writer stores the left side negative.
    const payload = JSON.parse(chartExportPayload(PYRAMID, "en", "Unexplained"));
    expect(payload).toMatchObject({ kind: "butterfly", horizontal: true, stacked: false, spine: "center", legend: true, target: null });
    expect(payload.series.map((entry: { values: number[] }) => entry.values)).toEqual(PYRAMID.series.map((entry) => entry.values));
    expect(JSON.parse(chartExportPayload({ ...PYRAMID, spine: "left" }, "en", "")).spine).toBe("left");
  });

  it("draws the rungs on the left axis and a rule where the sides meet, with spine left", async () => {
    const block = { ...PYRAMID, spine: "left" as const };
    const { container, onSettled } = draw(block);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    const layout = butterflyLayout(block, SLIDE.width, SLIDE.height, new Set(), "en");
    const names = Array.from(container.querySelectorAll<SVGTextElement>("text.deck-chart-tick-category"));
    expect(names.map((node) => node.getAttribute("text-anchor"))).toEqual(Array(7).fill("end"));
    for (const node of names) expect(Number(node.getAttribute("x"))).toBeLessThan(layout.plot.left);
    const spine = container.querySelector<SVGLineElement>("line.deck-chart-spine");
    expect(Number(spine?.getAttribute("x1"))).toBeCloseTo(layout.spineLeft, 0);
    for (const bar of barsOf(container)) expect(bar.from).toBeCloseTo(layout.spineLeft, 0);
    // The left side's values stay clear of the rungs' names.
    await waitFor(() => expect(container.querySelectorAll("text.deck-chart-value")).toHaveLength(10));
    const nameEdge = Math.max(...names.map((node) => Number(node.getAttribute("x"))));
    for (const value of valuesOf(container).filter((entry) => entry.series === 0)) {
      expect(value.x - labelRun(value.text, layout.values.outer?.size ?? 0)).toBeGreaterThan(nameEdge);
    }
  });

  it("leaves a hidden side, and a side whose values are off, unwritten", async () => {
    const hidden = draw(PYRAMID, SLIDE, new Set([0]));
    await waitFor(() => expect(hidden.onSettled).toHaveBeenCalled());
    expect(barsOf(hidden.container).every((bar) => bar.fill === "var(--deck-chart-2)")).toBe(true);
    await waitFor(() => expect(sideTexts(valuesOf(hidden.container), 1)).toHaveLength(4));
    expect(sideTexts(valuesOf(hidden.container), 0)).toEqual([]);
    hidden.unmount();

    const silent = draw({ ...PYRAMID, show_values: false });
    await waitFor(() => expect(silent.onSettled).toHaveBeenCalled());
    expect(silent.container.querySelectorAll("text.deck-chart-value")).toHaveLength(0);
    expect(silent.container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset.values).toBe("false");
  });

  it("takes a toned series' tone and the palette for the other, as every chart does", async () => {
    const toned = { ...PYRAMID, series: [{ ...PYRAMID.series[0], tone: "muted" as const }, PYRAMID.series[1]] };
    const { container, onSettled } = draw(toned);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(new Set(barsOf(container).map((bar) => bar.fill))).toEqual(new Set(["var(--deck-muted)", "var(--deck-chart-1)"]));
  });

  it("counts the side totals in the run its values take their decimals from", () => {
    expect(writtenValues({ ...PYRAMID, show_totals: true }).slice(-2)).toEqual([177, 49]);
    expect(writtenValues(PYRAMID)).toHaveLength(14);
  });

  it("leaves every other kind's zeros as they were: a line's point at 0 still writes its 0", async () => {
    const line: DeckChartBlock = {
      type: "chart",
      kind: "line",
      categories: ["Q1", "Q2", "Q3"],
      series: [{ name: "Hires", values: [12, 0, 18] }],
      show_values: true,
    };
    const { container, onSettled } = draw(line);
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelectorAll("text.deck-chart-value")).toHaveLength(3));
    expect(sideTexts(valuesOf(container), 0)).toEqual(["12", "0", "18"]);
  });
});

/* ── The figure: its legend over the two sides, and the side totals ─────────── */

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");
const style = (element: Element | null) => getComputedStyle(element as Element);

/** The chart canvas measures `width` x `height`; every other box nothing. */
function measure(width: number, height: number): void {
  for (const [property, value] of [
    ["clientWidth", width],
    ["clientHeight", height],
  ] as const) {
    Object.defineProperty(HTMLElement.prototype, property, {
      configurable: true,
      get(this: HTMLElement) {
        return this.classList.contains("deck-chart-canvas") ? value : 0;
      },
    });
  }
}

function renderOnSlide(block: DeckChartBlock) {
  return renderWithProviders(
    <div className="deck-root" data-deck-static="true">
      <div data-scheme="dark">
        <BlockList blocks={[block]} className="deck-blocks deck-blocks-slide" path="blocks" />
      </div>
    </div>,
  );
}

function renderInPanel(block: DeckChartBlock) {
  return renderWithProviders(
    <div className="deck-root" data-deck-static="true">
      <div data-scheme="light">
        <article className="deck-panel">
          <h3 className="deck-panel-title">Seniority mix</h3>
          <div className="deck-panel-body">
            <Block block={block} path="panels.0.block" />
          </div>
        </article>
      </div>
    </div>,
  );
}

describe("the butterfly's legend", () => {
  beforeAll(async () => {
    const sheet = document.createElement("style");
    sheet.textContent = stylesheet;
    document.head.appendChild(sheet);
    await import("./ChartCanvas");
  }, 30_000);

  afterEach(() => {
    Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
    Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
  });

  it("lays its legend over the two sides on a slide, the first series at the left end, the second at the right", () => {
    measure(SLIDE.width, SLIDE.height);
    const { container } = renderOnSlide({ ...PYRAMID, title: "Seniority mix" });
    const figure = container.querySelector<HTMLElement>("figure.deck-chart");
    const legend = container.querySelector<HTMLElement>(".deck-chart-legend");
    expect(figure?.dataset.legendSplit).toBe("true");
    expect(legend?.dataset.split).toBe("true");
    expect(style(legend).justifyContent).toBe("space-between");
    expect(texts(container, ".deck-chart-legend-name")).toEqual(["Contributors", "Managers"]);
    // Its own row between the head - the title alone, across it - and the canvas.
    const order = Array.from(figure?.children ?? []).map((child) => child.className);
    expect(order.slice(0, 3)).toEqual(["deck-chart-head", "deck-chart-legend", "deck-chart-canvas"]);
    expect(container.querySelector(".deck-chart-head .deck-chart-legend")).toBeNull();
    expect(style(container.querySelector(".deck-chart-title")).gridColumn).toBe("1 / -1");
  });

  it("gives a panel's split legend a row of its own across the panel, the canvas under it", () => {
    measure(PANEL.width, PANEL.height);
    const { container } = renderInPanel(PYRAMID);
    const legend = container.querySelector(".deck-chart-legend");
    expect(style(container.querySelector(".deck-panel")).gridTemplateRows).toBe("auto auto minmax(0, 1fr)");
    expect(style(container.querySelector(".deck-panel-title")).gridColumn).toBe("1 / -1");
    expect(style(legend).gridColumn).toBe("1 / -1");
    expect(style(legend).gridRow).toBe("2");
    expect(style(legend).justifyContent).toBe("space-between");
    expect(style(container.querySelector(".deck-chart-canvas")).gridRow).toBe("3");
  });

  it("keeps the usual legend where the block places it", () => {
    measure(SLIDE.width, SLIDE.height);
    const { container } = renderOnSlide({ ...PYRAMID, legend_position: "bottom" });
    expect(container.querySelector<HTMLElement>("figure.deck-chart")?.dataset.legendSplit).toBeUndefined();
    expect(container.querySelector<HTMLElement>(".deck-chart-legend")?.dataset.split).toBeUndefined();
    expect(container.querySelector<HTMLElement>("figure.deck-chart")?.dataset.legendPosition).toBe("bottom");
  });

  it("writes each side's total under its entry with show_totals, the right one right-aligned", async () => {
    measure(SLIDE.width, SLIDE.height);
    const { container } = renderOnSlide({ ...PYRAMID, show_totals: true });
    await waitFor(() => expect(texts(container, ".deck-chart-legend-total")).toEqual(["Total 177", "Total 49"]));
    const entries = container.querySelectorAll<HTMLElement>(".deck-chart-legend > li");
    expect(style(entries[0]).flexDirection).toBe("column");
    expect(style(entries[0]).alignItems).toBe("flex-start");
    expect(style(entries[1]).alignItems).toBe("flex-end");
    // Under the usual legend too, and nowhere without one.
    const placed = renderOnSlide({ ...PYRAMID, show_totals: true, legend_position: "right" });
    expect(texts(placed.container, ".deck-chart-legend-total")).toEqual(["Total 177", "Total 49"]);
    const bare = renderOnSlide({ ...PYRAMID, show_totals: true, show_legend: false });
    expect(bare.container.querySelectorAll(".deck-chart-legend-total")).toHaveLength(0);
  });
});
