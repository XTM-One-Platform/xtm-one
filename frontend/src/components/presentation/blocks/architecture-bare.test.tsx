/**
 * Zone logos and frameless nodes in the architecture block - `yarn test:dom`.
 *
 * A zone headed by the platform's mark (`logo_file_id`, its label optional
 * and drawn after the mark) and a node drawn as its content alone
 * (`frame: false`, the box kept for the grid, the edges meeting the content):
 * the geometry `layoutArchitecture` returns, what `ArchitectureBlockView`
 * draws for each, and the 5-column fixture the backend's export and
 * layout-check tests draw in Chromium (`backend/tests/data/architecture/`).
 * Every asset is a generic placeholder.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { fireEvent } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckArchitectureBlock, DeckArchitectureNode, DeckArchitectureZone, DeckAssetMeta, DeckNodeEmphasis } from "@/types";

import { DeckAssetMetaProvider, DeckAssetProvider } from "../asset-context";
import { ArchitectureBlockView } from "./Architecture";
import {
  ARCH,
  DENSITY_MIN,
  layoutArchitecture,
  segmentIntersectsRect,
  zoneHeadBox,
  type ArchitectureLayout,
  type LayoutNode,
  type LayoutZone,
  type Point,
  type Rect,
} from "./architecture-layout";

// The icon catalog, loaded natively (see `metrics.test.tsx`).
vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

// ── Placeholder assets ────────────────────────────────────────────────────────

/** Placeholder file ids: no file, no deck behind them. */
const ZONE_LOGO = "00000000-0000-4000-8000-00000000a001";
const NODE_LOGO = "00000000-0000-4000-8000-00000000b002";
/** A square mark and a bar, the shape of a logo and nothing more, on a transparent margin. */
const PLACEHOLDER_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 80">' +
  '<rect x="12" y="8" width="64" height="64" rx="16" fill="#8a94a6"/>' +
  '<rect x="92" y="28" width="136" height="24" rx="6" fill="#8a94a6"/></svg>';
const PLACEHOLDER_SRC = `data:image/svg+xml;base64,${btoa(PLACEHOLDER_SVG)}`;
/** The geometry the backend stores for such a file: its size, and the box of its ink. */
const ASSETS: Record<string, DeckAssetMeta> = {
  [ZONE_LOGO]: { w: 240, h: 80, trim: [0.05, 0.1, 0.95, 0.9], rev: "placeholder-zone" },
  [NODE_LOGO]: { w: 240, h: 80, rev: "placeholder-node" },
};
/** Width over height of the zone logo's mark: 216 x 64 px of ink. */
const MARK_RATIO = (0.9 * 240) / (0.8 * 80);

const BACKEND_FIXTURE = resolve(process.cwd(), "../backend/tests/data/architecture/zone-logo-frameless.html");
const BACKEND_GEOMETRY = resolve(process.cwd(), "../backend/tests/data/architecture/zone-logo-frameless.json");

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderBlock(block: DeckArchitectureBlock, { scheme = "dark", assets = ASSETS }: { scheme?: string; assets?: Record<string, DeckAssetMeta> } = {}) {
  return renderWithProviders(
    <div className="deck-root" data-scheme={scheme}>
      <DeckAssetProvider value={() => PLACEHOLDER_SRC}>
        <DeckAssetMetaProvider value={assets}>
          <ArchitectureBlockView block={block} />
        </DeckAssetMetaProvider>
      </DeckAssetProvider>
    </div>,
  );
}

const round = (n: number) => Math.round(n * 10) / 10;
const rect = (r: Rect) => [round(r.x), round(r.y), round(r.w), round(r.h)];
const point = (p: Point) => [round(p.x), round(p.y)];
const overlaps = (a: Rect, b: Rect): boolean => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
const inside = (inner: Rect, outer: Rect): boolean =>
  inner.x >= outer.x - 0.5 && inner.y >= outer.y - 0.5 && inner.x + inner.w <= outer.x + outer.w + 0.5 && inner.y + inner.h <= outer.y + outer.h + 0.5;

const nodeOf = (layout: ArchitectureLayout, id: string): LayoutNode => {
  const node = layout.nodes.find((n) => n.id === id);
  if (!node) throw new Error(`no node ${id}`);
  return node;
};

