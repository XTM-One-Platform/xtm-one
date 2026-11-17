/**
 * The gauge's box - `yarn test:dom`.
 *
 * A gauge is measured by what it draws - its half disc, the reading written
 * inside on its baseline - and that box is centred in the canvas: in a third
 * of a dashboard's panel grid the dial no longer stands on the panel's floor
 * under an empty half. `size` sets the dial's diameter, `fill` draws the
 * largest the canvas holds, and neither lets the box leave the canvas
 * (`chart-gauge.ts`). The drawn dial is read off the track Recharts draws.
 *
 * The other kinds are pinned by a snapshot of every mark they draw at the two
 * boxes a gauge was measured in - a third of a dashboard's panel grid and a
 * full-width content slide - recorded before the gauge's box existed, so a
 * change to the gauge's layout cannot move a bar, a line or a slice.
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";
import { GAUGE_DIAMETERS, GAUGE_INSET, gaugeArcPath, gaugeLayout, gaugeReadingHeight } from "./chart-gauge";

/** The canvas of a panel in a third of the dashboard grid, one row of panels. */
const PANEL = { width: 508, height: 574 } as const;
/** The canvas of a `md` chart across a content slide. */
const SLIDE = { width: 1670, height: 460 } as const;

const QUARTERS = ["Q1", "Q2", "Q3", "Q4"];

