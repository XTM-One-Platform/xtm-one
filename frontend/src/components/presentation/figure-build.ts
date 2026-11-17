/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * A deck figure built in, frame by frame, for a video scene.
 *
 * The render's headless browser draws ONE block of a deck on the figure page
 * (`VideoFigureStaticRender`), then asks for every frame of the scene with
 * `seek(t)`: nothing here runs on a clock, so a frame is a pure function of
 * `t` and the render can draw them in any order, as often as it likes.
 *
 * What the steps of a figure are is read from its block's own markup
 * (`FIGURE_PARTS`): a process flow's steps, a timeline's items, a roadmap's
 * phases, a chart's series, an architecture's zones and nodes, a diagram's
 * clusters and nodes; any other block builds in its children one after the
 * other. A figure laid out on a plane is built the way it is read: its
 * zones first, the frame the rest stands in, then each node on its own, row
 * by row, and an edge comes in with the later of the two nodes nearest its
 * ends, drawing itself from one to the other. An org chart is built a level
 * at a time (`levels`): the root, then each level - its cards, then its
 * bubbles growing to their size - each bus with the level it reaches, the
 * dotted lines on a last step of their own, every number counting up. Every piece is a paused Web
 * Animation whose time `seek` sets - it fades in and rises a few pixels; the
 * camera moves to each step as it comes in and ends on the whole figure, as a
 * transform of the slide canvas, kept clear of the subtitles' band.
 *
 * Built, a figure keeps moving (`motion: "drift"`): the whole figure
 * drifts in slowly to the end of the scene, a light pulse runs along each
 * edge from its source to its target, and the core node's glow breathes -
 * looping animations, seeked like the rest.
 */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** What the render reads of a figure before it plans the scene: its steps, in order. */
interface FigureDescription {
  /** `labels`: the words each step shows, one entry per line of text; `settle`: seconds until it is all in. */
  steps: { labels: string[]; box: Rect; settle: number }[];
  figure: Rect;
}

/** Viewport pixels the figure keeps clear of, at the top and the bottom of the frame. */
export interface SafeArea {
  top: number;
  bottom: number;
}

/** When each step comes in (seconds into the scene, one per step) and how the camera moves. */
export interface FigurePlan {
  times: number[];
  length: number;
  camera: "follow" | "still";
  /** Seconds the scene ends on the whole figure; `WHOLE_HOLD` when unset. */
  hold?: number;
  /** The subtitles' band, kept clear by every framing. */
  safe?: SafeArea;
  /** `drift` (the default): the figure keeps moving once built; `none`: it holds still. */
  motion?: "drift" | "none";
  /**
   * How far it drifts in by the scene's end (0.06: from 1.00 to 1.06); `DRIFT_ZOOM` when unset. The render caps it
   * so the figure, drifted and stood in space, stays inside the frame (`figure_fit.fit`).
   */
  drift?: number;
}

/** What a plan installed: the spans where something moves, and when every step is in with the whole figure framed. */
interface PlannedFigure {
  active: [number, number][];
  whole: number;
}

export interface FigureBuild {
  describe(): FigureDescription;
  /** Installs the animations; answers the spans of the scene where something moves. */
  plan(plan: FigurePlan): PlannedFigure;
  seek(t: number): void;
}

/** Which elements of a block are its pieces: the block markup's own classes. */
interface FigureParts {
  /** Panels that hold nodes (a zone, a cluster): the first step, together, before any node. */
  groups?: string;
  nodes: string;
  /** Lines between nodes: drawn with the later of their two nodes. */
  edges?: string;
  /** Labels and marks: with their series (`data-series`), else on a plane with the nearest edge, else the nearest piece. */
  extras?: string;
  /** What comes in with the last step (a legend). */
  last?: string;
  /** The nodes stand on a plane: one step each, in reading order, not in the order of the markup. */
  plane?: boolean;
  /** What a chart draws under its series (axes, grid): there from the first step. */
  frame?: string;
  /** The steps stand on one another (a chart's series share its axes): the camera keeps the whole figure. */
  whole?: boolean;
  /** The node the figure is built around, when its markup says so; else the one most edges meet, if any stands out. */
  core?: string;
  /** The halo a core node draws around itself: what breathes, instead of a shadow cast by the node. */
  glow?: string;
  /** Nodes drawn without a frame: nothing around them breathes, whatever their emphasis. */
  unframed?: string;
  /** The nodes are built a LEVEL at a time: the attribute carrying each node's (and each line's) level, the root's first. */
  levels?: string;
  /** Lines drawn after every level is in, on a step of their own (an org chart's dotted lines). */
  late?: string;
  /** Pieces that grow to their size from their centre, after the other pieces of their step (a team's bubble). */
  grow?: string;
}

