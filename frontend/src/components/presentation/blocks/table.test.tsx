// A table's `size` and `fill`: the type and padding of its cells,
// headers and footnote at each size and density - `md` the look a table
// always had - and the root the `fill` spread is wired on.
//
// The real stylesheet and the real component. happy-dom substitutes custom
// properties but evaluates no `calc()`, so a computed length reads as the
// expression the cascade built (`calc(18px * 1.25)`), which `px` evaluates;
// it drops a shorthand built of `calc()`, so the padding is read off the
// scale the cell rule multiplies its base by (`--deck-table-scale`).

import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckDensity, DeckSize, DeckTableBlock } from "@/types";
import { BlockList } from "./index";

const stylesheet = readFileSync(`${__dirname}/../presentation.css`, "utf8");

beforeAll(() => {
  const style = document.createElement("style");
  style.textContent = stylesheet;
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

/** Six columns, six rows, a footnote. */
const table = (
  size: DeckSize | undefined,
  density: DeckDensity = "normal",
  extra: Partial<DeckTableBlock> = {},
): DeckTableBlock => ({
  type: "table",
  headers: ["Metric", "Q1", "Q2", "Q3", "Q4", "Year"].map((text) => ({ text })),
  rows: Array.from({ length: 6 }, (_, row) =>
    Array.from({ length: 6 }, (_, cell) => ({ text: cell === 0 ? `Line ${row + 1}` : `${(row + 1) * (cell + 1)}` })),
  ),
  density,
  footnote: "Figures in EUR M, unaudited.",
  ...(size ? { size } : {}),
  ...extra,
});

/** The table in a slide body - the run the `fill` hook hands the free height to. */
function renderBody(block: DeckTableBlock) {
  return renderWithProviders(
    <div className="deck-root">
      <BlockList blocks={[block]} className="deck-blocks deck-blocks-slide" path="blocks" />
    </div>,
  );
}

/** What the table is drawn with, in canvas px. */
function drawn(container: HTMLElement) {
  const root = container.querySelector<HTMLElement>(".deck-table-block") as HTMLElement;
  const grid = root.querySelector<HTMLTableElement>(".deck-table") as HTMLTableElement;
  const fontOf = (selector: string) => px(getComputedStyle(root.querySelector(selector) as HTMLElement).fontSize);
  const scale = Number.parseFloat(getComputedStyle(root).getPropertyValue("--deck-table-scale"));
  const base = { normal: [18, 28], compact: [12, 22], tight: [8, 16] }[grid.dataset.density as DeckDensity];
  return {
    attributes: { size: root.dataset.size, fill: root.dataset.fill ?? null, density: grid.dataset.density },
    header: fontOf("th"),
    cell: fontOf("td"),
    footnote: fontOf(".deck-table-footnote"),
    padding: base.map((side) => Math.round(side * scale * 100) / 100),
  };
}

describe("table size", () => {
  it.each(["sm", "md", "lg"] as const)("draws its cells at size %s", (size) => {
    expect(drawn(renderBody(table(size)).container)).toMatchSnapshot();
  });

  it("is md when unset, and md is the look a table always had at every density", () => {
    const historical = {
      normal: { header: 24, cell: 24, padding: [18, 28] },
      compact: { header: 24, cell: 24, padding: [12, 22] },
      tight: { header: 20, cell: 20, padding: [8, 16] },
    } as const;
    for (const density of ["normal", "compact", "tight"] as const) {
      const unset = drawn(renderBody(table(undefined, density)).container);
      expect(unset.attributes).toEqual({ size: "md", fill: null, density });
      expect(unset).toMatchObject({ ...historical[density], footnote: 20 });
      expect(drawn(renderBody(table("md", density)).container)).toEqual(unset);
    }
  });

  it("steps the type one size each way and scales the padding, tight keeping its step down", () => {
    for (const density of ["normal", "compact", "tight"] as const) {
      const md = drawn(renderBody(table("md", density)).container);
      const sm = drawn(renderBody(table("sm", density)).container);
      const lg = drawn(renderBody(table("lg", density)).container);
      // lg: cells and headers one step up (sm -> md, tight's xs -> sm), the footnote xs -> sm.
      expect(lg.cell).toBe(density === "tight" ? 24 : 28);
      expect(lg.header).toBe(lg.cell);
      expect(lg.footnote).toBe(24);
      expect(lg.padding).toEqual(md.padding.map((side) => Math.round(side * 1.25 * 100) / 100));
      // sm: one step down, and never under the fine-print floor.
      expect(sm.cell).toBe(20);
      expect(sm.header).toBe(20);
      expect(sm.footnote).toBe(20);
      expect(sm.padding).toEqual(md.padding.map((side) => Math.round(side * 0.8 * 100) / 100));
    }
  });

  it("emits data-fill on the root the run stretches, and nothing without fill", () => {
    const packed = renderBody(table("md")).container.querySelector<HTMLElement>(".deck-table-block") as HTMLElement;
    expect(packed.dataset.fill).toBeUndefined();
    expect(packed.style.maxHeight).toBe("");
    const filled = renderBody(table("md", "normal", { fill: true })).container.querySelector<HTMLElement>(
      ".deck-table-block",
    ) as HTMLElement;
    expect(filled.dataset.fill).toBe("true");
    expect(filled.matches(".deck-blocks > *")).toBe(true);
    // The footnote stays outside the clipping wrap, after it.
    expect(filled.lastElementChild?.className).toBe("deck-table-footnote");
    expect(filled.querySelector(".deck-table-wrap > .deck-table-footnote")).toBeNull();
  });
});
