// @vitest-environment node
import { describe, expect, it as test } from 'vitest';
import {
  ARCH,
  layoutArchitecture,
  resolveZoneRef,
  segmentIntersectsRect,
  segmentsCross,
  type ArchitectureLayout,
  type LayoutEdge,
  type LayoutNode,
  type Point,
  type Rect,
} from '../src/components/presentation/blocks/architecture-layout';
import type { DeckArchitectureBlock } from '../src/types';

// ---------------------------------------------------------------------------
// Architecture diagram router - geometric guarantees, table-driven.
//
// `layoutArchitecture` is pure TypeScript (no DOM, no measurement), so it is
// exercised here directly, without a page: what the renderer promises about a
// routed picture is asserted on the geometry it returns. Every spec below is
// one the validator accepts; every invariant below holds for each of them.
// ---------------------------------------------------------------------------

type Spec = DeckArchitectureBlock;

const block = (partial: Omit<Spec, 'type'>): Spec => ({ type: 'architecture', ...partial });

/** Slide 11 of the reference deck: band, zone, circle core, labeled exchange. */
const SLIDE_11 = block({
  nodes: [
    { id: 'hub', label: 'XTM Hub', sublabel: 'Entry point', icon: 'globe', chips: ['Trials', 'Libraries'], row: 1, col: 1, col_span: 3 },
    { id: 'cti', label: 'OpenCTI', icon: 'brain', tone: 'info', row: 2, col: 2 },
    { id: 'core', label: 'XTM One', sublabel: 'Agentic core', icon: 'sparkles', shape: 'circle', emphasis: 'core', row: 3, col: 2 },
    { id: 'crq', label: 'OpenCRQ', icon: 'chart-line', tone: 'success', row: 4, col: 1 },
    { id: 'aev', label: 'OpenAEV', icon: 'crosshair', tone: 'warning', row: 4, col: 3 },
  ],
  edges: [
    { source: 'hub', target: 'cti', label: 'Onboarding' },
    { source: 'cti', target: 'crq', label: 'Threat landscape' },
    { source: 'cti', target: 'aev', label: 'Threat data' },
    { source: 'aev', target: 'cti', label: 'Security posture' },
    { source: 'core', target: 'cti', arrows: 'none', tone: 'muted' },
    { source: 'core', target: 'crq', arrows: 'none', tone: 'muted' },
    { source: 'core', target: 'aev', arrows: 'none', tone: 'muted' },
  ],
  zones: [{ label: 'XTM Platform', icon: 'layers', row: 2, col: 1, row_span: 3, col_span: 3 }],
  legend: [
    { label: 'Data flow', marker: 'arrow' },
    { label: 'Runs on the core', marker: 'line', tone: 'muted' },
  ],
  caption: 'Every module runs on the core',
});

/** A left-to-right chain with a return edge that has to detour around it. */
const PIPELINE = block({
  nodes: [
    { id: 'collect', label: 'Collect', icon: 'download', row: 1, col: 1 },
    { id: 'normalize', label: 'Normalize', icon: 'filter', row: 1, col: 2 },
    { id: 'enrich', label: 'Enrich', icon: 'sparkles', row: 1, col: 3 },
    { id: 'store', label: 'Store', icon: 'database', row: 1, col: 4 },
    { id: 'serve', label: 'Serve', icon: 'globe', row: 1, col: 5 },
  ],
  edges: [
    { source: 'collect', target: 'normalize', label: 'Raw' },
    { source: 'normalize', target: 'enrich', label: 'Clean' },
    { source: 'enrich', target: 'store', label: 'Rich' },
    { source: 'store', target: 'serve', label: 'Read' },
    { source: 'serve', target: 'collect', label: 'Feedback loop', dashed: true },
  ],
  zones: [],
  legend: [],
});

/** A core whose straight chord to its target would run through a third node. */
const BLOCKED_CORE = block({
  nodes: [
    { id: 'core', label: 'Core', shape: 'circle', emphasis: 'core', row: 1, col: 1 },
    { id: 'middle', label: 'Middle', row: 1, col: 2 },
    { id: 'edge', label: 'Edge', row: 1, col: 3 },
  ],
  edges: [
    { source: 'core', target: 'edge', arrows: 'none' },
    { source: 'core', target: 'middle', arrows: 'none' },
  ],
  zones: [],
  legend: [],
});