/** On the border of a rectangle - where a frameless node is met. */
function onRectBorder(p: Point, r: Rect): boolean {
  const eps = 0.5;
  const withinX = p.x >= r.x - eps && p.x <= r.x + r.w + eps;
  const withinY = p.y >= r.y - eps && p.y <= r.y + r.h + eps;
  return (
    ((Math.abs(p.y - r.y) <= eps || Math.abs(p.y - r.y - r.h) <= eps) && withinX) ||
    ((Math.abs(p.x - r.x) <= eps || Math.abs(p.x - r.x - r.w) <= eps) && withinY)
  );
}

/** Every edge leaves its source's content (or shape), reaches its target's, and runs through no third node. */
function expectEdgesMeetWhatIsDrawn(layout: ArchitectureLayout): void {
  for (const edge of layout.edges) {
    const source = nodeOf(layout, edge.edge.source);
    const target = nodeOf(layout, edge.edge.target);
    const first = edge.points[0];
    const last = edge.points[edge.points.length - 1];
    if (!source.frame) expect(onRectBorder(first, source.anchor), `${edge.key} starts off ${source.id}'s content`).toBe(true);
    if (!target.frame) expect(onRectBorder(last, target.anchor), `${edge.key} ends off ${target.id}'s content`).toBe(true);
    for (const node of layout.nodes) {
      if (node === source || node === target) continue;
      for (let i = 0; i + 1 < edge.points.length; i += 1) {
        expect(segmentIntersectsRect(edge.points[i], edge.points[i + 1], node), `${edge.key} runs through ${node.id}`).toBe(false);
      }
    }
  }
}

/** A zone as a reviewer reads it off the page: its panel, its mark, its title. */
function drawnZone(container: HTMLElement) {
  const zone = container.querySelector(".deck-arch-zone")!;
  const panel = zone.querySelector(".deck-arch-zone-panel")!;
  const cropped = zone.querySelector(":scope > svg");
  const image = zone.querySelector("image");
  const title = zone.querySelector(".deck-arch-zone-title");
  return {
    color: zone.getAttribute("data-color"),
    panel: ["x", "y", "width", "height"].map((name) => panel.getAttribute(name)),
    logo: image
      ? {
          box: cropped ? ["x", "y", "width", "height", "viewBox"].map((name) => cropped.getAttribute(name)) : null,
          image: ["x", "y", "width", "height", "preserveAspectRatio"].map((name) => image.getAttribute(name)),
          aspect: cropped?.getAttribute("preserveAspectRatio") ?? null,
        }
      : null,
    icon: zone.querySelector(".deck-arch-icon") ? "icon" : null,
    title: title ? { x: title.getAttribute("x"), y: title.getAttribute("y"), text: title.textContent } : null,
  };
}

/** A node as drawn: what paints it, in paint order, and where its hit area sits. */
function drawnNode(g: Element) {
  const hit = g.querySelector(".deck-arch-node-hit");
  return {
    shape: g.getAttribute("data-shape"),
    emphasis: g.getAttribute("data-emphasis"),
    frame: g.getAttribute("data-frame"),
    pieces: Array.from(g.children).map((child) => `${child.tagName.toLowerCase()}${child.getAttribute("class") ? `.${child.getAttribute("class")}` : ""}`),
    hit: hit ? ["x", "y", "width", "height"].map((name) => hit.getAttribute(name)) : null,
    text: Array.from(g.querySelectorAll("text")).map((text) => text.textContent),
  };
}

// ── Zone logos ────────────────────────────────────────────────────────────────

const zoneBlock = (zone: Partial<DeckArchitectureZone>): DeckArchitectureBlock => ({
  type: "architecture",
  nodes: [
    { id: "a", label: "Product A", icon: "brain", row: 1, col: 1 },
    { id: "b", label: "Product B", icon: "crosshair", row: 1, col: 2 },
  ],
  edges: [{ source: "a", target: "b", label: "Data" }],
  zones: [{ row: 1, col: 1, col_span: 2, color: "brand_1", ...zone }],
});

const LOGO_ALONE = zoneBlock({ logo_file_id: ZONE_LOGO });
const LOGO_AND_LABEL = zoneBlock({ logo_file_id: ZONE_LOGO, label: "Platform" });
const LABEL_ALONE = zoneBlock({ label: "Platform" });