export const FIGURE_PARTS: Record<string, FigureParts> = {
  process_flow: { nodes: ".deck-flow-step" },
  timeline: { nodes: ".deck-timeline-item" },
  roadmap: { nodes: ".deck-roadmap > *" },
  architecture: {
    groups: ".deck-arch-zone",
    nodes: ".deck-arch-node",
    edges: ".deck-arch-edge",
    last: ".deck-arch-legend",
    plane: true,
    core: '.deck-arch-node[data-emphasis="core"]',
    glow: ".deck-arch-node-glow",
    unframed: '.deck-arch-node[data-frame="false"]',
  },
  // An org chart is built level by level: the root, then each level - its
  // cards, then its bubbles growing to their size - each bus drawn with the
  // level it reaches, the dotted lines last; its numbers count up.
  org_chart: {
    groups: ".deck-org-zone",
    nodes: ".deck-org-node",
    edges: ".deck-org-link:not([data-dotted])",
    late: ".deck-org-link[data-dotted]",
    last: ".deck-org-legend",
    levels: "data-level",
    grow: '.deck-org-node[data-shape="circle"]',
    core: '.deck-org-node[data-emphasis="core"]',
    glow: ".deck-org-glow",
  },
  // The diagram block draws Mermaid's markup.
  diagram: {
    groups: "g.cluster",
    nodes: "g.node",
    edges: "g.edgePaths > path, path.flowchart-link",
    extras: "g.edgeLabels > *",
    plane: true,
  },
  // The chart block draws Recharts' markup: one series group per series, a pie's sectors one by one.
  chart: {
    nodes: ".recharts-bar, .recharts-line, .recharts-area, .recharts-scatter, .recharts-pie-sector",
    extras: ".deck-chart-value, .deck-chart-leader",
    frame: ".recharts-cartesian-axis, .recharts-cartesian-grid, .recharts-polar-grid, .recharts-polar-angle-axis",
    whole: true,
  },
};

/** Seconds a piece takes to come in (fading in as it rises `RISE_PX`), an edge to draw, and between two pieces of one step. */
export const PIECE_SECONDS = 0.6;
const RISE_PX = 8;
export const EDGE_SECONDS = 0.7;
export const EDGE_DELAY = 0.25;
export const STAGGER_SECONDS = 0.08;
const MAX_STAGGER = 0.8;
/** A chart's series: its bars grow, then its marks fade in half a piece later. */
const SERIES_SECONDS = PIECE_SECONDS * 1.5;
/** The camera's move to a step, how early it leaves, and how long the scene ends on the whole figure. */
export const CAMERA_MOVE = 0.6;
const CAMERA_LEAD = 0.2;
export const WHOLE_HOLD = 2;
/** Two steps sharing more than this of the larger one's area stand on one another: nothing to move between. */
const STACKED_STEPS = 0.5;
/** The margin around what the camera frames, and how far it zooms past the whole figure. */
const FRAME_MARGIN = 0.07;
export const MAX_ZOOM = 1.8;
const EASING = "cubic-bezier(0.22, 0.61, 0.36, 1)";
/** Once built, the whole figure drifts in this much by the end of the scene (1.00 to 1.06). */
export const DRIFT_ZOOM = 0.06;
/** A pulse runs along every edge once it is drawn: one each `FLOW_PERIOD` seconds, `FLOW_SECONDS` from end to end. */
const FLOW_PERIOD = 2.4;
const FLOW_SECONDS = 1.2;
/** How long a pulse is: a share of its edge, within bounds (the edge's own units). */
const FLOW_SHARE = 0.18;
const FLOW_MIN = 36;
const FLOW_MAX = 150;
/** The pause between an edge drawn and its first pulse. */
const FLOW_AFTER = 0.3;
/** One breath of the core node's glow, out and back. */
const BREATH_SECONDS = 3.2;
/** A node the figure is built around without its markup saying so: met by at least this many edges, more than any other. */
const HUB_EDGES = 3;
/** Seconds a number counts up from 0 to its value, from when its piece comes in. */
export const COUNT_SECONDS = 1.1;

// ── Geometry (pure) ────────────────────────────────────────────────────────

