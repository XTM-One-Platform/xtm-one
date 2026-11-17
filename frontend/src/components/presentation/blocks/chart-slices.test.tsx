/**
 * A pie's and a donut's values - `yarn test:dom`.
 *
 * Pinned here, on the real Recharts figure in happy-dom: the ring a pie keeps
 * for its values is sized from them, so a value never runs past the canvas
 * edge - the reported pie (`EUR 2.5M` / `EUR 1.6M`, `label_size` md, legend on
 * the right) in a one-third dashboard panel, and the same pie at every span
 * and in the narrow panels of a two-row dashboard; a pie whose values fit
 * keeps today's radius and today's label positions; values too wide for the
 * ring at the size asked are set a size down the text scale, and published as
 * shrunk; a value with no room is counted, never clipped; and with no room
 * outside at the smallest size the values are written inside the slices large
 * enough, inked from the slice. The canvases are the ones Chromium gave the
 * panels on the static render page (1920 x 1080 slide).
 */
import { describe, expect, test } from "vitest";
import { cleanup, render } from "@testing-library/react";

import type { DeckChartBlock } from "@/types";

import ChartCanvas from "./ChartCanvas";
import { chartTextScale, EDGE_PAD, INK_EM, LABEL_HALO, labelRun, labelWidth } from "./chart-layout";
import { DONUT_HOLE, LEADER, sliceArcs, sliceChartLayout, buildSlices } from "./chart-slices";

/** The canvas Chromium gives a chart in each dashboard panel, legend right unless said. */
const CANVAS = {
  span1: { width: 376, height: 574 },
  span1Bottom: { width: 508, height: 544 },
  span2: { width: 958, height: 574 },
  span3: { width: 1539, height: 574 },
  /** A one-column panel of a two-row dashboard with a long legend on its right. */
  twoRows: { width: 240, height: 235 },
  twoRowsSix: { width: 275, height: 235 },
  /** A content slide's full width, `fill`. */
  fullWidth: { width: 1670, height: 684 },
  /** A one-column panel whose legend names take most of it. */
  sliver: { width: 119, height: 574 },
} as const;

/** The reported pie. */
const REVENUE: DeckChartBlock = {
  type: "chart",
  kind: "pie",
  format: "currency",
  unit: "EUR",
  label_size: "md",
  legend_position: "right",
  series: [{ name: "Revenue", values: [2_500_000, 1_600_000] }],
  categories: ["Licences", "Services"],
};
const DONUT: DeckChartBlock = { ...REVENUE, kind: "donut" };
/** Six regions, three of them 5% each, side by side. */
const SIX: DeckChartBlock = {
  type: "chart",
  kind: "pie",
  format: "percent",
  label_size: "md",
  legend_position: "right",
  series: [{ name: "Share", values: [40, 30, 15, 5, 5, 5] }],
  categories: ["EMEA", "Americas", "APAC", "LATAM", "Africa", "Oceania"],
};

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

interface DrawnValue {
  text: string;
  size: number;
  role: string | undefined;
  category: number;
  x: number;
  y: number;
  anchor: string | null;
  baseline: string | null;
  segment: string;
  /** Its ink as the layout measures it: the run toward the edge with its widest glyph's excess. */
  run: Box;
  /** Its ink with its halo, as two values must keep apart. */
  halo: Box;
}

/** A value's ink: its glyphs on its anchor, the height of its figures on its baseline. */
function inkOf(size: number, x: number, y: number, anchor: string | null, baseline: string | null, length: number): Box {
  const height = size * INK_EM;
  const [left, right] = anchor === "middle" ? [x - length / 2, x + length / 2] : anchor === "start" ? [x, x + length] : [x - length, x];
  const [top, bottom] = baseline === "hanging" ? [y, y + height] : baseline === "central" ? [y - height / 2, y + height / 2] : [y - height, y];
  return { left, top, right, bottom };
}