describe("a zone logo", () => {
  const alone = layoutArchitecture(LOGO_ALONE, { assets: ASSETS });
  const both = layoutArchitecture(LOGO_AND_LABEL, { assets: ASSETS });
  const labelled = layoutArchitecture(LABEL_ALONE, { assets: ASSETS });
  const zoneOf = (layout: ArchitectureLayout): LayoutZone => layout.zones[0];

  test("alone, sits at the zone's top-left corner on the title row, as wide as its mark", () => {
    const zone = zoneOf(alone);
    expect(zone.title).toBeNull();
    const logo = zone.logo!;
    expect(logo.x).toBe(zone.x + 24);
    expect(logo.h).toBe(ARCH.ZONE_LOGO_H);
    expect(logo.w).toBeCloseTo(ARCH.ZONE_LOGO_H * MARK_RATIO, 5);
    expect(logo.y + logo.h / 2).toBeCloseTo(zone.titleY, 5);
    // Inside the panel's padding, above every node the zone holds.
    expect(logo.y).toBeGreaterThan(zone.y);
    for (const node of alone.nodes) expect(logo.y + logo.h).toBeLessThan(node.y);
  });

  test("with a label, the label follows the logo on the same row, centred on it", () => {
    const zone = zoneOf(both);
    const logo = zone.logo!;
    expect(zone.title).not.toBeNull();
    expect(zone.titleX).toBeCloseTo(logo.x + logo.w + 12, 5);
    expect(logo.y + logo.h / 2).toBeCloseTo(zone.titleY, 5);
    expect(zoneHeadBox(zone)).toMatchObject({ x: logo.x, h: logo.h });
    expect(zone.titleX + zone.title!.width).toBeLessThanOrEqual(zone.x + zone.w - 24 + 0.5);
  });

  test("leaves the zone's panel, tint and colour as they are", () => {
    for (const layout of [alone, both]) expect(rect(zoneOf(layout))).toEqual(rect(zoneOf(labelled)));
    for (const block of [LOGO_ALONE, LOGO_AND_LABEL]) {
      const { container, unmount } = renderBlock(block);
      const { container: plain, unmount: unmountPlain } = renderBlock(LABEL_ALONE);
      expect(drawnZone(container).panel).toEqual(drawnZone(plain).panel);
      expect(drawnZone(container).color).toBe("brand_1");
      unmount();
      unmountPlain();
    }
  });

  test("is no taller than the title row leaves it, on a compacted drawing too", () => {
    const compact = layoutArchitecture(LOGO_AND_LABEL, { room: { width: 4000, height: 120 }, assets: ASSETS });
    expect(compact.density).toBe(DENSITY_MIN);
    const zone = zoneOf(compact);
    const logo = zone.logo!;
    expect(logo.h).toBeLessThanOrEqual(ARCH.ZONE_LOGO_H);
    expect(logo.h).toBeLessThan(ARCH.ZONE_LOGO_H);
    expect(logo.y).toBeGreaterThan(zone.y);
    for (const node of compact.nodes) expect(logo.y + logo.h).toBeLessThan(node.y);
  });

  test("without stored geometry takes a wordmark's ratio and draws the file uncropped", () => {
    const layout = layoutArchitecture(LOGO_ALONE);
    expect(zoneOf(layout).logo!.w).toBeCloseTo(ARCH.ZONE_LOGO_H * 3, 5);
    const { container } = renderBlock(LOGO_ALONE, { assets: {} });
    const drawn = drawnZone(container);
    expect(drawn.logo?.box).toBeNull();
    expect(drawn.logo?.image[4]).toBe("xMinYMid meet");
  });

  test("keeps the edge labels off its head", () => {
    for (const layout of [alone, both]) {
      const head = zoneHeadBox(zoneOf(layout));
      for (const edge of layout.edges) if (edge.label) expect(overlaps(edge.label, head), edge.key).toBe(false);
    }
  });

  test("alone, is drawn as its mark cropped to its ink, with no title", () => {
    const { container } = renderBlock(LOGO_ALONE);
    const drawn = drawnZone(container);
    expect(drawn.title).toBeNull();
    expect(drawn.icon).toBeNull();
    // The viewBox is the trim box in the file's stored pixels: the margin is cut.
    expect(drawn.logo?.box?.[4]).toBe("12 8 216 64");
    expect(drawn).toMatchSnapshot();
  });

  test("with a label, is drawn as its mark then the title", () => {
    const { container } = renderBlock(LOGO_AND_LABEL);
    const drawn = drawnZone(container);
    expect(drawn.title?.text).toBe("Platform");
    expect(Number(drawn.title?.x)).toBeGreaterThan(Number(drawn.logo?.box?.[0]) + Number(drawn.logo?.box?.[2]));
    expect(drawn).toMatchSnapshot();
  });
});

// ── Frameless nodes ───────────────────────────────────────────────────────────