export function union(rects: Rect[]): Rect | null {
  const live = rects.filter((r) => r.w > 0 || r.h > 0);
  if (!live.length) return null;
  const x0 = Math.min(...live.map((r) => r.x));
  const y0 = Math.min(...live.map((r) => r.y));
  const x1 = Math.max(...live.map((r) => r.x + r.w));
  const y1 = Math.max(...live.map((r) => r.y + r.h));
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

const center = (r: Rect) => ({ x: r.x + r.w / 2, y: r.y + r.h / 2 });

/** Indexes of *rects* in reading order, grouped row by row (top to bottom, each row left to right). */
export function rows(rects: Rect[]): number[][] {
  if (!rects.length) return [];
  const heights = rects.map((r) => r.h).sort((a, b) => a - b);
  const gap = Math.max(8, heights[Math.floor(heights.length / 2)] * 0.5);
  const order = rects.map((_r, i) => i).sort((a, b) => center(rects[a]).y - center(rects[b]).y);
  const groups: number[][] = [];
  let rowY = Number.NEGATIVE_INFINITY;
  for (const index of order) {
    const y = center(rects[index]).y;
    if (y - rowY > gap) {
      groups.push([]);
      rowY = y;
    }
    groups[groups.length - 1].push(index);
  }
  return groups.map((group) => group.sort((a, b) => center(rects[a]).x - center(rects[b]).x));
}

/** The rect the camera frames for one step: at least half the figure each way, kept inside it. */
export function stepFrame(step: Rect, figure: Rect): Rect {
  const w = Math.min(figure.w, Math.max(step.w, figure.w / MAX_ZOOM));
  const h = Math.min(figure.h, Math.max(step.h, figure.h / MAX_ZOOM));
  const c = center(step);
  const x = Math.min(Math.max(c.x - w / 2, figure.x), figure.x + figure.w - w);
  const y = Math.min(Math.max(c.y - h / 2, figure.y), figure.y + figure.h - h);
  return { x, y, w, h };
}

/** How much of the larger of two rects they share (0-1): a node inside its zone shares little of it. */
function shared(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? (w * h) / Math.max(1, Math.max(a.w * a.h, b.w * b.h)) : 0;
}

/**
 * ``(seconds, rect)`` the camera passes by: each step framed as it comes in -
 * steps arriving within one move framed together - then the whole figure,
 * reached `hold` seconds before the scene ends. A step arriving too late to be
 * framed before that is seen as the camera pulls back.
 */
export function cameraKeyframes(steps: Rect[], figure: Rect, plan: FigurePlan, whole = false): [number, Rect][] {
  if (whole || plan.camera === "still" || steps.length < 2) return [[0, figure]];
  if (steps.some((step, i) => i > 0 && shared(step, steps[i - 1]) > STACKED_STEPS)) return [[0, figure]];
  const back = plan.length - (plan.hold ?? WHOLE_HOLD) - CAMERA_MOVE;
  if (back < CAMERA_MOVE) return [[0, figure]];
  const beats: { at: number; rect: Rect }[] = [];
  steps.forEach((step, i) => {
    const at = plan.times[Math.min(i, plan.times.length - 1)] ?? 0;
    const previous = beats[beats.length - 1];
    if (previous && at - previous.at < CAMERA_MOVE) previous.rect = union([previous.rect, step]) ?? previous.rect;
    else beats.push({ at, rect: step });
  });
  let frame = stepFrame(beats[0].rect, figure);
  const keys: [number, Rect][] = [[0, frame]];
  for (const beat of beats.slice(1)) {
    const leave = Math.max(keys[keys.length - 1][0], beat.at - CAMERA_LEAD);
    if (leave + CAMERA_MOVE > back) break;
    const next = stepFrame(beat.rect, figure);
    keys.push([leave, frame], [leave + CAMERA_MOVE, next]);
    frame = next;
  }
  const start = Math.max(keys[keys.length - 1][0], back);
  keys.push([start, frame], [start + CAMERA_MOVE, figure]);
  return keys;
}

const smooth = (u: number) => u * u * (3 - 2 * u);

export function cameraAt(keys: [number, Rect][], t: number): Rect {
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    const [t1, r1] = keys[i];
    if (t <= t1) {
      const [t0, r0] = keys[i - 1];
      const u = t1 > t0 ? smooth((t - t0) / (t1 - t0)) : 1;
      return { x: r0.x + (r1.x - r0.x) * u, y: r0.y + (r1.y - r0.y) * u, w: r0.w + (r1.w - r0.w) * u, h: r0.h + (r1.h - r0.h) * u };
    }
  }
  return keys[keys.length - 1][1];
}

const NO_SAFE_AREA: SafeArea = { top: 0, bottom: 0 };

/** The transform that puts *rect* (canvas pixels) in the middle of the viewport left by *safe*, a margin around it. */
export function framing(
  rect: Rect,
  viewport: { w: number; h: number },
  safe: SafeArea = NO_SAFE_AREA,
): { scale: number; x: number; y: number } {
  const room = Math.max(1, viewport.h - safe.top - safe.bottom);
  const scale = Math.min(
    (viewport.w * (1 - 2 * FRAME_MARGIN)) / Math.max(rect.w, 1),
    (room * (1 - 2 * FRAME_MARGIN)) / Math.max(rect.h, 1),
  );
  const c = center(rect);
  return { scale, x: viewport.w / 2 - c.x * scale, y: safe.top + room / 2 - c.y * scale };
}

/** Seconds from a step's time until all of it is in: its pieces (staggered) and its edges, drawn. */
export function settleSeconds(pieces: number, edges: number, pieceSeconds = PIECE_SECONDS): number {
  const shown = pieces > 0 ? Math.min(MAX_STAGGER, STAGGER_SECONDS * (pieces - 1)) + pieceSeconds : 0;
  return Math.max(shown, edges > 0 ? EDGE_DELAY + EDGE_SECONDS : 0);
}

/** When the last step has settled: the figure is built. */
export function builtAt(times: number[], settles: number[]): number {
  return times.reduce((last, t, i) => Math.max(last, t + (settles[i] ?? PIECE_SECONDS)), 0);
}

/**
 * When the figure starts drifting: once the camera is back on the whole figure
 * after following the steps, else (a still camera) once the figure is built.
 */
export function driftFrom(keys: [number, Rect][], built: number, length: number): number {
  return Math.min(length, keys.length > 1 ? keys[keys.length - 1][0] : built);
}

/** The drift's zoom at *t*: 1 until *from*, eased up to `1 + amount` at *to*. */
export function driftAt(t: number, from: number, to: number, amount = DRIFT_ZOOM): number {
  if (to - from <= 1e-6 || t <= from) return 1;
  return 1 + amount * smooth(Math.min(1, (t - from) / (to - from)));
}

