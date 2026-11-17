/**
 * A chart's pinned unit scale - `yarn test:dom`.
 *
 * One drawing per chart kind, read off the SVG the way the PPTX extractor
 * reads it: with `unit_scale: "M"` every value label and every tick of a
 * currency axis chart is written in millions, the chart's decimals applied;
 * a pie, a donut and a gauge draw exactly what they draw without it; and
 * `auto` draws exactly what an unset scale draws. The rules themselves are in
 * `chart-format.test.tsx`, their PPTX number formats in
 * `backend/tests/test_chart_export.py`.
 */
import { describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

import type { DeckChartBlock, DeckChartKind } from "@/types";

import ChartCanvas from "./ChartCanvas";

const QUARTERS = ["Q1", "Q2", "Q3", "Q4"];
const AMOUNTS = [480_000, 1_200_000, 2_500_000, 3_500_000];

/** Every axis kind, currency, one decimal, pinned to millions. */
const PINNED = {
  bar: { kind: "bar", series: [{ name: "ARR", values: AMOUNTS }] },
  stacked_bar: {
    kind: "stacked_bar",
    series: [
      { name: "Services", values: [480_000, 410_000, 620_000, 700_000] },
      { name: "Licences", values: [1_200_000, 1_350_000, 1_500_000, 1_850_000] },
    ],
  },
  line: { kind: "line", series: [{ name: "ARR", values: AMOUNTS }] },
  area: { kind: "area", series: [{ name: "ARR", values: AMOUNTS }] },
  combo: {
    kind: "combo",
    series: [
      { name: "Bookings", kind: "bar", values: AMOUNTS },
      { name: "Plan", kind: "line", values: [600_000, 1_100_000, 2_500_000, 3_500_000] },
    ],
  },
} satisfies Record<string, Pick<DeckChartBlock, "kind" | "series">>;

const axisChart = (shape: Pick<DeckChartBlock, "kind" | "series">): DeckChartBlock => ({
  type: "chart",
  categories: QUARTERS,
  format: "currency",
  unit: "EUR",
  decimals: 1,
  unit_scale: "M",
  ...shape,
});

/** Two rises, a fall under a million, the ending. */
const BRIDGE: DeckChartBlock = {
  type: "chart",
  kind: "waterfall",
  format: "currency",
  unit: "EUR",
  decimals: 2,
  unit_scale: "M",
  categories: ["Licences", "Services", "Churn", "Ending"],
  series: [{ name: "ARR", values: [2_500_000, 1_250_000, -250_000, 3_500_000] }],
  totals: [3],
};

/** A share of revenue in euros, pinned to millions - which a pie, a donut and a gauge ignore. */
const SHARE: DeckChartBlock = {
  type: "chart",
  kind: "pie",
  format: "currency",
  unit: "EUR",
  unit_scale: "M",
  categories: ["CE", "EE"],
  series: [{ name: "Revenue", values: [480_000, 1_200_000] }],
};

/** The kinds the scale does not apply to, with one set anyway. */
const IGNORED: Record<Extract<DeckChartKind, "pie" | "donut" | "gauge">, DeckChartBlock> = {
  pie: SHARE,
  donut: { ...SHARE, kind: "donut" },
  gauge: { ...SHARE, kind: "gauge", target: 1_000_000, categories: ["Bookings"], series: [{ name: "Bookings", values: [480_000] }] },
};

async function draw(block: DeckChartBlock) {
  const onSettled = vi.fn();
  const { container, unmount } = render(
    <div style={{ width: 1200, height: 560 }}>
      <ChartCanvas
        block={block}
        width={1200}
        height={560}
        hidden={new Set()}
        animate={false}
        locale="en"
        onSettled={onSettled}
        gapLabel="Unexplained"
        runningLabel="Running total"
      />
    </div>,
  );
  await waitFor(() => expect(onSettled).toHaveBeenCalled());
  const texts = (selector: string) =>
    Array.from(container.querySelectorAll(selector)).map((node) => (node.textContent ?? "").trim()).filter(Boolean);
  // Recharts numbers its clip paths and series ids per mount; nothing else in the drawing depends on the mount.
  const html = () =>
    new XMLSerializer()
      .serializeToString(container)
      .replace(/recharts\d+-clip/g, "recharts-clip")
      .replace(/_r_[0-9a-z]+_/g, "_r_");
  return { texts, html, unmount };
}

/** A currency amount in millions with one decimal: `€0.5M`, `-€0.2M`. */
const IN_MILLIONS = /^-?€\d+\.\dM$/;

describe("a pinned unit scale on every axis kind", () => {
  it.each(Object.entries(PINNED))("writes every value and tick of a %s chart in millions", async (_kind, shape) => {
    const { texts } = await draw(axisChart(shape));
    await waitFor(() => expect(texts(".deck-chart-value").length).toBeGreaterThan(0));
    const values = texts(".deck-chart-value");
    expect(values.filter((text) => !IN_MILLIONS.test(text))).toEqual([]);
    expect(values).toContain("€0.5M");
    expect(values).toContain("€1.2M");
    const ticks = texts(".deck-chart-tick-primary");
    expect(ticks.length).toBeGreaterThan(1);
    expect(ticks.filter((text) => !IN_MILLIONS.test(text))).toEqual([]);
    expect(ticks[0]).toBe("€0.0M");
  });

  it("writes the waterfall's steps and level in millions, the fall under a million too", async () => {
    const { texts } = await draw(BRIDGE);
    await waitFor(() => expect(texts(".deck-chart-value")).toHaveLength(4));
    const byBar = texts(".deck-chart-value");
    expect([...byBar].sort()).toEqual(["+€1.25M", "+€2.50M", "-€0.25M", "€3.50M"]);
    expect(texts(".deck-chart-tick-primary").every((tick) => /^€\d+\.\d\dM$/.test(tick))).toBe(true);
  });

  it("writes a stack whose segments are mixed under and over a million in one letter", async () => {
    const { texts } = await draw(axisChart({ ...PINNED.stacked_bar }));
    await waitFor(() => expect(texts(".deck-chart-value").length).toBeGreaterThan(0));
    expect(texts(".deck-chart-value").some((text) => text.endsWith("k"))).toBe(false);
  });

  it.each(Object.entries(IGNORED))("draws a %s exactly as it draws without a scale", async (_kind, block) => {
    const pinned = await draw(block);
    const pinnedHtml = pinned.html();
    pinned.unmount();
    const { unit_scale: _ignored, ...plain } = block;
    const without = await draw(plain);
    expect(pinnedHtml).toBe(without.html());
  });

  it.each([...Object.values(PINNED).map(axisChart), BRIDGE])("draws auto exactly as unset ($kind)", async (block) => {
    const { unit_scale: _pinned, ...plain } = block;
    const unset = await draw(plain);
    const unsetHtml = unset.html();
    unset.unmount();
    const auto = await draw({ ...plain, unit_scale: "auto" });
    expect(auto.html()).toBe(unsetHtml);
  });
});