/** Two pills side by side exchanging in both directions. */
const RECIPROCAL_PILLS = block({
  nodes: [
    { id: 'analyst', label: 'Analyst', shape: 'pill', icon: 'user', row: 1, col: 1 },
    { id: 'platform', label: 'Platform', shape: 'pill', icon: 'server', row: 1, col: 2 },
  ],
  edges: [
    { source: 'analyst', target: 'platform', label: 'Questions' },
    { source: 'platform', target: 'analyst', label: 'Answers' },
  ],
  zones: [],
  legend: [],
});

/**
 * Two cards stacked, exchanging in both directions. Unlabeled on purpose: two
 * label pills cannot both sit on 84px of vertical gap without touching a node,
 * and label placement is per edge (a known limit, not a routing guarantee).
 */
const RECIPROCAL_CARDS = block({
  nodes: [
    { id: 'sensor', label: 'Sensor', row: 1, col: 1 },
    { id: 'siem', label: 'SIEM', row: 2, col: 1 },
  ],
  edges: [
    { source: 'sensor', target: 'siem' },
    { source: 'siem', target: 'sensor' },
  ],
  zones: [],
  legend: [],
});

/** A one-column diagram with a legend wider than the drawing. */
const NARROW_LEGEND = block({
  nodes: [
    { id: 'a', label: 'Collector', row: 1, col: 1 },
    { id: 'b', label: 'Platform', row: 2, col: 1 },
  ],
  edges: [{ source: 'a', target: 'b', label: 'Events' }],
  zones: [],
  legend: [
    { label: 'Data flow between modules', marker: 'arrow' },
    { label: 'Runs on the shared core', marker: 'line', tone: 'muted' },
    { label: 'Planned for next quarter', marker: 'dashed' },
    { label: 'External system', marker: 'node', tone: 'warning' },
  ],
});

/** A 3 x 3 hub-and-spoke: eight straight ties out of a central core. */
const HUB_AND_SPOKE = block({
  nodes: [
    { id: 'n1', label: 'North west', row: 1, col: 1 },
    { id: 'n2', label: 'North', row: 1, col: 2 },
    { id: 'n3', label: 'North east', row: 1, col: 3 },
    { id: 'w', label: 'West', row: 2, col: 1 },
    { id: 'core', label: 'Core', shape: 'circle', emphasis: 'core', row: 2, col: 2 },
    { id: 'e', label: 'East', row: 2, col: 3 },
    { id: 's1', label: 'South west', row: 3, col: 1 },
    { id: 's2', label: 'South', row: 3, col: 2 },
    { id: 's3', label: 'South east', row: 3, col: 3 },
  ],
  edges: ['n1', 'n2', 'n3', 'w', 'e', 's1', 's2', 's3'].map((id) => ({ source: 'core', target: id, arrows: 'none' as const })),
  zones: [],
  legend: [],
});

/**
 * Labels at the validator's 40-character bound with no space to break at, on
 * every node kind: a token still wider than its box at the minimum font size
 * must be broken into runs that fit, never painted across the neighbour.
 */
const LONG_TOKENS = block({
  nodes: [
    { id: 'band', label: 'Supercalifragilisticexpialidocious-band', sublabel: 'Uninterruptible-sublabel-of-forty-chars!', icon: 'globe', row: 1, col: 1, col_span: 2 },
    { id: 'card', label: 'Pneumonoultramicroscopicsilicovolcanoco', sublabel: 'Another-long-unbroken-token-in-the-card', icon: 'brain', row: 2, col: 1 },
    { id: 'pill', label: 'Hippopotomonstrosesquippedaliophobia-pil', shape: 'pill', row: 2, col: 2 },
    { id: 'core', label: 'Floccinaucinihilipilification-core-label', shape: 'circle', emphasis: 'core', row: 3, col: 1 },
  ],
  edges: [
    { source: 'band', target: 'card' },
    { source: 'band', target: 'pill' },
    { source: 'core', target: 'card', arrows: 'none' },
    { source: 'core', target: 'pill', arrows: 'none' },
  ],
  zones: [],
  legend: [],
});

/**
 * One row where a curved edge skips the middle node: the arc has to
 * follow the detour corridor the scorer picked, not fly off the canvas the way
 * control points sized from the anchor distance sent it.
 */