/** *view* zoomed *zoom* times about the middle of the room *safe* leaves: what the framing centres stays centred. */
export function drifted(
  view: { scale: number; x: number; y: number },
  viewport: { w: number; h: number },
  safe: SafeArea,
  zoom: number,
): { scale: number; x: number; y: number } {
  if (zoom === 1) return view;
  const cx = viewport.w / 2;
  const cy = safe.top + Math.max(1, viewport.h - safe.top - safe.bottom) / 2;
  return { scale: view.scale * zoom, x: cx - (cx - view.x) * zoom, y: cy - (cy - view.y) * zoom };
}

/** The node most edges meet, when at least `HUB_EDGES` do and no other node is met by as many; else -1. */
export function hubNode(ends: [number, number][], nodes: number): number {
  const degree = new Array(nodes).fill(0);
  for (const [a, b] of ends) {
    if (a >= 0 && a < nodes) degree[a] += 1;
    if (b >= 0 && b < nodes && b !== a) degree[b] += 1;
  }
  const top = Math.max(0, ...degree);
  const hubs = degree.flatMap((d, i) => (d === top ? [i] : []));
  return top >= HUB_EDGES && hubs.length === 1 ? hubs[0] : -1;
}

/** Indexes of *levels* grouped level by level, the lowest level first (an unreadable level last), each group in markup order. */
export function levelGroups(levels: number[]): number[][] {
  const known = Array.from(new Set(levels.filter((level) => Number.isFinite(level)))).sort((a, b) => a - b);
  const groups = known.map((level) => levels.flatMap((l, i) => (l === level ? [i] : [])));
  const unknown = levels.flatMap((l, i) => (Number.isFinite(l) ? [] : [i]));
  return unknown.length ? [...groups, unknown] : groups;
}

/** The number a count shows at *t*: 0 until *start*, eased up to *value* over `COUNT_SECONDS`. */
export function countAt(value: number, start: number, t: number): number {
  if (t <= start) return 0;
  const u = Math.min(1, (t - start) / COUNT_SECONDS);
  return Math.round(value * (1 - (1 - u) ** 3));
}

/** Where something moves: each step until it has settled, the camera between its keyframes, and *extra* spans. */
export function activeSpans(
  times: number[],
  settles: number[],
  keys: [number, Rect][],
  extra: [number, number][] = [],
): [number, number][] {
  const spans: [number, number][] = times.map((t, i) => [t, t + (settles[i] ?? PIECE_SECONDS) + 0.05]);
  for (let i = 1; i < keys.length; i++) {
    if (keys[i][1] !== keys[i - 1][1]) spans.push([keys[i - 1][0], keys[i][0]]);
  }
  spans.push(...extra.map(([a, b]): [number, number] => [a, b]));
  spans.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [a, b] of spans) {
    const previous = merged[merged.length - 1];
    if (previous && a <= previous[1]) previous[1] = Math.max(previous[1], b);
    else merged.push([a, b]);
  }
  return merged;
}

// ── The figure on the page ─────────────────────────────────────────────────

interface Step {
  pieces: Element[];
  edges: Element[];
  /** The pieces whose words name the step (its nodes, its zones): what the narration is matched on. */
  named: Element[];
}

const stepOf = (piece: Element): Step => ({ pieces: [piece], edges: [], named: [piece] });

function box(el: Element, origin: DOMRect): Rect {
  const r = el.getBoundingClientRect();
  return { x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height };
}

/** Text a piece shows but never draws as words: an SVG node's description, its tooltip. */
const UNSHOWN = "desc, title, style, script";

/** The words of *el*, its text pieces joined by spaces: an SVG label's lines are separate `<tspan>`s. */
function wordsOf(el: Element): string {
  const parts: string[] = [];
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let node = walk.nextNode(); node; node = walk.nextNode()) {
    if (!node.parentElement?.closest(UNSHOWN)) parts.push(node.textContent ?? "");
  }
  return parts.join(" ").replace(/\s+/g, " ").trim();
}

/** The lines of text a piece shows, one entry each: a node's label, its sublabel, its chips. */
function texts(el: Element): string[] {
  const holders = new Set<Element>();
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let node = walk.nextNode(); node; node = walk.nextNode()) {
    const parent = node.parentElement;
    if (!parent || parent.closest(UNSHOWN) || !(node.textContent ?? "").trim()) continue;
    holders.add(parent.closest("text") ?? parent);
  }
  return [...new Set(Array.from(holders, wordsOf))].filter(Boolean);
}

function all(root: Element, selector: string | undefined): Element[] {
  return selector ? Array.from(root.querySelectorAll(selector)) : [];
}

/** Points along a line element, in the canvas's pixels, at shares of its length (0 its start, 1 its end). */
function along(edge: Element, origin: DOMRect, shares: number[]): { x: number; y: number }[] | null {
  const path = (edge instanceof SVGGeometryElement ? edge : edge.querySelector("path")) as SVGGeometryElement | null;
  const matrix = path?.getScreenCTM();
  if (!path || !matrix) return null;
  const length = path.getTotalLength();
  return shares.map((share) => {
    const p = path.getPointAtLength(length * share);
    return { x: matrix.a * p.x + matrix.c * p.y + matrix.e - origin.left, y: matrix.b * p.x + matrix.d * p.y + matrix.f - origin.top };
  });
}

