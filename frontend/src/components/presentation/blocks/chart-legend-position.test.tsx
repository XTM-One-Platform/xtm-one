// Where a chart's legend sits. `legend_position` moves the legend
// under the plot (`bottom`: one centred line, the plot taking the row the
// legend held at the top) or beside it (`right`: a column of entries), on a
// slide and in a dashboard panel alike; unset, the figure is the one it always
// was - the legend first in its column, on the title's row in a panel - and
// the entries, their toggles and their swatches are the same wherever it goes.
//
// happy-dom lays nothing out, so the cascade is read through `getComputedStyle`
// (the real stylesheet, the real components), as in `chart-fill.test.tsx`: the
// grid placements and the flex direction the stylesheet gives each part.

import { readFileSync } from "node:fs";
import { fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckChartBlock } from "@/types";
import { Block, BlockList } from "./index";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

/** The issue's panel: a stacked bar of three versions, too long a legend for a one-column panel's title row. */
const chart = (fields: Partial<DeckChartBlock> = {}): DeckChartBlock => ({
  type: "chart",
  kind: "stacked_bar",
  categories: ["Q1", "Q2", "Q3", "Q4"],
  series: [
    { name: "v1", values: [60, 40, 30, 20] },
    { name: "v2", values: [90, 150, 170, 100] },
    { name: "v3", values: [0, 0, 0, 180] },
  ],
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

function renderInPanel(block: DeckChartBlock) {
  return renderWithProviders(
    <div className="deck-root" data-deck-static="true">
      <div data-scheme="light">
        <article className="deck-panel">
          <h3 className="deck-panel-title">Installs by version</h3>
          <div className="deck-panel-body">
            <Block block={block} path="panels.0.block" />
          </div>
        </article>
      </div>
    </div>,
  );
}

interface Parts {
  figure: HTMLElement;
  legend: HTMLElement | null;
  canvas: HTMLElement;
  caption: HTMLElement | null;
  /** The figure's children in DOM order, by class. */
  order: string[];
}

function partsOf(container: HTMLElement): Parts {
  const figure = container.querySelector<HTMLElement>("figure.deck-chart");
  const canvas = container.querySelector<HTMLElement>(".deck-chart-canvas");
  expect(figure).not.toBeNull();
  expect(canvas).not.toBeNull();
  return {
    figure: figure as HTMLElement,
    legend: container.querySelector<HTMLElement>(".deck-chart-legend"),
    canvas: canvas as HTMLElement,
    caption: container.querySelector<HTMLElement>("figure.deck-chart > figcaption"),
    order: Array.from((figure as HTMLElement).children).map((child) =>
      child.tagName === "FIGCAPTION"
        ? "caption"
        : child.classList.contains("deck-chart-legend")
          ? "legend"
          : child.classList.contains("deck-chart-head")
            ? "head"
            : "canvas",
    ),
  };
}

const style = (element: Element | null) => getComputedStyle(element as Element);

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

describe("where a chart's legend sits", () => {
  it("unset, keeps the legend first in the column and on the panel title's row, with no position on the figure", () => {
    measure(0, 0);
    const slide = partsOf(renderOnSlide(chart({ caption: "Installs" })).container);
    expect(slide.figure.hasAttribute("data-legend-position")).toBe(false);
    expect(slide.order).toEqual(["legend", "canvas", "caption"]);
    expect(style(slide.figure).display).toBe("flex");
    expect(style(slide.figure).flexDirection).toBe("column");
    expect(style(slide.legend).justifyContent).not.toBe("center");
    expect(style(slide.legend).flexDirection).not.toBe("column");

    const { container } = renderInPanel(chart());
    const panel = partsOf(container);
    const article = container.querySelector<HTMLElement>(".deck-panel") as HTMLElement;
    expect(panel.figure.hasAttribute("data-legend-position")).toBe(false);
    expect(style(panel.figure).display).toBe("contents");
    expect(style(article.querySelector(".deck-panel-title")).gridColumn).toBe("1");
    expect(style(panel.legend).gridRow).toBe("1");
    expect(style(panel.legend).gridColumn).toBe("2");
    expect(style(panel.legend).justifyContent).toBe("flex-end");
    expect(style(panel.canvas).gridRow).toBe("2");
    expect(style(panel.canvas).gridColumn).toBe("1 / -1");
    expect(style(article).rowGap).toBe("16px");
  });

  it("bottom, on a slide, puts the legend after the canvas on a centred line, the figure still a column", () => {
    measure(0, 0);
    const { figure, legend, order } = partsOf(renderOnSlide(chart({ legend_position: "bottom", caption: "Installs" })).container);
    expect(figure.getAttribute("data-legend-position")).toBe("bottom");
    expect(order).toEqual(["canvas", "legend", "caption"]);
    expect(style(figure).display).toBe("flex");
    expect(style(figure).flexDirection).toBe("column");
    expect(style(legend).justifyContent).toBe("center");
    // Still a row of entries that may wrap - centred - when it does not fit.
    expect(style(legend).flexWrap).toBe("wrap");
    expect(style(legend).flexDirection).not.toBe("column");
  });

  it("bottom, in a panel, gives the title its row alone, the canvas the next and the legend a centred line under it", () => {
    measure(0, 0);
    const { container } = renderInPanel(chart({ legend_position: "bottom", caption: "Installs" }));
    const { figure, legend, canvas, caption, order } = partsOf(container);
    const article = container.querySelector<HTMLElement>(".deck-panel") as HTMLElement;
    expect(figure.getAttribute("data-legend-position")).toBe("bottom");
    expect(style(figure).display).toBe("contents");
    expect(style(article.querySelector(".deck-panel-title")).gridColumn).toBe("1 / -1");
    expect(style(canvas).gridRow).toBe("2");
    expect(style(canvas).gridColumn).toBe("1 / -1");
    expect(style(legend).gridRow).toBe("3");
    expect(style(legend).gridColumn).toBe("1 / -1");
    expect(style(legend).justifyContent).toBe("center");
    expect(style(caption).gridColumn).toBe("1 / -1");
    expect(order).toEqual(["canvas", "legend", "caption"]);
    // The rows pack closer so the plot keeps the height the title row gave back.
    expect(style(article).rowGap).toBe("8px");
  });

  it("right, on a slide, makes the figure a two-column grid with a column of entries beside the canvas", () => {
    measure(0, 0);
    const donut = chart({
      kind: "donut",
      categories: ["EMEA", "Americas", "APAC", "Other"],
      series: [{ name: "Share", values: [46, 28, 16, 10] }],
      legend_position: "right",
      caption: "Share",
    });
    const { figure, legend, canvas, caption, order } = partsOf(renderOnSlide(donut).container);
    expect(figure.getAttribute("data-legend-position")).toBe("right");
    expect(order).toEqual(["canvas", "legend", "caption"]);
    expect(style(figure).display).toBe("grid");
    expect(style(figure).gridTemplateColumns).toBe("minmax(0, 1fr) auto");
    // Two rows without a head: the canvas's `1fr` row, the caption's.
    expect(style(figure).gridTemplateRows).toBe("minmax(0, 1fr) auto");
    expect(style(canvas).gridColumn).toBe("1");
    expect(style(legend).gridColumn).toBe("2");
    expect(style(legend).flexDirection).toBe("column");
    expect(style(legend).flexWrap).toBe("nowrap");
    expect(style(caption).gridColumn).toBe("1 / -1");
  });

  it("with a title, keeps the title alone in the head and sends the legend where the block says", () => {
    measure(0, 0);
    const under = partsOf(renderOnSlide(chart({ title: "Installs by version", legend_position: "bottom" })).container);
    const head = under.figure.querySelector<HTMLElement>(":scope > .deck-chart-head") as HTMLElement;
    expect(head).not.toBeNull();
    expect(head.querySelector(".deck-chart-title")?.textContent).toBe("Installs by version");
    expect(head.querySelector(".deck-chart-legend")).toBeNull();
    expect(style(head.querySelector(".deck-chart-title")).gridColumn).toBe("1 / -1");
    expect(under.order).toEqual(["head", "canvas", "legend"]);

    const beside = partsOf(renderOnSlide(chart({ title: "Share", legend_position: "right", caption: "Share" })).container);
    const titled = beside.figure.querySelector<HTMLElement>(":scope > .deck-chart-head") as HTMLElement;
    expect(titled.querySelector(".deck-chart-legend")).toBeNull();
    expect(style(beside.figure).display).toBe("grid");
    // Three rows with a head: the head's, the canvas's `1fr` row, the caption's.
    expect(style(beside.figure).gridTemplateRows).toBe("auto minmax(0, 1fr) auto");
    expect(style(titled).gridColumn).toBe("1 / -1");
    expect(style(beside.legend).gridColumn).toBe("2");
    expect(style(beside.legend).flexDirection).toBe("column");

    // At the top (unset), the legend stays in the head beside the title.
    const top = partsOf(renderOnSlide(chart({ title: "Installs by version" })).container);
    const headTop = top.figure.querySelector<HTMLElement>(":scope > .deck-chart-head") as HTMLElement;
    expect(headTop.querySelector(".deck-chart-legend")).not.toBeNull();
    expect(top.figure.hasAttribute("data-legend-position")).toBe(false);
    expect(style(headTop.querySelector(".deck-chart-title")).gridColumn).toBe("1");
  });

  it("right, in a panel, puts the canvas and a column of entries side by side under a full-width title", () => {
    measure(0, 0);
    const { container } = renderInPanel(chart({ legend_position: "right" }));
    const { figure, legend, canvas } = partsOf(container);
    const article = container.querySelector<HTMLElement>(".deck-panel") as HTMLElement;
    expect(style(figure).display).toBe("contents");
    expect(style(article).gridTemplateColumns).toBe("minmax(0, 1fr) auto");
    expect(style(article.querySelector(".deck-panel-title")).gridColumn).toBe("1 / -1");
    expect(style(canvas).gridRow).toBe("2");
    expect(style(canvas).gridColumn).toBe("1");
    expect(style(legend).gridRow).toBe("2");
    expect(style(legend).gridColumn).toBe("2");
    expect(style(legend).flexDirection).toBe("column");
    expect(style(legend).alignSelf).toBe("center");
  });

  it("carries no position on a figure that draws no legend, so a placement alone changes nothing", () => {
    measure(0, 0);
    const silent = partsOf(renderOnSlide(chart({ legend_position: "bottom", show_legend: false })).container);
    expect(silent.legend).toBeNull();
    expect(silent.figure.hasAttribute("data-legend-position")).toBe(false);
    const gauge = chart({
      kind: "gauge",
      format: "percent",
      categories: ["NRR"],
      series: [{ name: "NRR", values: [86] }],
      legend_position: "right",
    });
    const dial = partsOf(renderOnSlide(gauge).container);
    expect(dial.legend).toBeNull();
    expect(dial.figure.hasAttribute("data-legend-position")).toBe(false);
    expect(style(dial.figure).display).toBe("flex");
  });

  it("keeps the same entries, target line and toggles in a moved legend, and publishes the side for the export", async () => {
    measure(640, 360);
    const { container } = renderOnSlide(chart({ legend_position: "bottom", target: 300, goal_label: "Plan" }));
    const { figure, legend } = partsOf(container);
    const names = Array.from((legend as HTMLElement).querySelectorAll(".deck-chart-legend-name")).map(
      (name) => name.textContent,
    );
    expect(names).toEqual(["v1", "v2", "v3", "Plan"]);
    expect((legend as HTMLElement).querySelectorAll(".deck-chart-legend-target")).toHaveLength(1);
    expect((legend as HTMLElement).querySelectorAll(".deck-chart-swatch")).toHaveLength(4);
    const toggles = Array.from((legend as HTMLElement).querySelectorAll<HTMLButtonElement>("button.deck-chart-legend-item"));
    expect(toggles).toHaveLength(3);
    for (const toggle of toggles) expect(toggle.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(toggles[2]);
    await waitFor(() => expect(toggles[2].getAttribute("aria-pressed")).toBe("false"));
    expect(toggles[2].getAttribute("data-hidden")).toBe("true");
    expect(toggles[0].getAttribute("aria-pressed")).toBe("true");
    const payload = JSON.parse(figure.getAttribute("data-deck-chart") as string) as Record<string, unknown>;
    expect(payload.legend).toBe(true);
    expect(payload.legend_position).toBe("bottom");
    const unset = partsOf(renderOnSlide(chart()).container);
    const before = JSON.parse(unset.figure.getAttribute("data-deck-chart") as string) as Record<string, unknown>;
    expect(before.legend_position).toBeNull();
  });
});
