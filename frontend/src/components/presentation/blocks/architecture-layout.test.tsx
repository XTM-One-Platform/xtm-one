/**
 * The architecture router and its fit pass on the 3-row "band / core / three
 * modules" diagram - `yarn test:dom`.
 *
 * `layoutArchitecture` is pure, so the geometry it returns is what is pinned:
 * a snapshot of the picture at 3 and at 5 columns (and compacted into a room
 * too short for it), and the routing contract on every edge of both - a path
 * starts on its source node's border, ends on its target's, and runs through
 * no third node. The general invariants of the router (no crossings on a
 * clear graph, labels on the canvas and off nodes and each other, lanes 44px
 * apart, determinism) are the fixture-driven `tests_unit/architecture-layout.test.ts`,
 * which carries these two fixtures as well.
 */
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckArchitectureBlock, DeckArchitectureNode } from "@/types";

import { ArchitectureBlockView } from "./Architecture";
import {
  ARCH,
  DENSITY_MIN,
  layoutArchitecture,
  segmentIntersectsRect,
  segmentsCross,
  type ArchitectureLayout,
  type LayoutEdge,
  type LayoutNode,
  type Point,
  type Rect,
} from "./architecture-layout";

// The icon catalog, loaded natively (see `metrics.test.tsx`).
vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

// ── Fixtures ──────────────────────────────────────────────────────────────────

const BAND: DeckArchitectureNode = {
  id: "band",
  label: "Band",
  sublabel: "Entry point",
  icon: "globe",
  chips: ["Chip A", "Chip B"],
  row: 1,
  col: 1,
  col_span: 3,
};
const CORE: DeckArchitectureNode = {
  id: "core",
  label: "Core",
  sublabel: "Shared runtime",
  icon: "sparkles",
  shape: "circle",
  emphasis: "core",
  row: 2,
  col: 2,
};
const MODULES: DeckArchitectureNode[] = [
  { id: "a", label: "Module A", icon: "brain", tone: "info", row: 3, col: 1 },
  { id: "b", label: "Module B", icon: "crosshair", tone: "warning", row: 3, col: 2 },
  { id: "c", label: "Module C", icon: "chart-line", tone: "success", row: 3, col: 3 },
];
const FLOWS: DeckArchitectureBlock["edges"] = [
  { source: "band", target: "core", label: "Onboarding" },
  { source: "core", target: "a", arrows: "none" },
  { source: "core", target: "b", arrows: "none" },
  { source: "core", target: "c", arrows: "none" },
  { source: "a", target: "b", label: "Threat data" },
  { source: "b", target: "c", label: "Exposure" },
];
const LEGEND: DeckArchitectureBlock["legend"] = [
  { label: "Data flow", marker: "arrow" },
  { label: "Runs on the core", marker: "line", tone: "muted" },
];

/** A band over the core over three modules, on 3 columns. */
export const THREE_COLUMNS: DeckArchitectureBlock = {
  type: "architecture",
  nodes: [BAND, CORE, ...MODULES],
  edges: FLOWS,
  zones: [{ label: "Platform", icon: "layers", row: 2, col: 1, row_span: 2, col_span: 3 }],
  legend: LEGEND,
};

const shifted = (node: DeckArchitectureNode, by: number): DeckArchitectureNode => ({ ...node, col: node.col + by });

/**
 * The same graph on 5 columns, the platform in the middle three and the
 * context it talks to on either side as ghost cards - a source feeding
 * Module A, an external card taking Module B's findings by an elbow that has
 * the core above it and Module C beside it.
 */
export const FIVE_COLUMNS: DeckArchitectureBlock = {
  type: "architecture",
  nodes: [
    shifted(BAND, 1),
    { id: "src", label: "Source", icon: "shield", emphasis: "ghost", icon_tile: false, row: 2, col: 1 },
    shifted(CORE, 1),
    { id: "ext", label: "External", icon: "radar", emphasis: "ghost", icon_tile: false, row: 2, col: 5 },
    ...MODULES.map((node) => shifted(node, 1)),
  ],
  edges: [
    ...FLOWS,
    { source: "src", target: "a", label: "Events", dashed: true },
    { source: "b", target: "ext", label: "Findings", route: "orthogonal" },
  ],
  zones: [{ label: "Platform", icon: "layers", row: 2, col: 2, row_span: 2, col_span: 3 }],
  legend: LEGEND,
};