const CURVED_SKIP = block({
  nodes: [
    { id: 'a', label: 'Ingest', icon: 'download', row: 1, col: 1 },
    { id: 'b', label: 'Process', icon: 'filter', row: 1, col: 2 },
    { id: 'c', label: 'Publish', icon: 'globe', row: 1, col: 3 },
  ],
  edges: [
    // Short labels: a pill has to fit the 96px gap (a label limit, as in PIPELINE).
    { source: 'a', target: 'b', label: 'Raw' },
    { source: 'b', target: 'c', label: 'Rich' },
    { source: 'a', target: 'c', label: 'Fast path', route: 'curve', dashed: true },
  ],
  zones: [],
  legend: [],
});

/**
 * A band over a circle core over three modules on 3 columns: three
 * straight ties fanning out of the core onto the top edge centres of the
 * cards, two labeled flows between side-by-side cards whose labels are wider
 * than the gap between them. The vitest snapshot of this picture, and the
 * same graph on 5 columns, is `blocks/architecture-layout.test.tsx`.
 */
const BAND_CORE_MODULES = block({
  nodes: [
    { id: 'band', label: 'Band', sublabel: 'Entry point', icon: 'globe', chips: ['Chip A', 'Chip B'], row: 1, col: 1, col_span: 3 },
    { id: 'core', label: 'Core', sublabel: 'Shared runtime', icon: 'sparkles', shape: 'circle', emphasis: 'core', row: 2, col: 2 },
    { id: 'a', label: 'Module A', icon: 'brain', tone: 'info', row: 3, col: 1 },
    { id: 'b', label: 'Module B', icon: 'crosshair', tone: 'warning', row: 3, col: 2 },
    { id: 'c', label: 'Module C', icon: 'chart-line', tone: 'success', row: 3, col: 3 },
  ],
  edges: [
    { source: 'band', target: 'core', label: 'Onboarding' },
    { source: 'core', target: 'a', arrows: 'none' },
    { source: 'core', target: 'b', arrows: 'none' },
    { source: 'core', target: 'c', arrows: 'none' },
    { source: 'a', target: 'b', label: 'Threat data' },
    { source: 'b', target: 'c', label: 'Exposure' },
  ],
  zones: [{ label: 'Platform', icon: 'layers', row: 2, col: 1, row_span: 2, col_span: 3 }],
  legend: [
    { label: 'Data flow', marker: 'arrow' },
    { label: 'Runs on the core', marker: 'line', tone: 'muted' },
  ],
});

/**
 * A zone headed by its logo across five columns, a frameless logo and a
 * frameless row of chips inside it, three framed products, and a frameless
 * core circle and pill tied straight. Placeholder file ids; with no
 * stored geometry the logos take their default ratio.
 */
const FRAMELESS = block({
  nodes: [
    { id: 'partner', label: 'Partner', logo_file_id: '00000000-0000-4000-8000-00000000b002', frame: false, row: 1, col: 1 },
    { id: 'core', label: 'Core', icon: 'sparkles', shape: 'circle', emphasis: 'core', frame: false, row: 1, col: 3 },
    { id: 'skills', label: 'Capabilities', chips: ['Detect', 'Respond', 'Report'], frame: false, row: 1, col: 5 },
    { id: 'alpha', label: 'Product A', icon: 'brain', row: 2, col: 2 },
    { id: 'beta', label: 'Product B', icon: 'crosshair', emphasis: 'ghost', row: 2, col: 3 },
    { id: 'gamma', label: 'Product C', icon: 'chart-line', row: 2, col: 4 },
    { id: 'actor', label: 'Actor', icon: 'users', shape: 'pill', frame: false, row: 3, col: 3 },
  ],
  edges: [
    { source: 'partner', target: 'alpha', label: 'Feeds' },
    { source: 'core', target: 'alpha', arrows: 'none' },
    { source: 'core', target: 'beta', arrows: 'none' },
    { source: 'core', target: 'gamma', arrows: 'none' },
    { source: 'gamma', target: 'skills', label: 'Uses' },
    { source: 'actor', target: 'beta', label: 'Asks' },
  ],
  zones: [{ label: 'Platform', logo_file_id: '00000000-0000-4000-8000-00000000a001', row: 1, col: 1, row_span: 2, col_span: 5 }],
});