function nearestPoint(point: { x: number; y: number }, points: ({ x: number; y: number } | null)[]): number {
  let best = -1;
  let bestDistance = Number.POSITIVE_INFINITY;
  points.forEach((p, i) => {
    const d = p ? Math.hypot(p.x - point.x, p.y - point.y) : Number.POSITIVE_INFINITY;
    if (d < bestDistance) [best, bestDistance] = [i, d];
  });
  return best;
}

function nearest(point: { x: number; y: number }, rects: Rect[]): number {
  let best = -1;
  let bestDistance = Number.POSITIVE_INFINITY;
  rects.forEach((r, i) => {
    const dx = Math.max(r.x - point.x, 0, point.x - (r.x + r.w));
    const dy = Math.max(r.y - point.y, 0, point.y - (r.y + r.h));
    const distance = dx * dx + dy * dy;
    if (distance < bestDistance) [best, bestDistance] = [i, distance];
  });
  return best;
}

interface Collected {
  steps: Step[];
  nodes: Element[];
  /** Each edge's two nodes (indexes into `nodes`, -1 for an end near none). */
  ends: [number, number][];
}

/** The steps of the figure at *root*, in the order they come in, its nodes and what each edge joins. */
function collectSteps(root: Element, kind: string, origin: DOMRect): Collected {
  const parts = FIGURE_PARTS[kind];
  const loose = (): Collected => ({ steps: Array.from(root.children).map(stepOf), nodes: [], ends: [] });
  if (!parts) return loose();
  const nodes = all(root, parts.nodes);
  const nodeRects = nodes.map((node) => box(node, origin));
  const steps: Step[] = [];
  const stepOfNode: number[] = new Array(nodes.length).fill(0);
  if (parts.levels) {
    // A level at a time: the zones with the first level under the root, each
    // line with the level it reaches, the late lines (and the legend) on a
    // step of their own after every level is in.
    const attr = parts.levels;
    const levelOf = (el: Element) => Number.parseFloat(el.getAttribute(attr) ?? "");
    const groups = levelGroups(nodes.map(levelOf));
    groups.forEach((group, s) => {
      const pieces = group.map((i) => nodes[i]);
      for (const i of group) stepOfNode[i] = s;
      steps.push({ pieces, edges: [], named: pieces });
    });
    if (!steps.length) return loose();
    const levels = groups.map((group) => levelOf(nodes[group[0]]));
    const zones = all(root, parts.groups);
    if (zones.length) steps[Math.min(1, steps.length - 1)].pieces.unshift(...zones);
    for (const edge of all(root, parts.edges)) {
      const at = levels.indexOf(levelOf(edge));
      steps[at >= 0 ? at : steps.length - 1].edges.push(edge);
    }
    const late = all(root, parts.late);
    if (late.length) steps.push({ pieces: [], edges: late, named: [] });
    for (const extra of all(root, parts.last)) steps[steps.length - 1].pieces.push(extra);
    return { steps, nodes, ends: [] };
  }
  if (!parts.plane) {
    nodes.forEach((node, i) => {
      stepOfNode[i] = i;
      steps.push(stepOf(node));
    });
  } else {
    // Read as a plane is read: the zones that frame it, then each node, row by row.
    const groups = all(root, parts.groups);
    if (nodes.length && groups.length) steps.push({ pieces: groups, edges: [], named: groups });
    for (const i of rows(nodeRects).flat()) {
      stepOfNode[i] = steps.length;
      steps.push(stepOf(nodes[i]));
    }
  }
  if (!steps.length) return loose();
  const edges = all(root, parts.edges);
  const ends = edges.map((edge): [number, number] => {
    const line = along(edge, origin, [0, 1]);
    return line ? [nearest(line[0], nodeRects), nearest(line[1], nodeRects)] : [-1, -1];
  });
  const edgeSteps = ends.map(([a, b]) =>
    a < 0 && b < 0 ? steps.length - 1 : Math.max(stepOfNode[a] ?? 0, stepOfNode[b] ?? 0),
  );
  edges.forEach((edge, i) => steps[edgeSteps[i]].edges.push(edge));
  const middles = edges.map((edge) => along(edge, origin, [0.5])?.[0] ?? null);
  const pieceRects = steps.map((step) => step.pieces.map((piece) => box(piece, origin)));
  for (const extra of all(root, parts.extras)) {
    const series = extra.getAttribute("data-series");
    if (series !== null && !parts.plane && Number.isFinite(Number(series))) {
      steps[Math.min(steps.length - 1, Math.max(0, Number(series)))].pieces.push(extra);
      continue;
    }
    const c = center(box(extra, origin));
    // A plane's labels name its edges: each comes in as the edge it sits on is drawn.
    const nearestEdge = nearestPoint(c, middles);
    if (parts.plane && nearestEdge >= 0) {
      steps[edgeSteps[nearestEdge]].edges.push(extra);
      continue;
    }
    const flat = pieceRects.flatMap((rects, si) => rects.map((r) => ({ r, si })));
    const hit = nearest(c, flat.map((entry) => entry.r));
    steps[hit < 0 ? steps.length - 1 : flat[hit].si].pieces.push(extra);
  }
  for (const extra of all(root, parts.last)) steps[steps.length - 1].pieces.push(extra);
  for (const extra of all(root, parts.frame)) steps[0].pieces.unshift(extra);
  return { steps, nodes, ends };
}

