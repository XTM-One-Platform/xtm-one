// A metrics grid's `size`: the type and padding of its tiles at each
// size, and the hold that draws a size the same on a slide shrunk to fit.
// A tile's `delta_tone`: the colour of a delta whose direction does
// not say whether it is good news, on every surface that draws a tile.
// A dashboard's `tile_size`: the type of its tile row, and the row as
// it always drew when the field is unset.
//
// The real stylesheet and the real components. happy-dom substitutes custom
// properties but evaluates no `calc()`, so a computed font size reads as the
// expression the cascade built (`calc(44px * calc(0.7 * calc(1 / 0.8)))`),
// which `px` evaluates. It drops a shorthand built of nested `calc()`, so the
// padding is read off the grid's inputs (`--deck-metric-pad-*` times
// `--deck-metric-k`), the product the tile rule draws.

import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { designTokenStylesheet } from "@/testing/design-tokens";

import { renderWithProviders } from "@/testing/providers";
import type {
  DashboardSlide,
  DeckBlock,
  DeckDashboardTileSize,
  DeckDensity,
  DeckMetricItem,
  DeckMetricsGridBlock,
  DeckSize,
  DeckTrend,
} from "@/types";
import { DELTA_TONES, TONES, TRENDS } from "../block-schema";
import { DashboardLayout } from "../layouts/dashboard";
import { BlockList } from "./index";
import { MetricTile } from "./metrics";

// The icon catalog, loaded: the lazy chunk the deck fetches is ~1,700 ESM
// modules vitest would transform one by one, while the package's own entry
// (loaded natively) exports the same map as `icons`.
vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

beforeAll(() => {
  const style = document.createElement("style");
  // The deck palette names design-system tokens the test document does not load.
  style.textContent = designTokenStylesheet() + stylesheet;
  document.head.appendChild(style);
});