const SHAPES: [string, Partial<DeckArchitectureNode>][] = [
  ["card", { icon: "shield", sublabel: "Context", chips: ["One", "Two"] }],
  ["band", { col_span: 2, logo_file_id: NODE_LOGO, chips: ["One"] }],
  ["card with a logo", { logo_file_id: NODE_LOGO }],
  ["pill", { shape: "pill", icon: "bell" }],
  ["circle", { shape: "circle", icon: "sparkles", sublabel: "Hub" }],
];
const EMPHASES: DeckNodeEmphasis[] = ["normal", "core", "ghost"];

/** The subject, a node under it and one beside it, tied both ways it can be. */
const subjectBlock = (shape: Partial<DeckArchitectureNode>, emphasis: DeckNodeEmphasis, frame: boolean): DeckArchitectureBlock => ({
  type: "architecture",
  nodes: [
    { id: "subject", label: "Subject", ...shape, emphasis, frame, row: 1, col: 1 },
    { id: "below", label: "Below", icon: "brain", row: 2, col: 1 },
    { id: "beside", label: "Beside", icon: "radar", row: 1, col: 3 },
  ],
  edges: [
    { source: "subject", target: "below", label: "Down" },
    { source: "beside", target: "subject", label: "Across" },
  ],
});

describe.each(SHAPES)("a frameless %s", (_name, shape) => {
  describe.each(EMPHASES)("of emphasis %s", (emphasis) => {
    const bare = layoutArchitecture(subjectBlock(shape, emphasis, false), { assets: ASSETS });
    const framed = layoutArchitecture(subjectBlock(shape, emphasis, true), { assets: ASSETS });

    test("keeps the box a framed one has, and is met on its content", () => {
      for (const node of bare.nodes) expect(rect(node), node.id).toEqual(rect(nodeOf(framed, node.id)));
      const subject = nodeOf(bare, "subject");
      expect(subject.frame).toBe(false);
      expect(inside(subject.anchor, subject)).toBe(true);
      expect(rect(subject.anchor)).toEqual(rect({ ...subject.content, x: subject.x + subject.content.x, y: subject.y + subject.content.y }));
      expect(rect(nodeOf(framed, "subject").anchor)).toEqual(rect(nodeOf(framed, "subject")));
      expectEdgesMeetWhatIsDrawn(bare);
    });

    test("draws its content and no shape, surface or glow - the same in both schemes", () => {
      const renders = ["dark", "light"].map((scheme) => {
        const { container, unmount } = renderBlock(subjectBlock(shape, emphasis, false), { scheme });
        const g = container.querySelector('.deck-arch-node[aria-label^="Subject"]')!;
        const out = drawnNode(g);
        expect(g.querySelector(".deck-arch-node-shape")).toBeNull();
        expect(g.querySelector(".deck-arch-node-glow")).toBeNull();
        expect(g.querySelectorAll(".deck-arch-node-hit")).toHaveLength(1);
        expect(out.text).toContain("Subject");
        if (shape.chips) expect(g.querySelectorAll(".deck-arch-chip")).toHaveLength(shape.chips.length);
        if (shape.logo_file_id) expect(g.querySelector("image.deck-arch-logo")).not.toBeNull();
        if (shape.icon) expect(g.querySelector(".deck-arch-icon")).not.toBeNull();
        unmount();
        return out;
      });
      expect(renders[1]).toEqual(renders[0]);
      expect(renders[0]).toMatchSnapshot();
      // Its framed twin keeps its card (and a core circle its glow).
      const { container } = renderBlock(subjectBlock(shape, emphasis, true));
      const twin = container.querySelector('.deck-arch-node[aria-label^="Subject"]')!;
      expect(twin.querySelector(".deck-arch-node-shape")).not.toBeNull();
      expect(twin.getAttribute("data-frame")).toBeNull();
      expect(twin.querySelector(".deck-arch-node-glow") !== null).toBe(shape.shape === "circle" && emphasis === "core");
    });
  });
});

// ── The 5-column fixture ──────────────────────────────────────────────────────

/**
 * A platform zone across the grid, headed by its logo and label: a frameless
 * logo at its left, a frameless row of chips at its right, three framed
 * products under them, each frameless node tied to a product.
 */
