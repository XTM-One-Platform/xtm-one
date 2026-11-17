/**
 * The map chart - `yarn test:dom`.
 *
 * A world choropleth by ISO 3166-1 alpha-2 code: every code has a shape on
 * the 1:50m atlas or a place to be marked; a country with a value is drawn on
 * its series' ramp, one without in the empty fill; its value is written
 * inside it at the largest size it holds on the text scale, else marked when
 * it is too small to see, and read on hover; a region zooms the map and keeps
 * its values to the region. The figure's legend is the ramp, where any legend
 * sits, on a slide and in a dashboard panel.
 *
 * The planner (`chart-map.ts`) is read directly; the drawing (`MapCanvas.tsx`)
 * and the figure (`chart.tsx`) are rendered with the real stylesheet, which
 * happy-dom reads but does not lay out - placements are read through
 * `getComputedStyle`, as in `chart-legend-position.test.tsx`.
 */
import { readFileSync } from "node:fs";
import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckChartBlock, DeckMapRegion } from "@/types";

import {
  chartExportPayload,
  legendPosition,
  legendShown,
  MAP_EMPTY_FILL,
  MAP_RAMP_FLOOR,
  mapClasses,
  mapRampColor,
  mapRampDomain,
  mapRampLegend,
  naturalBreaks,
  unitScaleApplies,
} from "./chart-format";
import { atlasCountries, MARKER_RADIUS, mapChartLayout, type MapLayout } from "./chart-map";
import { ISO_3166_NUMERIC, UNSHAPED_COUNTRY_POINTS } from "./chart-map-countries";
import { CHART_TEXT_SCALES } from "./chart-layout";
import { growsIn } from "./chart-settle";
import { Block, BlockList } from "./index";
import MapCanvas from "./MapCanvas";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

/** The issue's shape - four countries, one series, values on - with fictitious figures. */
const map = (fields: Partial<DeckChartBlock> = {}): DeckChartBlock => ({
  type: "chart",
  kind: "map",
  categories: ["FR", "US", "AU", "GB"],
  series: [{ name: "Customers", values: [84, 36, 14, 9] }],
  show_values: true,
  ...fields,
});

/** A content slide's chart (`size: md`), a span 2 dashboard panel, a narrow one. */
const SLIDE = [1700, 460] as const;
const PANEL = [1089, 300] as const;
const NARROW = [540, 300] as const;

const layoutOf = (block: DeckChartBlock, [width, height]: readonly [number, number] = SLIDE): MapLayout =>
  mapChartLayout(block, width, height, "en");
const codeOf = (block: DeckChartBlock, category: number) => block.categories[category];
const labelled = (block: DeckChartBlock, layout: MapLayout) => layout.labels.map((label) => codeOf(block, label.category)).sort();
const marked = (layout: MapLayout) => layout.markers.map((marker) => marker.code).sort();
const shapeOf = (layout: MapLayout, code: string) => layout.shapes.find((shape) => shape.code === code);

/** Every coordinate of an SVG path, as [x, y] pairs. */
function pathPoints(d: string): [number, number][] {
  const numbers = (d.match(/-?\d+(\.\d+)?/g) ?? []).map(Number);
  const points: [number, number][] = [];
  for (let index = 0; index + 1 < numbers.length; index += 2) points.push([numbers[index], numbers[index + 1]]);
  return points;
}

describe("the countries a map reads", () => {
  it("gives every ISO 3166-1 code a shape on the atlas or a place to be marked, never both", () => {
    const shaped = new Set(atlasCountries().map((country) => country.code).filter((code) => code !== null));
    const codes = Object.keys(ISO_3166_NUMERIC);
    expect(codes).toHaveLength(249);
    for (const code of codes) expect([code, shaped.has(code)]).toEqual([code, !(code in UNSHAPED_COUNTRY_POINTS)]);
    // A shape no code names (Kosovo, Somaliland) is drawn as land without a value.
    expect(atlasCountries().filter((country) => country.code === null).map((country) => country.name)).toEqual(
      expect.arrayContaining(["Kosovo", "Somaliland"]),
    );
  });

  it("draws Australia whole, its islands one country with it", () => {
    const australia = atlasCountries().filter((country) => country.code === "AU");
    expect(australia).toHaveLength(1);
    expect(australia[0].features.length).toBeGreaterThan(1);
  });
});

