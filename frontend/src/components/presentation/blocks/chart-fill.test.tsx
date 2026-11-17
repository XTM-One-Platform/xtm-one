// A `fill` chart in a column of a columns block: the stylesheet hands
// it the height its column leaves, never 0, and the export page never waits
// on a chart that has no room at all. A `fill` callout wrapped around such a
// chart hands it the height the card takes, so the plot never stops
// at its readable minimum over an empty band.
//
// happy-dom lays nothing out, so the cascade is read through
// `getComputedStyle` (the real stylesheet, the real components) and a box
// measures the floor its `min-height` gives it - the one height a layout
// cannot take away from it. The stylesheet is read off the disk: vitest
// hands a CSS import, `?raw` included, over empty.

import { readFileSync } from "node:fs";
import { waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckBlock, DeckCalloutBlock, DeckChartBlock } from "@/types";
import { BlockList } from "./index";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

const chart = (name: string): DeckChartBlock => ({
  type: "chart",
  kind: "bar",
  categories: ["EMEA", "NA", "APAC"],
  series: [{ name, values: [4.8, 4.2, 2.4] }],
  fill: true,
});

/** A metrics grid, then two equal columns of a level-4 heading over a `fill` chart. */
const SLIDE: DeckBlock[] = [
  {
    type: "metrics_grid",
    items: [
      { value: "EUR 12.4M", label: "Revenue" },
      { value: "109%", label: "Net retention" },
    ],
  },
  {
    type: "columns",
    equal_height: true,
    columns: [
      [{ type: "heading", content: "Revenue by region", level: 4 }, chart("Q3")],
      [{ type: "heading", content: "Pipeline coverage", level: 4 }, chart("Coverage")],
    ],
  },
];

/** One cell of the Board deck's 2 x 2: a `fill` card callout titling a `fill` chart. */
const card = (name: string): DeckCalloutBlock => ({
  type: "callout",
  variant: "card",
  header: "inline",
  title: name,
  fill: true,
  children: [chart(name)],
});

/** The 2 x 2 slide as it was built: two columns blocks of two cards each. */
const TWO_BY_TWO: DeckBlock[] = [
  { type: "columns", columns: [[card("ARR")], [card("Pipeline")]] },
  { type: "columns", columns: [[card("Churn")], [card("NRR")]] },
];

const CANVAS_WIDTH = 836;

/** Every box measures `height(el)`; the chart canvas is `CANVAS_WIDTH` wide. */
function measure(height: (el: HTMLElement) => number): void {
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return height(this);
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("deck-chart-canvas") ? CANVAS_WIDTH : 0;
    },
  });
}

const floor = (el: HTMLElement) => Number.parseFloat(getComputedStyle(el).minHeight) || 0;

function renderSlide({ exportPage, blocks = SLIDE }: { exportPage: boolean; blocks?: DeckBlock[] }) {
  return renderWithProviders(
    <div className="deck-root" data-deck-static={exportPage ? "true" : undefined}>
      <BlockList blocks={blocks} className="deck-blocks deck-blocks-slide" path="blocks" />
    </div>,
  );
}

beforeAll(async () => {
  const style = document.createElement("style");
  style.textContent = stylesheet;
  document.head.appendChild(style);
  // The drawing is a lazy chunk (Recharts): loaded here, a test waits on the
  // figure, not on the first transform of the library.
  await import("./ChartCanvas");
}, 30_000);

afterEach(() => {
  Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
  Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
});

describe("a fill chart in a column of a columns block", () => {
  it("is handed the column's room and draws with a height > 0", async () => {
    measure(floor);
    const { container } = renderSlide({ exportPage: true });

    // The run hands the columns block its free height, and the block hands
    // it to each chart's column, whatever the alignment.
    const columns = container.querySelector<HTMLElement>(".deck-blocks-slide > .deck-columns");
    expect(columns).not.toBeNull();
    expect(getComputedStyle(columns as HTMLElement).flexGrow).toBe("1");
    const cells = Array.from(container.querySelectorAll<HTMLElement>(".deck-column"));
    expect(cells).toHaveLength(2);
    for (const cell of cells) expect(getComputedStyle(cell).alignSelf).toBe("stretch");

    // Inside each column: the heading keeps its height, the chart takes the
    // rest - and its canvas is never laid out under a readable plot.
    const canvases = Array.from(container.querySelectorAll<HTMLElement>(".deck-column .deck-chart-canvas"));
    expect(canvases).toHaveLength(2);
    for (const canvas of canvases) {
      const figure = canvas.closest<HTMLElement>(".deck-chart") as HTMLElement;
      expect(figure.previousElementSibling?.classList.contains("deck-heading")).toBe(true);
      expect(getComputedStyle(figure).flexGrow).toBe("1");
      expect(getComputedStyle(canvas).flexGrow).toBe("1");
      expect(floor(canvas)).toBe(160);
    }

    // So each drawing is handed a height > 0 and settles: nothing is left for
    // the export page to wait on.
    await waitFor(
      () => {
        const surfaces = Array.from(container.querySelectorAll("svg.recharts-surface"));
        expect(surfaces).toHaveLength(2);
        for (const surface of surfaces) expect(Number(surface.getAttribute("height"))).toBeGreaterThan(0);
      },
      { timeout: 5_000 },
    );
    await waitFor(() => expect(container.querySelector("[data-deck-pending]")).toBeNull());
    expect(container.querySelector("[data-deck-unfinished]")).toBeNull();
  });

  it("with no room at all, stands empty on the export page instead of holding its ready signal", async () => {
    measure(() => 0);
    const { container } = renderSlide({ exportPage: true });

    const canvases = container.querySelectorAll(".deck-chart-canvas");
    expect(canvases).toHaveLength(2);
    // Neither pending (the render would never be ready) nor unfinished (the
    // export would fail): the layout check reports the empty chart instead.
    await waitFor(() => expect(container.querySelector("[data-deck-pending]")).toBeNull());
    expect(container.querySelector("[data-deck-unfinished]")).toBeNull();
    expect(container.querySelector("svg.recharts-surface")).toBeNull();
  });

  it("with no room in a live deck, waits for room as on a slide reveal hides", () => {
    measure(() => 0);
    const { container } = renderSlide({ exportPage: false });

    expect(container.querySelectorAll(".deck-chart-canvas[data-deck-pending]")).toHaveLength(2);
  });
});