/** A computed length happy-dom leaves as a `calc()` expression, in px. */
function px(expression: string): number {
  const tokens = expression.replace(/calc\(/g, "(").match(/\d*\.?\d+(?:px)?|[()*/+-]/g) ?? [];
  let at = 0;
  const factor = (): number => {
    const token = tokens[at++];
    if (token === "(") {
      const value = sum();
      at += 1;
      return value;
    }
    return Number.parseFloat(token);
  };
  const product = (): number => {
    let value = factor();
    while (tokens[at] === "*" || tokens[at] === "/") value = tokens[at++] === "*" ? value * factor() : value / factor();
    return value;
  };
  const sum = (): number => {
    let value = product();
    while (tokens[at] === "+" || tokens[at] === "-") value = tokens[at++] === "+" ? value + product() : value - product();
    return value;
  };
  return Math.round(sum() * 100) / 100;
}

/** A row of four KPI tiles, tight, as cards. */
const grid = (size: DeckSize | undefined, density: DeckDensity = "tight"): DeckMetricsGridBlock => ({
  type: "metrics_grid",
  columns: 4,
  density,
  variant: "card",
  ...(size ? { size } : {}),
  items: [
    { value: "EUR 12.4M", label: "Revenue", sublabel: "Quarter", delta: "+18% QoQ", trend: "up" },
    { value: "109%", label: "Net retention" },
    { value: "37", label: "New logos" },
    { value: "3.1 mo", label: "Payback" },
  ],
});

/** The blocks in a slide body; `fit` is the ratio a shrunk body publishes on its host. */
function renderBody(blocks: DeckBlock[], fit?: { ratio: number; released?: boolean }) {
  const run = <BlockList blocks={blocks} className="deck-blocks deck-blocks-slide" path="blocks" />;
  return renderWithProviders(
    <div className="deck-root">
      {fit ? (
        <div
          className="deck-fit"
          data-fit={fit.ratio.toFixed(3)}
          data-fit-released={fit.released ? "true" : undefined}
          style={{ ["--deck-fit" as string]: String(fit.ratio) }}
        >
          <div className="deck-fit-content">{run}</div>
        </div>
      ) : (
        run
      )}
    </div>,
  );
}

/** What the first tile of the grid is drawn with, in canvas px. */
function drawn(container: HTMLElement) {
  const root = container.querySelector<HTMLElement>(".deck-metrics") as HTMLElement;
  const tile = root.querySelector<HTMLElement>(".deck-metric") as HTMLElement;
  const fontOf = (selector: string) => px(getComputedStyle(tile.querySelector(selector) as HTMLElement).fontSize);
  const input = (name: string) => getComputedStyle(root).getPropertyValue(name);
  const k = input("--deck-metric-k");
  return {
    attributes: { size: root.dataset.size, density: root.dataset.density, hold: root.dataset.fitHold },
    value: fontOf(".deck-metric-value"),
    label: fontOf(".deck-metric-label"),
    sublabel: fontOf(".deck-metric-sublabel"),
    delta: fontOf(".deck-metric-delta"),
    padding: [px(`calc(${input("--deck-metric-pad-y")} * ${k})`), px(`calc(${input("--deck-metric-pad-x")} * ${k})`)],
  };
}

describe("metrics_grid size", () => {
  it.each(["sm", "md", "lg"] as const)("draws its tiles at size %s", (size) => {
    const { container } = renderBody([grid(size)]);
    expect(drawn(container)).toMatchSnapshot();
  });

  it("is md when unset, and md is the look a grid always had at every density", () => {
    const historical = {
      normal: { value: 72, padding: [32, 48] },
      compact: { value: 56, padding: [24, 32] },
      tight: { value: 44, padding: [16, 24] },
    } as const;
    for (const density of ["normal", "compact", "tight"] as const) {
      const unset = drawn(renderBody([grid(undefined, density)]).container);
      expect(unset.attributes.size).toBe("md");
      expect(unset).toMatchObject({ ...historical[density], label: 24, sublabel: 20, delta: 20 });
      expect(drawn(renderBody([grid("md", density)]).container)).toEqual(unset);
    }
  });

  it("sm draws the value at 70% of md and never sets the fine print under the type scale's floor", () => {
    const sm = drawn(renderBody([grid("sm")]).container);
    const md = drawn(renderBody([grid("md")]).container);
    expect(sm.value).toBeCloseTo(md.value * 0.7, 2);
    expect(sm.padding).toEqual(md.padding.map((side) => Math.round(side * 0.7 * 100) / 100));
    expect(sm.label).toBeLessThan(md.label);
    expect(Math.min(sm.label, sm.sublabel, sm.delta)).toBe(20);
  });

  it("is held at its size in a body shrunk to fit: the scaled body draws it as the unshrunk one", () => {
    const ratio = 0.8;
    const free = drawn(renderBody([grid("sm")]).container);
    const held = drawn(renderBody([grid("sm")], { ratio }).container);
    // Every length is divided by the ratio, so the body's `scale(0.8)` draws it at its size.
    expect(held.value * ratio).toBeCloseTo(free.value, 2);
    expect(held.label * ratio).toBeCloseTo(free.label, 2);
    expect(held.delta * ratio).toBeCloseTo(free.delta, 2);
    expect(held.padding.map((side) => side * ratio)).toEqual(free.padding.map((side) => expect.closeTo(side, 2)));
    // A host that let go draws it at its computed size: it shrinks with the body.
    expect(drawn(renderBody([grid("sm")], { ratio, released: true }).container)).toEqual(free);
  });

  it("holds the grid only: a metric tile in the same shrunk body shrinks with it", () => {
    const { container } = renderBody([{ type: "metric", value: "42%", label: "Fewer alerts" }, grid("md")], {
      ratio: 0.8,
    });
    const tiles = container.querySelectorAll<HTMLElement>(".deck-metric-value");
    expect(getComputedStyle(tiles[0]).fontSize).toBe("72px");
    expect(px(getComputedStyle(tiles[1]).fontSize)).toBe(55);
  });
});

/** The tiles of the Q3 2026 quarterly review: three falls that are good news. */
const LOSS: DeckMetricItem = { value: "EUR 1.7M", label: "Loss, Q3", delta: "-18.4% QoQ", trend: "down", delta_tone: "success" };
const BURN: DeckMetricItem = { value: "EUR 3.1M", label: "Burn, Q3", delta: "-12.5% QoQ", trend: "down", delta_tone: "success" };
const BURN_MULTIPLE: DeckMetricItem = { value: "1.27x", label: "Burn multiple", delta: "-0.20x", trend: "down", delta_tone: "success" };

/** The arrow `MetricTile` draws for each trend. */
const ARROW: Record<DeckTrend, string> = { up: "lucide-trending-up", down: "lucide-trending-down", flat: "lucide-minus" };

/** The deck tone an element is drawn in, named off the stylesheet's own tokens. */
function toneOf(element: Element): string {
  const root = element.closest(".deck-root") as HTMLElement;
  const colour = getComputedStyle(element).color;
  return TONES.find((tone) => getComputedStyle(root).getPropertyValue(`--deck-${tone}`).trim() === colour) ?? colour;
}

/** What a tile's delta draws: its arrow, the colour of the arrow and of the words, and the words. */
function deltaOf(tile: Element) {
  const delta = tile.querySelector<HTMLElement>(".deck-metric-delta");
  if (!delta) return null;
  const arrow = delta.querySelector("svg");
  return {
    trend: delta.dataset.trend ?? null,
    arrow: arrow ? (Object.values(ARROW).find((name) => arrow.classList.contains(name)) ?? null) : null,
    colour: toneOf(delta),
    arrowColour: arrow ? toneOf(arrow) : null,
    text: delta.textContent,
  };
}

function renderTile(metric: DeckMetricItem): HTMLElement {
  const { container } = renderWithProviders(
    <div className="deck-root">
      <MetricTile metric={metric} />
    </div>,
  );
  return container.querySelector(".deck-metric") as HTMLElement;
}

describe("delta_tone", () => {
  it("draws a loss that fell as a green down arrow", () => {
    const tile = renderTile(LOSS);
    const drawn = deltaOf(tile);
    expect(drawn).toEqual({
      trend: "down",
      arrow: "lucide-trending-down",
      colour: "success",
      arrowColour: "success",
      text: "-18.4% QoQ",
    });
    expect({ drawn, tile }).toMatchSnapshot();
  });

  it("leaves a tile without it as it was: the trend colours the delta", () => {
    for (const [trend, colour] of [
      ["up", "success"],
      ["down", "error"],
      ["flat", "muted"],
    ] as const) {
      const tile = renderTile({ value: "1", label: "One", delta: "+1", trend });
      expect(deltaOf(tile)).toMatchObject({ trend, arrow: ARROW[trend], colour, arrowColour: colour });
      expect(tile.querySelector(".deck-metric-delta")?.hasAttribute("data-tone")).toBe(false);
    }
    expect(deltaOf(renderTile({ value: "1", label: "One", delta: "+1" }))).toMatchObject({ trend: "flat", colour: "muted" });
  });

  it("wins over every trend, and the arrow keeps the trend's direction", () => {
    for (const tone of DELTA_TONES) {
      for (const trend of TRENDS) {
        const drawn = deltaOf(renderTile({ value: "1", label: "One", delta: "x", trend, delta_tone: tone }));
        expect(drawn, `${tone} on ${trend}`).toMatchObject({ trend, arrow: ARROW[trend], colour: tone, arrowColour: tone });
      }
    }
  });

  it("colours the delta only: the tile's own tone still draws its value", () => {
    for (const tone of ["accent", "error"] as const) {
      const tile = renderTile({ ...LOSS, tone });
      expect(toneOf(tile.querySelector(".deck-metric-value") as HTMLElement)).toBe(tone);
      expect(deltaOf(tile)?.colour).toBe("success");
    }
  });

  it("is drawn the same by a metric block, a grid's tiles and a dashboard's tiles", () => {
    const tiles = [LOSS, BURN, BURN_MULTIPLE];
    const metric = renderBody([{ type: "metric", ...LOSS }]).container;
    const grid = renderBody([{ type: "metrics_grid", items: tiles }]).container;
    const slide = {
      layout: "dashboard",
      title: "Q3 at a glance",
      tiles,
      panels: [{ title: "Burn multiple", block: { type: "metric", value: "1.27x", label: "Burn multiple" } }],
    } as DashboardSlide;
    const dashboard = renderWithProviders(
      <div className="deck-root">
        <DashboardLayout slide={slide} />
      </div>,
    ).container;
    const drawn = (root: Element, selector: string) =>
      Array.from(root.querySelectorAll(`${selector} .deck-metric`)).map(deltaOf);
    const expected = tiles.map((tile) => ({
      trend: "down",
      arrow: "lucide-trending-down",
      colour: "success",
      arrowColour: "success",
      text: tile.delta,
    }));
    expect(drawn(metric, ".deck-blocks")).toEqual(expected.slice(0, 1));
    expect(drawn(grid, ".deck-metrics")).toEqual(expected);
    expect(drawn(dashboard, ".deck-dashboard-tiles")).toEqual(expected);
  });

  it("is kept for the card on a plain tile, which draws no delta", () => {
    const { container } = renderBody([{ type: "metric", ...LOSS, variant: "plain" }]);
    expect(container.querySelector(".deck-metric")).not.toBeNull();
    expect(container.querySelector(".deck-metric-delta")).toBeNull();
  });
});

/** A tile of the regional ARR dashboard: its comparison sublabel wrapped at 20px in a row of six. */
const region = (index: number): DeckMetricItem => ({
  value: "EUR 12.3M",
  label: `Region ${String.fromCharCode(65 + index)} ARR`,
  sublabel: "Q3 25 EUR 4.5M \u00b7 Q2 EUR 6.7M",
  delta: "+34.5% YoY",
  trend: "up",
  sparkline: [4.5, 5.2, 6.7, 12.3],
});

function renderDashboard(count: number, tileSize?: DeckDashboardTileSize, format?: "page") {
  const slide = {
    layout: "dashboard",
    title: "Regional ARR",
    tiles: Array.from({ length: count }, (_, index) => region(index)),
    panels: [{ title: "ARR", block: { type: "metric", value: "EUR 64.2M", label: "ARR" } }],
    ...(tileSize ? { tile_size: tileSize } : {}),
  } as DashboardSlide;
  return renderWithProviders(
    <div className="deck-root" data-deck-format={format}>
      <DashboardLayout slide={slide} />
    </div>,
  ).container;
}

/** What the first tile of the row is drawn with, in canvas px. */
function tileRow(container: HTMLElement) {
  const row = container.querySelector<HTMLElement>(".deck-dashboard-tiles") as HTMLElement;
  const tile = row.querySelector<HTMLElement>(".deck-metric") as HTMLElement;
  const style = (selector: string) => getComputedStyle(tile.querySelector(selector) as Element);
  const box = getComputedStyle(tile);
  const sparkline = style(".deck-sparkline");
  return {
    attributes: { count: row.dataset.count, size: row.dataset.size },
    value: px(style(".deck-metric-value").fontSize),
    label: px(style(".deck-metric-label").fontSize),
    sublabel: px(style(".deck-metric-sublabel").fontSize),
    delta: px(style(".deck-metric-delta").fontSize),
    padding: [px(box.paddingTop), px(box.paddingLeft)],
    sparkline: [px(sparkline.width), px(sparkline.height)],
  };
}

describe("dashboard tile_size", () => {
  /** The row before the field, off the stylesheet it shipped with: the tight
   *  tile up to six tiles, the crowded step at seven and eight. */
  const HISTORICAL = {
    roomy: { value: 44, label: 20, sublabel: 20, delta: 20, padding: [16, 24], sparkline: [120, 40] },
    crowded: { value: 36, label: 20, sublabel: 20, delta: 20, padding: [12, 16], sparkline: [84, 30] },
  } as const;

  it.each([2, 3, 4, 5, 6, 7, 8])("draws a row of %i tiles without the field as it always drew, and md the same", (count) => {
    const unset = tileRow(renderDashboard(count));
    expect(unset).toEqual({
      attributes: { count: String(count), size: "md" },
      ...(count >= 7 ? HISTORICAL.crowded : HISTORICAL.roomy),
    });
    expect(tileRow(renderDashboard(count, "md"))).toEqual(unset);
  });

  it.each([2, 5, 6, 7, 8])("sm draws a row of %i tiles at the crowded step with 16px notes", (count) => {
    expect(tileRow(renderDashboard(count, "sm"))).toEqual({
      attributes: { count: String(count), size: "sm" },
      ...HISTORICAL.crowded,
      label: 16,
      sublabel: 16,
      delta: 16,
    });
  });

  it("sets the notes off the fine print, so a portrait page keeps its own type scale", () => {
    const page = tileRow(renderDashboard(6, "sm", "page"));
    expect([page.label, page.sublabel, page.delta]).toEqual([20.8, 20.8, 20.8]);
    expect(tileRow(renderDashboard(6, undefined, "page")).sublabel).toBe(26);
  });

  it("draws the six regional tiles at each size", () => {
    expect({
      unset: tileRow(renderDashboard(6)),
      sm: tileRow(renderDashboard(6, "sm")),
      unsetCrowded: tileRow(renderDashboard(8)),
    }).toMatchSnapshot();
  });
});
