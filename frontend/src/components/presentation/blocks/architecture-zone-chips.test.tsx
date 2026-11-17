/**
 * Zone chips in the architecture block - `yarn test:dom`.
 *
 * A zone's `chips` are one row of pills on its title row - at its end by
 * default, at its start (after the logo / label) or at its centre with
 * `chips_align` - never wrapped, taking no grid cell: the geometry
 * `layoutArchitecture` returns, what `ArchitectureBlockView` draws, and the
 * fixtures the backend's export and layout-check tests draw in Chromium
 * (`backend/tests/data/architecture/`). Every asset is a generic placeholder.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { fireEvent } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckArchitectureBlock, DeckArchitectureZone, DeckAssetMeta, DeckTone } from "@/types";

import { DeckAssetMetaProvider, DeckAssetProvider } from "../asset-context";
import { ArchitectureBlockView } from "./Architecture";
import {
  ARCH,
  DENSITY_MIN,
  layoutArchitecture,
  segmentIntersectsRect,
  zoneHeadBox,
  type ArchitectureLayout,
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
const ASSETS: Record<string, DeckAssetMeta> = {
  [ZONE_LOGO]: { w: 240, h: 80, trim: [0.05, 0.1, 0.95, 0.9], rev: "placeholder-zone" },
  [NODE_LOGO]: { w: 240, h: 80, rev: "placeholder-node" },
};

const CHIPS = ["AI agents", "Shared context", "Automation", "Integrations"];
const TONES: DeckTone[] = ["accent", "info", "success", "warning", "error", "muted"];

const DATA = resolve(process.cwd(), "../backend/tests/data/architecture");

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderBlock(block: DeckArchitectureBlock, scheme = "dark") {
  return renderWithProviders(
    <div className="deck-root" data-scheme={scheme}>
      <DeckAssetProvider value={() => PLACEHOLDER_SRC}>
        <DeckAssetMetaProvider value={ASSETS}>
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

/** The zone's padding: where its head row may draw. */
const padding = (zone: LayoutZone) => ({ left: zone.x + 24, right: zone.x + zone.w - 24 });
/** Where the zone's logo / label ends; the row's left edge when it has neither. */
const headEnd = (zone: LayoutZone) => {
  const head = zoneHeadBox(zone);
  return head.w > 0 ? head.x + head.w : null;
};

/** One row, centred on the title row, every chip whole and in the order written. */
function expectOneRow(zone: LayoutZone, labels: string[], height: number = ARCH.CHIP_H): void {
  expect(zone.chips.map((chip) => chip.text.lines.map((line) => line.runs.map((run) => run.text).join("")).join("|"))).toEqual(labels);
  for (const chip of zone.chips) {
    expect(chip.h).toBe(height);
    expect(chip.y + chip.h / 2).toBeCloseTo(zone.titleY, 5);
    expect(chip.text.lines).toHaveLength(1);
  }
  for (let i = 1; i < zone.chips.length; i += 1) expect(zone.chips[i].x).toBeGreaterThan(zone.chips[i - 1].x + zone.chips[i - 1].w);
  const first = zone.chips[0];
  const last = zone.chips[zone.chips.length - 1];
  expect(rect(zone.chipsBox!)).toEqual(rect({ x: first.x, y: first.y, w: last.x + last.w - first.x, h: height }));
}

/** A zone as drawn: its head and its chips, as a reviewer reads them off the page. */
function drawnZone(container: HTMLElement) {
  const zone = container.querySelector(".deck-arch-zone")!;
  return {
    tone: zone.getAttribute("data-tone"),
    color: zone.getAttribute("data-color"),
    overflow: zone.getAttribute("data-chips-overflow"),
    logo: zone.querySelector("image") ? "logo" : null,
    title: zone.querySelector(".deck-arch-zone-title")?.textContent ?? null,
    chips: Array.from(zone.querySelectorAll(".deck-arch-zone-chip")).map((chip) => {
      const pill = chip.querySelector("rect")!;
      const text = chip.querySelector("text")!;
      return {
        className: chip.getAttribute("class"),
        pill: ["x", "y", "width", "height", "rx"].map((name) => pill.getAttribute(name)),
        text: { x: text.getAttribute("x"), y: text.getAttribute("y"), size: text.getAttribute("font-size"), value: text.textContent },
      };
    }),
  };
}

const zoneOf = (layout: ArchitectureLayout): LayoutZone => layout.zones[0];

// ── The head row ──────────────────────────────────────────────────────────────