describe("a fill chart in a fill callout, 2 x 2", () => {
  it("is handed the height the card takes, through the column and the callout", async () => {
    measure(floor);
    const { container } = renderSlide({ exportPage: true, blocks: TWO_BY_TWO });

    // Each row of cards takes its share of the run, and each card's column
    // stretches to the row: the callout inside is a filler, as a bare chart is.
    const rows = Array.from(container.querySelectorAll<HTMLElement>(".deck-blocks-slide > .deck-columns"));
    expect(rows).toHaveLength(2);
    for (const row of rows) expect(getComputedStyle(row).flexGrow).toBe("1");
    const cells = Array.from(container.querySelectorAll<HTMLElement>(".deck-column"));
    expect(cells).toHaveLength(4);
    for (const cell of cells) expect(getComputedStyle(cell).alignSelf).toBe("stretch");

    // Inside each card: the callout grows in its column, its run grows in the
    // callout's body and the chart grows in the run - so
    // the canvas is the card's remaining height, never its 160px floor alone.
    const canvases = Array.from(container.querySelectorAll<HTMLElement>(".deck-callout .deck-chart-canvas"));
    expect(canvases).toHaveLength(4);
    for (const canvas of canvases) {
      const figure = canvas.closest<HTMLElement>(".deck-chart") as HTMLElement;
      const run = figure.parentElement as HTMLElement;
      const callout = run.closest<HTMLElement>(".deck-callout") as HTMLElement;
      expect(callout.dataset.fill).toBe("true");
      expect(getComputedStyle(callout).flexGrow).toBe("1");
      expect(getComputedStyle(run.parentElement as HTMLElement).flexGrow).toBe("1"); // .deck-callout-body
      expect(getComputedStyle(run).flexGrow).toBe("1");
      expect(getComputedStyle(figure).flexGrow).toBe("1");
      expect(getComputedStyle(canvas).flexGrow).toBe("1");
      expect(floor(canvas)).toBe(160);
    }

    await waitFor(
      () => {
        const surfaces = Array.from(container.querySelectorAll("svg.recharts-surface"));
        expect(surfaces).toHaveLength(4);
        for (const surface of surfaces) expect(Number(surface.getAttribute("height"))).toBeGreaterThan(0);
      },
      { timeout: 5_000 },
    );
    await waitFor(() => expect(container.querySelector("[data-deck-pending]")).toBeNull());
  });

  it("leaves the run of a callout that wraps no filling chart as it was", () => {
    // The row's own rule is a four-deep child chain inside `:has()`, which
    // happy-dom matches on its prefix alone: the row is read on a live deck
    // by `tests_e2e/presentation-chart-title.spec.ts`, the run here.
    measure(floor);
    const note: DeckCalloutBlock = {
      type: "callout",
      title: "Note",
      fill: true,
      children: [{ type: "text", content: "Read me." }],
    };
    const { container } = renderSlide({
      exportPage: true,
      blocks: [{ type: "columns", columns: [[note], [note]] }],
    });
    const run = container.querySelector<HTMLElement>(".deck-callout-body > .deck-blocks") as HTMLElement;
    expect(getComputedStyle(run).flexGrow).not.toBe("1");
    const plainChart: DeckCalloutBlock = { ...card("Plain"), children: [{ ...chart("Plain"), fill: false }] };
    const unfilled = renderSlide({ exportPage: true, blocks: [{ type: "columns", columns: [[plainChart], [plainChart]] }] });
    const unfilledRun = unfilled.container.querySelector<HTMLElement>(".deck-callout-body > .deck-blocks") as HTMLElement;
    expect(getComputedStyle(unfilledRun).flexGrow).not.toBe("1");
  });
});