// ── Helpers ───────────────────────────────────────────────────────────────────

const round = (n: number) => Math.round(n * 10) / 10;
const point = (p: Point) => [round(p.x), round(p.y)];
const rect = (r: Rect) => [round(r.x), round(r.y), round(r.w), round(r.h)];

/** The picture as a reviewer reads it: every box, every path, every label. */
function summarize(layout: ArchitectureLayout) {
  return {
    size: [layout.width, layout.height],
    naturalHeight: layout.naturalHeight,
    density: layout.density,
    nodes: layout.nodes.map((node) => ({
      id: node.id,
      kind: node.kind,
      box: rect(node),
      text: node.textAnchor,
      tile: node.mediaTile,
      label: rect(node.labelBox),
    })),
    zones: layout.zones.map((zone) => rect(zone)),
    edges: layout.edges.map((edge) => ({
      key: edge.key,
      points: edge.points.map(point),
      label: edge.label ? rect(edge.label) : null,
    })),
    legend: layout.legend.map((item) => [item.marker, round(item.x), round(item.y)]),
  };
}

const segments = (points: Point[]): [Point, Point][] => {
  const out: [Point, Point][] = [];
  for (let i = 0; i + 1 < points.length; i += 1) out.push([points[i], points[i + 1]]);
  return out;
};

const nodeOf = (layout: ArchitectureLayout, id: string): LayoutNode => {
  const node = layout.nodes.find((n) => n.id === id);
  if (!node) throw new Error(`no node ${id}`);
  return node;
};

const edgeOf = (layout: ArchitectureLayout, source: string, target: string): LayoutEdge => {
  const edge = layout.edges.find((e) => e.edge.source === source && e.edge.target === target);
  if (!edge) throw new Error(`no edge ${source} -> ${target}`);
  return edge;
};

/** Does `p` lie on the node's border: a rectangle's four sides, a circle's circumference, a pill's flat runs and caps? */
function onBorder(p: Point, node: LayoutNode): boolean {
  const eps = 0.5;
  if (node.kind === "circle") return Math.abs(Math.hypot(p.x - node.cx, p.y - node.cy) - node.radius) <= eps;
  const withinX = p.x >= node.x - eps && p.x <= node.x + node.w + eps;
  const withinY = p.y >= node.y - eps && p.y <= node.y + node.h + eps;
  const onTopOrBottom = (Math.abs(p.y - node.y) <= eps || Math.abs(p.y - node.y - node.h) <= eps) && withinX;
  if (node.kind === "pill") {
    const r = node.radius;
    const onCap = [node.x + r, node.x + node.w - r].some((cx) => Math.abs(Math.hypot(p.x - cx, p.y - node.cy) - r) <= eps);
    return onTopOrBottom || onCap;
  }
  const onLeftOrRight = (Math.abs(p.x - node.x) <= eps || Math.abs(p.x - node.x - node.w) <= eps) && withinY;
  return onTopOrBottom || onLeftOrRight;
}

const overlaps = (a: Rect, b: Rect): boolean => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;

/** The routing contract: every path leaves its source, reaches its target, and touches nothing else. */
function expectRoutedFromSourceToTarget(layout: ArchitectureLayout): void {
  for (const edge of layout.edges) {
    const source = nodeOf(layout, edge.edge.source);
    const target = nodeOf(layout, edge.edge.target);
    const first = edge.points[0];
    const last = edge.points[edge.points.length - 1];
    expect(onBorder(first, source), `${edge.key} starts at ${first.x},${first.y}, off ${source.id}`).toBe(true);
    expect(onBorder(last, target), `${edge.key} ends at ${last.x},${last.y}, off ${target.id}`).toBe(true);
    for (const node of layout.nodes) {
      if (node === source || node === target) continue;
      for (const [a, b] of segments(edge.points)) {
        expect(segmentIntersectsRect(a, b, node), `${edge.key} runs through ${node.id}`).toBe(false);
      }
    }
  }
}