/** Two products under a two-column zone, the zone head as given. */
const headBlock = (zone: Partial<DeckArchitectureZone>): DeckArchitectureBlock => ({
  type: "architecture",
  nodes: [
    { id: "a", label: "Product A", icon: "brain", row: 1, col: 1 },
    { id: "b", label: "Product B", icon: "crosshair", row: 1, col: 2 },
    { id: "c", label: "Product C", icon: "radar", row: 1, col: 3 },
  ],
  edges: [
    { source: "a", target: "b", label: "Data" },
    { source: "b", target: "c" },
  ],
  zones: [{ row: 1, col: 1, col_span: 3, color: "brand_1", ...zone }],
});

const HEADS: [string, Partial<DeckArchitectureZone>][] = [
  ["alone", { chips: CHIPS }],
  ["with a logo", { logo_file_id: ZONE_LOGO, chips: CHIPS }],
  ["with a logo and a label", { logo_file_id: ZONE_LOGO, label: "Platform", chips: CHIPS }],
];

describe.each(HEADS)("zone chips %s", (_name, head) => {
  const layout = layoutArchitecture(headBlock(head), { assets: ASSETS });
  const bare = layoutArchitecture(headBlock({ ...head, chips: undefined }), { assets: ASSETS });
  const zone = zoneOf(layout);

  test("draw one row at the zone's end, inside its padding, centred on the title row", () => {
    expectOneRow(zone, CHIPS);
    expect(zone.chipsOverflow).toBe(0);
    expect(zone.chipsBox!.x + zone.chipsBox!.w).toBeCloseTo(padding(zone).right, 5);
    const end = headEnd(zone);
    if (end !== null) expect(zone.chipsBox!.x).toBeGreaterThanOrEqual(end + 16 - 0.5);
    expect(inside(zone.chipsBox!, zone)).toBe(true);
  });

  test("take no cell: the nodes, the logo, the label and the panel are where they are without them", () => {
    for (const node of layout.nodes) expect(rect(node), node.id).toEqual(rect(bare.nodes.find((n) => n.id === node.id)!));
    for (const node of layout.nodes) expect(overlaps(node, zone.chipsBox!), node.id).toBe(false);
    expect(rect(zone)).toEqual(rect(zoneOf(bare)));
    if (zone.logo) expect(rect(zone.logo)).toEqual(rect(zoneOf(bare).logo!));
    if (head.label) expect(zone.titleX).toBe(zoneOf(bare).titleX);
  });

  test("are drawn in the zone, tinted by its colour, the same in both schemes", () => {
    const renders = ["dark", "light"].map((scheme) => {
      const { container, unmount } = renderBlock(headBlock(head), scheme);
      const drawn = drawnZone(container);
      unmount();
      return drawn;
    });
    expect(renders[1]).toEqual(renders[0]);
    const drawn = renders[0];
    expect(drawn.color).toBe("brand_1");
    expect(drawn.overflow).toBeNull();
    expect(drawn.logo).toBe(head.logo_file_id ? "logo" : null);
    expect(drawn.title).toBe(head.label ?? null);
    expect(drawn.chips.map((chip) => chip.text.value)).toEqual(CHIPS);
    for (const chip of drawn.chips) expect(chip.className).toBe("deck-arch-chip deck-arch-zone-chip");
    expect(drawn).toMatchSnapshot();
  });
});

describe("chips_align", () => {
  // Three chips: centred, four would meet this head on three columns and step down.
  const head = { logo_file_id: ZONE_LOGO, label: "Platform", chips: CHIPS.slice(0, 3) };
  const at = (align: DeckArchitectureZone["chips_align"]) => zoneOf(layoutArchitecture(headBlock({ ...head, chips_align: align }), { assets: ASSETS }));

  test("end, the default, puts the row in the top-right corner", () => {
    const zone = at("end");
    expect(rect(zoneOf(layoutArchitecture(headBlock(head), { assets: ASSETS })).chipsBox!)).toEqual(rect(zone.chipsBox!));
    expect(zone.chipsBox!.x + zone.chipsBox!.w).toBeCloseTo(padding(zone).right, 5);
  });

  test("start puts it after the logo / label", () => {
    const zone = at("start");
    expectOneRow(zone, head.chips);
    expect(zone.chipsBox!.x).toBeCloseTo(headEnd(zone)! + 16, 5);
  });

  test("center puts it on the zone's middle", () => {
    const zone = at("center");
    expectOneRow(zone, head.chips);
    expect(zone.chipsBox!.x + zone.chipsBox!.w / 2).toBeCloseTo(zone.x + zone.w / 2, 5);
    expect(zone.chipsBox!.x).toBeGreaterThan(headEnd(zone)! + 16);
  });

  test.each(["start", "center", "end"] as const)("%s is drawn the same in both schemes", (align) => {
    const renders = ["dark", "light"].map((scheme) => {
      const { container, unmount } = renderBlock(headBlock({ ...head, chips_align: align }), scheme);
      const drawn = drawnZone(container);
      unmount();
      return drawn;
    });
    expect(renders[1]).toEqual(renders[0]);
    expect(renders[0]).toMatchSnapshot();
  });

  test("with chips alone, start and end are the two corners of the head row", () => {
    const start = zoneOf(layoutArchitecture(headBlock({ chips: CHIPS, chips_align: "start" })));
    const end = zoneOf(layoutArchitecture(headBlock({ chips: CHIPS })));
    expect(start.chipsBox!.x).toBeCloseTo(padding(start).left, 5);
    expect(end.chipsBox!.x + end.chipsBox!.w).toBeCloseTo(padding(end).right, 5);
  });
});