describe("the ramp", () => {
  it("runs from the lowest value to the highest, from zero when every country has one value", () => {
    expect(mapRampDomain(map())).toEqual({ min: 9, max: 84 });
    expect(mapRampDomain(map({ categories: ["FR"], series: [{ name: "C", values: [12] }] }))).toEqual({ min: 0, max: 12 });
  });

  it("draws the highest value in the series' colour, the lowest a wash of it, a country without one in the empty fill", () => {
    const layout = layoutOf(map());
    expect(shapeOf(layout, "FR")?.fill).toBe(mapRampColor(1, "var(--deck-chart-1)"));
    expect(shapeOf(layout, "FR")?.fill).toBe("color-mix(in oklab, var(--deck-chart-1) 100%, var(--deck-map-paper))");
    expect(shapeOf(layout, "GB")?.fill).toBe(`color-mix(in oklab, var(--deck-chart-1) ${MAP_RAMP_FLOOR}%, var(--deck-map-paper))`);
    // US at 36 sits 36% of the way from 9 to 84: 30% of the colour, and 36% of the 70% left.
    expect(MAP_RAMP_FLOOR).toBe(30);
    expect(shapeOf(layout, "US")?.fill).toBe("color-mix(in oklab, var(--deck-chart-1) 55.2%, var(--deck-map-paper))");
    expect(shapeOf(layout, "DE")?.fill).toBe(MAP_EMPTY_FILL);
    expect(layout.shapes.filter((shape) => shape.category !== null).map((shape) => shape.code).sort()).toEqual(["AU", "FR", "GB", "US"]);
  });

  it("ramps to a toned series' tone", () => {
    const toned = map({ series: [{ name: "Incidents", values: [84, 36, 14, 9], tone: "error" }] });
    expect(shapeOf(layoutOf(toned), "FR")?.fill).toBe("color-mix(in oklab, var(--deck-error) 100%, var(--deck-map-paper))");
  });

  it("writes its two ends as the chart writes a value", () => {
    expect(mapRampLegend(map(), "en")).toMatchObject({ name: "Customers", low: "9", high: "84" });
    const revenue = map({ format: "currency", series: [{ name: "Revenue", values: [4_200_000, 1_800_000, 650_000, 300_000] }] });
    expect(mapRampLegend(revenue, "en")).toMatchObject({ low: "€300k", high: "€4.2M" });
  });
});