function expectLabelsClear(layout: ArchitectureLayout): void {
  const labels = layout.edges.flatMap((edge) => (edge.label ? [{ key: edge.key, label: edge.label }] : []));
  for (const { key, label } of labels) {
    expect(label.x).toBeGreaterThanOrEqual(0);
    expect(label.y).toBeGreaterThanOrEqual(0);
    expect(label.x + label.w).toBeLessThanOrEqual(layout.width);
    expect(label.y + label.h).toBeLessThanOrEqual(layout.height);
    for (const node of layout.nodes) expect(overlaps(label, node), `label of ${key} on ${node.id}`).toBe(false);
  }
  for (let i = 0; i < labels.length; i += 1) {
    for (let j = i + 1; j < labels.length; j += 1) {
      expect(overlaps(labels[i].label, labels[j].label), `labels of ${labels[i].key} and ${labels[j].key}`).toBe(false);
    }
  }
}

// ── The two layouts ───────────────────────────────────────────────────────────

describe.each([
  ["3 columns", THREE_COLUMNS],
  ["5 columns", FIVE_COLUMNS],
])("band / core / three modules on %s", (_name, block) => {
  const layout = layoutArchitecture(block);

  test("is laid out as pinned", () => {
    expect(summarize(layout)).toMatchSnapshot();
  });

  test("every edge leaves its source, reaches its target, and runs through no other node", () => {
    expect(layout.edges).toHaveLength(block.edges?.length ?? 0);
    expectRoutedFromSourceToTarget(layout);
  });

  test("no label sits on a node or on another label", () => {
    expectLabelsClear(layout);
  });

  test("the ties from the core land on the top edge centre of each module, symmetric about the core", () => {
    const core = nodeOf(layout, "core");
    for (const id of ["a", "b", "c"]) {
      const module = nodeOf(layout, id);
      const tie = edgeOf(layout, "core", id);
      expect(tie.points).toHaveLength(2);
      const end = tie.points[1];
      expect(end.x).toBeCloseTo(module.cx, 5);
      expect(end.y).toBeCloseTo(module.y, 5);
      // Radial out of the circle, toward that very point.
      const start = tie.points[0];
      expect(Math.hypot(start.x - core.cx, start.y - core.cy)).toBeCloseTo(core.radius, 5);
      const cross = (end.x - core.cx) * (start.y - core.cy) - (end.y - core.cy) * (start.x - core.cx);
      expect(Math.abs(cross)).toBeLessThan(1e-6);
    }
    const left = edgeOf(layout, "core", "a").points[0];
    const right = edgeOf(layout, "core", "c").points[0];
    expect(left.x - core.cx).toBeCloseTo(-(right.x - core.cx), 5);
    expect(left.y).toBeCloseTo(right.y, 5);
  });

  test("rows are as tall as their content, whatever the column count", () => {
    // The same nodes measure the same on both grids: a row's height comes
    // from what is in it, never from how wide the drawing is.
    const three = layoutArchitecture(THREE_COLUMNS);
    for (const id of ["band", "core", "a", "b", "c"]) {
      expect(nodeOf(layout, id).h).toBe(nodeOf(three, id).h);
      expect(nodeOf(layout, id).y).toBe(nodeOf(three, id).y);
    }
  });

  test("is deterministic", () => {
    expect(summarize(layoutArchitecture(block))).toEqual(summarize(layout));
  });
});

describe("the 3-column diagram's edges", () => {
  test("never cross: the two flows between the modules run between them, the ties fan out below the core", () => {
    const layout = layoutArchitecture(THREE_COLUMNS);
    for (let i = 0; i < layout.edges.length; i += 1) {
      for (let j = i + 1; j < layout.edges.length; j += 1) {
        for (const [a, b] of segments(layout.edges[i].points)) {
          for (const [c, d] of segments(layout.edges[j].points)) {
            expect(segmentsCross(a, b, c, d), `${layout.edges[i].key} x ${layout.edges[j].key}`).toBe(false);
          }
        }
      }
    }
  });
});

