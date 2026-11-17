/**
 * `logo_height` on an architecture node - `yarn test:dom`.
 *
 * A card's or band's logo sized on the `logo_grid` steps (sm 28 / md 40 /
 * lg 56 / xl 72 canvas px), cropped to its ink and weighted by optical
 * balance, the card growing to fit. Unset, the node keeps the fixed
 * `ARCH.LOGO_H` box fitted on the whole file. Every asset is a placeholder.
 */
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckArchitectureBlock, DeckArchitectureNode, DeckAssetMeta, DeckLogoHeight } from "@/types";

import { DeckAssetMetaProvider, DeckAssetProvider } from "../asset-context";
import { LOGO_HEIGHT_PX } from "../logo-sizing";
import { ArchitectureBlockView } from "./Architecture";
import { ARCH, SIZED_FIT_MIN, SIZED_LOGO_INSET, layoutArchitecture, nodeLogoSize, type LayoutNode } from "./architecture-layout";

vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

/** Placeholder file ids: a wide wordmark (4:1 of ink) and a square mark, each on a transparent margin. */
const WORDMARK = "00000000-0000-4000-8000-00000000c001";
const SQUARE = "00000000-0000-4000-8000-00000000c002";
const ASSETS: Record<string, DeckAssetMeta> = {
  [WORDMARK]: { w: 500, h: 150, trim: [0.1, 1 / 6, 0.9, 5 / 6], rev: "wordmark" },
  [SQUARE]: { w: 200, h: 200, trim: [0.1, 0.1, 0.9, 0.9], rev: "square" },
};
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#8a94a6"/></svg>';
const SRC = `data:image/svg+xml;base64,${btoa(SVG)}`;

function row(logoHeight: DeckLogoHeight | undefined, extra: Partial<DeckArchitectureNode> = {}): DeckArchitectureBlock {
  const nodes: DeckArchitectureNode[] = [1, 2, 3, 4, 5].map((col) => ({
    id: `n${col}`,
    label: `Stage ${col}`,
    logo_file_id: col % 2 ? WORDMARK : SQUARE,
    ...(logoHeight ? { logo_height: logoHeight } : {}),
    row: 1,
    col,
    ...extra,
  }));
  return { type: "architecture", nodes, edges: [1, 2, 3, 4].map((i) => ({ source: `n${i}`, target: `n${i + 1}` })) } as DeckArchitectureBlock;
}

const byId = (nodes: LayoutNode[], id: string) => nodes.find((node) => node.id === id)!;

