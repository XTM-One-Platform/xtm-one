// A chart's title: drawn in the figure's own frame the way a dashboard
// panel draws its title - a head row first, the title on the left and the
// legend on the right of the same row, the canvas under both - so a `fill`
// chart's plot takes the rest of the frame, instead of a callout wrapped
// around the chart to title it. A chart without a title renders the DOM it
// always had; in a dashboard panel the panel titles the chart (the validator
// refuses a second title) and the stage draws such a draft as the panel.
//
// happy-dom lays nothing out, so the cascade is read through `getComputedStyle`
// (the real stylesheet, the real components), as in `chart-fill.test.tsx`.

import { readFileSync } from "node:fs";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckChartBlock } from "@/types";
import { Block, BlockList } from "./index";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug"];

/** The 8-point line chart of the brief, with a second series so a legend shows. */
const chart = (fields: Partial<DeckChartBlock> = {}): DeckChartBlock => ({
  type: "chart",
  kind: "line",
  categories: MONTHS,
  series: [
    { name: "Signups", values: [120, 135, 150, 149, 170, 182, 190, 205] },
    { name: "Plan", values: [120, 130, 140, 150, 160, 170, 180, 190] },
  ],
  fill: true,
  ...fields,
});

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
      <div data-scheme="light">
        <BlockList blocks={[block]} className="deck-blocks deck-blocks-slide" path="blocks" />
      </div>
    </div>,
  );
}

function renderInPanel(block: DeckChartBlock, panelTitle: string) {
  return renderWithProviders(
    <div className="deck-root" data-deck-static="true">
      <div data-scheme="light">
        <article className="deck-panel">
          <h3 className="deck-panel-title">{panelTitle}</h3>
          <div className="deck-panel-body">
            <Block block={block} path="panels.0.block" />
          </div>
        </article>
      </div>
    </div>,
  );
}

const figureOf = (container: HTMLElement): HTMLElement => {
  const figure = container.querySelector<HTMLElement>("figure.deck-chart");
  expect(figure).not.toBeNull();
  return figure as HTMLElement;
};

const TYPE = ["fontFamily", "fontSize", "fontWeight", "lineHeight", "color"] as const;

beforeAll(async () => {
  const style = document.createElement("style");
  style.textContent = stylesheet;
  document.head.appendChild(style);
  await import("./ChartCanvas");
}, 30_000);

afterEach(() => {
  Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
  Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
});

describe("a chart's title", () => {
  it("opens the figure with a head: the title first, set like a dashboard panel's title", () => {
    measure(640, 360);
    const { container } = renderOnSlide(chart({ title: "Monthly signups", caption: "Self-serve, EU region" }));
    const figure = figureOf(container);
    const head = figure.firstElementChild as HTMLElement;
    expect(head.classList.contains("deck-chart-head")).toBe(true);
    const title = head.firstElementChild as HTMLElement;
    expect(title.tagName).toBe("H3");
    expect(title.classList.contains("deck-chart-title")).toBe(true);
    expect(title.textContent).toBe("Monthly signups");
    // Then the canvas, then the caption under the plot, as before.
    expect(head.nextElementSibling?.classList.contains("deck-chart-canvas")).toBe(true);
    expect(figure.lastElementChild?.tagName).toBe("FIGCAPTION");
    // The same type as the panel title it is styled after: one rule for both.
    const panel = renderInPanel(chart(), "Pipeline");
    const panelTitle = getComputedStyle(panel.container.querySelector(".deck-panel-title") as HTMLElement);
    const ownTitle = getComputedStyle(title);
    for (const property of TYPE) expect(ownTitle[property]).toBe(panelTitle[property]);
    expect(ownTitle.fontWeight).toBe("500");
    panel.unmount();
  });

  it("sets the legend on the title's row, on the right, and leaves the rest of the frame to the canvas", () => {
    measure(640, 360);
    const { container } = renderOnSlide(chart({ title: "Monthly signups" }));
    const figure = figureOf(container);
    const head = figure.querySelector(".deck-chart-head") as HTMLElement;
    const headStyle = getComputedStyle(head);
    expect(headStyle.display).toBe("grid");
    expect(headStyle.gridTemplateColumns).toBe("fit-content(60%) minmax(0, 1fr)");
    // The head keeps its own height in the figure's column; the canvas grows.
    expect(headStyle.flexGrow).toBe("0");
    expect(headStyle.flexShrink).toBe("0");
    const title = getComputedStyle(figure.querySelector(".deck-chart-title") as HTMLElement);
    expect([title.gridColumn, title.gridRow]).toEqual(["1", "1"]);
    const legend = head.querySelector(".deck-chart-legend") as HTMLElement;
    expect(legend).not.toBeNull();
    const legendStyle = getComputedStyle(legend);
    expect([legendStyle.gridColumn, legendStyle.gridRow, legendStyle.justifyContent]).toEqual(["2", "1", "flex-end"]);
    const canvas = getComputedStyle(figure.querySelector(".deck-chart-canvas") as HTMLElement);
    expect(canvas.flexGrow).toBe("1");
    // A `fill` chart's canvas has no height of its own: the frame's rest is its room.
    expect(canvas.height).toBe("auto");
    expect(getComputedStyle(figure).display).toBe("flex");
    expect(getComputedStyle(figure).flexDirection).toBe("column");
  });

  it("changes nothing for a chart without a title", () => {
    measure(640, 360);
    const { container } = renderOnSlide(chart({ caption: "Self-serve, EU region" }));
    const figure = figureOf(container);
    expect(figure.querySelector(".deck-chart-head")).toBeNull();
    expect(figure.querySelector(".deck-chart-title")).toBeNull();
    expect(figure.firstElementChild?.classList.contains("deck-chart-legend")).toBe(true);
    expect(figure.children[1].classList.contains("deck-chart-canvas")).toBe(true);
  });

  it("is drawn as the panel the save keeps when a draft carries one in a dashboard panel", () => {
    measure(640, 360);
    const { container } = renderInPanel(chart({ title: "Monthly signups" }), "Signups");
    const figure = figureOf(container);
    expect(getComputedStyle(figure).display).toBe("contents");
    // The head is lifted away like the figure, so the legend is a panel grid
    // item in the panel's first row, and the chart's own title is not drawn.
    expect(getComputedStyle(figure.querySelector(".deck-chart-head") as HTMLElement).display).toBe("contents");
    expect(getComputedStyle(figure.querySelector(".deck-chart-title") as HTMLElement).display).toBe("none");
    const legend = getComputedStyle(figure.querySelector(".deck-chart-legend") as HTMLElement);
    expect([legend.gridColumn, legend.gridRow]).toEqual(["2", "1"]);
    expect(getComputedStyle(container.querySelector(".deck-panel-title") as HTMLElement).display).not.toBe("none");
  });
});