/**
 * A zone across five columns over rows 2-4, headed by its logo and a row of
 * four chips: a frameless anchor at (2,3), a framed product under it
 * and two at the bottom corners, the edges ending on the anchor or between
 * them. The vitest twin, which draws it for the backend's Chromium tests, is
 * `blocks/architecture-zone-chips.test.tsx`.
 */
const ZONE_CHIPS = block({
  nodes: [
    { id: 'anchor', label: 'Core', logo_file_id: '00000000-0000-4000-8000-00000000b002', frame: false, row: 2, col: 3 },
    { id: 'middle', label: 'Product A', icon: 'brain', row: 3, col: 3 },
    { id: 'left', label: 'Product B', icon: 'crosshair', row: 4, col: 1 },
    { id: 'right', label: 'Product C', icon: 'chart-line', row: 4, col: 5 },
  ],
  edges: [
    { source: 'middle', target: 'anchor', label: 'Context' },
    { source: 'left', target: 'anchor', label: 'Signals' },
    { source: 'right', target: 'anchor', label: 'Findings' },
    { source: 'left', target: 'middle', label: 'Feeds' },
    { source: 'middle', target: 'right', label: 'Acts' },
  ],
  zones: [
    {
      logo_file_id: '00000000-0000-4000-8000-00000000a001',
      chips: ['AI agents', 'Shared context', 'Automation', 'Integrations'],
      row: 2,
      col: 1,
      row_span: 3,
      col_span: 5,
    },
  ],
});

/**
 * Pinned sides, a pair on lanes and a zone end: a context node above
 * a platform zone feeding its border, a centre node in the zone's first row
 * with a squared exchange to each bottom corner, every side pinned, and the
 * two corners exchanging along the bottom row on lanes 1 and 2. The vitest
 * twin, which draws it for the backend's Chromium tests, is
 * `blocks/architecture-edge-sides.test.tsx`.
 */
const EDGE_SIDES = block({
  nodes: [
    { id: 'context', label: 'Context', icon: 'radar', row: 1, col: 3 },
    { id: 'centre', label: 'Core', icon: 'sparkles', row: 2, col: 3 },
    { id: 'left', label: 'Product A', icon: 'brain', row: 3, col: 1 },
    { id: 'right', label: 'Product B', icon: 'crosshair', row: 3, col: 5 },
  ],
  edges: [
    { source: 'context', target: 'zone:platform', label: 'Feeds' },
    { source: 'centre', target: 'left', source_side: 'left', target_side: 'top', label: 'Tasks' },
    { source: 'left', target: 'centre', source_side: 'top', target_side: 'left', label: 'Results' },
    { source: 'centre', target: 'right', source_side: 'right', target_side: 'top', label: 'Tasks' },
    { source: 'right', target: 'centre', source_side: 'top', target_side: 'right', label: 'Results' },
    { source: 'left', target: 'right', route: 'straight', lane: 1, label: 'Requests' },
    { source: 'right', target: 'left', route: 'straight', lane: 2, label: 'Answers' },
  ],
  zones: [{ id: 'platform', label: 'Platform', row: 2, col: 1, row_span: 2, col_span: 5 }],
});