describe("a row that meets the head", () => {
  // One column: 276px of head row for a label and the chips.
  const narrow = (chips: string[], head: Partial<DeckArchitectureZone> = { label: "Hub" }): DeckArchitectureBlock => ({
    type: "architecture",
    nodes: [
      { id: "a", label: "Product A", icon: "brain", row: 1, col: 1 },
      { id: "b", label: "Product B", icon: "crosshair", row: 2, col: 1 },
    ],
    edges: [{ source: "a", target: "b" }],
    zones: [{ ...head, row: 1, col: 1, row_span: 2, chips }],
  });
  const three = ["Detect", "Respond", "Act"];

  test("steps the chips down one size when that clears it", () => {
    const zone = zoneOf(layoutArchitecture(narrow(three)));
    expect(zone.chipsOverflow).toBe(0);
    expectOneRow(zone, three, 26);
    expect(zone.chips[0].text.fontSize).toBe(15);
    expect(zone.chipsBox!.x).toBeGreaterThanOrEqual(headEnd(zone)! + 16 - 0.5);
    expect(zone.chipsBox!.x + zone.chipsBox!.w).toBeCloseTo(padding(zone).right, 5);
    // The label is what the row met: alone on the head row it keeps a node chip's size.
    expectOneRow(zoneOf(layoutArchitecture(narrow(three, {}))), three);
  });

  test("never wraps nor cuts a row that still does not fit: drawn whole, its overflow measured and published", () => {
    const block = narrow(CHIPS);
    const zone = zoneOf(layoutArchitecture(block));
    expectOneRow(zone, CHIPS, 26);
    const room = padding(zone).right - (headEnd(zone)! + 16);
    expect(zone.chipsOverflow).toBeCloseTo(zone.chipsBox!.w - room, 5);
    expect(zone.chipsOverflow).toBeGreaterThan(0);
    const { container } = renderBlock(block);
    const drawn = drawnZone(container);
    expect(drawn.overflow).toBe(String(Math.ceil(zone.chipsOverflow)));
    expect(drawn.chips.map((chip) => chip.text.value)).toEqual(CHIPS);
  });

  test("stays on the title row, above the nodes, on a compacted drawing", () => {
    const block = headBlock({ logo_file_id: ZONE_LOGO, label: "Platform", chips: CHIPS });
    const compact = layoutArchitecture(block, { room: { width: 4000, height: 120 }, assets: ASSETS });
    expect(compact.density).toBe(DENSITY_MIN);
    const zone = zoneOf(compact);
    expectOneRow(zone, CHIPS);
    expect(zone.chipsBox!.y).toBeGreaterThan(zone.y);
    for (const node of compact.nodes) expect(zone.chipsBox!.y + zone.chipsBox!.h).toBeLessThan(node.y);
  });
});

describe("the router and the chip row", () => {
  // Three products on the zone's first row; the outer two exchange around the
  // middle one, whose plain elbow runs above the row - through a centred chip
  // row as wide as the zone - unless the chips are in the way.
  const around = (chips: string[] | undefined): DeckArchitectureBlock => ({
    type: "architecture",
    nodes: [
      { id: "a", label: "Product A", row: 1, col: 1 },
      { id: "b", label: "Product B", row: 1, col: 2 },
      { id: "c", label: "Product C", row: 1, col: 3 },
    ],
    edges: [{ source: "a", target: "c", label: "Skips", route: "orthogonal" }],
    zones: [{ label: "Platform", row: 1, col: 1, col_span: 3, chips, chips_align: chips ? "center" : undefined }],
  });
  const crosses = (layout: ArchitectureLayout, box: Rect) =>
    layout.edges.some((edge) => edge.points.slice(1).some((p, i) => segmentIntersectsRect(edge.points[i], p, box)));
  const chips = ["Detect", "Respond", "Report", "Hunt", "Share", "Learn"];

  test("routes an edge around the chip row and keeps its label off it", () => {
    const withChips = layoutArchitecture(around(chips));
    const box = zoneOf(withChips).chipsBox!;
    // Without the chips, the edge takes the corridor the row now holds.
    expect(crosses(layoutArchitecture(around(undefined)), box)).toBe(true);
    expect(crosses(withChips, box)).toBe(false);
    for (const edge of withChips.edges) if (edge.label) expect(overlaps(edge.label, box)).toBe(false);
  });
});