describe("the layout", () => {
  it("fits the world inside the canvas on a slide, in a dashboard panel and in a narrow one", () => {
    for (const room of [SLIDE, PANEL, NARROW]) {
      const layout = layoutOf(map(), room);
      const points = layout.shapes.flatMap((shape) => pathPoints(shape.d));
      expect(points.length).toBeGreaterThan(1000);
      for (const [x, y] of points) {
        expect(x).toBeGreaterThanOrEqual(-0.1);
        expect(x).toBeLessThanOrEqual(room[0] + 0.1);
        expect(y).toBeGreaterThanOrEqual(-0.1);
        expect(y).toBeLessThanOrEqual(room[1] + 0.1);
      }
      // The world reaches both sides of a wide canvas or its top and bottom.
      const xs = points.map(([x]) => x);
      const ys = points.map(([, y]) => y);
      const spanX = Math.max(...xs) - Math.min(...xs);
      const spanY = Math.max(...ys) - Math.min(...ys);
      expect(spanX > room[0] * 0.9 || spanY > room[1] * 0.9).toBe(true);
      expect(layout.outside).toEqual([]);
    }
  });

  it("leaves Antarctica out of a world map, unless the map names it", () => {
    expect(shapeOf(layoutOf(map()), "AQ")).toBeUndefined();
    const polar = map({ categories: ["FR", "AQ"], series: [{ name: "Stations", values: [3, 9] }] });
    expect(shapeOf(layoutOf(polar), "AQ")?.category).toBe(1);
  });

  it("writes the values of the countries large enough to hold one, on the text scale, and leaves the rest to the hover", () => {
    const block = map();
    const layout = layoutOf(block);
    expect(labelled(block, layout)).toEqual(["AU", "US"]);
    const ladder = CHART_TEXT_SCALES.md.values;
    for (const label of layout.labels) expect(ladder).toContain(label.size);
    expect(layout.labels.find((label) => codeOf(block, label.category) === "US")?.text).toBe("36");
    // Each value is written in its country's colour, inside the canvas.
    for (const label of layout.labels) {
      expect(label.fill).toBe(layout.shapes.find((shape) => shape.category === label.category)?.fill);
      expect(label.x).toBeGreaterThan(0);
      expect(label.x).toBeLessThan(SLIDE[0]);
      expect(label.y).toBeGreaterThan(0);
      expect(label.y).toBeLessThan(SLIDE[1]);
    }
    expect(layoutOf(map({ show_values: false })).labels).toEqual([]);
  });

  it("writes each value at the largest size its country holds, and a small country never shrinks the others", () => {
    const ladder = CHART_TEXT_SCALES.md.values;
    const block = map({ categories: ["US", "ES"], series: [{ name: "C", values: [36, 6] }] });
    const size = (code: string) => layoutOf(block).labels.find((label) => codeOf(block, label.category) === code)?.size;
    expect(size("US")).toBe(ladder[0]);
    // Spain holds its value only smaller, and is written at that size.
    expect(size("ES")).toBeLessThan(ladder[0]);
    expect(size("ES")).toBeGreaterThanOrEqual(ladder[ladder.length - 1]);
    // A smaller text scale writes smaller values.
    const small = layoutOf(map({ categories: ["US"], series: [{ name: "C", values: [36] }], label_size: "sm" }));
    expect(small.labels.map((label) => label.size)).toEqual([CHART_TEXT_SCALES.sm.values[0]]);
  });

  it("keeps two values apart", () => {
    const block = map({
      categories: ["US", "CA", "MX", "BR", "AR", "RU", "CN", "IN", "AU", "DZ", "CD", "KZ", "SA", "IR", "MN"],
      series: [{ name: "C", values: [9, 8, 7, 6, 5, 4, 3, 2, 1, 12, 11, 10, 13, 14, 15] }],
    });
    for (const room of [SLIDE, PANEL]) {
      const boxes = layoutOf(block, room).labels.map((label) => ({
        x0: label.x - (label.text.length * label.size * 0.6) / 2,
        x1: label.x + (label.text.length * label.size * 0.6) / 2,
        y0: label.y - label.size / 2,
        y1: label.y + label.size / 2,
      }));
      expect(boxes.length).toBeGreaterThan(8);
      boxes.forEach((a, i) =>
        boxes.slice(i + 1).forEach((b) => expect(a.x1 < b.x0 || b.x1 < a.x0 || a.y1 < b.y0 || b.y1 < a.y0).toBe(true)),
      );
    }
  });

  it("marks a country too small to see, and one with no shape of its own, in its colour", () => {
    const block = map({ categories: ["FR", "SG", "MT", "RE"], series: [{ name: "Offices", values: [8, 3, 1, 2] }] });
    const layout = layoutOf(block);
    expect(marked(layout)).toEqual(["MT", "RE", "SG"]);
    for (const marker of layout.markers) {
      expect(marker.r).toBe(MARKER_RADIUS);
      expect(marker.fill).toBe(shapeOf(layout, marker.code)?.fill ?? marker.fill);
      expect(marker.fill).not.toBe(MAP_EMPTY_FILL);
    }
    // Reunion, which the atlas draws inside France, is marked where it lies: east of Madagascar.
    const reunion = layout.markers.find((marker) => marker.code === "RE");
    const madagascar = pathPoints(shapeOf(layout, "MG")?.d ?? "");
    expect(reunion?.x).toBeGreaterThan(Math.max(...madagascar.map(([x]) => x)));
    expect(layout.outside).toEqual([]);
  });

  it("zooms on a region, writes the values inside it and counts the countries beyond it", () => {
    const block = map({ region: "europe" });
    const layout = layoutOf(block);
    expect(layout.region).toBe("europe");
    expect(labelled(block, layout)).toEqual(["FR", "GB"]);
    expect(layout.outside.map((category) => codeOf(block, category)).sort()).toEqual(["AU", "US"]);
    // Germany is drawn well over twice as large as on the world map, whose height a wide slide's canvas sets.
    const span = (one: MapLayout) => {
      const xs = pathPoints(shapeOf(one, "DE")?.d ?? "").map(([x]) => x);
      return Math.max(...xs) - Math.min(...xs);
    };
    expect(span(layout)).toBeGreaterThan(2.5 * span(layoutOf(map())));
  });

  it.each([
    ["americas", ["BR", "CA", "US"], ["FR", "JP"]],
    ["apac", ["AU", "IN", "JP"], ["BR", "FR"]],
  ] as [DeckMapRegion, string[], string[]][])("zooms on %s", (region, inside, beyond) => {
    const codes = [...inside, ...beyond];
    const block = map({ region, categories: codes, series: [{ name: "C", values: codes.map((_, index) => index + 1) }] });
    const layout = layoutOf(block, [1200, 640]);
    expect(layout.outside.map((category) => codeOf(block, category)).sort()).toEqual([...beyond].sort());
    expect(labelled(block, layout).every((code) => inside.includes(code))).toBe(true);
  });
});