describe("a diagonal edge between two non-adjacent cells takes the column gap", () => {
  const layout = layoutArchitecture(FIVE_COLUMNS);
  const edge = edgeOf(layout, "b", "ext");

  test("leaves Module B, not the core, and reaches the external card", () => {
    const source = nodeOf(layout, "b");
    const target = nodeOf(layout, "ext");
    const core = nodeOf(layout, "core");
    expect(onBorder(edge.points[0], source)).toBe(true);
    expect(onBorder(edge.points[edge.points.length - 1], target)).toBe(true);
    for (const [p, q] of segments(edge.points)) {
      expect(segmentIntersectsRect(p, q, core), "runs through the core").toBe(false);
      expect(segmentIntersectsRect(p, q, nodeOf(layout, "c")), "runs through Module C").toBe(false);
    }
  });

  test("takes the free corridor between the columns rather than either elbow", () => {
    // Both plain elbows run through a node (the core above, Module C beside),
    // so the route is the column gap between Module B and Module C.
    expect(edge.points).toHaveLength(4);
    const corridorX = edge.points[1].x;
    expect(corridorX).toBeGreaterThan(nodeOf(layout, "b").x + nodeOf(layout, "b").w);
    expect(corridorX).toBeLessThan(nodeOf(layout, "c").x);
    expect(edge.points[2].x).toBe(corridorX);
  });
});

// ── The fit pass ──────────────────────────────────────────────────────────────

describe("the fit pass", () => {
  const natural = layoutArchitecture(THREE_COLUMNS);

  test("leaves a drawing that fits its room exactly as it is", () => {
    const roomy = layoutArchitecture(THREE_COLUMNS, { room: { width: 1760, height: natural.height + 40 } });
    expect(roomy.density).toBe(1);
    expect(summarize(roomy)).toEqual(summarize(natural));
  });

  test("compacts the rows of a drawing taller than its room before anything else shrinks", () => {
    const room = { width: 1760, height: natural.height - 120 };
    const compact = layoutArchitecture(THREE_COLUMNS, { room });
    expect(compact.density).toBeLessThan(1);
    expect(compact.density).toBeGreaterThanOrEqual(DENSITY_MIN);
    expect(compact.height).toBeLessThanOrEqual(room.height + 0.5);
    expect(compact.naturalHeight).toBe(natural.height);
    // Closer rows and tighter cards, the type untouched: the same label
    // lines, the same widths.
    expect(nodeOf(compact, "a").y - (nodeOf(compact, "core").y + nodeOf(compact, "core").h)).toBeLessThan(
      nodeOf(natural, "a").y - (nodeOf(natural, "core").y + nodeOf(natural, "core").h),
    );
    expect(nodeOf(compact, "a").h).toBeLessThan(nodeOf(natural, "a").h);
    expect(compact.width).toBe(natural.width);
    for (const node of compact.nodes) {
      const before = nodeOf(natural, node.id);
      expect(node.label.fontSize).toBe(before.label.fontSize);
      expect(node.label.lines.length).toBe(before.label.lines.length);
    }
    // The routing contract holds on the compacted picture too.
    expectRoutedFromSourceToTarget(compact);
    expectLabelsClear(compact);
    expect(summarize(compact)).toMatchSnapshot();
  });

  test("is bisected to the largest density that fits", () => {
    const room = { width: 1760, height: natural.height - 60 };
    const compact = layoutArchitecture(THREE_COLUMNS, { room });
    const denser = layoutArchitecture(THREE_COLUMNS, { room: { ...room, height: room.height - 60 } });
    expect(compact.height).toBeLessThanOrEqual(room.height + 0.5);
    expect(denser.density).toBeLessThan(compact.density);
    expect(denser.height).toBeLessThan(compact.height);
  });

  test("hands a drawing still too tall at the floor over as it stands", () => {
    const floor = layoutArchitecture(THREE_COLUMNS, { room: { width: 1760, height: 200 } });
    expect(floor.density).toBe(DENSITY_MIN);
    expect(floor.height).toBeGreaterThan(200);
    expect(floor.naturalHeight).toBe(natural.height);
  });

  test("a drawing shrunk to a narrow room has that much more height, and is not compacted for it", () => {
    // Drawn at 700 / 1088 of its size the natural height fits 600px: nothing to compact.
    const narrow = layoutArchitecture(THREE_COLUMNS, { room: { width: 700, height: 600 } });
    expect(narrow.density).toBe(1);
    expect(summarize(narrow)).toEqual(summarize(natural));
  });

  test("is never magnified: a room wider and taller than the drawing changes nothing", () => {
    const huge = layoutArchitecture(THREE_COLUMNS, { room: { width: 4000, height: 3000 } });
    expect(summarize(huge)).toEqual(summarize(natural));
  });
});

// ── Ghost cards without a tile ────────────────────────────────────────────────