// ── Brand colour and tones ────────────────────────────────────────────────────

describe("the chips wear the zone's tone or colour", () => {
  test.each([...TONES.map((tone) => ({ tone })), { color: "brand_1" as const }])("%o", (paint) => {
    const { container } = renderBlock(headBlock({ logo_file_id: ZONE_LOGO, chips: CHIPS, color: undefined, ...paint }));
    const zone = container.querySelector(".deck-arch-zone")!;
    expect(zone.getAttribute("data-tone")).toBe("tone" in paint ? paint.tone : null);
    expect(zone.getAttribute("data-color")).toBe("color" in paint ? paint.color : null);
    // Inside the zone's group, under nothing that sets a tone of its own: the
    // stylesheet tints `.deck-arch-chip rect` with the zone's `--tone`.
    const chips = Array.from(zone.querySelectorAll(":scope > .deck-arch-zone-chip"));
    expect(chips).toHaveLength(CHIPS.length);
    for (const chip of chips) expect(chip.querySelector("[data-tone], [data-color]")).toBeNull();
  });
});

// ── The fixtures the backend draws in Chromium ────────────────────────────────

/**
 * A platform zone across five columns over rows 2-4, headed by its logo and
 * its four chips: a frameless anchor at (2,3), a framed product under it and
 * two at the bottom corners, every edge ending on the anchor or between them.
 */
export const ZONE_CHIPS_GRID: DeckArchitectureBlock = {
  type: "architecture",
  nodes: [
    { id: "anchor", label: "Core", logo_file_id: NODE_LOGO, frame: false, row: 2, col: 3 },
    { id: "middle", label: "Product A", icon: "brain", color: "brand_2", row: 3, col: 3 },
    { id: "left", label: "Product B", icon: "crosshair", color: "brand_3", row: 4, col: 1 },
    { id: "right", label: "Product C", icon: "chart-line", color: "brand_4", row: 4, col: 5 },
  ],
  edges: [
    { source: "middle", target: "anchor", label: "Context" },
    { source: "left", target: "anchor", label: "Signals" },
    { source: "right", target: "anchor", label: "Findings" },
    { source: "left", target: "middle", label: "Feeds" },
    { source: "middle", target: "right", label: "Acts" },
  ],
  zones: [{ logo_file_id: ZONE_LOGO, color: "brand_1", chips: CHIPS, row: 2, col: 1, row_span: 3, col_span: 5 }],
  size: "lg",
};

/** One column, a logo, a label and six chips: a row no size can fit, for the layout check. */
export const ZONE_CHIPS_OVERFLOW: DeckArchitectureBlock = {
  type: "architecture",
  nodes: [
    { id: "a", label: "Product A", icon: "brain", row: 1, col: 1 },
    { id: "b", label: "Product B", icon: "crosshair", row: 2, col: 1 },
  ],
  edges: [{ source: "a", target: "b" }],
  zones: [
    {
      label: "Platform",
      logo_file_id: ZONE_LOGO,
      chips: ["Detect", "Respond", "Report", "Hunt", "Share", "Learn"],
      row: 1,
      col: 1,
      row_span: 2,
    },
  ],
};

