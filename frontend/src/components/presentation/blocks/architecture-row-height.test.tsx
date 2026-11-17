/**
 * One card height per architecture row when logo and icon cards mix - `yarn test:dom`.
 *
 * A row that mixes logo cards (sized by `logo_height` or in the fixed box)
 * and tiled-icon cards shares one media size: the icon tile grows with the
 * row's logo step, never under `ARCH.ICON_TILE`, and every card of the row is
 * as tall as its tallest content, its label on the row's one line. A row of
 * one kind keeps its geometry. Every asset is a placeholder.
 */
import { describe, expect, test } from "vitest";

import type { DeckArchitectureBlock, DeckArchitectureNode, DeckAssetMeta, DeckLogoHeight } from "@/types";

import { ARCH, layoutArchitecture, type LayoutNode } from "./architecture-layout";

/** Placeholder file ids: a wide wordmark and a square mark, each on a transparent margin. */
const WORDMARK = "00000000-0000-4000-8000-00000000d001";
const SQUARE = "00000000-0000-4000-8000-00000000d002";
const ASSETS: Record<string, DeckAssetMeta> = {
  [WORDMARK]: { w: 500, h: 150, trim: [0.1, 1 / 6, 0.9, 5 / 6], rev: "wordmark" },
  [SQUARE]: { w: 200, h: 200, trim: [0.1, 0.1, 0.9, 0.9], rev: "square" },
};

const edges = [1, 2, 3, 4].map((i) => ({ source: `n${i}`, target: `n${i + 1}` }));

/** Five cards on one row: logo, icon, logo, icon, logo. */
function mixed(logoHeight: DeckLogoHeight | undefined): DeckArchitectureBlock {
  const nodes: DeckArchitectureNode[] = [1, 2, 3, 4, 5].map((col) =>
    col % 2
      ? {
          id: `n${col}`,
          label: `Stage ${col}`,
          sublabel: "Vendor",
          logo_file_id: col === 3 ? SQUARE : WORDMARK,
          ...(logoHeight ? { logo_height: logoHeight } : {}),
          row: 1,
          col,
        }
      : { id: `n${col}`, label: `Stage ${col}`, sublabel: "Built in", icon: "shield", row: 1, col },
  );
  return { type: "architecture", nodes, edges } as DeckArchitectureBlock;
}

const cards = (block: DeckArchitectureBlock) => layoutArchitecture(block, { assets: ASSETS }).nodes;
const spread = (values: number[]) => Math.max(...values) - Math.min(...values);

describe("a row that mixes logo and icon cards", () => {
  test.each(["sm", "md", "lg", "xl"] as const)("%s: five cards of one height and one width", (step) => {
    const nodes = cards(mixed(step));
    expect(spread(nodes.map((node) => node.h))).toBeLessThanOrEqual(0.01);
    expect(spread(nodes.map((node) => node.w))).toBeLessThanOrEqual(0.01);
  });

  test.each(["sm", "md", "lg", "xl"] as const)("%s: labels and sublabels on one line", (step) => {
    const nodes = cards(mixed(step));
    expect(spread(nodes.map((node) => node.y + node.labelBox.y))).toBeLessThanOrEqual(0.01);
    expect(spread(nodes.map((node) => node.y + node.sublabelBox!.y))).toBeLessThanOrEqual(0.01);
  });

  test.each(["sm", "md", "lg", "xl"] as const)("%s: the icon tile and the logo box are one size", (step) => {
    const nodes = cards(mixed(step));
    const boxes = nodes.map((node) => node.mediaBox!.h);
    expect(spread(boxes)).toBeLessThanOrEqual(0.01);
    const tile = nodes.find((node) => node.media === "icon")!.mediaRect!;
    expect(tile.w).toBeCloseTo(tile.h, 5);
    expect(tile.h).toBeCloseTo(boxes[0], 5);
    expect(tile.h).toBeGreaterThanOrEqual(ARCH.ICON_TILE);
  });

  test("the tile grows with the row's step and stays inside the card", () => {
    const tile = (step: DeckLogoHeight) => cards(mixed(step)).find((node) => node.media === "icon")!;
    expect(tile("lg").mediaRect!.h).toBeGreaterThan(tile("sm").mediaRect!.h);
    for (const step of ["sm", "md", "lg", "xl"] as const) {
      const node: LayoutNode = tile(step);
      expect(node.mediaRect!.y + node.mediaRect!.h).toBeLessThanOrEqual(node.labelBox.y);
      expect(node.mediaRect!.x).toBeGreaterThanOrEqual(0);
      expect(node.mediaRect!.x + node.mediaRect!.w).toBeLessThanOrEqual(node.w + 1e-6);
    }
  });

  test("a card with a longer label stretches the whole row, the others keep the row's line", () => {
    const block = mixed("md");
    block.nodes[1] = { ...block.nodes[1], label: "A much longer stage label that wraps" };
    const nodes = cards(block);
    expect(spread(nodes.map((node) => node.h))).toBeLessThanOrEqual(0.01);
    expect(spread(nodes.map((node) => node.y + node.labelBox.y))).toBeLessThanOrEqual(0.01);
  });
});

describe("a row of one kind keeps its geometry", () => {
  test("icon cards only: the 52 px tile", () => {
    const block = mixed("md");
    block.nodes = block.nodes.map((node, i) => ({ id: node.id, label: node.label, icon: "shield", row: 1, col: i + 1 }));
    for (const node of cards(block)) {
      expect(node.mediaRect!.h).toBe(ARCH.ICON_TILE);
      expect(node.mediaBox!.h).toBe(ARCH.ICON_TILE);
    }
  });

  test("unsized logos mixed with icons: the fixed boxes, as before", () => {
    for (const node of cards(mixed(undefined))) {
      expect(node.mediaBox!.h).toBe(node.media === "icon" ? ARCH.ICON_TILE : ARCH.LOGO_H);
    }
  });

  test("unsized logo cards only: the fixed ARCH.LOGO_H box", () => {
    const block = mixed(undefined);
    block.nodes = block.nodes.map((node, i) => ({ id: node.id, label: node.label, logo_file_id: WORDMARK, row: 1, col: i + 1 }));
    for (const node of cards(block)) expect(node.mediaBox!.h).toBe(ARCH.LOGO_H);
  });

  test("a mixed row leaves the other rows alone", () => {
    const block = mixed("lg");
    block.nodes.push({ id: "below", label: "Below", icon: "radar", row: 2, col: 3 });
    const below = cards(block).find((node) => node.id === "below")!;
    expect(below.mediaRect!.h).toBe(ARCH.ICON_TILE);
  });
});