/** The chart drawn at `width` x `height`: its values, its sectors' radius and its facts. */
function draw(block: DeckChartBlock, { width, height }: { width: number; height: number }, hidden: ReadonlySet<number> = new Set()) {
  const { container } = render(
    <ChartCanvas
      block={block}
      width={width}
      height={height}
      hidden={hidden}
      animate={false}
      locale="en"
      onSettled={() => {}}
      gapLabel="Unexplained"
      runningLabel="Running total"
    />,
  );
  const values: DrawnValue[] = Array.from(container.querySelectorAll<SVGTextElement>("text.deck-chart-value")).map((text) => {
    const content = text.textContent ?? "";
    const size = Number.parseFloat(text.style.fontSize);
    const x = Number(text.getAttribute("x"));
    const y = Number(text.getAttribute("y"));
    const anchor = text.getAttribute("text-anchor");
    const baseline = text.getAttribute("dominant-baseline");
    const ink = inkOf(size, x, y, anchor, baseline, labelWidth(content, size));
    const excess = labelRun(content, size) - labelWidth(content, size);
    const run = {
      ...ink,
      left: ink.left - (anchor === "end" ? excess : anchor === "middle" ? excess / 2 : 0),
      right: ink.right + (anchor === "start" ? excess : anchor === "middle" ? excess / 2 : 0),
    };
    const halo = { left: ink.left - LABEL_HALO / 2, top: ink.top - LABEL_HALO / 2, right: ink.right + LABEL_HALO / 2, bottom: ink.bottom + LABEL_HALO / 2 };
    return {
      text: content,
      size,
      role: text.dataset.role,
      category: Number(text.dataset.category),
      x,
      y,
      anchor,
      baseline,
      segment: text.style.getPropertyValue("--deck-segment"),
      run,
      halo,
    };
  });
  // A pie's sector is `M cx,cy L ... A r,r ...`: the radius Recharts drew it at.
  const radii = Array.from(container.querySelectorAll("path.recharts-sector")).map((path) =>
    Number(/A\s*([\d.]+),/.exec(path.getAttribute("d") ?? "")?.[1]),
  );
  const leaders = container.querySelectorAll(".deck-chart-slice-leader").length;
  const facts = { ...(container.querySelector<HTMLElement>(".deck-chart-facts")?.dataset ?? {}) };
  cleanup();
  return { values, radius: Math.max(0, ...radii), leaders, facts };
}

const onCanvas = (box: Box, { width, height }: { width: number; height: number }): boolean =>
  box.left >= EDGE_PAD - 0.01 && box.right <= width - EDGE_PAD + 0.01 && box.top >= EDGE_PAD - 0.01 && box.bottom <= height - EDGE_PAD + 0.01;
const touches = (a: Box, b: Box): boolean => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

/** Every value drawn stays inside the canvas, and no two of them paint over each other. */
function expectReadable(values: readonly DrawnValue[], canvas: { width: number; height: number }) {
  for (const value of values) expect(onCanvas(value.run, canvas), `${value.text} at ${JSON.stringify(value.run)}`).toBe(true);
  values.forEach((value, index) => {
    for (const other of values.slice(index + 1)) expect(touches(value.halo, other.halo), `${value.text} / ${other.text}`).toBe(false);
  });
}

/** Today's radius: the fixed ring every pie kept for its values. */
const todayRadius = ({ width, height }: { width: number; height: number }): number => Math.max(Math.min(width, height) / 2 - 64, 24);