/** Whether *el* paints: shown, not see-through, not a definition another element draws from. */
function painted(el: Element): boolean {
  if (el.closest("defs, clipPath, mask, marker, symbol")) return false;
  const style = getComputedStyle(el);
  return style.display !== "none" && style.visibility !== "hidden" && Number.parseFloat(style.opacity || "1") > 0;
}

/** *rect* cut to *by*; empty when they do not meet. */
export function within(rect: Rect, by: Rect): Rect {
  const x = Math.max(rect.x, by.x);
  const y = Math.max(rect.y, by.y);
  return { x, y, w: Math.max(0, Math.min(rect.x + rect.w, by.x + by.w) - x), h: Math.max(0, Math.min(rect.y + rect.h, by.y + by.h) - y) };
}

/** What *el* paints of its box: cut by every ancestor up to *root* that clips what overflows it (an SVG's viewport). */
function paintedBox(el: Element, root: Element, origin: DOMRect): Rect {
  let rect = box(el, origin);
  for (let up = el.parentElement; up && up !== root.parentElement; up = up.parentElement) {
    const style = getComputedStyle(up);
    if (style.overflowX !== "visible" || style.overflowY !== "visible") rect = within(rect, box(up, origin));
  }
  return rect;
}

/**
 * What the figure draws, not the slot it sits in: an SVG is as large as its slot, its drawing often smaller. Every
 * element it paints counts - a step's own box leaves out a label drawn past it, which the frame would then
 * cut once the figure drifts in - as far as what holds it lets it show.
 */
function extent(root: Element, steps: Step[], origin: DOMRect): Rect | null {
  const pieces = steps.flatMap((step) => [...step.pieces, ...step.edges]);
  // Drawn marks outside every step (a chart's legend, a timeline's track) are part of the picture too.
  const leaves = Array.from(root.querySelectorAll("*")).filter((el) => el.childElementCount === 0 && painted(el));
  return union([...pieces.map((el) => box(el, origin)), ...leaves.map((el) => paintedBox(el, root, origin))]);
}

/**
 * A piece fading in as it rises into place. `translate` composes with the
 * piece's own `transform` (an SVG node's `translate(x y)` attribute
 * included), which a `transform` keyframe would replace.
 */
function comeIn(el: Element, delay: number): Animation {
  return el.animate(
    [
      { opacity: 0, translate: `0 ${RISE_PX}px` },
      { opacity: 1, translate: "0 0" },
    ],
    { duration: PIECE_SECONDS * 1000, delay: delay * 1000, fill: "both", easing: EASING },
  );
}

/** The lines an edge draws: the edge itself when it is one, else the paths it holds. */
function linesOf(edge: Element): SVGGeometryElement[] {
  return (edge instanceof SVGGeometryElement ? [edge] : Array.from(edge.querySelectorAll("path"))) as SVGGeometryElement[];
}

/** A line drawing itself from one end to the other; a dashed one, whose dashes are its look, fades in instead. */
function draw(edge: Element, delay: number): Animation[] {
  const out: Animation[] = [];
  for (const path of linesOf(edge)) {
    const dashed = getComputedStyle(path).strokeDasharray !== "none";
    const length = path.getTotalLength();
    if (dashed || !length) {
      out.push(path.animate([{ opacity: 0 }, { opacity: 1 }], { duration: EDGE_SECONDS * 1000, delay: delay * 1000, fill: "both" }));
      continue;
    }
    path.style.strokeDasharray = `${length} ${length}`;
    out.push(
      path.animate([{ strokeDashoffset: length }, { strokeDashoffset: 0 }], {
        duration: EDGE_SECONDS * 1000,
        delay: delay * 1000,
        fill: "both",
        easing: EASING,
      }),
      // A dash hides the stroke, not the path's arrowheads (markers): the path stays hidden until its line starts.
      path.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 60, delay: delay * 1000, fill: "both" }),
    );
  }
  // The arrowhead and the label come in once the line reaches them.
  const rest = edge instanceof SVGGeometryElement ? [] : Array.from(edge.children).filter((child) => child.tagName !== "path");
  for (const el of rest) out.push(el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 250, delay: (delay + EDGE_SECONDS * 0.8) * 1000, fill: "both" }));
  if (!rest.length && !(edge instanceof SVGGeometryElement)) out.push(edge.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 120, delay: delay * 1000, fill: "both" }));
  return out;
}

/**
 * A light pulse running along each line of *edge*, from its source to its
 * target, once every `FLOW_PERIOD` seconds from *delay* on. The pulse is a copy
 * of the line laid over it - its own dash, thicker, glowing, without its
 * arrowheads - so the line keeps its look; the copies are what *added* gets.
 */