describe("logo_height on a card", () => {
  test("unset keeps the fixed box: the media box is ARCH.LOGO_H high, the logo fitted on the whole file", () => {
    const layout = layoutArchitecture(row(undefined), { assets: ASSETS });
    for (const node of layout.nodes) {
      expect(node.mediaBox!.h).toBe(ARCH.LOGO_H);
      expect(nodeLogoSize(node.node, ASSETS)).toBeNull();
    }
  });

  test("lg draws every logo of a five-node row at 56 px or more, and the card grows to fit", () => {
    const small = layoutArchitecture(row(undefined), { assets: ASSETS });
    const layout = layoutArchitecture(row("lg"), { assets: ASSETS });
    for (const node of layout.nodes) {
      expect(node.mediaRect!.h).toBeGreaterThanOrEqual(LOGO_HEIGHT_PX.lg);
      expect(node.h).toBeGreaterThan(byId(small.nodes, node.id).h);
      // The content stays inside the card.
      expect(node.mediaRect!.y + node.mediaRect!.h).toBeLessThanOrEqual(node.labelBox.y);
      expect(node.mediaRect!.w).toBeLessThanOrEqual(node.w + 1e-6);
    }
  });

  test("optical balance: the square mark grows past the step, the wordmark stays at it", () => {
    const layout = layoutArchitecture(row("lg"), { assets: ASSETS });
    const wordmark = byId(layout.nodes, "n1").mediaRect!;
    const square = byId(layout.nodes, "n2").mediaRect!;
    expect(wordmark.h).toBeCloseTo(LOGO_HEIGHT_PX.lg, 5);
    expect(wordmark.w / wordmark.h).toBeCloseTo(4, 5);
    expect(square.h).toBeGreaterThan(LOGO_HEIGHT_PX.lg);
    expect(square.h).toBeLessThanOrEqual(LOGO_HEIGHT_PX.lg * 1.5);
    expect(square.w).toBeCloseTo(square.h, 5);
  });

  test.each(["sm", "md", "lg", "xl"] as const)("%s: the wordmark is drawn at its step, the column widening for it", (step) => {
    const layout = layoutArchitecture(row(step), { assets: ASSETS });
    const node = byId(layout.nodes, "n1");
    expect(node.mediaRect!.h).toBeCloseTo(LOGO_HEIGHT_PX[step], 5);
    expect(node.mediaRect!.w).toBeLessThanOrEqual(node.w + 1e-6);
  });

  test("xl is drawn larger than lg", () => {
    const lg = byId(layoutArchitecture(row("lg"), { assets: ASSETS }).nodes, "n1").mediaRect!;
    const xl = byId(layoutArchitecture(row("xl"), { assets: ASSETS }).nodes, "n1").mediaRect!;
    expect(xl.h).toBeGreaterThan(lg.h + 10);
  });

  test("a band's logo takes its step too", () => {
    const block: DeckArchitectureBlock = {
      type: "architecture",
      nodes: [
        { id: "band", label: "Platform", logo_file_id: SQUARE, logo_height: "xl", row: 1, col: 1, col_span: 3 },
        { id: "other", label: "Other", icon: "shield", row: 2, col: 1 },
      ],
      edges: [{ source: "band", target: "other" }],
    } as DeckArchitectureBlock;
    const band = byId(layoutArchitecture(block, { assets: ASSETS }).nodes, "band");
    expect(band.kind).toBe("band");
    expect(band.mediaRect!.h).toBeGreaterThanOrEqual(LOGO_HEIGHT_PX.xl);
    expect(band.h).toBeGreaterThanOrEqual(band.mediaRect!.h + 2 * ARCH.CARD_PAD_Y - 1e-6);
  });
});

/** A 5:1 wordmark of ink (the measured ratio of a real product wordmark) and a 16:9 slide body. */
const WIDE = "00000000-0000-4000-8000-00000000c003";
const WIDE_ASSETS: Record<string, DeckAssetMeta> = {
  ...ASSETS,
  [WIDE]: { w: 500, h: 140, trim: [0, 1 / 7, 1, 6 / 7], rev: "wide" },
};
const BODY = { width: 1728, height: 600 };

function wideRow(step: DeckLogoHeight): DeckArchitectureBlock {
  const block = row(step);
  block.nodes = block.nodes.map((node, i) => ({ ...node, logo_file_id: i % 2 ? SQUARE : WIDE }));
  return block;
}