const CASES: [string, Spec][] = [
  ['slide 11', SLIDE_11],
  ['band, core and three modules', BAND_CORE_MODULES],
  ['unbroken 40-character labels', LONG_TOKENS],
  ['pipeline with a return edge', PIPELINE],
  ['blocked core chord', BLOCKED_CORE],
  ['reciprocal pills', RECIPROCAL_PILLS],
  ['reciprocal cards', RECIPROCAL_CARDS],
  ['narrow diagram, wide legend', NARROW_LEGEND],
  ['hub and spoke', HUB_AND_SPOKE],
  ['curved edge skipping a node', CURVED_SKIP],
  ['frameless nodes in a zone headed by its logo', FRAMELESS],
  ['a zone headed by its logo and a row of chips', ZONE_CHIPS],
  ['pinned sides, a pair on lanes and a zone end', EDGE_SIDES],
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const segments = (points: Point[]): [Point, Point][] => {
  const out: [Point, Point][] = [];
  for (let i = 0; i + 1 < points.length; i += 1) out.push([points[i], points[i + 1]]);
  return out;
};

const overlaps = (a: Rect, b: Rect): boolean =>
  a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;

const finite = (point: Point): boolean => Number.isFinite(point.x) && Number.isFinite(point.y);

const inside = (point: Point, layout: ArchitectureLayout): boolean =>
  point.x >= -0.5 && point.y >= -0.5 && point.x <= layout.width + 0.5 && point.y <= layout.height + 0.5;

const nodeRect = (layout: ArchitectureLayout, id: string): Rect => {
  const node = layout.nodes.find((n) => n.id === id);
  if (!node) throw new Error(`no node ${id}`);
  return { x: node.x, y: node.y, w: node.w, h: node.h };
};

const strip = (layout: ArchitectureLayout): string =>
  JSON.stringify({
    width: layout.width,
    height: layout.height,
    nodes: layout.nodes,
    zones: layout.zones,
    edges: layout.edges,
    legend: layout.legend,
  });

const edgeBetween = (layout: ArchitectureLayout, source: string, target: string): LayoutEdge => {
  const edge = layout.edges.find((e) => e.edge.source === source && e.edge.target === target);
  if (!edge) throw new Error(`no edge ${source} -> ${target}`);
  return edge;
};

const CURVE_SAMPLE_COUNT = 64;

/**
 * The drawn curve itself, sampled from the path data (`M sx sy C c1x c1y, c2x
 * c2y, tx ty`) rather than from `edge.points`: the audit must not trust the
 * router's own polyline approximation of its arc. `[]` for a path with no
 * cubic, so every fixture's edges can be run through it.
 */
const sampleCurve = (d: string): Point[] => {
  const match = /^M ([-\d.]+) ([-\d.]+) C ([-\d.]+) ([-\d.]+), ([-\d.]+) ([-\d.]+), ([-\d.]+) ([-\d.]+)$/.exec(d);
  if (!match) return [];
  const [sx, sy, c1x, c1y, c2x, c2y, tx, ty] = match.slice(1).map(Number);
  const out: Point[] = [];
  for (let i = 0; i <= CURVE_SAMPLE_COUNT; i += 1) {
    const u = i / CURVE_SAMPLE_COUNT;
    const v = 1 - u;
    out.push({
      x: v * v * v * sx + 3 * v * v * u * c1x + 3 * v * u * u * c2x + u * u * u * tx,
      y: v * v * v * sy + 3 * v * v * u * c1y + 3 * v * u * u * c2y + u * u * u * ty,
    });
  }
  return out;
};

const pointInRect = (point: Point, rect: Rect): boolean =>
  point.x > rect.x && point.x < rect.x + rect.w && point.y > rect.y && point.y < rect.y + rect.h;

/** On the rectangle's four sides. */
const onRectBorder = (point: Point, rect: Rect): boolean => {
  const eps = 0.5;
  const withinX = point.x >= rect.x - eps && point.x <= rect.x + rect.w + eps;
  const withinY = point.y >= rect.y - eps && point.y <= rect.y + rect.h + eps;
  const onTopOrBottom = (Math.abs(point.y - rect.y) <= eps || Math.abs(point.y - rect.y - rect.h) <= eps) && withinX;
  const onLeftOrRight = (Math.abs(point.x - rect.x) <= eps || Math.abs(point.x - rect.x - rect.w) <= eps) && withinY;
  return onTopOrBottom || onLeftOrRight;
};

/**
 * On the node's border: a rectangle's four sides, a circle's circumference, a
 * pill's flat runs and its two caps - and for a frameless node the sides of
 * its content box, where its edges meet what is drawn.
 */
const onBorder = (point: Point, node: LayoutNode): boolean => {
  const eps = 0.5;
  if (!node.frame) return onRectBorder(point, node.anchor);
  if (node.kind === 'circle') return Math.abs(Math.hypot(point.x - node.cx, point.y - node.cy) - node.radius) <= eps;
  const withinX = point.x >= node.x - eps && point.x <= node.x + node.w + eps;
  const withinY = point.y >= node.y - eps && point.y <= node.y + node.h + eps;
  const onTopOrBottom = (Math.abs(point.y - node.y) <= eps || Math.abs(point.y - node.y - node.h) <= eps) && withinX;
  if (node.kind === 'pill') {
    const r = node.radius;
    const onCap = [node.x + r, node.x + node.w - r].some((cx) => Math.abs(Math.hypot(point.x - cx, point.y - node.cy) - r) <= eps);
    return onTopOrBottom || onCap;
  }
  const onLeftOrRight = (Math.abs(point.x - node.x) <= eps || Math.abs(point.x - node.x - node.w) <= eps) && withinY;
  return onTopOrBottom || onLeftOrRight;
};

// ---------------------------------------------------------------------------
// Invariants that hold for every case
// ---------------------------------------------------------------------------

for (const [name, spec] of CASES) {
  describe(`architecture router: ${name}`, () => {
    const layout = layoutArchitecture(spec);

    test('routes every edge with finite geometry inside the canvas', () => {
      expect(layout.edges).toHaveLength(spec.edges?.length ?? 0);
      for (const edge of layout.edges) {
        expect(edge.points.length).toBeGreaterThanOrEqual(2);
        for (const point of edge.points) {
          expect(finite(point)).toBe(true);
          expect(inside(point, layout)).toBe(true);
        }
        expect(edge.d).not.toMatch(/NaN|Infinity/);
        // A curved edge is audited on the arc it draws, not on its endpoints:
        // every sample of the cubic must stay on the canvas too.
        for (const point of sampleCurve(edge.d)) {
          expect(finite(point)).toBe(true);
          expect(inside(point, layout), `${edge.key} curve leaves the canvas at ${point.x},${point.y}`).toBe(true);
        }
      }
    });

    test('no edge runs through a node that is not one of its ends', () => {
      for (const edge of layout.edges) {
        for (const node of layout.nodes) {
          if (node.id === edge.edge.source || node.id === edge.edge.target) continue;
          for (const [a, b] of segments(edge.points)) {
            expect(segmentIntersectsRect(a, b, node), `${edge.key} through ${node.id}`).toBe(false);
          }
        }
      }
    });

    test('every path starts on its source node and ends on its target node, or on the border of the zone it names', () => {
      const endsOn = (point: Point, ref: string): boolean => {
        const zone = resolveZoneRef(ref, spec.zones ?? []);
        if (zone !== null) return onRectBorder(point, layout.zones[zone]);
        const node = layout.nodes.find((n) => n.id === ref);
        expect(node, `${ref} is drawn`).toBeTruthy();
        return onBorder(point, node!);
      };
      for (const edge of layout.edges) {
        expect(endsOn(edge.points[0], edge.edge.source), `${edge.key} does not start on ${edge.edge.source}`).toBe(true);
        expect(endsOn(edge.points[edge.points.length - 1], edge.edge.target), `${edge.key} does not end on ${edge.edge.target}`).toBe(true);
      }
    });

    test('no two edges cross', () => {
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

    test('labels stay on canvas and off nodes and each other', () => {
      const labels = layout.edges.flatMap((edge) => (edge.label ? [edge.label] : []));
      for (const label of labels) {
        expect(label.x).toBeGreaterThanOrEqual(0);
        expect(label.y).toBeGreaterThanOrEqual(0);
        expect(label.x + label.w).toBeLessThanOrEqual(layout.width);
        expect(label.y + label.h).toBeLessThanOrEqual(layout.height);
        for (const node of layout.nodes) expect(overlaps(label, node), `label "${label.text.lines[0]?.runs[0]?.text}" on ${node.id}`).toBe(false);
      }
      for (let i = 0; i < labels.length; i += 1) {
        for (let j = i + 1; j < labels.length; j += 1) expect(overlaps(labels[i], labels[j])).toBe(false);
      }
    });

    test('node text stays inside its node', () => {
      for (const node of layout.nodes) {
        const blocks = [
          [node.label, node.labelBox],
          ...(node.sublabel && node.sublabelBox ? [[node.sublabel, node.sublabelBox] as const] : []),
        ] as const;
        for (const [text, box] of blocks) {
          expect(text.width, `${node.id} text wider than its box`).toBeLessThanOrEqual(box.w + 0.5);
          expect(box.x + text.width, `${node.id} text past the node`).toBeLessThanOrEqual(node.w + 0.5);
          expect(box.y + text.height, `${node.id} text below the node`).toBeLessThanOrEqual(node.h + 0.5);
          for (const line of text.lines) expect(line.width, `${node.id} line wider than its box`).toBeLessThanOrEqual(box.w + 0.5);
        }
      }
    });

    test('a zone chip row sits in its zone, off every node, edge and label', () => {
      for (const zone of layout.zones) {
        if (!zone.chipsBox) continue;
        expect(zone.chipsOverflow).toBe(0);
        expect(zone.chipsBox.x).toBeGreaterThanOrEqual(zone.x);
        expect(zone.chipsBox.x + zone.chipsBox.w).toBeLessThanOrEqual(zone.x + zone.w);
        expect(zone.chipsBox.y).toBeGreaterThanOrEqual(zone.y);
        expect(new Set(zone.chips.map((chip) => chip.y)).size).toBe(1);
        for (const node of layout.nodes) expect(overlaps(zone.chipsBox, node), `chips on ${node.id}`).toBe(false);
        for (const edge of layout.edges) {
          for (const [a, b] of segments(edge.points)) expect(segmentIntersectsRect(a, b, zone.chipsBox), `${edge.key} through the chips`).toBe(false);
          if (edge.label) expect(overlaps(edge.label, zone.chipsBox), `${edge.key} label on the chips`).toBe(false);
        }
      }
    });

    test('the legend sits inside the drawing', () => {
      for (const item of layout.legend) {
        expect(item.x).toBeGreaterThanOrEqual(ARCH.PAD);
        expect(item.textX + item.text.width).toBeLessThanOrEqual(layout.width - ARCH.PAD + 0.5);
        expect(item.y + ARCH.LEGEND_H).toBeLessThanOrEqual(layout.height);
      }
    });

    test('is deterministic', () => {
      expect(strip(layoutArchitecture(spec))).toBe(strip(layout));
    });
  });
}

// ---------------------------------------------------------------------------
// Case-specific guarantees
// ---------------------------------------------------------------------------

describe('architecture router: specifics', () => {
  test('an auto core tie is a straight chord when clear and elbows when a node is in the way', () => {
    const layout = layoutArchitecture(BLOCKED_CORE);
    expect(edgeBetween(layout, 'core', 'middle').points).toHaveLength(2);
    const blocked = edgeBetween(layout, 'core', 'edge');
    expect(blocked.points.length).toBeGreaterThan(2);
    for (const [a, b] of segments(blocked.points)) expect(segmentIntersectsRect(a, b, nodeRect(layout, 'middle'))).toBe(false);
  });

  test('an explicit straight route is drawn as written, even through a node', () => {
    const layout = layoutArchitecture({
      ...BLOCKED_CORE,
      edges: [{ source: 'core', target: 'edge', arrows: 'none', route: 'straight' }],
    });
    expect(layout.edges[0].points).toHaveLength(2);
  });

  test('an exchange between two side-by-side pills runs as two separate lanes', () => {
    const layout = layoutArchitecture(RECIPROCAL_PILLS);
    const forth = edgeBetween(layout, 'analyst', 'platform');
    const back = edgeBetween(layout, 'platform', 'analyst');
    const gap = Math.abs(forth.points[0].y - back.points[back.points.length - 1].y);
    expect(gap).toBeGreaterThanOrEqual(ARCH.LANE - 0.5);
    expect(new Set(layout.edges.flatMap((edge) => edge.points.map((point) => Math.round(point.y)))).size).toBeGreaterThan(1);
  });

  test('an exchange between two stacked cards runs as two separate lanes', () => {
    const layout = layoutArchitecture(RECIPROCAL_CARDS);
    const down = edgeBetween(layout, 'sensor', 'siem');
    const up = edgeBetween(layout, 'siem', 'sensor');
    expect(Math.abs(down.points[0].x - up.points[up.points.length - 1].x)).toBeGreaterThanOrEqual(ARCH.LANE - 0.5);
  });

  test('a legend wider than the drawing wraps into rows and grows the height', () => {
    const wrapped = layoutArchitecture(NARROW_LEGEND);
    const single = layoutArchitecture({ ...NARROW_LEGEND, legend: NARROW_LEGEND.legend?.slice(0, 1) });
    expect(new Set(wrapped.legend.map((item) => item.y)).size).toBeGreaterThan(1);
    expect(wrapped.height).toBeGreaterThan(single.height);
  });

  test('a return edge along a chain detours instead of running through the chain', () => {
    const layout = layoutArchitecture(PIPELINE);
    const back = edgeBetween(layout, 'serve', 'collect');
    expect(back.points.length).toBeGreaterThan(2);
    const rowTop = Math.min(...layout.nodes.map((node) => node.y));
    const rowBottom = Math.max(...layout.nodes.map((node) => node.y + node.h));
    const corridor = back.points.slice(1, -1);
    expect(corridor.every((point) => point.y < rowTop || point.y > rowBottom)).toBe(true);
  });

  test('straight ties out of a core land on the centre of the facing side of each card', () => {
    const layout = layoutArchitecture(BAND_CORE_MODULES);
    const core = layout.nodes.find((node) => node.id === 'core')!;
    for (const id of ['a', 'b', 'c']) {
      const card = nodeRect(layout, id);
      const end = edgeBetween(layout, 'core', id).points[1];
      expect(end.x).toBeCloseTo(card.x + card.w / 2, 5);
      expect(end.y).toBeCloseTo(card.y, 5);
    }
    // The fan is symmetric about the core.
    const left = edgeBetween(layout, 'core', 'a').points[0];
    const right = edgeBetween(layout, 'core', 'c').points[0];
    expect(left.x - core.cx).toBeCloseTo(-(right.x - core.cx), 5);
    expect(left.y).toBeCloseTo(right.y, 5);
  });

  test('a diagonal pair boxed in by its neighbours is routed through the gap between the columns', () => {
    // Module B under the core, the external card up to its right past Module
    // C: both plain elbows run through a node, the column gap does not.
    const layout = layoutArchitecture(
      block({
        nodes: [
          { id: 'core', label: 'Core', shape: 'circle', emphasis: 'core', row: 1, col: 1 },
          { id: 'ext', label: 'External', row: 1, col: 3 },
          { id: 'b', label: 'Module B', row: 2, col: 1 },
          { id: 'c', label: 'Module C', row: 2, col: 2 },
        ],
        edges: [{ source: 'b', target: 'ext', route: 'orthogonal' }],
        zones: [],
        legend: [],
      }),
    );
    const edge = edgeBetween(layout, 'b', 'ext');
    const source = nodeRect(layout, 'b');
    const beside = nodeRect(layout, 'c');
    expect(edge.points[0].x).toBeCloseTo(source.x + source.w, 5);
    expect(edge.points).toHaveLength(4);
    expect(edge.points[1].x).toBeGreaterThan(source.x + source.w);
    expect(edge.points[1].x).toBeLessThan(beside.x);
    for (const id of ['core', 'c']) {
      for (const [a, b] of segments(edge.points)) expect(segmentIntersectsRect(a, b, nodeRect(layout, id))).toBe(false);
    }
  });

  test('a curved edge skipping a node follows the detour corridor and stays on the canvas', () => {
    const layout = layoutArchitecture(CURVED_SKIP);
    const skip = edgeBetween(layout, 'a', 'c');
    const samples = sampleCurve(skip.d);
    expect(samples).toHaveLength(CURVE_SAMPLE_COUNT + 1);
    const middle = nodeRect(layout, 'b');
    const rowTop = Math.min(...layout.nodes.map((node) => node.y));
    const rowBottom = Math.max(...layout.nodes.map((node) => node.y + node.h));
    // The control points must not sit off the canvas and take the arc
    // with them; the whole drawn cubic stays inside the drawing.
    for (const point of samples) {
      expect(inside(point, layout), `curve at ${point.x},${point.y} leaves the canvas`).toBe(true);
      expect(pointInRect(point, middle), `curve at ${point.x},${point.y} runs through the skipped node`).toBe(false);
    }
    // The arc bulges toward the scored corridor (above or below the row), so
    // its midpoint clears the row like the elbow's corridor segment would.
    const apex = samples[CURVE_SAMPLE_COUNT / 2];
    expect(apex.y < rowTop || apex.y > rowBottom).toBe(true);
    // The recorded extent is the arc, not the chord: the audits above and
    // `drawnBottom` read the true geometry.
    expect(skip.points.length).toBeGreaterThan(2);
    const recordedApex = skip.points[Math.floor(skip.points.length / 2)];
    expect(Math.abs(recordedApex.y - apex.y)).toBeLessThan(1);
    // The straight neighbours are untouched by the curved skip.
    expect(edgeBetween(layout, 'a', 'b').points).toHaveLength(2);
    expect(edgeBetween(layout, 'b', 'c').points).toHaveLength(2);
  });
});