export const FIVE_COLUMNS_BARE: DeckArchitectureBlock = {
  type: "architecture",
  nodes: [
    { id: "partner", label: "Partner", logo_file_id: NODE_LOGO, frame: false, row: 2, col: 1 },
    { id: "skills", label: "Capabilities", chips: ["Detect", "Respond", "Report"], frame: false, row: 2, col: 5 },
    { id: "alpha", label: "Product A", icon: "brain", color: "brand_2", row: 3, col: 2 },
    { id: "beta", label: "Product B", icon: "crosshair", color: "brand_3", row: 3, col: 3 },
    { id: "gamma", label: "Product C", icon: "chart-line", color: "brand_4", row: 3, col: 4 },
  ],
  edges: [
    { source: "partner", target: "alpha", label: "Feeds" },
    { source: "gamma", target: "skills", label: "Uses" },
  ],
  zones: [{ label: "Platform", logo_file_id: ZONE_LOGO, color: "brand_1", row: 2, col: 1, row_span: 2, col_span: 5 }],
  size: "lg",
};

describe("the 5-column grid with frameless nodes and a zone logo", () => {
  const layout = layoutArchitecture(FIVE_COLUMNS_BARE, { assets: ASSETS });
  const zone = layout.zones[0];

  test("overlaps nothing: no two nodes, no node and the zone head, no label and anything", () => {
    for (let i = 0; i < layout.nodes.length; i += 1) {
      for (let j = i + 1; j < layout.nodes.length; j += 1) {
        expect(overlaps(layout.nodes[i], layout.nodes[j]), `${layout.nodes[i].id} x ${layout.nodes[j].id}`).toBe(false);
      }
    }
    const head = zoneHeadBox(zone);
    expect(inside(head, zone)).toBe(true);
    for (const node of layout.nodes) expect(overlaps(head, node), `zone head x ${node.id}`).toBe(false);
    const labels = layout.edges.flatMap((edge) => (edge.label ? [edge.label] : []));
    for (const label of labels) {
      expect(overlaps(label, head)).toBe(false);
      for (const node of layout.nodes) expect(overlaps(label, node), `label on ${node.id}`).toBe(false);
    }
    expect(labels).toHaveLength(2);
    expect(overlaps(labels[0], labels[1])).toBe(false);
  });

  test("routes every edge from content to content, through no third node", () => {
    expect(layout.edges).toHaveLength(2);
    expectEdgesMeetWhatIsDrawn(layout);
    expect(layout.edges.map((edge) => ({ key: edge.key, points: edge.points.map(point) }))).toMatchSnapshot();
  });

  test("is the fixture the backend's export and layout-check tests draw in Chromium", () => {
    // `ARCHITECTURE_FIXTURE=write yarn test:dom` rewrites both files after a deliberate change to the drawing.
    const { container } = renderBlock(FIVE_COLUMNS_BARE);
    for (const image of Array.from(container.querySelectorAll("image"))) fireEvent.load(image);
    expect(container.querySelector("[data-deck-pending]")).toBeNull();
    const figure = container.querySelector("figure.deck-architecture")!;
    const html = `${stableIds(new XMLSerializer().serializeToString(figure))}\n`;
    const geometry = `${JSON.stringify(
      {
        viewBox: [layout.width, layout.height],
        nodes: layout.nodes.map((node) => ({ id: node.id, frame: node.frame, box: rect(node), anchor: rect(node.anchor) })),
        zone: { panel: rect(zone), logo: rect(zone.logo!), head: rect(zoneHeadBox(zone)) },
        edges: layout.edges.map((edge) => ({
          source: edge.edge.source,
          target: edge.edge.target,
          start: point(edge.points[0]),
          end: point(edge.points[edge.points.length - 1]),
        })),
      },
      null,
      2,
    )}\n`;
    if (process.env.ARCHITECTURE_FIXTURE === "write") {
      writeFileSync(BACKEND_FIXTURE, html);
      writeFileSync(BACKEND_GEOMETRY, geometry);
    }
    expect(readFileSync(BACKEND_FIXTURE, "utf8").replace(/\r\n/g, "\n")).toBe(html);
    expect(readFileSync(BACKEND_GEOMETRY, "utf8").replace(/\r\n/g, "\n")).toBe(geometry);
  });
});

/** React's ids depend on what rendered before; the fixture names them by order of appearance instead. */
function stableIds(html: string): string {
  const ids = Array.from(new Set(Array.from(html.matchAll(/ id="([^"]+)"/g), (match) => match[1])));
  // Longest first, so an id that contains another is renamed whole.
  return [...ids]
    .sort((a, b) => b.length - a.length)
    .reduce((out, id) => out.split(id).join(`fixture-${ids.indexOf(id)}`), html);
}