describe("the drawing", () => {
  const draw = (block: DeckChartBlock, locale = "en") => {
    const onSettled = vi.fn();
    const view = render(
      <div className="deck-root">
        <div className="deck-chart-canvas" style={{ width: SLIDE[0], height: SLIDE[1] }}>
          <MapCanvas block={block} width={SLIDE[0]} height={SLIDE[1]} locale={locale} onSettled={onSettled} noDataLabel="No data" />
        </div>
      </div>,
    );
    return { ...view, onSettled };
  };

  it("draws the countries, the values and the markers, settles, and publishes what it drew", async () => {
    const block = map({ categories: ["FR", "US", "AU", "GB", "SG"], series: [{ name: "Customers", values: [84, 36, 14, 9, 4] }] });
    const { container, onSettled } = draw(block);
    await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1));
    const svg = container.querySelector("svg.deck-chart-map");
    expect(svg?.getAttribute("width")).toBe(String(SLIDE[0]));
    expect(container.querySelectorAll("path.deck-chart-country").length).toBeGreaterThan(150);
    expect(container.querySelector('path.deck-chart-country[data-code="FR"]')?.getAttribute("data-category")).toBe("0");
    expect(Array.from(container.querySelectorAll("text.deck-chart-value")).map((text) => text.textContent)).toEqual(
      expect.arrayContaining(["36", "14"]),
    );
    const value = container.querySelector<SVGTextElement>("text.deck-chart-value");
    expect(value?.dataset.role).toBe("country");
    expect(value?.style.getPropertyValue("--deck-segment")).toContain("color-mix(in oklab");
    expect(container.querySelector('circle.deck-chart-marker[data-code="SG"]')).not.toBeNull();
    const facts = container.querySelector<HTMLElement>(".deck-chart-facts");
    expect(facts?.dataset).toMatchObject({
      region: "world",
      valuesAsked: "5",
      valuesWritten: "5",
      valuesLabelled: "2",
      valuesMarked: "1",
      valuesOutside: "0",
      rampMin: "4",
      rampMax: "84",
    });
    // A country too small for its value is read on hover, not reported.
    expect(facts?.dataset.valuesReason).toBeUndefined();
    expect(svg?.getAttribute("aria-label")).toBe("Customers: France 84, United States 36, Australia 14, United Kingdom 9, Singapore 4");
  });

  it("reports the countries a region leaves out, by code, for the layout check", async () => {
    const { container, onSettled } = draw(map({ region: "europe" }));
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
    expect(container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset).toMatchObject({
      region: "europe",
      valuesAsked: "4",
      valuesWritten: "2",
      valuesReason: "region",
      valuesCountries: "US,AU",
      valuesOutside: "2",
    });
  });

  it("writes a country's name and value on hover, in the reader's language, and says when it has none", async () => {
    const { container } = draw(map(), "fr");
    const france = container.querySelector('path.deck-chart-country[data-code="FR"]') as SVGPathElement;
    fireEvent.pointerMove(france, { clientX: 400, clientY: 120 });
    await waitFor(() => expect(container.querySelector(".deck-chart-tooltip-title")?.textContent).toBe("France"));
    expect(container.querySelector(".deck-chart-tooltip-name")?.textContent).toBe("Customers");
    expect(container.querySelector(".deck-chart-tooltip-value")?.textContent).toBe("84");
    // The swatch is the country's own colour: the top of the ramp.
    const swatch = container.querySelector<HTMLElement>(".deck-chart-tip .deck-chart-swatch");
    expect(swatch?.style.getPropertyValue("--deck-tip-swatch")).toBe(france.getAttribute("fill"));
    expect(container.querySelector("path.deck-chart-country-hover")?.getAttribute("d")).toBe(france.getAttribute("d"));

    const germany = container.querySelector('path.deck-chart-country[data-code="DE"]') as SVGPathElement;
    fireEvent.pointerMove(germany, { clientX: 410, clientY: 110 });
    await waitFor(() => expect(container.querySelector(".deck-chart-tooltip-title")?.textContent).toBe("Allemagne"));
    expect(container.querySelector(".deck-chart-tooltip-value")?.textContent).toBe("No data");

    fireEvent.pointerLeave(container.querySelector("svg.deck-chart-map") as SVGSVGElement);
    await waitFor(() => expect(container.querySelector(".deck-chart-tooltip")).toBeNull());
  });

  it("grows nothing in: it is complete once painted", () => {
    expect(growsIn(map(), new Set())).toBe(false);
  });
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