describe("icon_tile: false", () => {
  const block: DeckArchitectureBlock = {
    type: "architecture",
    nodes: [
      { id: "a", label: "SIEM", icon: "shield", emphasis: "ghost", icon_tile: false, row: 1, col: 1 },
      { id: "b", label: "Ticketing and on-call", icon: "bell", emphasis: "ghost", icon_tile: false, row: 1, col: 2 },
      { id: "t", label: "Tiled", icon: "bell", row: 1, col: 3 },
      { id: "c", label: "Core", shape: "circle", emphasis: "core", row: 2, col: 2 },
    ],
    edges: [
      { source: "c", target: "a", arrows: "none" },
      { source: "c", target: "b", arrows: "none" },
      { source: "c", target: "t", arrows: "none" },
    ],
  };
  const layout = layoutArchitecture(block);

  test("draws the icon inline and keeps the label on the card's vertical centre", () => {
    for (const id of ["a", "b"]) {
      const node = nodeOf(layout, id);
      expect(node.mediaTile).toBe(false);
      expect(node.textAnchor).toBe("start");
      expect(node.mediaBox).toMatchObject({ w: ARCH.ICON, h: ARCH.ICON });
      expect(node.labelBox.y + node.labelBox.h / 2).toBeCloseTo(node.h / 2, 5);
      expect(node.mediaBox!.y + node.mediaBox!.h / 2).toBeCloseTo(node.h / 2, 5);
      // The icon sits before the text, inside the card.
      expect(node.mediaBox!.x).toBeGreaterThanOrEqual(ARCH.CARD_PAD_X);
      expect(node.labelBox.x).toBeGreaterThan(node.mediaBox!.x + node.mediaBox!.w);
      expect(node.labelBox.x + node.label.width).toBeLessThanOrEqual(node.w - ARCH.CARD_PAD_X + 0.5);
    }
  });

  test("two such cards on one row align: one label line or two, both labels on the row's centre", () => {
    const a = nodeOf(layout, "a");
    const b = nodeOf(layout, "b");
    expect(a.label.lines).toHaveLength(1);
    expect(b.label.lines).toHaveLength(2);
    expect(a.h).toBe(b.h);
    expect(a.y + a.labelBox.y + a.labelBox.h / 2).toBeCloseTo(b.y + b.labelBox.y + b.labelBox.h / 2, 5);
  });

  test("a tiled card keeps the tile above its label", () => {
    const tiled = nodeOf(layout, "t");
    expect(tiled.mediaTile).toBe(true);
    expect(tiled.textAnchor).toBe("middle");
    expect(tiled.labelBox.y).toBeGreaterThan(tiled.mediaBox!.y + tiled.mediaBox!.h);
  });
});

// ── An edge into a zone on the core column ────────────────────────────

describe("an edge label on a 1-row grid with a zone on the core column", () => {
  const block: DeckArchitectureBlock = {
    type: "architecture",
    nodes: [
      { id: "sensors", label: "Sensors", icon: "radar", row: 1, col: 1 },
      { id: "core", label: "XTM One", icon: "sparkles", shape: "circle", emphasis: "core", row: 1, col: 2 },
      { id: "act", label: "Response", icon: "zap", row: 1, col: 3 },
    ],
    edges: [
      { source: "sensors", target: "core", label: "Telemetry" },
      { source: "core", target: "act", label: "Actions" },
    ],
    zones: [{ label: "Platform", icon: "layers", row: 1, col: 2, row_span: 1, col_span: 1 }],
  };
  const layout = layoutArchitecture(block);

  test("each label sits on its own edge, clear of every node (before: Telemetry 116 px under its line)", () => {
    for (const edge of layout.edges) {
      const label = edge.label!;
      const lineY = edge.points[0].y;
      expect(edge.points.every((p) => p.y === lineY)).toBe(true);
      expect(label.y).toBeLessThan(lineY);
      expect(label.y + label.h).toBeGreaterThan(lineY);
      for (const node of layout.nodes) {
        const clear =
          label.x + label.w <= node.x || label.x >= node.x + node.w || label.y + label.h <= node.y || label.y >= node.y + node.h;
        expect(clear, `${edge.key} on ${node.id}`).toBe(true);
      }
    }
  });

  test("a pill wider than its gap stops short of the arrowhead (4.6 of 12 units showed)", () => {
    for (const edge of layout.edges) {
      const label = edge.label!;
      const tip = edge.points[edge.points.length - 1].x;
      const gap = tip - edge.points[0].x;
      const shown = tip - (label.x + label.w);
      // The whole head when the gap leaves it, else every unit the gap has once
      // the pill is pushed 2.5 off the source ("Telemetry": 9.3 spare, 6.8 shown).
      expect(shown, edge.key).toBeGreaterThanOrEqual(Math.min(ARCH.ARROW_HEAD, gap - label.w - 2.5) - 0.01);
      expect(label.x - edge.points[0].x, edge.key).toBeGreaterThanOrEqual(2);
    }
  });

  test("a pill that fits its gap keeps the whole arrowhead clear", () => {
    const wide = layoutArchitecture({
      ...block,
      edges: [{ source: "sensors", target: "core", label: "Feed" }],
    });
    const edge = wide.edges[0];
    const tip = edge.points[edge.points.length - 1].x;
    expect(tip - (edge.label!.x + edge.label!.w)).toBeGreaterThanOrEqual(ARCH.ARROW_HEAD);
  });
});