describe("logo_height in a fitted block (UX round 1, B1 and B2)", () => {
  test("a 5:1 wordmark on a five-node row reads at its step on the slide, after the block's fit scale", () => {
    const layout = layoutArchitecture(wideRow("lg"), { assets: WIDE_ASSETS, room: BODY });
    const fit = Math.min(1, BODY.width / layout.width);
    for (const node of layout.nodes) {
      expect(node.mediaRect!.h * fit).toBeGreaterThanOrEqual(LOGO_HEIGHT_PX.lg - 0.5);
    }
    // Labels stay readable: 26 canvas px at the fit scale is 16 px or more.
    expect(ARCH.LABEL_FS * fit).toBeGreaterThanOrEqual(16);
  });

  test.each(["sm", "lg", "xl"] as const)("%s: every card of the row shares one height and one label baseline", (step) => {
    const layout = layoutArchitecture(wideRow(step), { assets: WIDE_ASSETS, room: BODY });
    const first = layout.nodes[0];
    for (const node of layout.nodes) {
      expect(node.h).toBeCloseTo(first.h, 5);
      expect(node.y + node.labelBox.y).toBeCloseTo(first.y + first.labelBox.y, 5);
      expect(node.mediaBox!.h).toBeCloseTo(first.mediaBox!.h, 5);
    }
  });

  test("the optical balance survives the shared box: the square mark is still drawn taller than the wordmark", () => {
    const layout = layoutArchitecture(wideRow("lg"), { assets: WIDE_ASSETS, room: BODY });
    expect(byId(layout.nodes, "n2").mediaRect!.h).toBeGreaterThan(byId(layout.nodes, "n1").mediaRect!.h);
  });

  test.each(["lg", "xl"] as const)("%s: a 5:1 wordmark keeps the card padding's air to the border (UX round 2, B1)", (step) => {
    const layout = layoutArchitecture(wideRow(step), { assets: WIDE_ASSETS, room: BODY });
    for (const node of layout.nodes) {
      const mark = node.mediaRect!;
      expect(mark.x).toBeGreaterThanOrEqual(SIZED_LOGO_INSET - 1e-6);
      expect(node.w - (mark.x + mark.w)).toBeGreaterThanOrEqual(SIZED_LOGO_INSET - 1e-6);
    }
  });

  test.each(["lg", "xl"] as const)("%s: the labels stay readable, a 19 px sublabel at 16 px or more on the slide (UX round 2, B3)", (step) => {
    const block = wideRow(step);
    block.nodes = block.nodes.map((node) => ({ ...node, sublabel: "Detail" }));
    const layout = layoutArchitecture(block, { assets: WIDE_ASSETS, room: BODY });
    const fit = Math.min(1, BODY.width / layout.width);
    expect(fit).toBeGreaterThanOrEqual(SIZED_FIT_MIN - 1e-6);
    expect(ARCH.SUB_FS * fit).toBeGreaterThanOrEqual(16 - 1e-6);
  });

  test("xl on a 5:1 wordmark: drawn larger than lg where the row has room, and the shortfall published otherwise (UX round 2, B2)", () => {
    const lgLayout = layoutArchitecture(wideRow("lg"), { assets: WIDE_ASSETS, room: BODY });
    const xlLayout = layoutArchitecture(wideRow("xl"), { assets: WIDE_ASSETS, room: BODY });
    const onSlide = (layout: typeof lgLayout, id: string) => byId(layout.nodes, id).mediaRect!.h * Math.min(1, BODY.width / layout.width);
    // The square marks have the room: xl reads visibly larger than lg.
    expect(onSlide(xlLayout, "n2")).toBeGreaterThan(onSlide(lgLayout, "n2") + 10);
    // The wordmark is never drawn smaller at xl than at lg.
    expect(onSlide(xlLayout, "n1")).toBeGreaterThanOrEqual(onSlide(lgLayout, "n1") - 0.5);
    // Where the capped column cannot reach the step, the drawn height says so: the layout check compares it to the step.
    const drawn = onSlide(xlLayout, "n1");
    if (drawn < LOGO_HEIGHT_PX.xl - 0.5) {
      const { container } = renderWithProviders(
        <div className="deck-root" data-scheme="dark">
          <DeckAssetProvider value={() => SRC}>
            <DeckAssetMetaProvider value={WIDE_ASSETS}>
              <ArchitectureBlockView block={wideRow("xl")} />
            </DeckAssetMetaProvider>
          </DeckAssetProvider>
        </div>,
      );
      const sized = Array.from(container.querySelectorAll("[data-logo-step]"));
      expect(sized).toHaveLength(5);
      expect(sized.every((mark) => mark.getAttribute("data-logo-step") === String(LOGO_HEIGHT_PX.xl))).toBe(true);
    }
  });

  test("lg on a 256x124 wordmark file in a five-node row: the capped mark publishes the height it is drawn at, under its step", () => {
    // A 256x124 file whose ink is a 5:1 wordmark on a tall transparent margin.
    const FILE = "00000000-0000-4000-8000-00000000c004";
    const assets: Record<string, DeckAssetMeta> = { ...WIDE_ASSETS, [FILE]: { w: 256, h: 124, trim: [0, 0.2944, 1, 0.7056], rev: "file" } };
    const block = row("lg");
    block.nodes = block.nodes.map((node, i) => ({ ...node, logo_file_id: i === 0 ? FILE : SQUARE }));
    // The body of a 16:9 brand slide with its side margins (the probe's figure is about 1690 canvas px wide).
    const room = { width: 1690, height: 600 };
    const layout = layoutArchitecture(block, { assets, room });
    const fit = Math.min(1, room.width / layout.width);
    const wordmark = byId(layout.nodes, "n1");
    // The column cap holds the labels at 16 px or more, so the 5:1 mark fills its card's width under 56 px.
    expect(fit).toBeGreaterThanOrEqual(SIZED_FIT_MIN - 1e-6);
    expect(wordmark.mediaRect!.h * fit).toBeLessThan(LOGO_HEIGHT_PX.lg - 1);
    expect(wordmark.w - wordmark.mediaRect!.w).toBeCloseTo(2 * SIZED_LOGO_INSET, 0);
    // What the layout check reads: the drawn height in drawing units, never the file's box.
    const { container } = renderWithProviders(
      <div className="deck-root" data-scheme="dark">
        <DeckAssetProvider value={() => SRC}>
          <DeckAssetMetaProvider value={assets}>
            <ArchitectureBlockView block={block} />
          </DeckAssetMetaProvider>
        </DeckAssetProvider>
      </div>,
    );
    const mark = Array.from(container.querySelectorAll("[data-logo-step]")).find((el) => el.querySelector("image")?.getAttribute("height") === "124");
    expect(mark!.getAttribute("data-logo-step")).toBe(String(LOGO_HEIGHT_PX.lg));
    const drawn = Number(mark!.getAttribute("data-logo-drawn"));
    expect(drawn).toBeGreaterThan(0);
    // The nested svg crops to the ink: its viewport is the drawn height, the file inside it is taller.
    const viewport = mark!.querySelector("svg");
    expect(Number(viewport!.getAttribute("height"))).toBeCloseTo(drawn, 0);
    expect(Number(mark!.querySelector("image")!.getAttribute("height"))).toBe(124);
  });

  test("unset: the grid and the fit are those of main", () => {
    const unset = layoutArchitecture(row(undefined), { assets: ASSETS, room: BODY });
    expect(unset.width).toBe(2 * ARCH.PAD + 5 * ARCH.COL_W + 4 * ARCH.COL_GAP);
    for (const node of unset.nodes) expect(node.w).toBe(ARCH.COL_W);
  });
});

describe("the drawing", () => {
  test("a sized logo is cropped to its ink: a nested svg whose viewBox is the trim box", () => {
    const { container } = renderWithProviders(
      <div className="deck-root" data-scheme="dark">
        <DeckAssetProvider value={() => SRC}>
          <DeckAssetMetaProvider value={ASSETS}>
            <ArchitectureBlockView block={row("lg")} />
          </DeckAssetMetaProvider>
        </DeckAssetProvider>
      </div>,
    );
    const images = Array.from(container.querySelectorAll("image.deck-arch-logo"));
    expect(images).toHaveLength(5);
    const square = images.find((image) => image.getAttribute("width") === "200")!;
    expect(square.parentElement!.tagName.toLowerCase()).toBe("svg");
    expect(square.parentElement!.getAttribute("viewBox")).toBe("20 20 160 160");
  });
});