function flow(edge: Element, delay: number, added: Element[]): Animation[] {
  const out: Animation[] = [];
  for (const line of linesOf(edge)) {
    const length = line.getTotalLength();
    if (!length) continue;
    const style = getComputedStyle(line);
    const width = Number.parseFloat(style.strokeWidth) || 2;
    const dash = Math.min(FLOW_MAX, Math.max(FLOW_MIN, length * FLOW_SHARE));
    const pulse = line.cloneNode(false) as SVGGeometryElement;
    for (const name of ["id", "class", "style", "marker-start", "marker-mid", "marker-end"]) pulse.removeAttribute(name);
    pulse.setAttribute("aria-hidden", "true");
    Object.assign(pulse.style, {
      fill: "none",
      stroke: style.stroke,
      strokeWidth: `${width * 1.8}px`,
      strokeLinecap: "round",
      // One dash, its gap longer than the line: before the pulse sets off and once it has arrived, nothing shows.
      strokeDasharray: `${dash} ${length + dash}`,
      strokeDashoffset: `${dash}`,
      filter: `drop-shadow(0 0 ${width * 2}px ${style.stroke})`,
      pointerEvents: "none",
    });
    line.after(pulse);
    added.push(pulse);
    out.push(
      pulse.animate(
        [
          { strokeDashoffset: dash, offset: 0 },
          { strokeDashoffset: -length, offset: FLOW_SECONDS / FLOW_PERIOD },
          { strokeDashoffset: -length, offset: 1 },
        ],
        { duration: FLOW_PERIOD * 1000, delay: delay * 1000, iterations: Infinity, easing: "linear" },
      ),
    );
  }
  return out;
}

/**
 * The core node breathing from *delay* on: the halo it draws grows and fades
 * back, or - a node without one - a soft glow of its own colour comes and goes.
 * Each breath starts and ends at rest, so it never jumps.
 */
function breathe(node: Element, glowSelector: string | undefined, delay: number): Animation {
  const timing = { duration: BREATH_SECONDS * 1000, delay: delay * 1000, iterations: Infinity, easing: "ease-in-out" };
  const halo = glowSelector ? (node.querySelector(glowSelector) as SVGElement | HTMLElement | null) : null;
  if (halo) {
    halo.style.transformBox = "fill-box";
    halo.style.transformOrigin = "50% 50%";
    return halo.animate(
      [
        { opacity: 1, scale: "1", offset: 0 },
        { opacity: 1, scale: "1.14", offset: 0.4 },
        { opacity: 0.6, scale: "0.95", offset: 0.8 },
        { opacity: 1, scale: "1", offset: 1 },
      ],
      timing,
    );
  }
  const shape = node.querySelector("rect, circle, ellipse, polygon, path") ?? node;
  const style = getComputedStyle(shape);
  const colour = style.stroke && style.stroke !== "none" ? style.stroke : style.color;
  return node.animate(
    [
      { filter: `drop-shadow(0 0 0px ${colour})`, offset: 0 },
      { filter: `drop-shadow(0 0 16px ${colour})`, offset: 0.5 },
      { filter: `drop-shadow(0 0 0px ${colour})`, offset: 1 },
    ],
    timing,
  );
}

/** A piece growing to its size from its centre (a team's bubble), fading in as it does. */
function growIn(el: Element, delay: number): Animation {
  const style = (el as SVGElement | HTMLElement).style;
  style.transformBox = "fill-box";
  style.transformOrigin = "50% 50%";
  return el.animate(
    [
      { opacity: 0, scale: "0.2" },
      { opacity: 1, scale: "1" },
    ],
    { duration: PIECE_SECONDS * 1000 * 1.2, delay: delay * 1000, fill: "both", easing: EASING },
  );
}

/** A chart's series grows: its bars from their base, its lines drawn, the rest fading in. */
function grow(series: Element, delay: number): Animation[] {
  const out: Animation[] = [];
  const bars = Array.from(series.querySelectorAll(".recharts-rectangle")) as SVGGraphicsElement[];
  for (const bar of bars) {
    const r = bar.getBBox();
    const horizontal = r.width > r.height * 1.5;
    bar.style.transformBox = "fill-box";
    bar.style.transformOrigin = horizontal ? "0% 50%" : "50% 100%";
    out.push(
      bar.animate([{ transform: horizontal ? "scaleX(0)" : "scaleY(0)" }, { transform: "none" }], {
        duration: PIECE_SECONDS * 1000,
        delay: delay * 1000,
        fill: "both",
        easing: EASING,
      }),
    );
  }
  // A line is drawn along its length; an area's fill would show at once under a drawn stroke, so it fades in.
  const curves = Array.from(series.querySelectorAll("path.recharts-curve:not(.recharts-area-area)"));
  for (const curve of curves) out.push(...draw(curve, delay));
  const drawn = new Set<Element>([...bars, ...curves]);
  if (!drawn.size) return [comeIn(series, delay)];
  for (const other of Array.from(series.querySelectorAll("path, circle, text")).filter((el) => !drawn.has(el) && !Array.from(drawn).some((d) => d.contains(el)))) {
    out.push(comeIn(other, delay + PIECE_SECONDS * 0.5));
  }
  return out;
}