// ── One near-node rule for edges that enter no zone ───────────────────

describe("an edge label on a 1-row grid with no zone", () => {
  const block: DeckArchitectureBlock = {
    type: "architecture",
    nodes: [
      { id: "sensors", label: "Sensors", icon: "radar", row: 1, col: 1 },
      { id: "core", label: "XTM One", icon: "sparkles", shape: "circle", emphasis: "core", row: 1, col: 2 },
      { id: "act", label: "Response", icon: "zap", row: 1, col: 3 },
    ],
    edges: [
      { source: "sensors", target: "core", label: "Telemetry" },
      { source: "core", target: "act", label: "Actions" },
    ],
  };

  test("sits on its line beside a core circle, as it does with a zone", () => {
    const layout = layoutArchitecture(block);
    for (const edge of layout.edges) {
      const lineY = edge.points[0].y;
      expect(edge.label!.y, edge.key).toBeLessThan(lineY);
      expect(edge.label!.y + edge.label!.h, edge.key).toBeGreaterThan(lineY);
    }
  });

  test("between two cards the gap is narrower than the pill: it takes the nearest slide, just under the row", () => {
    const cards = layoutArchitecture({
      ...block,
      nodes: block.nodes.map(({ shape: _shape, emphasis: _emphasis, ...node }) => node),
    });
    const rowBottom = Math.max(...cards.nodes.map((node) => node.y + node.h));
    for (const edge of cards.edges) {
      const label = edge.label!;
      const gap = edge.points[edge.points.length - 1].x - edge.points[0].x;
      expect(label.w, edge.key).toBeGreaterThan(gap);
      expect(label.y, edge.key).toBeGreaterThanOrEqual(rowBottom);
      expect(label.y - rowBottom, edge.key).toBeLessThanOrEqual(16);
    }
  });
});

// ── The renderer ──────────────────────────────────────────────────────────────

describe("ArchitectureBlockView", () => {
  test("publishes the drawing's canvas width and natural ratio, and draws no tile for an inline icon", () => {
    const { container } = renderWithProviders(<ArchitectureBlockView block={FIVE_COLUMNS} />);
    const layout = layoutArchitecture(FIVE_COLUMNS);
    const figure = container.querySelector<HTMLElement>(".deck-architecture");
    expect(figure).not.toBeNull();
    expect(figure!.style.getPropertyValue("--deck-architecture-w")).toBe(`${layout.width}px`);
    expect(figure!.style.getPropertyValue("--deck-architecture-ratio")).toBe(String(layout.width / layout.naturalHeight));
    expect(figure!.style.getPropertyValue("--deck-architecture-drawn-h")).toBe(`${layout.height}px`);
    expect(container.querySelector("svg")?.getAttribute("viewBox")).toBe(`0 0 ${layout.width} ${layout.height}`);
    // Four icon tiles: the band and the three modules; the two ghosts draw none.
    expect(container.querySelectorAll(".deck-arch-node-tile")).toHaveLength(4);
    const ghost = container.querySelector('.deck-arch-node[aria-label="Source"]');
    expect(ghost).not.toBeNull();
    expect(ghost!.querySelector(".deck-arch-node-tile")).toBeNull();
    expect(ghost!.querySelector("text")?.getAttribute("text-anchor")).toBe("start");
  });
});