describe("the figure", () => {
  beforeAll(async () => {
    const sheet = document.createElement("style");
    sheet.textContent = stylesheet;
    document.head.appendChild(sheet);
    await import("./MapCanvas");
  }, 30_000);

  afterEach(() => {
    Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
    Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
  });

  const onSlide = (block: DeckChartBlock) =>
    renderWithProviders(
      <div className="deck-root" data-deck-static="true">
        <div data-scheme="light">
          <BlockList blocks={[block]} className="deck-blocks deck-blocks-slide" path="blocks" />
        </div>
      </div>,
    );

  const inPanel = (block: DeckChartBlock) =>
    renderWithProviders(
      <div className="deck-root" data-deck-static="true">
        <div data-scheme="dark">
          <article className="deck-panel">
            <h3 className="deck-panel-title">Customers by country</h3>
            <div className="deck-panel-body">
              <Block block={block} path="panels.0.block" />
            </div>
          </article>
        </div>
      </div>,
    );

  it("draws the map on a content slide, its legend the ramp, its title and caption around it", async () => {
    measure(SLIDE[0], SLIDE[1]);
    const { container } = onSlide(map({ title: "Customers by country", caption: "End of quarter" }));
    await waitFor(() => expect(container.querySelector("svg.deck-chart-map")).not.toBeNull());
    const figure = container.querySelector<HTMLElement>("figure.deck-chart") as HTMLElement;
    expect(figure.dataset.kind).toBe("map");
    expect(figure.dataset.framed).toBe("true");
    const legend = container.querySelector<HTMLElement>(".deck-chart-head > .deck-chart-legend.deck-chart-ramp") as HTMLElement;
    expect(Array.from(legend.children).map((part) => part.textContent)).toEqual(["Customers", "9", "", "84"]);
    expect(legend.querySelector("button")).toBeNull();
    const bar = legend.querySelector<HTMLElement>(".deck-chart-ramp-bar") as HTMLElement;
    expect(bar.style.getPropertyValue("--deck-ramp-from")).toBe(mapRampColor(0, "var(--deck-chart-1)"));
    expect(bar.style.getPropertyValue("--deck-ramp-to")).toBe(mapRampColor(1, "var(--deck-chart-1)"));
    expect(container.querySelector("figcaption")?.textContent).toBe("End of quarter");
    await waitFor(() => expect(container.querySelector(".deck-chart-canvas")?.hasAttribute("data-deck-pending")).toBe(false));
    // The figure as data: a map, where it is zoomed, the legend it draws.
    expect(JSON.parse(figure.dataset.deckChart ?? "{}")).toMatchObject({ kind: "map", region: "world", legend: true });
  });

  it("turns the ramp upright beside the plot when the legend sits right", () => {
    measure(0, 0);
    const { container } = onSlide(map({ legend_position: "right" }));
    const legend = container.querySelector(".deck-chart-ramp") as HTMLElement;
    expect(getComputedStyle(legend).flexDirection).toBe("column");
    const order = (selector: string) => getComputedStyle(legend.querySelector(selector) as Element).order;
    expect([order('[data-end="high"]'), order(".deck-chart-ramp-bar"), order('[data-end="low"]')]).toEqual(["1", "2", "3"]);
  });

  it("draws the map in a dashboard panel, the ramp on the title's row", async () => {
    measure(PANEL[0], PANEL[1]);
    const { container } = inPanel(map({ label_size: "sm" }));
    await waitFor(() => expect(container.querySelector("svg.deck-chart-map")).not.toBeNull());
    const legend = container.querySelector(".deck-chart-ramp") as HTMLElement;
    expect(getComputedStyle(legend).gridRow).toBe("1");
    expect(getComputedStyle(legend).gridColumn).toBe("2");
    expect(getComputedStyle(container.querySelector(".deck-chart-canvas") as Element).gridRow).toBe("2");
  });

  it("draws no legend when told not to", () => {
    measure(0, 0);
    const { container } = onSlide(map({ show_legend: false }));
    expect(container.querySelector(".deck-chart-legend")).toBeNull();
  });
});