function draw(block: DeckChartBlock, { width, height }: { width: number; height: number }) {
  return render(
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
}

const GEOMETRY = ["d", "x", "y", "width", "height", "cx", "cy", "r", "x1", "y1", "x2", "y2", "points", "transform"];
const PAINT = ["fill", "stroke", "text-anchor", "style"];
const rounded = (value: string) => value.replace(/-?\d+\.\d+/g, (number) => String(Math.round(Number(number) * 100) / 100));

/** Every mark of the drawing, in document order: its geometry, its paint and its text; then the HTML it lays over the plot. */
function marks(container: HTMLElement): string[] {
  const svg = container.querySelector("svg.recharts-surface") as SVGSVGElement;
  const drawn = Array.from(svg.querySelectorAll("path, rect, line, circle, text, polyline, polygon")).map((element) => {
    const attributes = [...GEOMETRY, ...PAINT]
      .filter((name) => element.hasAttribute(name))
      .map((name) => `${name}=${rounded(element.getAttribute(name) as string)}`);
    const text = element.tagName === "text" ? ` "${element.textContent}"` : "";
    return `${element.tagName} ${attributes.join(" ")}${text}`;
  });
  const overlays = Array.from(container.querySelectorAll<HTMLElement>("[class^='deck-chart']"))
    .filter((element) => !(element instanceof SVGElement))
    .map((element) => `${element.className} ${element.getAttribute("style") ?? ""} "${element.textContent ?? ""}"`);
  return [...drawn, ...overlays];
}

const OTHER_KINDS: Record<string, DeckChartBlock> = {
  bar: { type: "chart", kind: "bar", categories: QUARTERS, series: [{ name: "Trials", values: [64, 78, 91, 112] }], target: 100 },
  stacked_bar: {
    type: "chart",
    kind: "stacked_bar",
    categories: QUARTERS,
    series: [
      { name: "Critical", values: [12, 9, 14, 8] },
      { name: "High", values: [30, 34, 28, 31] },
      { name: "Medium", values: [42, 40, 45, 39] },
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
  pie: { type: "chart", kind: "pie", categories: ["CE", "EE", "Trial"], series: [{ name: "Share", values: [54, 36, 10] }], format: "percent" },
  donut: { type: "chart", kind: "donut", categories: ["CE", "EE", "Trial"], series: [{ name: "Installs", values: [5400, 3600, 1000] }] },
};

/** The issue's two dials: 8 of a target of 10, and 27.5 of 40 written as currency. */
const COVERAGE: DeckChartBlock = {
  type: "chart",
  kind: "gauge",
  categories: ["Coverage"],
  series: [{ name: "Coverage", values: [8] }],
  target: 10,
  goal_label: "Target",
};
const SPEND: DeckChartBlock = {
  type: "chart",
  kind: "gauge",
  categories: ["Spend"],
  series: [{ name: "Spend", values: [27.5] }],
  target: 40,
  goal_label: "Scale",
  format: "currency",
};

/** The dial as drawn: the track's outer arc starts at its left end, on the baseline (`M cx-R,cy A R,R ...`). */
function dial(container: HTMLElement) {
  const track = container.querySelector(".recharts-pie-sector path") as SVGPathElement;
  const [startX, baseline, radius] = (track.getAttribute("d") as string)
    .match(/M\s*(-?[\d.]+),(-?[\d.]+)\s*A\s*(-?[\d.]+)/)!
    .slice(1)
    .map(Number) as number[];
  const reading = container.querySelector<HTMLElement>(".deck-chart-gauge") as HTMLElement;
  return {
    left: startX,
    baseline,
    radius,
    readingBottom: Number.parseFloat(reading.style.bottom),
    readingSize: Number.parseFloat(reading.style.fontSize),
    value: container.querySelector(".deck-chart-gauge-value")?.textContent,
    goal: container.querySelector(".deck-chart-gauge-max")?.textContent,
  };
}

describe("the gauge's box", () => {
  it.each([
    ["8 of 10", COVERAGE, "8", "Target 10"],
    ["27.5 of 40 in euros", SPEND, "€27.5", "Scale €40"],
  ])("stands %s in the middle of a third of the panel grid, with as much room above as below", (_name, block, value, goal) => {
    const drawn = dial(draw(block, PANEL).container);
    const above = drawn.baseline - drawn.radius;
    const below = PANEL.height - drawn.baseline;
    expect(Math.abs(above - below)).toBeLessThanOrEqual(1);
    expect(drawn.left).toBeCloseTo(PANEL.width / 2 - drawn.radius, 1);
    // The reading stands on the baseline, inside the half disc it is centred with.
    expect(drawn.readingBottom).toBeCloseTo(below, 1);
    expect(gaugeReadingHeight(drawn.readingSize)).toBeLessThan(drawn.radius);
    expect([drawn.value, drawn.goal]).toEqual([value, goal]);
  });

  it("draws sm, md and lg as three diameters, each centred", () => {
    const radii = (["sm", "md", "lg"] as const).map((size) => {
      const drawn = dial(draw({ ...COVERAGE, size }, PANEL).container);
      expect(Math.abs(drawn.baseline - drawn.radius - (PANEL.height - drawn.baseline))).toBeLessThanOrEqual(1);
      return drawn.radius;
    });
    expect(radii.map((radius) => radius * 2)).toEqual([GAUGE_DIAMETERS.sm, GAUGE_DIAMETERS.md, GAUGE_DIAMETERS.lg]);
  });

  it("grows to the room with fill - the panel's width, the canvas' height when that is shorter - reading inside", () => {
    const wide = dial(draw({ ...SPEND, fill: true }, PANEL).container);
    expect(wide.radius).toBe(PANEL.width / 2 - GAUGE_INSET);
    expect(wide.left).toBe(GAUGE_INSET);
    expect(wide.baseline - wide.radius).toBeCloseTo(PANEL.height - wide.baseline, 1);
    const short = { width: 508, height: 235 };
    const low = dial(draw({ ...SPEND, fill: true }, short).container);
    expect(low.radius).toBe(short.height - 2 * GAUGE_INSET);
    expect(low.baseline - low.radius).toBe(GAUGE_INSET);
    expect(short.height - low.baseline).toBe(GAUGE_INSET);
    expect(gaugeReadingHeight(low.readingSize)).toBeLessThan(low.radius);
    // `size: "full"` is `fill`, as the stylesheet reads it.
    expect(dial(draw({ ...SPEND, size: "full" }, short).container).radius).toBe(low.radius);
  });

  it("never draws a size past the canvas, its box inside it whatever the size", () => {
    for (const [width, height] of [
      [508, 574],
      [508, 235],
      [481, 300],
      [1670, 460],
      [300, 160],
      [240, 120],
    ]) {
      for (const block of [{ size: "sm" }, { size: "md" }, { size: "lg" }, { fill: true }] as const) {
        const layout = gaugeLayout(block, width, height);
        expect(layout.radius * 2).toBeLessThanOrEqual(width - 2 * GAUGE_INSET);
        expect(layout.box.top).toBeGreaterThanOrEqual(GAUGE_INSET - 0.5);
        expect(layout.box.top + layout.box.height).toBeLessThanOrEqual(height - GAUGE_INSET + 0.5);
        expect(layout.box.top - (height - layout.box.top - layout.box.height)).toBeCloseTo(0, 6);
        expect(layout.cy).toBeCloseTo(layout.box.top + layout.box.height, 6);
      }
    }
  });

  it("centres the reading where a small dial's reading stands taller than its radius", () => {
    const layout = gaugeLayout({}, 120, 300);
    expect(layout.radius).toBe(120 / 2 - GAUGE_INSET);
    expect(layout.box.height).toBe(gaugeReadingHeight(layout.readingSize));
    expect(layout.box.height).toBeGreaterThan(layout.radius);
    expect(layout.box.top).toBeCloseTo((300 - layout.box.height) / 2, 6);
  });
});

/** A path's commands, each with its numbers. */
const commands = (d: string) =>
  Array.from(d.matchAll(/([MALZ])([^MALZ]*)/g), ([, command, args]) => ({
    command,
    numbers: (args.match(/-?[\d.]+/g) ?? []).map(Number),
  }));

/** The point at `radius` and `angle` (degrees, counter-clockwise from 3 o'clock) around 0,0. */
const pointAt = (radius: number, angle: number) => [
  radius * Math.cos((-angle * Math.PI) / 180),
  radius * Math.sin((-angle * Math.PI) / 180),
];

describe("the reading arc", () => {
  it("starts square on the track's own start, its first edge straight across the ring on the baseline", () => {
    const { container } = draw(SPEND, PANEL);
    const track = commands(container.querySelector(".recharts-pie-sector path")?.getAttribute("d") as string);
    const arc = commands(container.querySelector(".deck-chart-gauge-arc")?.getAttribute("d") as string);
    const { baseline, radius } = dial(container);
    const inner = radius - Math.min(radius * 0.26, 56);
    expect(arc[0]).toEqual({ command: "M", numbers: track[0].numbers });
    expect(arc[0].numbers).toEqual([PANEL.width / 2 - radius, baseline]);
    // The last arc comes back to the inner start on the baseline; `Z` closes
    // the straight edge from there to the outer start.
    const back = arc[arc.length - 2];
    expect(back.command).toBe("A");
    const [endX, endY] = back.numbers.slice(-2);
    expect(endX).toBeCloseTo(PANEL.width / 2 - inner, 1);
    expect(endY).toBe(baseline);
    expect(arc[arc.length - 1].command).toBe("Z");
    // 27.5 of 40: a rounded tip, a half disc as wide as the ring.
    expect(arc[2]).toMatchObject({ command: "A" });
    expect(arc[2].numbers[0]).toBeCloseTo((radius - inner) / 2, 1);
  });

  it("rounds its tip into a half disc that ends at the value", () => {
    const d = gaugeArcPath({ cx: 0, cy: 0, innerRadius: 80, outerRadius: 100, startAngle: 180, endAngle: 90 }, true);
    const [move, outer, cap, inner, close] = commands(d);
    expect(move.numbers).toEqual(pointAt(100, 180).map((n) => Math.round(n * 100) / 100 || 0));
    // The cap's base stands back from the value by its own radius along the
    // ring's middle, so the half disc reaches the value and no further.
    const base = 90 + ((10 / 90) * 180) / Math.PI;
    const [x, y] = pointAt(100, base);
    expect(outer.numbers.slice(-2)[0]).toBeCloseTo(x, 1);
    expect(outer.numbers.slice(-2)[1]).toBeCloseTo(y, 1);
    expect(cap.numbers.slice(0, 2)).toEqual([10, 10]);
    expect(cap.numbers.slice(-2)[0]).toBeCloseTo(pointAt(80, base)[0], 1);
    expect(inner.numbers.slice(-2)).toEqual([-80, 0]);
    expect(close.command).toBe("Z");
  });

  it("ends square on the track's other end when the reading fills the dial", () => {
    const d = gaugeArcPath({ cx: 0, cy: 0, innerRadius: 80, outerRadius: 100, startAngle: 180, endAngle: 0 }, false);
    expect(commands(d).map((step) => step.command)).toEqual(["M", "A", "L", "A", "Z"]);
    expect(commands(d)[1].numbers.slice(-2)).toEqual([100, 0]);
    expect(commands(d)[2].numbers).toEqual([80, 0]);
    // The drawing asks for that square end exactly when the value reaches the target.
    const full = draw({ ...COVERAGE, series: [{ name: "Coverage", values: [10] }] }, PANEL).container;
    const arc = full.querySelector(".deck-chart-gauge-arc")?.getAttribute("d") as string;
    expect(commands(arc).map((step) => step.command)).toEqual(["M", "A", "L", "A", "Z"]);
  });

  it("ends square at the value when the arc is too short to hold its cap, and draws nothing at zero", () => {
    const short = gaugeArcPath({ cx: 0, cy: 0, innerRadius: 80, outerRadius: 100, startAngle: 180, endAngle: 176 }, true);
    expect(commands(short).map((step) => step.command)).toEqual(["M", "A", "L", "A", "Z"]);
    expect(gaugeArcPath({ cx: 0, cy: 0, innerRadius: 80, outerRadius: 100, startAngle: 180, endAngle: 180 }, true)).toBe("");
  });
});

describe("the other kinds keep their drawing", () => {
  for (const [kind, block] of Object.entries(OTHER_KINDS)) {
    for (const [where, box] of Object.entries({ panel: PANEL, slide: SLIDE })) {
      it(`draws a ${kind} chart in a ${where} as before`, () => {
        const { container } = draw(block, box);
        expect(marks(container)).toMatchSnapshot();
      });
    }
  }
});
