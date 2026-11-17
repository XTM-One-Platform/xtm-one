// A chart's surface. The figure stands on a card unless its block says
// `framed: false`, a dashboard panel is its chart's card, and nothing about the
// surface follows the build: the waiting placeholder is painted only on a
// canvas that holds no drawing, never under one that is growing in - it used to
// show on every slide entry and vanish once the values were written.
//
// happy-dom lays nothing out, so the cascade is read through `getComputedStyle`
// (the real stylesheet, the real components), as in `chart-fill.test.tsx` - on
// paper, whose surfaces are plain colours: happy-dom computes no `color-mix()`,
// which the dark scheme's surfaces are.

import { readFileSync } from "node:fs";
import { waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { designTokenStylesheet } from "@/testing/design-tokens";

import { renderWithProviders } from "@/testing/providers";
import type { DeckChartBlock } from "@/types";
import { Block, BlockList } from "./index";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

const chart = (fields: Partial<DeckChartBlock> = {}): DeckChartBlock => ({
  type: "chart",
  kind: "bar",
  categories: ["Q1", "Q2", "Q3"],
  series: [{ name: "ARR", values: [8.4, 10.5, 13.3] }],
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

/** On a paper slide of a live deck, or of the export page (no motion there). */
function renderOnSlide(block: DeckChartBlock, { exportPage = false } = {}) {
  return renderWithProviders(
    <div className="deck-root" data-deck-static={exportPage ? "true" : undefined}>
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
          <div className="deck-panel-body">
            <Block block={block} path="panels.0.block" />
          </div>
        </article>
      </div>
    </div>,
  );
}

interface Surface {
  drawn: boolean;
  pending: boolean;
  background: string;
}

/** The canvas' background at every change under it, with whether it held a drawing and its ready signal then. */
function recordSurfaces(canvas: HTMLElement): { samples: Surface[]; stop: () => void } {
  const samples: Surface[] = [];
  const sample = () =>
    samples.push({
      drawn: canvas.querySelector(":scope > .recharts-wrapper") !== null,
      pending: canvas.hasAttribute("data-deck-pending"),
      background: getComputedStyle(canvas).backgroundColor,
    });
  sample();
  const observer = new MutationObserver(sample);
  observer.observe(canvas, { subtree: true, childList: true, attributes: true });
  return { samples, stop: () => observer.disconnect() };
}

const figureOf = (container: HTMLElement): HTMLElement => {
  const figure = container.querySelector<HTMLElement>("figure.deck-chart");
  expect(figure).not.toBeNull();
  return figure as HTMLElement;
};

beforeAll(async () => {
  const style = document.createElement("style");
  // The deck palette names design-system tokens the test document does not load.
  style.textContent = designTokenStylesheet() + stylesheet;
  document.head.appendChild(style);
  await import("./ChartCanvas");
}, 30_000);

afterEach(() => {
  Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
  Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
});

describe("a chart's surface", () => {
  it("keeps the same canvas surface while the figure grows in as once it has settled", async () => {
    // A canvas with no room holds no drawing: the placeholder stands.
    measure(0, 0);
    const empty = renderOnSlide(chart());
    const waiting = empty.container.querySelector<HTMLElement>(".deck-chart-canvas") as HTMLElement;
    expect(waiting.hasAttribute("data-deck-pending")).toBe(true);
    const placeholder = getComputedStyle(waiting).backgroundColor;
    empty.unmount();

    measure(640, 360);
    const { container } = renderOnSlide(chart());
    const canvas = container.querySelector<HTMLElement>(".deck-chart-canvas") as HTMLElement;
    const { samples, stop } = recordSurfaces(canvas);
    await waitFor(() => expect(canvas.querySelector(":scope > .recharts-wrapper")).not.toBeNull(), { timeout: 5_000 });
    await waitFor(() => expect(canvas.hasAttribute("data-deck-pending")).toBe(false), { timeout: 5_000 });
    stop();

    const settled = getComputedStyle(canvas).backgroundColor;
    expect(settled).not.toBe(placeholder);
    // The build ran with its drawing on screen, ready signal held...
    expect(samples.some((surface) => surface.drawn && surface.pending)).toBe(true);
    // ...and every frame that held a drawing painted the settled surface.
    for (const surface of samples.filter((entry) => entry.drawn)) expect(surface.background).toBe(settled);
    for (const surface of samples.filter((entry) => !entry.drawn && entry.pending)) {
      expect(surface.background).toBe(placeholder);
    }
  });

  it("stands on the card a dashboard panel stands on, unless the block says framed: false", () => {
    measure(640, 360);
    const panel = renderInPanel(chart());
    const card = getComputedStyle(panel.container.querySelector(".deck-panel") as HTMLElement);
    const panelFigure = getComputedStyle(figureOf(panel.container));
    const cardBackground = card.backgroundColor;
    const cardBorder = card.borderTopWidth;
    // In a panel the panel is the card: the figure is no box of its own.
    expect(panelFigure.display).toBe("contents");
    panel.unmount();

    const framed = renderOnSlide(chart(), { exportPage: true });
    const figure = getComputedStyle(figureOf(framed.container));
    expect(figureOf(framed.container).dataset.framed).toBe("true");
    expect(cardBackground).not.toBe("");
    expect(figure.backgroundColor).toBe(cardBackground);
    expect(figure.borderTopWidth).toBe(cardBorder);
    expect(figure.paddingTop).toBe("16px");
    framed.unmount();

    const bare = renderOnSlide(chart({ framed: false }), { exportPage: true });
    const bareFigure = figureOf(bare.container);
    expect(bareFigure.hasAttribute("data-framed")).toBe(false);
    expect(getComputedStyle(bareFigure).backgroundColor).not.toBe(cardBackground);
    expect(getComputedStyle(bareFigure).paddingTop).not.toBe("16px");
  });
});