/** The issue's classed map: seven countries, two of them tied, no values, no markers. */
const classed = (fields: Partial<DeckChartBlock> = {}): DeckChartBlock =>
  map({
    categories: ["FR", "US", "AU", "GB", "SA", "SG", "BE"],
    series: [{ name: "Employees", values: [120, 40, 12, 12, 7, 6, 1] }],
    show_values: false,
    legend_style: "classes",
    markers: false,
    ...fields,
  });

const step = (position: number) => mapRampColor(position, "var(--deck-chart-1)");

describe("the classes of a classed map", () => {
  it("cuts sorted values by natural breaks, the least spread within each run", () => {
    expect(naturalBreaks([1, 6, 7, 12, 40, 120], 5)).toEqual([0, 1, 3, 4, 5]);
    expect(naturalBreaks([1, 2, 3, 10, 11, 12, 50, 51], 3)).toEqual([0, 3, 6]);
    // Never more runs than values, never fewer than one; a tie keeps the earliest cut.
    expect(naturalBreaks([4, 9], 5)).toEqual([0, 1]);
    expect(naturalBreaks([7], 3)).toEqual([0]);
    expect(naturalBreaks([1, 2, 3], 2)).toEqual([0, 1]);
    expect(naturalBreaks([], 5)).toEqual([]);
    // Two close figures in the billions keep their difference.
    expect(naturalBreaks([5_000_000_000, 5_000_000_001, 5_000_000_900, 5_000_000_901], 2)).toEqual([0, 2]);
  });

  it("buckets the issue's values into five classes, each read as the issue asks and drawn as one step of the ramp", () => {
    expect(mapClasses(classed(), "en")).toEqual([
      { min: 1, max: 1, label: "1", color: step(0) },
      { min: 6, max: 7, label: "6 - 7", color: step(0.25) },
      { min: 12, max: 12, label: "12", color: step(0.5) },
      { min: 40, max: 40, label: "40", color: step(0.75) },
      { min: 120, max: 120, label: "120 +", color: step(1) },
    ]);
  });

  it("forces the count it names, never past the distinct values, and opens the top class over a range", () => {
    const two = mapClasses(classed({ classes: 2 }), "en");
    expect(two.map((entry) => entry.label)).toEqual(["1 - 40", "120 +"]);
    expect(two.map((entry) => entry.color)).toEqual([step(0), step(1)]);
    expect(mapClasses(classed({ classes: 7 }), "en")).toHaveLength(6);
    const ties = map({ categories: ["FR", "DE"], series: [{ name: "C", values: [3, 3] }], legend_style: "classes" });
    expect(mapClasses(ties, "en")).toEqual([{ min: 3, max: 3, label: "3", color: step(1) }]);
    // A count outside 2-7 (an old draft, a hand-edited spec) falls back to up to five.
    expect(mapClasses(classed({ classes: 9 }), "en")).toHaveLength(5);
  });

  it("writes its ranges as the chart writes a value", () => {
    const revenue = classed({
      format: "currency",
      series: [{ name: "Revenue", values: [4_200_000, 1_800_000, 650_000, 300_000, 120_000, 90_000, 15_000] }],
      classes: 3,
    });
    expect(mapClasses(revenue, "en").map((entry) => entry.label)).toEqual(["€15k - €650k", "€1.8M", "€4.2M +"]);
  });

  it("fills each country with its class's colour, ties alike, and leaves the rest in the empty fill", () => {
    const layout = layoutOf(classed());
    expect(shapeOf(layout, "FR")?.fill).toBe(step(1));
    expect(shapeOf(layout, "US")?.fill).toBe(step(0.75));
    expect(shapeOf(layout, "AU")?.fill).toBe(step(0.5));
    expect(shapeOf(layout, "GB")?.fill).toBe(shapeOf(layout, "AU")?.fill);
    expect(shapeOf(layout, "SA")?.fill).toBe(step(0.25));
    expect(shapeOf(layout, "BE")?.fill).toBe(step(0));
    expect(shapeOf(layout, "DE")?.fill).toBe(MAP_EMPTY_FILL);
  });

  it("draws the issue's map with no marker and no value; omitting both fields keeps today's ramp and markers", () => {
    const layout = layoutOf(classed());
    expect(layout.markers).toEqual([]);
    expect(layout.labels).toEqual([]);
    // Singapore keeps no pixel of outline at world scale: no dot and no shape, yet not reported as hidden.
    expect(shapeOf(layout, "SG")).toBeUndefined();
    expect(layout.outside).toEqual([]);
    const today = classed({ legend_style: undefined, markers: undefined });
    const before = layoutOf(today);
    expect(marked(before)).toContain("SG");
    expect(shapeOf(before, "FR")?.fill).toBe(mapRampColor(1, "var(--deck-chart-1)"));
    expect(shapeOf(before, "US")?.fill).toBe(mapRampColor((40 - 1) / (120 - 1), "var(--deck-chart-1)"));
  });

  it("marks a small country in its class's colour while markers stay on", () => {
    const layout = layoutOf(classed({ markers: undefined }));
    const singapore = layout.markers.find((marker) => marker.code === "SG");
    expect(singapore?.fill).toBe(step(0.25));
  });

  it("drops the dot of every country the atlas shapes, and keeps a territory's the atlas draws inside another", () => {
    const block = map({
      categories: ["FR", "SG", "MT", "BE", "RE", "GF"],
      series: [{ name: "Offices", values: [8, 3, 1, 4, 2, 5] }],
      markers: false,
    });
    expect(marked(layoutOf(block))).toEqual(["GF", "RE"]);
    expect(marked(layoutOf({ ...block, markers: undefined }))).toEqual(expect.arrayContaining(["BE", "GF", "MT", "RE", "SG"]));
    expect(layoutOf(block).outside).toEqual([]);
  });

  it("places its legend right of the plot unless the block says where; a ramp keeps the top", () => {
    expect(legendPosition(classed())).toBe("right");
    expect(legendPosition(classed({ legend_position: "bottom" }))).toBe("bottom");
    expect(legendPosition(classed({ legend_style: undefined }))).toBe("top");
    // The word is a map's: another kind keeps the historical default.
    expect(legendPosition({ kind: "bar", legend_style: "classes" })).toBe("top");
  });
});