describe("the reported pie in a one-third dashboard panel", () => {
  test("legend right: both values fully inside the canvas, at the size asked, outside with a leader", () => {
    const { values, radius, leaders, facts } = draw(REVENUE, CANVAS.span1);
    expect(values.map((value) => value.text)).toEqual(["€2.5M", "€1.6M"]);
    expect(values.every((value) => value.size === chartTextScale(REVENUE).tick && value.role === "value")).toBe(true);
    expectReadable(values, CANVAS.span1);
    expect(leaders).toBe(2);
    // The fixed 64px ring wrote them past both edges; the ring is now theirs.
    expect(radius).toBeLessThan(todayRadius(CANVAS.span1));
    expect(facts).toMatchObject({ values: "true", valuesAsked: "2", valuesWritten: "2", labelSize: "22", valuesPlacement: "outside" });
    expect(facts.valuesReason).toBeUndefined();
    expect(facts.shrunkSize).toBeUndefined();
  });

  test.each([
    ["span 1, legend bottom", CANVAS.span1Bottom],
    ["span 2", CANVAS.span2],
    ["span 3", CANVAS.span3],
    ["a content slide's full width", CANVAS.fullWidth],
  ] as const)("%s: every value inside the canvas", (_name, canvas) => {
    for (const block of [REVENUE, DONUT]) {
      const { values, facts } = draw(block, canvas);
      expect(values).toHaveLength(2);
      expectReadable(values, canvas);
      expect(facts).toMatchObject({ valuesWritten: "2", labelSize: "22" });
    }
  });

  test("a donut keeps its hole a share of the radius the values leave it", () => {
    const layout = sliceChartLayout(DONUT, CANVAS.span1.width, CANVAS.span1.height, new Set(), "en");
    expect(layout.innerRadius).toBeCloseTo(layout.outerRadius * DONUT_HOLE, 6);
    expectReadable(draw(DONUT, CANVAS.span1).values, CANVAS.span1);
  });
});

describe("a pie whose values fit keeps its look", () => {
  test("label_size sm across a content slide: today's radius, within a pixel", () => {
    const block: DeckChartBlock = { ...REVENUE, label_size: "sm", legend_position: undefined };
    const { radius, values, facts } = draw(block, CANVAS.fullWidth);
    expect(Math.abs(radius - todayRadius(CANVAS.fullWidth))).toBeLessThanOrEqual(1);
    expect(values.every((value) => value.size === 18)).toBe(true);
    expect(facts.shrunkSize).toBeUndefined();
  });

  test("where the ring holds the values, each sits where Recharts writes a pie's label: on the leader's end", () => {
    for (const canvas of [CANVAS.span2, CANVAS.span3, CANVAS.fullWidth]) {
      const { values, radius } = draw(REVENUE, canvas);
      expect(radius).toBeCloseTo(todayRadius(canvas), 6);
      const arcs = sliceArcs(buildSlices(REVENUE, new Set()), 1);
      values.forEach((value) => {
        const arc = arcs.find((each) => each.index === value.category);
        if (!arc) throw new Error(`no slice ${value.category}`);
        const angle = (-Math.PI / 180) * arc.midAngle;
        const x = canvas.width / 2 + Math.cos(angle) * (radius + LEADER);
        const y = canvas.height / 2 + Math.sin(angle) * (radius + LEADER);
        expect(value.x).toBeCloseTo(x, 6);
        expect(value.y).toBeCloseTo(y, 6);
        expect(value.anchor).toBe(x > canvas.width / 2 ? "start" : "end");
        expect(value.baseline).toBe("central");
      });
    }
  });

  test("show_values false, or past the slices a pie labels, keeps the bare disc", () => {
    const quiet = draw({ ...REVENUE, show_values: false }, CANVAS.span1);
    expect(quiet.values).toEqual([]);
    expect(quiet.radius).toBeCloseTo(Math.min(CANVAS.span1.width, CANVAS.span1.height) / 2 - 12, 6);
    expect(quiet.facts).toMatchObject({ values: "false", valuesAsked: "0", valuesWritten: "0" });
    const many: DeckChartBlock = {
      ...SIX,
      series: [{ name: "Share", values: Array.from({ length: 25 }, () => 4) }],
      categories: Array.from({ length: 25 }, (_, index) => `C${index}`),
    };
    const dense = draw(many, CANVAS.span2);
    expect(dense.values).toEqual([]);
    expect(dense.facts).toMatchObject({ valuesAsked: "25", valuesWritten: "0", valuesReason: "dense" });
  });
});