describe("the 5-column grid with a zone headed by its logo and its chips", () => {
  const layout = layoutArchitecture(ZONE_CHIPS_GRID, { assets: ASSETS });
  const zone = zoneOf(layout);
  const without = layoutArchitecture({ ...ZONE_CHIPS_GRID, zones: [{ ...ZONE_CHIPS_GRID.zones![0], chips: undefined }] }, { assets: ASSETS });

  test("draws the logo top left and the four chips in one row top right", () => {
    expect(zone.logo!.x).toBe(zone.x + 24);
    expectOneRow(zone, CHIPS);
    expect(zone.chipsOverflow).toBe(0);
    expect(zone.chipsBox!.x + zone.chipsBox!.w).toBeCloseTo(padding(zone).right, 5);
    expect(zone.chipsBox!.x).toBeGreaterThan(zone.logo!.x + zone.logo!.w + 16);
  });

  test("overlaps nothing: no two nodes, no node and the head or the chips, no label and anything", () => {
    for (let i = 0; i < layout.nodes.length; i += 1) {
      for (let j = i + 1; j < layout.nodes.length; j += 1) expect(overlaps(layout.nodes[i], layout.nodes[j])).toBe(false);
    }
    const head = zoneHeadBox(zone);
    for (const node of layout.nodes) {
      expect(overlaps(head, node), `head x ${node.id}`).toBe(false);
      expect(overlaps(zone.chipsBox!, node), `chips x ${node.id}`).toBe(false);
    }
    const labels = layout.edges.flatMap((edge) => (edge.label ? [edge.label] : []));
    expect(labels).toHaveLength(ZONE_CHIPS_GRID.edges!.length);
    for (const [index, label] of labels.entries()) {
      expect(overlaps(label, head)).toBe(false);
      expect(overlaps(label, zone.chipsBox!)).toBe(false);
      for (const node of layout.nodes) expect(overlaps(label, node), `label on ${node.id}`).toBe(false);
      for (const other of labels.slice(index + 1)) expect(overlaps(label, other)).toBe(false);
    }
    for (const edge of layout.edges) {
      for (const node of layout.nodes) {
        if (node.id === edge.edge.source || node.id === edge.edge.target) continue;
        for (let i = 0; i + 1 < edge.points.length; i += 1) expect(segmentIntersectsRect(edge.points[i], edge.points[i + 1], node)).toBe(false);
      }
    }
  });

  test("leaves the nodes and every edge as they are without the chips", () => {
    expect(layout.nodes.map((node) => rect(node))).toEqual(without.nodes.map((node) => rect(node)));
    expect(layout.edges.map(({ key, d, points, label }) => ({ key, d, points, label }))).toEqual(
      without.edges.map(({ key, d, points, label }) => ({ key, d, points, label })),
    );
    expect(layout.edges.map((edge) => ({ key: edge.key, points: edge.points.map(point) }))).toMatchSnapshot();
  });

  test("is the fixture the backend's export and layout-check tests draw in Chromium", () => {
    // `ARCHITECTURE_FIXTURE=write yarn test:dom` rewrites the files after a deliberate change to the drawing.
    const geometry = `${JSON.stringify(
      {
        viewBox: [layout.width, layout.height],
        nodes: layout.nodes.map((node) => ({ id: node.id, frame: node.frame, box: rect(node) })),
        zone: {
          panel: rect(zone),
          logo: rect(zone.logo!),
          chips: zone.chips.map((chip) => ({ box: rect(chip), font_size: chip.text.fontSize })),
          chips_box: rect(zone.chipsBox!),
        },
        edges: layout.edges.map((edge) => ({ source: edge.edge.source, target: edge.edge.target, points: edge.points.map(point) })),
      },
      null,
      2,
    )}\n`;
    expectFixture("zone-chips", ZONE_CHIPS_GRID, geometry);
  });

  test("and the narrow zone whose chips no size fits", () => {
    expect(zoneOf(layoutArchitecture(ZONE_CHIPS_OVERFLOW, { assets: ASSETS })).chipsOverflow).toBeGreaterThan(0);
    expectFixture("zone-chips-overflow", ZONE_CHIPS_OVERFLOW, null);
  });
});

/** The figure as the page serializes it, its React ids named by order of appearance; written on demand, else compared. */
function expectFixture(name: string, block: DeckArchitectureBlock, geometry: string | null): void {
  const { container, unmount } = renderBlock(block);
  for (const image of Array.from(container.querySelectorAll("image"))) fireEvent.load(image);
  expect(container.querySelector("[data-deck-pending]")).toBeNull();
  const figure = container.querySelector("figure.deck-architecture")!;
  const html = `${stableIds(new XMLSerializer().serializeToString(figure))}\n`;
  unmount();
  const files: [string, string][] = [[`${name}.html`, html], ...(geometry ? [[`${name}.json`, geometry] as [string, string]] : [])];
  for (const [file, content] of files) {
    if (process.env.ARCHITECTURE_FIXTURE === "write") writeFileSync(resolve(DATA, file), content);
    expect(readFileSync(resolve(DATA, file), "utf8").replace(/\r\n/g, "\n"), file).toBe(content);
  }
}

function stableIds(html: string): string {
  const ids = Array.from(new Set(Array.from(html.matchAll(/ id="([^"]+)"/g), (match) => match[1])));
  return [...ids].sort((a, b) => b.length - a.length).reduce((out, id) => out.split(id).join(`fixture-${ids.indexOf(id)}`), html);
}