describe("the classed map's figure", () => {
  beforeAll(async () => {
    const sheet = document.createElement("style");
    sheet.textContent = stylesheet;
    document.head.appendChild(sheet);
    await import("./MapCanvas");
  }, 30_000);

  afterEach(() => {
    Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
    Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
  });

  const onSlide = (block: DeckChartBlock) =>
    renderWithProviders(
      <div className="deck-root" data-deck-static="true">
        <div data-scheme="light">
          <BlockList blocks={[block]} className="deck-blocks deck-blocks-slide" path="blocks" />
        </div>
      </div>,
    );

  const inPanel = (block: DeckChartBlock) =>
    renderWithProviders(
      <div className="deck-root" data-deck-static="true">
        <div data-scheme="dark">
          <article className="deck-panel">
            <h3 className="deck-panel-title">Employees by country</h3>
            <div className="deck-panel-body">
              <Block block={block} path="panels.0.block" />
            </div>
          </article>
        </div>
      </div>,
    );

  const rows = (legend: Element) =>
    Array.from(legend.querySelectorAll("li")).map((row) => ({
      label: row.textContent,
      dot: row.querySelector<HTMLElement>(".deck-chart-swatch")?.style.getPropertyValue("--deck-class-swatch"),
    }));

  it("draws the issue's map on a slide: no marker, no value, its classes in a column beside the plot", async () => {
    measure(SLIDE[0], SLIDE[1]);
    const { container } = onSlide(classed({ title: "Employees by country" }));
    await waitFor(() => expect(container.querySelector("svg.deck-chart-map")).not.toBeNull());
    const figure = container.querySelector<HTMLElement>("figure.deck-chart") as HTMLElement;
    expect(figure.dataset.legendPosition).toBe("right");
    expect(container.querySelector(".deck-chart-ramp")).toBeNull();
    const legend = figure.querySelector(":scope > .deck-chart-legend.deck-chart-classes") as HTMLElement;
    expect(rows(legend).map((row) => row.label)).toEqual(["1", "6 - 7", "12", "40", "120 +"]);
    expect(rows(legend).length).toBeLessThanOrEqual(5);
    expect(legend.querySelector("button")).toBeNull();
    expect(legend.querySelectorAll(".deck-chart-swatch[aria-hidden='true']")).toHaveLength(5);
    // A column, the highest class on top, beside the plot; each dot round.
    expect(getComputedStyle(legend).flexDirection).toBe("column-reverse");
    expect(getComputedStyle(legend).gridColumn).toBe("2");
    expect(getComputedStyle(legend.querySelector(".deck-chart-swatch") as Element).borderRadius).toBe("50%");
    // The head holds the title alone.
    expect(figure.querySelector(".deck-chart-head .deck-chart-legend")).toBeNull();
    expect(container.querySelectorAll("circle.deck-chart-marker")).toHaveLength(0);
    expect(container.querySelectorAll("text.deck-chart-value")).toHaveLength(0);
    expect(container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset.valuesMarked).toBe("0");
  });

  it("colours each dot as the countries of its class are filled", async () => {
    measure(SLIDE[0], SLIDE[1]);
    const { container } = onSlide(classed());
    await waitFor(() => expect(container.querySelector("svg.deck-chart-map")).not.toBeNull());
    const legend = container.querySelector(".deck-chart-classes") as HTMLElement;
    const dots = rows(legend).map((row) => row.dot);
    const fill = (code: string) => container.querySelector(`path.deck-chart-country[data-code="${code}"]`)?.getAttribute("fill");
    expect(dots).toEqual([step(0), step(0.25), step(0.5), step(0.75), step(1)]);
    expect(fill("BE")).toBe(dots[0]);
    expect(fill("SA")).toBe(dots[1]);
    expect(fill("AU")).toBe(dots[2]);
    expect(fill("GB")).toBe(dots[2]);
    expect(fill("US")).toBe(dots[3]);
    expect(fill("FR")).toBe(dots[4]);
  });

  it("lays its classes out in a row where the block puts the legend over or under the plot", () => {
    measure(0, 0);
    const under = onSlide(classed({ legend_position: "bottom" })).container;
    const legend = under.querySelector(".deck-chart-classes") as HTMLElement;
    expect((legend.closest("figure") as HTMLElement).dataset.legendPosition).toBe("bottom");
    // The legend's own row (happy-dom reports the unset default as empty), never the column beside the plot.
    expect(getComputedStyle(legend).flexDirection).not.toMatch(/column/);
  });

  it("draws the classes beside the map in a dashboard panel, the title across it", async () => {
    measure(PANEL[0], PANEL[1]);
    const { container } = inPanel(classed({ label_size: "sm" }));
    await waitFor(() => expect(container.querySelector("svg.deck-chart-map")).not.toBeNull());
    const legend = container.querySelector(".deck-chart-classes") as HTMLElement;
    expect(getComputedStyle(legend).gridColumn).toBe("2");
    expect(getComputedStyle(legend).gridRow).toBe("2");
    expect(getComputedStyle(container.querySelector(".deck-panel-title") as Element).gridColumn).toBe("1 / -1");
    expect(container.querySelectorAll("circle.deck-chart-marker")).toHaveLength(0);
  });

  it("draws no legend when told not to, and the ramp when the legend is the ramp", () => {
    measure(0, 0);
    expect(onSlide(classed({ show_legend: false })).container.querySelector(".deck-chart-legend")).toBeNull();
    const ramp = onSlide(classed({ legend_style: "ramp" })).container;
    expect(ramp.querySelector(".deck-chart-classes")).toBeNull();
    expect(ramp.querySelector(".deck-chart-ramp")).not.toBeNull();
    expect(ramp.querySelector<HTMLElement>("figure.deck-chart")?.dataset.legendPosition).toBeUndefined();
  });
});

describe("what a map reads of the other chart fields", () => {
  it("draws its legend by default, pins no unit scale, and publishes its region for the export", () => {
    expect(legendShown(map())).toBe(true);
    expect(unitScaleApplies({ format: "currency", kind: "map" })).toBe(false);
    expect(JSON.parse(chartExportPayload(map({ region: "apac" }), "en", "Unexplained"))).toMatchObject({ kind: "map", region: "apac" });
    expect(JSON.parse(chartExportPayload(map({ kind: "bar", region: undefined }), "en", "Unexplained"))).not.toHaveProperty("region");
  });
});