/** The build of the figure at *root*, the block a slide *canvas* holds, drawn in a viewport the window's size. */
export function installFigure(root: Element, canvas: HTMLElement, kind: string): FigureBuild {
  canvas.style.transform = "none";
  const origin = canvas.getBoundingClientRect();
  const parts = FIGURE_PARTS[kind];
  const { steps, nodes, ends } = collectSteps(root, kind, origin);
  const figure = extent(root, steps, origin) ?? union([box(root, origin)]) ?? { x: 0, y: 0, w: origin.width, h: origin.height };
  const stepRects = steps.map((step) => union([...step.pieces, ...step.edges].map((el) => box(el, origin))) ?? figure);
  const pieceSeconds = kind === "chart" ? SERIES_SECONDS : PIECE_SECONDS;
  const settles = steps.map((step) => settleSeconds(step.pieces.length, step.edges.length, pieceSeconds));
  // The edges proper, not the labels a plane hands to the edge they sit on: what a pulse runs along.
  const lines = new Set(all(root, parts?.edges));
  const declared = all(root, parts?.core);
  const hub = hubNode(ends, nodes.length);
  const unframed = new Set(all(root, parts?.unframed));
  const cores = (declared.length ? declared : hub >= 0 ? [nodes[hub]] : []).filter((node) => !unframed.has(node));
  let animations: Animation[] = [];
  let pulses: Element[] = [];
  // The numbers that count up (`data-count-to`), each from when its piece comes in.
  const counters = Array.from(root.querySelectorAll("[data-count-to]")).map((el) => ({
    el,
    value: Number(el.getAttribute("data-count-to")) || 0,
    suffix: el.getAttribute("data-count-suffix") ?? "",
    text: el.textContent ?? "",
    start: Number.POSITIVE_INFINITY,
  }));
  const numberFormat = new Intl.NumberFormat(document.documentElement.lang || undefined, { maximumFractionDigits: 0 });
  const growSelector = parts?.grow;
  let keys: [number, Rect][] = [[0, figure]];
  let safe: SafeArea = NO_SAFE_AREA;
  let drift = { from: 0, to: 0, amount: DRIFT_ZOOM };
  const viewport = { w: window.innerWidth, h: window.innerHeight };

  const place = (t: number) => {
    const zoom = driftAt(t, drift.from, drift.to, drift.amount);
    const view = drifted(framing(cameraAt(keys, t), viewport, safe), viewport, safe, zoom);
    canvas.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  };

  return {
    describe() {
      return {
        steps: steps.map((step, i) => ({
          labels: [...new Set(step.named.flatMap(texts))],
          box: stepRects[i],
          settle: settles[i],
        })),
        figure,
      };
    },
    plan(plan: FigurePlan) {
      for (const animation of animations) animation.cancel();
      for (const pulse of pulses) pulse.remove();
      animations = [];
      pulses = [];
      const moving = (plan.motion ?? "drift") === "drift";
      // From when each loop runs: an edge's pulse once it is drawn, the core's breath once it is in.
      const loops: number[] = [];
      for (const counter of counters) counter.start = Number.POSITIVE_INFINITY;
      steps.forEach((step, i) => {
        const at = plan.times[Math.min(i, plan.times.length - 1)] ?? 0;
        // What grows comes after the rest of its step: a level's cards, then its bubbles.
        const growing = growSelector ? step.pieces.filter((piece) => piece.matches(growSelector)) : [];
        const ordered = [...step.pieces.filter((piece) => !growing.includes(piece)), ...growing];
        ordered.forEach((piece, j) => {
          const delay = at + Math.min(MAX_STAGGER, j * STAGGER_SECONDS) + (growing.includes(piece) ? PIECE_SECONDS * 0.5 : 0);
          if (kind === "chart" && piece.matches(FIGURE_PARTS.chart.nodes)) animations.push(...grow(piece, delay));
          else if (growing.includes(piece)) animations.push(growIn(piece, delay));
          else animations.push(comeIn(piece, delay));
          for (const counter of counters) if (piece.contains(counter.el)) counter.start = Math.min(counter.start, delay + PIECE_SECONDS * 0.3);
          if (moving && cores.includes(piece)) {
            animations.push(breathe(piece, parts?.glow, delay + PIECE_SECONDS));
            loops.push(delay + PIECE_SECONDS);
          }
        });
        for (const edge of step.edges) {
          animations.push(...draw(edge, at + EDGE_DELAY));
          if (moving && lines.has(edge)) {
            const from = at + EDGE_DELAY + EDGE_SECONDS + FLOW_AFTER;
            animations.push(...flow(edge, from, pulses));
            loops.push(from);
          }
        }
      });
      for (const animation of animations) animation.pause();
      safe = plan.safe ?? NO_SAFE_AREA;
      keys = cameraKeyframes(stepRects, figure, plan, parts?.whole);
      const built = builtAt(plan.times, settles);
      drift = moving
        ? { from: driftFrom(keys, built, plan.length), to: plan.length, amount: plan.drift ?? DRIFT_ZOOM }
        : { from: 0, to: 0, amount: 0 };
      const extra: [number, number][] = moving ? [[Math.min(drift.from, ...loops), plan.length]] : [];
      const lastKey = keys.length > 1 ? keys[keys.length - 1][0] : 0;
      return { active: activeSpans(plan.times, settles, keys, extra), whole: Math.min(plan.length, Math.max(built, lastKey)) };
    },
    seek(t: number) {
      for (const animation of animations) animation.currentTime = t * 1000;
      for (const counter of counters) {
        counter.el.textContent = Number.isFinite(counter.start)
          ? `${numberFormat.format(countAt(counter.value, counter.start, t))}${counter.suffix}`
          : counter.text;
      }
      place(t);
    },
  };
}