describe("values with no room at the size asked", () => {
  test("a narrow panel sets them a size down the text scale and says so", () => {
    const { values, facts, radius } = draw(REVENUE, CANVAS.twoRows);
    expect(values).toHaveLength(2);
    expectReadable(values, CANVAS.twoRows);
    const size = values[0].size;
    expect(size).toBeLessThan(22);
    expect(chartTextScale(REVENUE).values).toContain(size);
    expect(facts).toMatchObject({ labelSize: String(size), valuesPlacement: "outside", shrunkSize: "22" });
    // At 22px the ring would leave the disc under the least it keeps.
    expect(Number(facts.shrunkRoom)).toBeLessThan(Number(facts.shrunkNeed));
    expect(radius).toBeGreaterThanOrEqual(Number(facts.shrunkNeed) - 1);
  });

  test("six slices, three of them 5%, in narrow panels: each value fits outside or is counted, never clipped", () => {
    const canvases = [CANVAS.twoRowsSix, CANVAS.twoRows, { width: 369, height: 250 }, { width: 300, height: 200 }, { width: 220, height: 180 }];
    for (const block of [SIX, { ...SIX, kind: "donut" as const }]) {
      for (const canvas of canvases) {
        const { values, facts } = draw(block, canvas);
        expectReadable(values, canvas);
        expect(facts.valuesAsked).toBe("6");
        expect(Number(facts.valuesWritten)).toBe(values.length);
        if (values.length < 6) {
          expect(facts.valuesReason).toBe("narrow");
          expect(Number(facts.valuesNeed)).toBeGreaterThan(0);
          // What is left out is a small slice's value: the larger slices are written first.
          expect(values.map((value) => value.text)).toEqual(expect.arrayContaining(["40%", "30%", "15%"]));
          expect(facts.valuesShare).toBe("0.05");
        } else {
          expect(facts.valuesReason).toBeUndefined();
        }
      }
    }
  });

  test("the measured two-row panel: the pie gives up one 5% value at the size asked, the donut writes all six smaller", () => {
    const pie = draw(SIX, CANVAS.twoRowsSix);
    expect(pie.facts).toMatchObject({ valuesWritten: "5", valuesReason: "narrow", valuesShare: "0.05", labelSize: "22" });
    expect(Number(pie.facts.valuesRoom)).toBeLessThan(Number(pie.facts.valuesNeed));
    const donut = draw({ ...SIX, kind: "donut" }, CANVAS.twoRowsSix);
    expect(donut.facts).toMatchObject({ valuesWritten: "6", shrunkSize: "22" });
    expect(donut.facts.valuesReason).toBeUndefined();
  });

  test("with no room outside at the smallest size, the values go inside the slices that hold them", () => {
    const { values, facts, radius, leaders } = draw(REVENUE, CANVAS.sliver);
    // The disc takes the canvas: no ring is kept for values outside it.
    expect(radius).toBeCloseTo(CANVAS.sliver.width / 2 - 12, 6);
    expect(leaders).toBe(0);
    expect(values).toEqual([
      expect.objectContaining({ text: "€2.5M", role: "segment", category: 0, size: 12, anchor: "middle", segment: "var(--deck-chart-1)" }),
    ]);
    expectReadable(values, CANVAS.sliver);
    expect(facts).toMatchObject({
      values: "true",
      valuesAsked: "2",
      valuesWritten: "1",
      valuesReason: "narrow",
      valuesPlacement: "inside",
      labelSize: "12",
      shrunkSize: "22",
    });
    // The services slice would hold its value in a larger disc only.
    expect(Number(facts.valuesRoom)).toBeLessThan(Number(facts.valuesNeed));
  });

  test("a slice the legend hides writes nothing, and the others are laid out without it", () => {
    const { values, facts } = draw(SIX, CANVAS.twoRowsSix, new Set([0]));
    expect(values.some((value) => value.category === 0)).toBe(false);
    expect(facts.valuesAsked).toBe("5");
    expectReadable(values, CANVAS.twoRowsSix);
  });
});
