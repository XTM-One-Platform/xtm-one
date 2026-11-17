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
 * The fleet "Data flow" view (#9829): lane order and geometry, pure.
 *
 * Three fixed lanes, left to right: where it starts (sources), the agents,
 * where it goes (outputs: the integrations written to, then a "Reads only"
 * group). Edges: one start edge per (source, agent) pair (a trunk bundling the
 * pair's assignments), the handover arcs between agents in a gutter beside the
 * agent lane, one write or read edge per (agent, output).
 *
 * Order (`orderFlowLanes`): the agent order is searched exhaustively up to
 * eight agents (else kept), sources and outputs follow the barycentre of the
 * agents they link to and an insertion descent; the crossings counted by the
 * model are bipartite inversions per lane pair, handover arcs against the start
 * trunks (left gutter) or output fans (right gutter) of the agents between
 * their ends, and interleaved arcs in one gutter. The approved mockups (UX
 * round 5, item 1cad5b91) count 28 on the reference fixture.
 *
 * Geometry (`layoutFlow`): a flow chart in the grammar of OpenCRQ's exposure
 * flow (#9981): three columns of about a quarter of the width, filled bands as
 * wide as the assignments they carry in the gaps between them, the nodes of
 * each column spread over the full height. Every card is sized from the content
 * it holds (name on up to two lines, chip rows, status lines, the open detail,
 * its band stacks), so the canvas is sized to its content and nothing is
 * clipped. Text widths come from `measure` (a canvas
 * measureText in the browser, an estimate in tests).
 *
 * No React, no DOM: unit-tested in fleet-flow-layout.test.ts.
 */

type Side = "L" | "R";

export interface FlowLaneModel {
  sources: string[];
  agents: string[];
  /** Outputs written to (a write edge reaches them). */
  writes: string[];
  /** Outputs only read from: drawn after the writes, under "Reads only". */
  reads: string[];
  /** One (source, agent) pair per start trunk. */
  starts: ReadonlyArray<readonly [string, string]>;
  handovers: ReadonlyArray<readonly [string, string]>;
  /** One (agent, output) pair per write or read edge. */
  outs: ReadonlyArray<readonly [string, string]>;
}

export interface FlowCrossings {
  start: number;
  writeRead: number;
  arcTrunk: number;
  arcArc: number;
  total: number;
  arcSides: Side[];
}

export interface FlowLaneOrder {
  sources: string[];
  agents: string[];
  outputs: string[];
  crossings: FlowCrossings;
}

function inversions(pairs: Array<[number, number]>): number {
  let count = 0;
  for (let i = 0; i < pairs.length; i++) {
    for (let j = i + 1; j < pairs.length; j++) {
      if ((pairs[i][0] - pairs[j][0]) * (pairs[i][1] - pairs[j][1]) < 0) count++;
    }
  }
  return count;
}

function indexOf(order: readonly string[]): Map<string, number> {
  return new Map(order.map((name, i) => [name, i]));
}

/** The crossings the model counts for one order of the three lanes. */
export function countCrossings(
  sources: readonly string[],
  agents: readonly string[],
  outputs: readonly string[],
  model: Pick<FlowLaneModel, "starts" | "handovers" | "outs">,
): FlowCrossings {
  const sp = indexOf(sources);
  const ap = indexOf(agents);
  const op = indexOf(outputs);
  const start = inversions(model.starts.map(([s, a]) => [sp.get(s) ?? 0, ap.get(a) ?? 0]));
  const writeRead = inversions(model.outs.map(([a, o]) => [ap.get(a) ?? 0, op.get(o) ?? 0]));
  const trunksInto = new Map<string, number>();
  for (const [, a] of model.starts) trunksInto.set(a, (trunksInto.get(a) ?? 0) + 1);
  const outsFrom = new Map<string, number>();
  for (const [a] of model.outs) outsFrom.set(a, (outsFrom.get(a) ?? 0) + 1);
  let arcTrunk = 0;
  const spans: Array<[number, number, Side]> = [];
  const arcSides: Side[] = [];
  for (const [a, b] of model.handovers) {
    const pa = ap.get(a) ?? 0;
    const pb = ap.get(b) ?? 0;
    const lo = Math.min(pa, pb);
    const hi = Math.max(pa, pb);
    let left = 0;
    let right = 0;
    for (const [agent, n] of trunksInto) {
      const p = ap.get(agent) ?? -1;
      if (lo < p && p < hi) left += n;
    }
    for (const [agent, n] of outsFrom) {
      const p = ap.get(agent) ?? -1;
      if (lo < p && p < hi) right += n;
    }
    const side: Side = left <= right ? "L" : "R";
    arcSides.push(side);
    spans.push([lo, hi, side]);
    arcTrunk += Math.min(left, right);
  }
  let arcArc = 0;
  for (let i = 0; i < spans.length; i++) {
    for (let j = i + 1; j < spans.length; j++) {
      const [a, b, s1] = spans[i];
      const [c, d, s2] = spans[j];
      if (s1 === s2 && ((a < c && c < b && b < d) || (c < a && a < d && d < b))) arcArc++;
    }
  }
  return { start, writeRead, arcTrunk, arcArc, total: start + writeRead + arcTrunk + arcArc, arcSides };
}

function byBarycentre(nodes: readonly string[], links: Map<string, string[]>, pos: Map<string, number>): string[] {
  const key = (n: string) => {
    const ps = (links.get(n) ?? []).map((a) => pos.get(a) ?? 0);
    return ps.length ? ps.reduce((x, y) => x + y, 0) / ps.length : Number.MAX_SAFE_INTEGER;
  };
  return nodes
    .map((n, i) => ({ n, i, k: key(n) }))
    .sort((x, y) => x.k - y.k || x.i - y.i)
    .map((x) => x.n);
}

/** Insertion descent: move one node anywhere while the cost drops. */
function descend(seq: readonly string[], cost: (s: string[]) => number): string[] {
  let current = [...seq];
  let best = cost(current);
  let improved = true;
  while (improved) {
    improved = false;
    outer: for (let i = 0; i < current.length; i++) {
      for (let j = 0; j < current.length; j++) {
        if (i === j) continue;
        const cand = [...current.slice(0, i), ...current.slice(i + 1)];
        cand.splice(j, 0, current[i]);
        const c = cost(cand);
        if (c < best) {
          best = c;
          current = cand;
          improved = true;
          break outer;
        }
      }
    }
  }
  return current;
}

function* permutations(items: readonly string[]): Generator<string[]> {
  if (items.length <= 1) {
    yield [...items];
    return;
  }
  for (let i = 0; i < items.length; i++) {
    const rest = [...items.slice(0, i), ...items.slice(i + 1)];
    for (const tail of permutations(rest)) yield [items[i], ...tail];
  }
}

/** Agents searched exhaustively up to this count; above it, the given order is kept. */
const EXHAUSTIVE_AGENTS = 8;
/** The best agent orders by barycentre alone that the descent then refines. */
const REFINED_ORDERS = 20;

/** The lane order with the fewest crossings the model counts (deterministic). */
export function orderFlowLanes(model: FlowLaneModel): FlowLaneOrder {
  const sLinks = new Map<string, string[]>();
  for (const [s, a] of model.starts) sLinks.set(s, [...(sLinks.get(s) ?? []), a]);
  const oLinks = new Map<string, string[]>();
  for (const [a, o] of model.outs) oLinks.set(o, [...(oLinks.get(o) ?? []), a]);

  const arrange = (agents: string[], refine: boolean) => {
    const pos = indexOf(agents);
    let so = byBarycentre(model.sources, sLinks, pos);
    let wo = byBarycentre(model.writes, oLinks, pos);
    let ro = byBarycentre(model.reads, oLinks, pos);
    if (refine) {
      so = descend(so, (s) => inversions(model.starts.map(([x, a]) => [s.indexOf(x), pos.get(a) ?? 0])));
      wo = descend(wo, (w) =>
        inversions(model.outs.filter(([, o]) => w.includes(o)).map(([a, o]) => [pos.get(a) ?? 0, w.indexOf(o)])),
      );
      const offset = wo.length;
      ro = descend(ro, (r) =>
        inversions(model.outs.filter(([, o]) => r.includes(o)).map(([a, o]) => [pos.get(a) ?? 0, offset + r.indexOf(o)])),
      );
    }
    const outputs = [...wo, ...ro];
    return { sources: so, agents, outputs, crossings: countCrossings(so, agents, outputs, model) };
  };

  const candidates = model.agents.length <= EXHAUSTIVE_AGENTS ? [...permutations(model.agents)] : [[...model.agents]];
  const ranked = candidates
    .map((agents, i) => {
      const c = arrange(agents, false).crossings;
      return { agents, i, total: c.total, arcs: c.arcTrunk };
    })
    .sort((x, y) => x.total - y.total || x.arcs - y.arcs || x.i - y.i)
    .slice(0, REFINED_ORDERS);
  let best: FlowLaneOrder | null = null;
  for (const { agents } of ranked) {
    const order = arrange(agents, true);
    if (
      !best ||
      order.crossings.total < best.crossings.total ||
      (order.crossings.total === best.crossings.total && order.crossings.arcTrunk < best.crossings.arcTrunk)
    ) {
      best = order;
    }
  }
  return best ?? arrange([...model.agents], true);
}

// ---------------------------------------------------------------- geometry

export interface FlowChipInput {
  /** What the chip says (an event in words, a schedule, a prefix). */
  label: string;
  /** The raw key, shown as the chip's tooltip. */
  title?: string;
}

export interface FlowSourceInput {
  id: string;
  /** The source's name as rendered (one or two lines). */
  name?: string;
  chips: FlowChipInput[];
  /** Lines drawn under the name when the source is not connected (reason, link). */
  statusLines: number;
  /**
   * The status text itself, when there is one: its lines are measured at the card's
   * width so a longer translation never runs over the chips (UX #9882 r3 B1).
   */
  statusText?: string;
  /** Lines of the in-card detail when the source is open (0: closed). */
  detailLines: number;
  /**
   * The texts of the open detail, each wrapped at the card's width and its lines
   * added to `detailLines`, so nothing in the detail is cut (UX #9882 r4 B3).
   */
  detailTexts?: FlowText[];
}

/** A text the layout wraps at a card's width; `medium` for the 500 weight. */
interface FlowText {
  text: string;
  medium?: boolean;
}

export interface FlowAgentInput {
  id: string;
  /** The agent's name as rendered: measured so a long name takes its two lines, never cut. */
  name?: string;
  /** Fixed lines under the name: 1 ("3 assignments"), 2 ("Cannot start" and its link). */
  statusLines: number;
  /** A status text wrapped at the card's width, its lines added to `statusLines` (UX #9882 r4 B2). */
  statusText?: string;
}

export interface FlowOutputInput {
  id: string;
  statusLines: number;
  /** The output's name as rendered (one or two lines). */
  name?: string;
  /** The caption under the name ("Written by 3 agents, 12 assignments"), measured at the card width. */
  caption?: string;
}

export interface FlowLayoutOptions {
  /** The canvas width (the card's inner width). */
  width: number;
  /** The source whose chips are all shown (with "Show less"). */
  expandedSource?: string | null;
  /** Width of a text at a font size, in px. */
  measure?: MeasureText;
  /** Assignments a (source, agent) pair carries: the band's width (default 1). */
  startWeight?: (source: string, agent: string) => number;
  /**
   * Output cards drawn before the rest collapse into one "+N more" card (default
   * OUTPUT_LIMIT; Infinity draws every output). Keeps a large fleet near one
   * screen tall (UX #9992 r1 B1).
   */
  outputLimit?: number;
}

/** The id of the card that stands for the collapsed outputs. */
export const MORE_OUTPUTS = "\u0000more-outputs";

/** Width of a text at a font size (and weight), in px, as the browser renders it. */
export type MeasureText = (text: string, px: number, weight?: number) => number;

/**
 * A measure that adds the letter-spacing the page renders with to every
 * character (a canvas does not apply it): without it a label measured at 138 px
 * renders at 140.3 px and wraps one line more (UX #9882 r4 B1).
 */
export function withLetterSpacing(measure: MeasureText, spacing: number): MeasureText {
  if (!spacing) return measure;
  return (text, px, weight) => measure(text, px, weight) + spacing * [...text].length;
}

export interface FlowChipBox {
  label: string;
  title?: string;
  x: number;
  y: number;
  w: number;
  /** 20 px for one line, 16 px more per wrapped line, as measured (UX #9882 r2 B3, r3 B2). */
  h: number;
  /** "+N more" or "Show less". */
  toggle?: "more" | "less";
}

interface FlowBox {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface FlowSourceBox extends FlowBox {
  chips: FlowChipBox[];
  /** Top of the chip rows, under the name and any status lines. */
  chipsTop: number;
  /** Top of the open detail, under the chips. */
  detailTop: number;
}

type FlowEdgeKind = "start" | "handover" | "write" | "read";

interface FlowEdgePath {
  kind: FlowEdgeKind;
  /** A filled band outline (start, write, read) or a stroked line (handover). */
  d: string;
  /** The nodes the edge joins: hover lights the edge when the hovered agent is one of them. */
  ends: string[];
  /**
   * The assignments a start band carries (its (source, agent) pair). 0 on an
   * output band: the API's write and read edges carry no assignment, so no
   * count is claimed for them (UX #9992 r1 B2).
   */
  weight: number;
  /** The band's width (px), proportional to its weight on one scale for the whole chart. */
  width: number;
  /** The end point and direction of its arrowhead, when it has one. */
  head?: { x: number; y: number; dir: 1 | -1 };
  /** The middle of a band, where its count shows on hover. */
  mid?: { x: number; y: number };
}

export interface FlowLayout {
  order: FlowLaneOrder;
  height: number;
  lanes: { sourceX: number; agentX: number; outputX: number; columnWidth: number; readsLabelY: number | null };
  /** Px of band per assignment. */
  unit: number;
  sources: FlowSourceBox[];
  agents: FlowBox[];
  /** The output cards drawn; the last one is MORE_OUTPUTS when outputs are collapsed. */
  outputs: FlowBox[];
  /** The outputs folded into the MORE_OUTPUTS card, in lane order (empty when every output is drawn). */
  hiddenOutputs: string[];
  edges: FlowEdgePath[];
  /** Smallest gap between a handover arrowhead and a start trunk or output fan, px. */
  minHandoverClearance: number | null;
}

const FLOW_TYPE = { name: 14, small: 12 } as const;
const CHIP = { height: 20, lineExtra: 16, gap: 4, padX: 8, collapseAt: 4, shownCollapsed: 3 } as const;
/** One 12 px status line (content-compact, 150% line height; UX #9882 r2 B5). */
const LINE = 18;
const PORT_PITCH = 10;
const LANE_TOP = 30;
const ARROW_HALF = 3;
/** Each column takes this share of the width, within [COLUMN_MIN, COLUMN_MAX] px (#9981: space to the columns). */
const COLUMN_SHARE = 0.24;
const COLUMN_MIN = 176;
const COLUMN_MAX = 420;
/** A name takes at most two lines, never cut on a 1440 px screen (tooltip past two). */
const NAME_LINES = 2;
/** Px of band per assignment, at most and at least, and the thinnest band drawn. */
const BAND_UNIT = 6;
const BAND_UNIT_MIN = 2;
const BAND_MIN = 2;
/** The tallest band stack on one side of a node, px. */
const MAX_STACK = 150;
/** Vertical padding inside a node above and below its band stacks. */
const NODE_PAD = 10;
/** Output cards drawn before the rest fold into "+N more" (UX #9992 r1 B1: 30 outputs made the chart 3 screens tall). */
const OUTPUT_LIMIT = 12;
/** The smallest drawing height: a small fleet still reads as a chart. */
const MIN_SPAN = 280;
/** Room for the "Reads only" label above the first read-only output. */
const READS_LABEL = 28;
/** Horizontal room beside a name: the kind swatch, gap and count (sources), the swatch (outputs). */
const SOURCE_NAME_PAD = 60;
/**
 * The output card's name box, from its markup: 1 px border on each side, pl-3
 * and pr-2 (12 + 8), the 16 px swatch and its gap-2 (8): 2 + 20 + 24 = 46.
 * At 40 the layout measured a box 6 px wider than the page renders, counted a
 * 152 px name as one line in a 150 px box and clipped its caption (UX #9992 r3 B1).
 */
const OUTPUT_NAME_PAD = 46;
/** The output card's caption box: 1 px border on each side, pl-3 and pr-2. */
const OUTPUT_TEXT_PAD = 22;

const estimate = (text: string, px: number) => text.length * px * 0.6;

/** Horizontal padding of a status line inside a source card (px-2 on each side). */
const STATUS_PAD = 16;
/** Horizontal padding of a status line inside an agent card (px-3 on each side). */
const AGENT_PAD = 24;

/**
 * Lines a text takes when wrapped word by word at *width* (the browser's own
 * greedy wrap). A word wider than the line counts the lines it spans.
 */
export function wrapLines(
  text: string,
  width: number,
  px: number,
  measure: MeasureText = estimate,
  weight?: number,
): number {
  const words = text.split(/\s+/).filter(Boolean);
  if (!words.length || width <= 0) return 0;
  const space = measure(" ", px, weight);
  let lines = 1;
  let used = 0;
  for (const word of words) {
    const w = measure(word, px, weight);
    if (used > 0 && used + space + w > width) {
      lines += 1;
      used = 0;
    }
    if (used === 0 && w > width) {
      lines += Math.ceil(w / width) - 1;
      used = w % width || width;
    } else {
      used += (used > 0 ? space : 0) + w;
    }
  }
  return lines;
}

/**
 * The chip rows of a source: wrapped at *width*, collapsed past four chips unless
 * expanded. A label wider than the card gets the full width on two lines rather
 * than being cut (UX #9882 r2 B3: an English schedule needs about 205 px).
 */
export function layoutChips(
  chips: readonly FlowChipInput[],
  width: number,
  expanded: boolean,
  measure: MeasureText = estimate,
  labels: { more: (n: number) => string; less: string } = { more: (n) => `+${n} more`, less: "Show less" },
): { chips: FlowChipBox[]; height: number } {
  const dense = chips.length > CHIP.collapseAt;
  const shown = dense && !expanded ? chips.slice(0, CHIP.shownCollapsed) : [...chips];
  const items: Array<FlowChipInput & { toggle?: "more" | "less" }> = shown.map((c) => ({ ...c }));
  if (dense) {
    items.push(
      expanded
        ? { label: labels.less, toggle: "less" }
        : { label: labels.more(chips.length - CHIP.shownCollapsed), toggle: "more" },
    );
  }
  const out: FlowChipBox[] = [];
  let x = 0;
  let y = 0;
  let rowH = 0;
  for (const item of items) {
    // +2 px: canvas widths are fractional and the browser wraps on the sub-pixel (UX #9882 r3 B3).
    const natural = Math.ceil(measure(item.label, FLOW_TYPE.small)) + 2 * CHIP.padX + 2;
    // The toggle never wraps; a long label takes the full width on as many lines as it measures (r3 B2).
    const wraps = natural > width && !item.toggle;
    const w = natural > width ? width : natural;
    const lines = wraps ? Math.max(2, wrapLines(item.label, width - 2 * CHIP.padX, FLOW_TYPE.small, measure)) : 1;
    const h = CHIP.height + CHIP.lineExtra * (lines - 1);
    if (x > 0 && x + w > width) {
      x = 0;
      y += rowH + CHIP.gap;
      rowH = 0;
    }
    out.push({ label: item.label, title: item.title, x, y, w, h, toggle: item.toggle });
    x += w + CHIP.gap;
    rowH = Math.max(rowH, h);
  }
  return { chips: out, height: items.length ? y + rowH : 0 };
}

const fmt = (n: number) => Number(n.toFixed(1));

/** A filled band of *w* px from (x0, y0) to (x1, y1), both its top edges: the flow chart grammar of OpenCRQ's exposure flow. */
function bandPath(x0: number, y0: number, x1: number, y1: number, w: number): string {
  const mid = (x0 + x1) / 2;
  return (
    `M${fmt(x0)},${fmt(y0)} C${fmt(mid)},${fmt(y0)} ${fmt(mid)},${fmt(y1)} ${fmt(x1)},${fmt(y1)} ` +
    `L${fmt(x1)},${fmt(y1 + w)} C${fmt(mid)},${fmt(y1 + w)} ${fmt(mid)},${fmt(y0 + w)} ${fmt(x0)},${fmt(y0 + w)} Z`
  );
}

/**
 * The stack positions of a lane: its nodes spread over the whole height *span*
 * (no empty half canvas, #9981; UX #9992 r1 B1: no gap cap, so a short lane spans
 * the same height as the tallest one), the gap between two nodes at least
 * *minGap*, a single node centred, *extraBefore* px added before the node at
 * that index (the "Reads only" label).
 */
function spread(
  names: readonly string[],
  heights: Map<string, number>,
  span: number,
  minGap: number,
  extraBefore?: { index: number; px: number },
): Map<string, number> {
  const pos = new Map<string, number>();
  if (!names.length) return pos;
  const extra = extraBefore && extraBefore.index > 0 && extraBefore.index < names.length ? extraBefore.px : 0;
  const sum = names.reduce((n, id) => n + (heights.get(id) ?? 0), 0) + extra;
  const gap = names.length > 1 ? Math.max(minGap, (span - sum) / (names.length - 1)) : 0;
  const block = sum + gap * (names.length - 1);
  let y = LANE_TOP + Math.max(0, (span - block) / 2);
  names.forEach((id, i) => {
    if (extra && i === extraBefore!.index) y += extra;
    pos.set(id, Math.round(y));
    y += (heights.get(id) ?? 0) + gap;
  });
  return pos;
}

/** The natural height of a lane: its nodes and the smallest gaps. */
const laneHeight = (names: readonly string[], heights: Map<string, number>, minGap: number, extra = 0) =>
  names.length ? names.reduce((n, id) => n + (heights.get(id) ?? 0), 0) + minGap * (names.length - 1) + extra : 0;

/**
 * Every node and band of the view, as a flow chart (#9981): three columns of
 * about a quarter of the width each, the bands in the two gaps between them.
 * One band per (source, agent) pair, as wide as the assignments it carries;
 * one band per (agent, output) pair, as wide as the agent's assignments on a
 * scale that keeps the widest agent fan within its card. Bands leave and enter
 * a node stacked in the order of their other end, so they never cross inside a
 * node. The handover arcs keep a port band of their own at the top of each
 * agent card, above its band stacks, so an arrowhead never touches a band.
 */
export function layoutFlow(
  model: FlowLaneModel,
  content: { sources: FlowSourceInput[]; agents: FlowAgentInput[]; outputs: FlowOutputInput[] },
  options: FlowLayoutOptions,
  order: FlowLaneOrder = orderFlowLanes(model),
  chipLabels?: { more: (n: number) => string; less: string },
): FlowLayout {
  const measure = options.measure ?? estimate;
  const colW = Math.max(COLUMN_MIN, Math.min(COLUMN_MAX, Math.round(options.width * COLUMN_SHARE)));
  const srcW = colW;
  const agW = colW;
  const outW = colW;
  const agX = Math.round((options.width - agW) / 2);
  const outX = options.width - outW;
  const apos = indexOf(order.agents);
  const spos = indexOf(order.sources);
  // Past the output limit, the outputs fewest agents reach fold into one "+N more" card
  // (reads before writes), so a large fleet stays near one screen (UX #9992 r1 B1).
  const limit = Math.max(2, options.outputLimit ?? OUTPUT_LIMIT);
  const reachers = (o: string) => new Set(model.outs.filter(([, x]) => x === o).map(([a]) => a)).size;
  const hidden = new Set<string>();
  if (order.outputs.length > limit) {
    const lanePos = indexOf(order.outputs);
    const keep = [...order.outputs]
      .sort(
        (x, y) =>
          Number(model.reads.includes(x)) - Number(model.reads.includes(y)) ||
          reachers(y) - reachers(x) ||
          (lanePos.get(x) ?? 0) - (lanePos.get(y) ?? 0),
      )
      .slice(0, limit - 1);
    for (const o of order.outputs) if (!keep.includes(o)) hidden.add(o);
  }
  const shownOf = (o: string) => (hidden.has(o) ? MORE_OUTPUTS : o);
  const laneOutputs = [...order.outputs.filter((o) => !hidden.has(o)), ...(hidden.size ? [MORE_OUTPUTS] : [])];
  const opos = indexOf(laneOutputs);
  // The (agent, output) pairs drawn, the folded outputs merged into one pair per agent.
  const outPairs: Array<[string, string]> = [];
  for (const [a, o] of model.outs) {
    const shown = shownOf(o);
    if (!outPairs.some(([x, y]) => x === a && y === shown)) outPairs.push([a, shown]);
  }
  const isReadPair = (a: string, o: string) =>
    o === MORE_OUTPUTS
      ? !model.outs.some(([x, y]) => x === a && hidden.has(y) && !model.reads.includes(y))
      : model.reads.includes(o);
  const srcIn = new Map(content.sources.map((s) => [s.id, s]));
  const agIn = new Map(content.agents.map((a) => [a.id, a]));
  const outIn = new Map(content.outputs.map((o) => [o.id, o]));
  const nameLines = (name: string | undefined, width: number) =>
    name ? Math.min(NAME_LINES, Math.max(1, wrapLines(name, width, FLOW_TYPE.small, measure, 500))) : 1;

  // Band weights: the assignments of each (source, agent) pair, and of each agent.
  const weightOf = options.startWeight ?? (() => 1);
  const startW = new Map<string, number>();
  for (const [s, a] of model.starts) {
    const key = `${s}\u0000${a}`;
    startW.set(key, (startW.get(key) ?? 0) + Math.max(1, weightOf(s, a)));
  }
  const pairW = (s: string, a: string) => startW.get(`${s}\u0000${a}`) ?? 1;
  const load = new Map<string, number>();
  for (const [s, a] of model.starts) load.set(a, (load.get(a) ?? 0) + Math.max(1, weightOf(s, a)));
  const outWeight = (a: string) => Math.max(1, load.get(a) ?? 0);
  const startsOf = (s: string) =>
    model.starts.filter(([x]) => x === s).map(([, a]) => a).sort((x, y) => (apos.get(x) ?? 0) - (apos.get(y) ?? 0));
  const startersOf = (a: string) =>
    model.starts.filter(([, x]) => x === a).map(([s]) => s).sort((x, y) => (spos.get(x) ?? 0) - (spos.get(y) ?? 0));
  const outsOf = (a: string) =>
    outPairs.filter(([x]) => x === a).map(([, o]) => o).sort((x, y) => (opos.get(x) ?? 0) - (opos.get(y) ?? 0));
  const writersOf = (o: string) =>
    outPairs.filter(([, x]) => x === o).map(([a]) => a).sort((x, y) => (apos.get(x) ?? 0) - (apos.get(y) ?? 0));

  // One scale for every start band: the fullest node side holds at most MAX_STACK px of bands.
  let fullest = 0;
  for (const s of order.sources) fullest = Math.max(fullest, startsOf(s).reduce((n, a) => n + pairW(s, a), 0));
  for (const a of order.agents) fullest = Math.max(fullest, load.get(a) ?? 0);
  const unitL = fullest > 0 ? Math.max(BAND_UNIT_MIN, Math.min(BAND_UNIT, MAX_STACK / fullest)) : BAND_UNIT;
  const bandL = (s: string, a: string) => Math.max(BAND_MIN, pairW(s, a) * unitL);
  // What reaches an agent leaves it: its stack split evenly over the outputs it reaches.
  const bandR = (a: string) => Math.max(BAND_MIN, (outWeight(a) * unitL) / Math.max(1, outsOf(a).length));
  const stackOut = (s: string) => startsOf(s).reduce((n, a) => n + bandL(s, a), 0);
  const stackInAgent = (a: string) => startersOf(a).reduce((n, s) => n + bandL(s, a), 0);
  const stackOutAgent = (a: string) => outsOf(a).reduce((n) => n + bandR(a), 0);
  const stackInOutput = (o: string) => writersOf(o).reduce((n, a) => n + bandR(a), 0);

  // Handover arcs: side from the model, one port per arc end at the top of the card side.
  const arcs = model.handovers.map(([a, b], i) => ({
    a,
    b,
    side: order.crossings.arcSides[i] ?? "L",
    span: Math.abs((apos.get(a) ?? 0) - (apos.get(b) ?? 0)),
  }));
  type Port = { arc: number; span: number };
  const ports = new Map<string, Record<Side, Port[]>>();
  for (const a of order.agents) ports.set(a, { L: [], R: [] });
  arcs.forEach((arc, i) => {
    for (const end of [arc.a, arc.b]) ports.get(end)?.[arc.side].push({ arc: i, span: arc.span });
  });
  const portBlock = (a: string, side: Side) => {
    const n = ports.get(a)?.[side].length ?? 0;
    return n ? n * PORT_PITCH + PORT_PITCH : 0;
  };

  const agH = new Map<string, number>();
  for (const a of order.agents) {
    const input = agIn.get(a);
    const lines = nameLines(input?.name, agW - AGENT_PAD);
    const measuredStatus = input?.statusText ? wrapLines(input.statusText, agW - AGENT_PAD, FLOW_TYPE.small, measure) : 0;
    const text = 30 + LINE * (lines - 1) + LINE * ((input?.statusLines ?? 1) + measuredStatus) + 12;
    const left = portBlock(a, "L") + stackInAgent(a);
    const right = portBlock(a, "R") + stackOutAgent(a);
    agH.set(a, Math.max(64, text, 2 * NODE_PAD + Math.max(left, right)));
  }
  const srcChips = new Map<string, { chips: FlowChipBox[]; height: number }>();
  const srcH = new Map<string, number>();
  const srcStatus = new Map<string, number>();
  const srcName = new Map<string, number>();
  for (const s of order.sources) {
    const input = srcIn.get(s);
    const lines = nameLines(input?.name, srcW - SOURCE_NAME_PAD);
    srcName.set(s, lines);
    const chips = layoutChips(input?.chips ?? [], srcW - 20, options.expandedSource === s, measure, chipLabels);
    srcChips.set(s, chips);
    const measured = input?.statusText ? wrapLines(input.statusText, srcW - STATUS_PAD, FLOW_TYPE.small, measure) : 0;
    srcStatus.set(s, Math.max(input?.statusLines ?? 0, measured));
    const status = (srcStatus.get(s) ?? 0) * LINE;
    const detailText = (input?.detailTexts ?? []).reduce(
      (n, t) => n + Math.max(1, wrapLines(t.text, srcW - STATUS_PAD, FLOW_TYPE.small, measure, t.medium ? 500 : undefined)),
      0,
    );
    const detail = ((input?.detailLines ?? 0) + detailText) * LINE;
    const text = 36 + LINE * (lines - 1) + status + chips.height + (detail ? detail + 8 : 0) + 10;
    srcH.set(s, Math.max(text, stackOut(s) + 2 * NODE_PAD));
  }
  const outH = new Map<string, number>();
  for (const o of laneOutputs) {
    const input = outIn.get(o);
    const lines = nameLines(input?.name, outW - OUTPUT_NAME_PAD);
    const caption = input?.caption ? wrapLines(input.caption, outW - OUTPUT_TEXT_PAD, FLOW_TYPE.small, measure) : 1;
    const text = 16 + LINE * (lines + caption + (input?.statusLines ?? 0)) + 8;
    outH.set(o, Math.max(52, text, stackInOutput(o) + 2 * NODE_PAD));
  }

  const writeNames = laneOutputs.filter((o) => model.writes.includes(o));
  const readNames = laneOutputs.filter((o) => model.reads.includes(o));
  const outputNames = [...writeNames, ...readNames, ...(hidden.size ? [MORE_OUTPUTS] : [])];
  const readsExtra = writeNames.length && readNames.length ? READS_LABEL : 0;
  const span = Math.max(
    MIN_SPAN,
    laneHeight(order.sources, srcH, 14),
    laneHeight(order.agents, agH, 22),
    laneHeight(outputNames, outH, 14, readsExtra),
  );
  const sy = spread(order.sources, srcH, span, 14);
  const ay = spread(order.agents, agH, span, 22);
  const oy = spread(outputNames, outH, span, 14, { index: writeNames.length, px: readsExtra });
  const height = LANE_TOP + span + 20;

  // Where each band stack starts on a node side: centred under the ports (agents) or in the card (others).
  const stackTop = (top: number, h: number, stack: number, reserved = 0) =>
    top + NODE_PAD + reserved + Math.max(0, (h - 2 * NODE_PAD - reserved - stack) / 2);
  const agentStackTop = (a: string, side: Side) =>
    stackTop(ay.get(a) ?? 0, agH.get(a) ?? 0, side === "L" ? stackInAgent(a) : stackOutAgent(a), portBlock(a, side));
  const portY = new Map<string, number>();
  for (const a of order.agents) {
    const top = ay.get(a) ?? 0;
    for (const side of ["L", "R"] as const) {
      [...ports.get(a)![side]]
        .sort((x, y) => y.span - x.span || x.arc - y.arc)
        .forEach((p, k) => portY.set(`${p.arc}:${a}`, top + NODE_PAD + PORT_PITCH * k + PORT_PITCH / 2));
    }
  }
  const gutter = new Map<number, number>();
  for (const side of ["L", "R"] as const) {
    arcs
      .map((arc, i) => ({ arc, i }))
      .filter(({ arc }) => arc.side === side)
      .sort((x, y) => x.arc.span - y.arc.span)
      .forEach(({ i }, k) => gutter.set(i, side === "L" ? agX - 14 - 9 * k : agX + agW + 14 + 9 * k));
  }

  const edges: FlowEdgePath[] = [];
  // Start bands: stacked on the source's right side by agent, on the agent's left side by source.
  for (const s of order.sources) {
    let y = stackTop(sy.get(s) ?? 0, srcH.get(s) ?? 0, stackOut(s));
    for (const a of startsOf(s)) {
      const w = bandL(s, a);
      const y1 = agentStackTop(a, "L") + startersOf(a).slice(0, startersOf(a).indexOf(s)).reduce((n, x) => n + bandL(x, a), 0);
      edges.push({
        kind: "start",
        d: bandPath(srcW, y, agX, y1, w),
        ends: [s, a],
        weight: pairW(s, a),
        width: w,
        mid: { x: fmt((srcW + agX) / 2), y: fmt((y + y1) / 2 + w / 2) },
      });
      y += w;
    }
  }
  arcs.forEach((arc, i) => {
    const ya = portY.get(`${i}:${arc.a}`) ?? 0;
    const yb = portY.get(`${i}:${arc.b}`) ?? 0;
    const gx = gutter.get(i) ?? agX;
    const ex = arc.side === "L" ? agX : agX + agW;
    const end = ex + (arc.side === "L" ? -1 : 1);
    edges.push({
      kind: "handover",
      d: `M${ex},${fmt(ya)} L${gx},${fmt(ya)} L${gx},${fmt(yb)} L${end},${fmt(yb)}`,
      ends: [arc.a, arc.b],
      weight: 1,
      width: 1.5,
      head: { x: ex, y: yb, dir: arc.side === "L" ? 1 : -1 },
    });
  });
  // Output bands: stacked on the agent's right side by output, on the output's left side by agent.
  const rx0 = agX + agW;
  for (const a of order.agents) {
    let y = agentStackTop(a, "R");
    for (const o of outsOf(a)) {
      const w = bandR(a);
      const writers = writersOf(o);
      const y1 =
        stackTop(oy.get(o) ?? 0, outH.get(o) ?? 0, stackInOutput(o)) +
        writers.slice(0, writers.indexOf(a)).reduce((n, x) => n + bandR(x), 0);
      edges.push({
        kind: isReadPair(a, o) ? "read" : "write",
        d: bandPath(rx0, y, outX, y1, w),
        ends: [a, o],
        weight: 0,
        width: w,
        mid: { x: fmt((rx0 + outX) / 2), y: fmt((y + y1) / 2 + w / 2) },
      });
      y += w;
    }
  }

  let clearance: number | null = null;
  arcs.forEach((arc, i) => {
    for (const end of [arc.a, arc.b]) {
      const gap = agentStackTop(end, arc.side) - (portY.get(`${i}:${end}`) ?? 0) - ARROW_HALF;
      clearance = clearance === null ? gap : Math.min(clearance, gap);
    }
  });

  return {
    order,
    height,
    lanes: {
      sourceX: 0,
      agentX: agX,
      outputX: outX,
      columnWidth: colW,
      readsLabelY: readNames.length ? (oy.get(readNames[0]) ?? 0) - 6 : null,
    },
    unit: unitL,
    sources: order.sources.map((s) => {
      const top = sy.get(s) ?? 0;
      const chips = srcChips.get(s)!;
      const chipsTop = top + 34 + LINE * ((srcName.get(s) ?? 1) - 1) + (srcStatus.get(s) ?? 0) * LINE;
      return { id: s, x: 0, y: top, w: srcW, h: srcH.get(s) ?? 0, chips: chips.chips, chipsTop, detailTop: chipsTop + chips.height + 8 };
    }),
    agents: order.agents.map((a) => ({ id: a, x: agX, y: ay.get(a) ?? 0, w: agW, h: agH.get(a) ?? 0 })),
    outputs: outputNames.map((o) => ({ id: o, x: outX, y: oy.get(o) ?? 0, w: outW, h: outH.get(o) ?? 0 })),
    hiddenOutputs: order.outputs.filter((o) => hidden.has(o)),
    edges,
    minHandoverClearance: clearance,
  };
}

/**
 * The nodes the hover of *agent* keeps at full opacity: the agent, the sources
 * that start it, the outputs it reaches and its handover partners (UX round 5
 * B1). An edge stays lit only when one of its ends is the agent itself.
 */
export function hoverPath(model: FlowLaneModel, agent: string): Set<string> {
  const lit = new Set<string>([agent]);
  for (const [s, a] of model.starts) if (a === agent) lit.add(s);
  for (const [a, o] of model.outs) if (a === agent) lit.add(o);
  for (const [a, b] of model.handovers) {
    if (a === agent) lit.add(b);
    if (b === agent) lit.add(a);
  }
  return lit;
}

/**
 * The full path through *node* (#9981): a source lights the agents it starts and
 * the outputs they reach; an output the agents reaching it and the sources that
 * start them; an agent its own sources and outputs (handover partners apart).
 */
export function flowPath(model: FlowLaneModel, node: string): Set<string> {
  const lit = new Set<string>([node]);
  const agentsFrom = new Set(model.starts.filter(([s]) => s === node).map(([, a]) => a));
  const agentsTo = new Set(model.outs.filter(([, o]) => o === node).map(([a]) => a));
  const isAgent = model.agents.includes(node);
  if (isAgent) agentsFrom.add(node);
  if (isAgent) agentsTo.add(node);
  for (const a of agentsFrom) {
    lit.add(a);
    for (const [x, o] of model.outs) if (x === a) lit.add(o);
  }
  for (const a of agentsTo) {
    lit.add(a);
    for (const [s, x] of model.starts) if (x === a) lit.add(s);
  }
  return lit;
}

/**
 * The folded outputs *agent* reaches, writes and reads apart (UX #9992 r2 B1): the
 * band from an agent to the "+N more" card names these, never every folded output.
 * *readsOnly* says whether the agent only reads the output (its edges are reads);
 * by default an output is a read when the lane model lists it as read-only.
 */
export function foldedOutputsOf(
  model: FlowLaneModel,
  hidden: readonly string[],
  agent: string,
  readsOnly: (agent: string, output: string) => boolean = (_, o) => model.reads.includes(o),
): { writes: string[]; reads: string[] } {
  const mine = new Set(model.outs.filter(([a]) => a === agent).map(([, o]) => o));
  const reached = hidden.filter((o) => mine.has(o));
  return { writes: reached.filter((o) => !readsOnly(agent, o)), reads: reached.filter((o) => readsOnly(agent, o)) };
}

/**
 * Whether the "+N more" card is lit (UX #9992 r2 B2): at rest, or when the lit path
 * reaches one of the outputs folded into it, so a lit band never ends on a dim card.
 */
export function moreOutputsLit(lit: ReadonlySet<string> | null, hidden: readonly string[]): boolean {
  return !lit || hidden.some((o) => lit.has(o));
}

/** The nodes a selected source keeps lit: itself and the agents it starts (UX r2 A2). */
export function sourcePath(model: FlowLaneModel, source: string): Set<string> {
  const lit = new Set<string>([source]);
  for (const [s, a] of model.starts) if (s === source) lit.add(a);
  return lit;
}
