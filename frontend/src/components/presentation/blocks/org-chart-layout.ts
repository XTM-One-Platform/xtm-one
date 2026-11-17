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
 * Layout engine of the native `org_chart` block - ONE engine for the viewer,
 * the PDF and PPTX exports, the layout check and the video figure.
 *
 * The agent authors a TREE of seats (`OrgChartBlock` in `presentation_spec.py`:
 * each seat names its manager) and this module turns it into geometry on the
 * 1920px canvas:
 *
 * - a tidy tree (Reingold-Tilford, with Walker's spreading of the small
 *   subtrees between two large ones): one band per level, the shapes of a
 *   level centred on one line, every card of a level as wide and as tall as
 *   the others, children centred under their parent, siblings evenly spaced,
 *   subtrees pushed apart wherever their boxes meet at any depth;
 * - one bus per parent - a stub from the parent, one line along the gap, a
 *   drop per child - never one elbow per child;
 * - leaves folded into a vertical spine under one rail when the widest level
 *   does not fit the room (`spine`), and a refusal - never type shrunk under
 *   `ORG.MIN_TYPE_PX` - when the drawing still does not fit;
 * - bubbles on the sqrt scale (a circle's AREA is its headcount) between
 *   `BUBBLE_MIN_D` and `BUBBLE_MAX_D`, the number always legible, the label
 *   inside the circle or under it, counted in the seat's footprint so it
 *   never meets a neighbour;
 * - rings around the root for `radial`, each label upright beside its circle
 *   on the ring's tangent, bounded by the arc its seat owns;
 * - dotted lines (secondary managers) routed LAST, orthogonally, around
 *   every seat;
 * - the roll-up of every subtree: seats filled today, open and planned seats
 *   apart, and the target once all are filled.
 *
 * Pure and deterministic: no DOM and no measurement - text is measured by
 * the architecture engine's conservative glyph table (`estimateTextWidth`) -
 * so the same spec gives the same picture everywhere it is drawn. The
 * renderer hands in the ROOM it has and the seats the viewer opened in place;
 * the drawing is never magnified. `facts` is the engine's account of its own
 * picture, published for the layout check.
 */
import type {
  DeckNodeEmphasis,
  DeckOrgChartBlock,
  DeckOrgChartNode,
  DeckOrgCountMode,
  DeckOrgNodeKind,
  DeckOrgOrientation,
  DeckPaletteColor,
  DeckSize,
  DeckTone,
} from "@/types";

import { markupToPlainText } from "../markup";
import { estimateTextWidth, segmentIntersectsRect, type Point, type Rect } from "./architecture-layout";

export type { Rect };

// ── Canvas metrics (px on the 1920x1080 canvas) ───────────────────────────────

export const ORG = {
  PAD: 28,
  /** Between two siblings, two cousins, two top-level branches drawn as zones, two roots. */
  SIBLING_GAP: 28,
  COUSIN_GAP: 44,
  BRANCH_GAP: 76,
  ROOT_GAP: 96,
  /** Between two levels: the bus runs along its middle. */
  LEVEL_GAP: 80,
  CARD_PAD_X: 18,
  CARD_PAD_Y: 16,
  CARD_RADIUS: 16,
  MEDIA_GAP: 14,
  CHIP_GAP_TOP: 10,
  CHIP_H: 26,
  CHIP_PAD: 10,
  CHIP_GAP: 6,
  CHIP_FS: 14,
  NAME_FS: 23,
  NAME_LH: 28,
  ROLE_FS: 18,
  ROLE_LH: 23,
  BADGE_FS: 15,
  BADGE_H: 28,
  BADGE_PAD: 11,
  ROLLUP_FS: 16,
  ROLLUP_H: 28,
  ROLLUP_PAD: 12,
  UNDER_GAP: 10,
  CARD_MIN_W: 232,
  CARD_MAX_W: 400,
  COMPACT_MIN_W: 168,
  COMPACT_MAX_W: 270,
  BUBBLE_MIN_D: 76,
  BUBBLE_MAX_D: 224,
  BUBBLE_NUMBER_MIN_FS: 18,
  BUBBLE_NUMBER_MAX_FS: 56,
  BUBBLE_LABEL_FS: 16,
  BUBBLE_LABEL_LH: 20,
  SPINE_INDENT: 30,
  SPINE_GAP: 14,
  ZONE_PAD: 22,
  ZONE_RADIUS: 28,
  LEGEND_FS: 17,
  LEGEND_H: 34,
  LEGEND_GAP: 34,
  LEGEND_ITEM_GAP: 30,
  LEGEND_SWATCH: 26,
  RING_GAP: 64,
  DOTTED_MARGIN: 14,
  /** The smallest type a seat's name, role or number may be seen at once the drawing is fitted: under it the engine refuses. */
  MIN_TYPE_PX: 12,
} as const;

const PHOTO: Record<DeckSize, number> = { sm: 40, md: 52, lg: 68 };
const COMPACT_PHOTO: Record<DeckSize, number> = { sm: 30, md: 38, lg: 48 };
const PERSON_D: Record<DeckSize, number> = { sm: 64, md: 84, lg: 108 };
const LABEL_SCALE: Record<DeckSize, number> = { sm: 0.88, md: 1, lg: 1.14 };
/** The compact style: smaller type, tighter cards and gaps. */
const COMPACT = 0.84;
const EPS = 0.01;
const ELLIPSIS = "…";

/** The palette colours a branch wears, in branch order (`group_by_branch`). */
const BRANCH_COLORS: readonly DeckPaletteColor[] = ["chart_1", "chart_2", "chart_3", "chart_4", "chart_5", "chart_6"];

/** The tone a kind wears when the seat names none: the kind's one meaning, deck-wide. */
const KIND_TONES: Record<DeckOrgNodeKind | "fold", DeckTone | undefined> = {
  person: undefined,
  vacant: "warning",
  planned: "accent",
  group: "info",
  bubble: "info",
  external: "muted",
  fold: "muted",
};

// ── The words a chart writes (translated by the renderer) ─────────────────────

export interface OrgWords {
  /** The badge of a vacant seat. */
  open: string;
  /** The badge of a planned seat with no period. */
  planned: string;
  /** A fold card: "12 people". */
  people: (count: number) => string;
  /** A roll-up's open and planned seats: "+3 open", "+4 planned". */
  openCount: (count: number) => string;
  plannedCount: (count: number) => string;
  /** What a seat at the depth limit cuts: "+12 below". */
  below: (count: number) => string;
  /** A vacant seat's target date: "by Q3". */
  by: (period: string) => string;
  /** The legend entry of the dotted line, and of each kind. */
  dotted: string;
  kinds: Record<DeckOrgNodeKind, string>;
}

export const ENGLISH_WORDS: OrgWords = {
  open: "Open",
  planned: "Planned",
  people: (count) => `${count} ${count === 1 ? "person" : "people"}`,
  openCount: (count) => `+${count} open`,
  plannedCount: (count) => `+${count} planned`,
  below: (count) => `+${count} below`,
  by: (period) => `by ${period}`,
  dotted: "Dotted line",
  kinds: {
    person: "Person",
    vacant: "Open seat",
    planned: "Planned hire",
    group: "Team",
    bubble: "Team, by size",
    external: "External",
  },
};

// ── The roll-up ───────────────────────────────────────────────────────────────

/** The seats of a subtree: filled today, open, planned, and the target once all are filled. */
export interface Rollup {
  current: number;
  open: number;
  planned: number;
  target: number;
}

const ZERO: Rollup = { current: 0, open: 0, planned: 0, target: 0 };

function add(a: Rollup, b: Rollup): Rollup {
  const current = a.current + b.current;
  const open = a.open + b.open;
  const planned = a.planned + b.planned;
  return { current, open, planned, target: current + open + planned };
}

/**
 * What ONE seat counts for, on its own: a person 1, an external seat its
 * count (1 by default), a group or a bubble its headcount - unless it has
 * reports, which are then its total (its own `count` is the DECLARED total
 * the layout check holds them to) - and an open or a planned seat its count,
 * apart from the seats filled today.
 */
function ownCount(node: DeckOrgChartNode, hasReports: boolean): Rollup {
  switch (node.kind) {
    case "person":
      return { current: 1, open: 0, planned: 0, target: 1 };
    case "external": {
      const n = node.count ?? 1;
      return { current: n, open: 0, planned: 0, target: n };
    }
    case "group":
    case "bubble": {
      const n = hasReports ? 0 : (node.count ?? 0);
      return { current: n, open: 0, planned: 0, target: n };
    }
    case "vacant": {
      const n = node.count ?? 1;
      return { current: 0, open: n, planned: 0, target: n };
    }
    case "planned": {
      const n = node.count ?? 1;
      return { current: 0, open: 0, planned: n, target: n };
    }
  }
}

/** What a roll-up writes in a count mode: "14 · +3 open · +4 planned", "21", "14 → 21". */
export function rollupText(rollup: Rollup, mode: DeckOrgCountMode, words: OrgWords, format: (n: number) => string): string {
  if (mode === "target") return format(rollup.target);
  if (mode === "both") return rollup.target === rollup.current ? format(rollup.current) : `${format(rollup.current)} → ${format(rollup.target)}`;
  const parts = [format(rollup.current)];
  if (rollup.open) parts.push(words.openCount(rollup.open));
  if (rollup.planned) parts.push(words.plannedCount(rollup.planned));
  return parts.join(" · ");
}

// ── The tree ──────────────────────────────────────────────────────────────────

interface TreeNode {
  id: string;
  spec: DeckOrgChartNode;
  parent: TreeNode | null;
  children: TreeNode[];
  /** The subtree, itself included. */
  rollup: Rollup;
}

export interface OrgTree {
  roots: TreeNode[];
  byId: Map<string, TreeNode>;
}

/**
 * The seats as a forest, children in spec order, every roll-up computed.
 * Total on any input: a seat whose manager is unknown or part of a cycle
 * (the validator refuses both) is drawn as a root rather than dropped.
 */
export function buildTree(block: DeckOrgChartBlock): OrgTree {
  const byId = new Map<string, TreeNode>();
  for (const spec of block.nodes) {
    if (!byId.has(spec.id)) byId.set(spec.id, { id: spec.id, spec, parent: null, children: [], rollup: ZERO });
  }
  const reaches = (from: TreeNode, target: TreeNode): boolean => {
    for (let node: TreeNode | null = from; node; node = node.parent) if (node === target) return true;
    return false;
  };
  for (const node of byId.values()) {
    const manager = node.spec.manager ? byId.get(node.spec.manager) : undefined;
    if (!manager || manager === node || reaches(manager, node)) continue;
    node.parent = manager;
    manager.children.push(node);
  }
  const listed = (block.roots ?? []).map((id) => byId.get(id)).filter((node): node is TreeNode => !!node && !node.parent);
  const rest = Array.from(byId.values()).filter((node) => !node.parent && !listed.includes(node));
  const roots = [...listed, ...rest];
  const roll = (node: TreeNode): Rollup => {
    let total = ownCount(node.spec, node.children.length > 0);
    for (const child of node.children) total = add(total, roll(child));
    node.rollup = total;
    return total;
  };
  for (const root of roots) roll(root);
  return { roots, byId };
}

/** A group or bubble with reports whose declared `count` is not what its reports add up to. */
export interface CountMismatch {
  id: string;
  declared: number;
  computed: number;
}

export function countMismatches(tree: OrgTree): CountMismatch[] {
  const out: CountMismatch[] = [];
  for (const node of tree.byId.values()) {
    const { kind, count } = node.spec;
    if ((kind !== "group" && kind !== "bubble") || count === undefined || !node.children.length) continue;
    if (count !== node.rollup.current) out.push({ id: node.id, declared: count, computed: node.rollup.current });
  }
  return out;
}

// ── What is drawn: folds, the depth limit, the seats the viewer opened ────────

type OrgDrawnKind = DeckOrgNodeKind | "fold";

interface Drawn {
  id: string;
  /** The seat a click acts on: itself, or the manager a fold card belongs to. */
  sourceId: string;
  spec: DeckOrgChartNode | null;
  kind: OrgDrawnKind;
  level: number;
  parent: Drawn | null;
  children: Drawn[];
  rollup: Rollup;
  /** What it does not draw under it: folded, absorbed into a team's card, or cut by the depth limit. */
  hidden: Rollup | null;
  hiddenBy: "fold" | "absorbed" | "limit" | null;
  foldable: boolean;
  open: boolean;
  /** Its top-level branch (the root's child it descends from). */
  branch: number;
}

const childrenRollup = (node: TreeNode): Rollup => node.children.reduce((sum, child) => add(sum, child.rollup), ZERO);

function drawForest(tree: OrgTree, block: DeckOrgChartBlock, opened: ReadonlySet<string>): Drawn[] {
  const kept = new Set(block.expand ?? []);
  const limit = block.depth_limit;
  const below = block.collapse_below;
  const draw = (node: TreeNode, level: number, parent: Drawn | null, branch: number): Drawn => {
    const drawn: Drawn = {
      id: node.id,
      sourceId: node.id,
      spec: node.spec,
      kind: node.spec.kind,
      level,
      parent,
      children: [],
      rollup: node.rollup,
      hidden: null,
      hiddenBy: null,
      foldable: false,
      open: false,
      branch,
    };
    if (!node.children.length) return drawn;
    const pinned = kept.has(node.id) || opened.has(node.id);
    // `depth_limit` and `collapse_below` count the root as level 1.
    const atLimit = limit !== undefined && level + 1 >= limit;
    const folds = node.spec.collapsed === true || (below !== undefined && level + 1 >= below);
    if (atLimit || folds) {
      drawn.foldable = true;
      drawn.open = opened.has(node.id);
    }
    if (!pinned && atLimit) {
      drawn.hidden = childrenRollup(node);
      drawn.hiddenBy = "limit";
      return drawn;
    }
    if (!pinned && folds) {
      drawn.hidden = childrenRollup(node);
      if (node.spec.kind === "group" || node.spec.kind === "bubble") {
        // A team folds into its own card: the count it shows is the roll-up.
        drawn.hiddenBy = "absorbed";
        return drawn;
      }
      drawn.hiddenBy = "fold";
      drawn.children = [
        {
          id: `${node.id}~fold`,
          sourceId: node.id,
          spec: null,
          kind: "fold",
          level: level + 1,
          parent: drawn,
          children: [],
          rollup: drawn.hidden,
          hidden: null,
          hiddenBy: null,
          foldable: true,
          open: false,
          branch,
        },
      ];
      return drawn;
    }
    drawn.children = node.children.map((child, index) => draw(child, level + 1, drawn, level === 0 ? index : branch));
    return drawn;
  };
  return tree.roots.map((root, index) => draw(root, 0, null, index));
}

function flatten(forest: Drawn[]): Drawn[] {
  const out: Drawn[] = [];
  const walk = (node: Drawn) => {
    out.push(node);
    node.children.forEach(walk);
  };
  forest.forEach(walk);
  return out;
}

// ── Text ──────────────────────────────────────────────────────────────────────

/** One line of text fitted to a width: never wrapped, never cut inside a word unless one word alone is too wide. */
export interface FittedLine {
  text: string;
  full: string;
  width: number;
  truncated: boolean;
  /** Cut inside a word: the one finding a line can still carry (`org_label`). */
  broken: boolean;
}

export function fitLine(full: string, fontSize: number, weight: number, maxWidth: number): FittedLine {
  const width = estimateTextWidth(full, fontSize, weight);
  if (width <= maxWidth) return { text: full, full, width, truncated: false, broken: false };
  const words = full.split(/\s+/).filter(Boolean);
  for (let count = words.length - 1; count >= 1; count -= 1) {
    const text = `${words.slice(0, count).join(" ")}${ELLIPSIS}`;
    const w = estimateTextWidth(text, fontSize, weight);
    if (w <= maxWidth) return { text, full, width: w, truncated: true, broken: false };
  }
  let chunk = "";
  for (const ch of Array.from(words[0] ?? full)) {
    if (estimateTextWidth(`${chunk}${ch}${ELLIPSIS}`, fontSize, weight) > maxWidth) break;
    chunk += ch;
  }
  const text = `${chunk}${ELLIPSIS}`;
  return { text, full, width: estimateTextWidth(text, fontSize, weight), truncated: true, broken: true };
}

/** "Alice Martin" -> "AM"; one word -> its first two letters. */
export function initialsOf(name: string): string {
  const words = name.split(/[\s\-.]+/).filter(Boolean);
  if (!words.length) return "";
  if (words.length === 1) return Array.from(words[0]).slice(0, 2).join("").toUpperCase();
  const first = Array.from(words[0])[0] ?? "";
  const last = Array.from(words[words.length - 1])[0] ?? "";
  return `${first}${last}`.toUpperCase();
}

// ── The layout ────────────────────────────────────────────────────────────────

export interface OrgText {
  text: string;
  full: string;
  /** The line's anchor (its start, or its middle) and its vertical centre. */
  x: number;
  y: number;
  fontSize: number;
  weight: number;
  anchor: "start" | "middle";
  /** The width the line was fitted to: what the layout check holds the painted glyphs to. */
  maxWidth: number;
  truncated: boolean;
  broken: boolean;
  /** A number the video counts up to, with what is written after it. */
  count?: { value: number; suffix: string };
}

export interface OrgMedia {
  kind: "photo" | "icon" | "initials" | "empty";
  x: number;
  y: number;
  size: number;
  fileId?: string;
  icon?: string;
  initials?: string;
}

export interface OrgPill {
  text: OrgText;
  x: number;
  y: number;
  w: number;
  h: number;
  role: "count" | "open" | "period" | "planned" | "below" | "rollup" | "chip" | "more";
}

type OrgShape = "card" | "circle";

export interface OrgNodeLayout {
  /** The seat's id, or `<manager>~fold` for the card a manager's folded reports become. */
  id: string;
  /** The seat a click opens: itself, or the fold card's manager. */
  sourceId: string;
  spec: DeckOrgChartNode | null;
  kind: OrgDrawnKind;
  shape: OrgShape;
  /** 0 = the root's level. */
  level: number;
  parentId: string | null;
  childIds: string[];
  /** The shape's box: a card, or the square around a circle. */
  x: number;
  y: number;
  w: number;
  h: number;
  /** A card's corner radius, a circle's radius. */
  radius: number;
  /** The shape and everything drawn with it (a circle's label, a manager's roll-up). */
  footprint: Rect;
  tone?: DeckTone;
  color?: DeckPaletteColor;
  emphasis: DeckNodeEmphasis;
  main: OrgText | null;
  sub: OrgText | null;
  /** A team circle's number, written large. */
  number: OrgText | null;
  media: OrgMedia | null;
  chips: OrgPill[];
  badge: OrgPill | null;
  rollupPill: OrgPill | null;
  /** Its whole subtree, and what it folds or cuts. */
  rollup: Rollup;
  hidden: Rollup | null;
  foldable: boolean;
  open: boolean;
  /** Stacked in a spine column under its parent. */
  spine: boolean;
  /** Its secondary managers that are drawn. */
  dotted: string[];
  ariaLabel: string;
}

export interface OrgLink {
  key: string;
  kind: "line" | "dotted";
  from: string;
  to: string[];
  /** The level of the seats it reaches; a dotted line is -1 (it comes last). */
  level: number;
  /** One polyline per stroke, in canvas px. */
  points: Point[][];
  d: string;
}

interface OrgZone {
  key: string;
  root: string;
  color: DeckPaletteColor;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface OrgLegendEntry {
  key: string;
  label: string;
  kind?: DeckOrgNodeKind;
  tone?: DeckTone;
  dotted?: boolean;
  x: number;
  y: number;
  text: OrgText;
}

/** The engine's account of its own picture, read by the layout check (`LAYOUT_AUDIT_JS`). */
interface OrgFacts {
  /** Seats in the spec, and seats drawn (fold cards included). */
  seats: number;
  drawn: number;
  /** Something folds: collapse_below, depth_limit or a collapsed seat. */
  folded: boolean;
  /** The scale the room draws the chart at, and the smallest type the room then sees. */
  scale: number;
  minType: number;
  minAllowed: number;
  refused: boolean;
  /** The widest level, when the drawing is still wider than the room (canvas px, unscaled). */
  wide: { level: number; width: number; room: number } | null;
  spine: boolean;
  /** Seats whose name or role is cut inside a word. */
  broken: string[];
  /** Parents whose cards are drawn at different heights. */
  unequal: string[];
  /** Lines drawn through a seat: `<link key>|<seat id>`. */
  crossings: string[];
  /** Labels of a circle that meet another seat: `<seat>|<other>`. */
  overlaps: string[];
  /** Declared team totals the tree does not add up to. */
  counts: CountMismatch[];
  /** Levels (1-based) holding planned seats, none of them dated. */
  undated: number[];
}

export interface OrgChartLayout {
  width: number;
  height: number;
  orientation: DeckOrgOrientation;
  nodes: OrgNodeLayout[];
  links: OrgLink[];
  zones: OrgZone[];
  legend: OrgLegendEntry[];
  facts: OrgFacts;
  /** The drawing cannot be read in its room: the seats drawn, and the type it would need to shrink to. */
  refused: { seats: number; minType: number } | null;
  /** How many levels are drawn. */
  levels: number;
}

export interface OrgLayoutOptions {
  /** The room the drawing has, in canvas px; none = the natural layout. */
  room?: { width: number; height: number } | null;
  /** Seats the viewer opened in place (a click on a folded seat): drawn open whatever folds. */
  opened?: ReadonlySet<string>;
  words?: OrgWords;
  locale?: string;
}

// ── Metrics per style ─────────────────────────────────────────────────────────

interface Metrics {
  compact: boolean;
  nameFs: number;
  nameLh: number;
  roleFs: number;
  roleLh: number;
  chipFs: number;
  chipH: number;
  badgeFs: number;
  badgeH: number;
  rollupFs: number;
  rollupH: number;
  padX: number;
  padY: number;
  photo: number;
  personD: number;
  minW: number;
  maxW: number;
  siblingGap: number;
  cousinGap: number;
  levelGap: number;
  radius: number;
}

function metricsFor(block: DeckOrgChartBlock): Metrics {
  const compact = block.style === "compact";
  const k = LABEL_SCALE[block.label_size ?? "md"] * (compact ? COMPACT : 1);
  const photoSize = block.photo_size ?? "md";
  const scaled = (base: number, floor: number) => Math.max(floor, Math.round(base * k));
  return {
    compact,
    nameFs: scaled(ORG.NAME_FS, 16),
    nameLh: scaled(ORG.NAME_LH, 20),
    roleFs: scaled(ORG.ROLE_FS, 13),
    roleLh: scaled(ORG.ROLE_LH, 17),
    chipFs: scaled(ORG.CHIP_FS, 12),
    chipH: scaled(ORG.CHIP_H, 22),
    badgeFs: scaled(ORG.BADGE_FS, 13),
    badgeH: scaled(ORG.BADGE_H, 24),
    rollupFs: scaled(ORG.ROLLUP_FS, 13),
    rollupH: scaled(ORG.ROLLUP_H, 24),
    padX: compact ? 12 : ORG.CARD_PAD_X,
    padY: compact ? 10 : ORG.CARD_PAD_Y,
    photo: (compact ? COMPACT_PHOTO : PHOTO)[photoSize],
    personD: Math.round(PERSON_D[photoSize] * (compact ? COMPACT : 1)),
    minW: compact ? ORG.COMPACT_MIN_W : ORG.CARD_MIN_W,
    maxW: compact ? ORG.COMPACT_MAX_W : ORG.CARD_MAX_W,
    siblingGap: compact ? 18 : ORG.SIBLING_GAP,
    cousinGap: compact ? 30 : ORG.COUSIN_GAP,
    levelGap: compact ? 60 : ORG.LEVEL_GAP,
    radius: compact ? 12 : ORG.CARD_RADIUS,
  };
}

// ── What a seat says, before it is placed ─────────────────────────────────────

interface Content {
  shape: OrgShape;
  main: string;
  sub: string;
  media: OrgMedia["kind"] | null;
  icon?: string;
  initials?: string;
  fileId?: string;
  badge: { text: string; role: OrgPill["role"]; count?: OrgText["count"] } | null;
  chips: string[];
  /** A team circle's number. */
  number?: { value: number; text: string };
  diameter: number;
  rollup: string | null;
  tone?: DeckTone;
  color?: DeckPaletteColor;
  emphasis: DeckNodeEmphasis;
}

interface Context {
  block: DeckOrgChartBlock;
  metrics: Metrics;
  words: OrgWords;
  format: (n: number) => string;
  orientation: DeckOrgOrientation;
  maxCount: number;
}

const plain = (value: string | undefined | null) => markupToPlainText(value).replace(/\s+/g, " ").trim();

function drawnAsCircle(node: Drawn, ctx: Context): boolean {
  if (ctx.orientation === "radial") return node.level > 0;
  if (node.kind === "bubble") return true;
  const style = ctx.block.style ?? "cards";
  if (style === "bubbles") return true;
  return style === "hybrid" && (node.kind === "group" || node.kind === "fold");
}

/** The diameter a headcount is drawn at: the AREA proportional to the count, between the bounds. */
export function bubbleDiameter(count: number, maxCount: number): number {
  if (!(maxCount > 0) || !(count > 0)) return ORG.BUBBLE_MIN_D;
  const d = ORG.BUBBLE_MAX_D * Math.sqrt(Math.min(count, maxCount) / maxCount);
  return Math.round(Math.max(ORG.BUBBLE_MIN_D, Math.min(ORG.BUBBLE_MAX_D, d)));
}

/** The headcount a team seat shows: its roll-up when it holds its reports, else what it declares. */
function teamCount(node: Drawn): number | undefined {
  if (node.kind === "fold" || node.hiddenBy === "absorbed") return node.rollup.current;
  if (node.kind !== "group" && node.kind !== "bubble") return undefined;
  return node.spec?.count ?? (node.children.length ? node.rollup.current : undefined);
}

function contentOf(node: Drawn, ctx: Context): Content {
  const { block, words, format, metrics } = ctx;
  const spec = node.spec;
  const kind = node.kind;
  const shape: OrgShape = drawnAsCircle(node, ctx) ? "circle" : "card";
  const branchColor = block.group_by_branch && node.level > 0 ? BRANCH_COLORS[node.branch % BRANCH_COLORS.length] : undefined;
  const emphasis: DeckNodeEmphasis = spec?.emphasis ?? (!node.parent && kind !== "fold" ? "core" : "normal");
  // An open or planned seat keeps its kind's tone in every branch: the kind means the same deck-wide.
  const keepsKindTone = kind === "vacant" || kind === "planned";
  const color = spec?.color ?? (spec?.tone || keepsKindTone ? undefined : branchColor);
  const tone = color ? undefined : (spec?.tone ?? KIND_TONES[kind] ?? (emphasis === "core" ? "accent" : undefined));
  const content: Content = {
    shape,
    main: "",
    sub: "",
    media: null,
    badge: null,
    chips: (spec?.chips ?? []).map(plain).filter(Boolean),
    diameter: 0,
    rollup: null,
    tone,
    color,
    emphasis,
  };
  const name = plain(spec?.name);
  const role = plain(spec?.role);
  const label = spec?.count_label ? ` ${spec.count_label}` : "";
  const counted = (n: number, role: OrgPill["role"] = "count") => ({ text: `${format(n)}${label}`, role, count: { value: n, suffix: label } });
  switch (kind) {
    case "person":
      content.main = name;
      content.sub = role;
      content.media = spec?.photo_file_id ? "photo" : spec?.icon ? "icon" : "initials";
      break;
    case "external":
      content.main = name || role;
      content.sub = name ? role : "";
      content.media = spec?.photo_file_id ? "photo" : spec?.icon || !name ? "icon" : "initials";
      if ((spec?.count ?? 1) > 1) content.badge = counted(spec?.count ?? 1);
      break;
    case "vacant":
      content.main = (spec?.count ?? 1) > 1 ? `${format(spec?.count ?? 1)} × ${role}` : role;
      content.sub = spec?.period ? words.by(spec.period) : "";
      content.media = "empty";
      content.badge = { text: words.open, role: "open" };
      break;
    case "planned":
      content.main = (spec?.count ?? 1) > 1 ? `+${format(spec?.count ?? 1)} ${role}` : role;
      content.media = "empty";
      content.badge = spec?.period ? { text: spec.period, role: "period" } : { text: words.planned, role: "planned" };
      break;
    case "group":
    case "bubble": {
      content.main = role;
      content.sub = name;
      const n = teamCount(node);
      if (shape === "card") {
        content.media = "icon";
        if (n !== undefined) content.badge = counted(n);
      } else {
        content.number = { value: n ?? 0, text: format(n ?? 0) };
        if (spec?.icon) content.media = "icon";
      }
      break;
    }
    case "fold": {
      content.main = words.people(node.rollup.current);
      content.sub = [node.rollup.open ? words.openCount(node.rollup.open) : "", node.rollup.planned ? words.plannedCount(node.rollup.planned) : ""]
        .filter(Boolean)
        .join(" · ");
      if (shape === "card") content.media = "icon";
      else content.number = { value: node.rollup.current, text: format(node.rollup.current) };
      break;
    }
  }
  if (content.media === "icon") content.icon = spec?.icon ?? (kind === "external" ? "briefcase" : "users");
  if (content.media === "initials") content.initials = initialsOf(name);
  if (content.media === "photo") content.fileId = spec?.photo_file_id;
  // A seat at the depth limit says what it cuts: "+12 below".
  if (node.hiddenBy === "limit" && node.hidden && !content.badge) {
    content.badge = { text: words.below(node.hidden.target), role: "below" };
  }
  // A manager's roll-up under it (`show_counts`): its whole subtree, itself included.
  if (block.show_counts && kind !== "fold" && (node.children.length || node.hidden)) {
    content.rollup = rollupText(node.rollup, block.count_mode ?? "current", words, format);
  }
  if (shape === "circle") {
    content.diameter = content.number ? bubbleDiameter(content.number.value, ctx.maxCount) : metrics.personD;
    if (!node.parent && emphasis === "core") content.diameter = Math.round(content.diameter * 1.2);
  }
  return content;
}

// ── A seat's content inside its shape ─────────────────────────────────────────

interface Sized {
  node: Drawn;
  content: Content;
  shapeW: number;
  shapeH: number;
  /** What is drawn under the shape (a circle's label, a manager's roll-up), centred on it. */
  underW: number;
  underH: number;
  spine: boolean;
  /** Relative to the shape's top-left. */
  main: OrgText | null;
  sub: OrgText | null;
  number: OrgText | null;
  media: OrgMedia | null;
  chips: OrgPill[];
  badge: OrgPill | null;
  rollupPill: OrgPill | null;
}

function pillWidth(text: string, fontSize: number, pad: number, weight = 500): number {
  return Math.ceil(estimateTextWidth(text, fontSize, weight) + pad * 2);
}

function makeText(line: FittedLine, x: number, y: number, fontSize: number, weight: number, anchor: OrgText["anchor"], maxWidth: number): OrgText {
  return { text: line.text, full: line.full, x, y, fontSize, weight, anchor, maxWidth, truncated: line.truncated, broken: line.broken };
}

/** A card's natural width: what its longest line, its media, its chips and its badge ask, within the style's bounds. */
function cardNaturalWidth(content: Content, m: Metrics): number {
  const media = content.media ? m.photo + ORG.MEDIA_GAP : 0;
  const text = Math.max(estimateTextWidth(content.main, m.nameFs, 600), estimateTextWidth(content.sub, m.roleFs, 400));
  const chips = content.chips.reduce((sum, chip, i) => sum + pillWidth(chip, m.chipFs, ORG.CHIP_PAD) + (i ? ORG.CHIP_GAP : 0), 0);
  const badge = content.badge ? pillWidth(content.badge.text, m.badgeFs, ORG.BADGE_PAD, 600) + 24 : 0;
  const natural = Math.ceil(Math.max(m.padX * 2 + media + text, m.padX * 2 + chips, badge));
  return Math.max(m.minW, Math.min(m.maxW, natural));
}

function cardHeight(content: Content, m: Metrics): number {
  const textH = m.nameLh + (content.sub ? m.roleLh : 0);
  const body = Math.max(content.media ? m.photo : 0, textH);
  return Math.ceil(m.padY * 2 + body + (content.chips.length ? ORG.CHIP_GAP_TOP + m.chipH : 0));
}

function chipPill(text: string, x: number, y: number, w: number, m: Metrics, role: OrgPill["role"]): OrgPill {
  const inner = w - ORG.CHIP_PAD * 2;
  const line = fitLine(text, m.chipFs, 500, inner);
  return { text: makeText(line, x + w / 2, y + m.chipH / 2, m.chipFs, 500, "middle", inner), x, y, w, h: m.chipH, role };
}

/** A card's text, media, chips and badge, inside a card of `w` x `h` (every card of its level). */
function placeCard(sized: Sized, w: number, h: number, m: Metrics): void {
  const { content } = sized;
  const mediaW = content.media ? m.photo + ORG.MEDIA_GAP : 0;
  const textW = Math.max(20, w - m.padX * 2 - mediaW);
  const chipsH = content.chips.length ? ORG.CHIP_GAP_TOP + m.chipH : 0;
  const bodyH = h - m.padY * 2 - chipsH;
  const textH = m.nameLh + (content.sub ? m.roleLh : 0);
  const textTop = m.padY + Math.max(0, (bodyH - textH) / 2);
  const textX = m.padX + mediaW;
  sized.main = content.main ? makeText(fitLine(content.main, m.nameFs, 600, textW), textX, textTop + m.nameLh / 2, m.nameFs, 600, "start", textW) : null;
  sized.sub = content.sub ? makeText(fitLine(content.sub, m.roleFs, 400, textW), textX, textTop + m.nameLh + m.roleLh / 2, m.roleFs, 400, "start", textW) : null;
  sized.media = content.media
    ? { kind: content.media, x: m.padX, y: m.padY + Math.max(0, (bodyH - m.photo) / 2), size: m.photo, fileId: content.fileId, icon: content.icon, initials: content.initials }
    : null;
  // One row of chips: as many as fit, then "+N" for the rest (every chip is in the tooltip).
  sized.chips = [];
  const room = w - m.padX * 2;
  let x = m.padX;
  const y = h - m.padY - m.chipH;
  for (let i = 0; i < content.chips.length; i += 1) {
    const cw = pillWidth(content.chips[i], m.chipFs, ORG.CHIP_PAD);
    const rest = content.chips.length - i - 1;
    const moreW = rest ? pillWidth(`+${rest}`, m.chipFs, ORG.CHIP_PAD) + ORG.CHIP_GAP : 0;
    if (x - m.padX + cw + moreW > room && i > 0) {
      sized.chips.push(chipPill(`+${content.chips.length - i}`, x, y, pillWidth(`+${content.chips.length - i}`, m.chipFs, ORG.CHIP_PAD), m, "more"));
      break;
    }
    sized.chips.push(chipPill(content.chips[i], x, y, Math.min(cw, room), m, "chip"));
    x += cw + ORG.CHIP_GAP;
  }
  if (content.badge) {
    const bw = Math.min(pillWidth(content.badge.text, m.badgeFs, ORG.BADGE_PAD, 600), Math.max(40, w - 24));
    const bx = w - bw - 12;
    const by = -m.badgeH / 2;
    const line = fitLine(content.badge.text, m.badgeFs, 600, bw - ORG.BADGE_PAD * 2);
    const text = makeText(line, bx + bw / 2, by + m.badgeH / 2, m.badgeFs, 600, "middle", bw - ORG.BADGE_PAD * 2);
    if (content.badge.count && !line.truncated) text.count = content.badge.count;
    sized.badge = { text, x: bx, y: by, w: bw, h: m.badgeH, role: content.badge.role };
  }
}

/** A circle's number inside it, its label inside when it fits there and under it otherwise. */
function placeCircle(sized: Sized, m: Metrics): void {
  const { content } = sized;
  const d = content.diameter;
  sized.shapeW = d;
  sized.shapeH = d;
  const inner = d * 0.78;
  const labelW = Math.max(200, Math.min(300, d + 160));
  const under: { line: FittedLine; fs: number; weight: number }[] = [];
  if (content.number) {
    const fs = Math.round(Math.max(ORG.BUBBLE_NUMBER_MIN_FS, Math.min(ORG.BUBBLE_NUMBER_MAX_FS, d * 0.3)));
    const labelFs = Math.max(ORG.BUBBLE_LABEL_FS, m.roleFs - 2);
    const inside = content.main ? fitLine(content.main, labelFs, 500, inner) : null;
    const fitsInside = !!inside && !inside.truncated && d >= 120 && fs + ORG.BUBBLE_LABEL_LH + 8 <= inner;
    const numberY = fitsInside ? d / 2 - ORG.BUBBLE_LABEL_LH / 2 : d / 2;
    const numberLine = fitLine(content.number.text, fs, 700, inner);
    sized.number = makeText(numberLine, d / 2, numberY, fs, 700, "middle", inner);
    if (!numberLine.truncated) sized.number.count = { value: content.number.value, suffix: "" };
    if (fitsInside && inside) sized.main = makeText(inside, d / 2, numberY + fs / 2 + ORG.BUBBLE_LABEL_LH / 2 + 2, labelFs, 500, "middle", inner);
    else if (content.main) under.push({ line: fitLine(content.main, labelFs + 1, 600, labelW), fs: labelFs + 1, weight: 600 });
    if (content.sub) under.push({ line: fitLine(content.sub, labelFs, 400, labelW), fs: labelFs, weight: 400 });
    if (content.media === "icon" && content.icon) {
      const size = Math.round(Math.min(28, d * 0.18));
      sized.media = { kind: "icon", x: (d - size) / 2, y: Math.max(6, numberY - fs / 2 - size - 6), size, icon: content.icon };
    }
  } else {
    // A person, an open or a planned seat drawn as a circle: the photo or initials inside, the words under it.
    const size = Math.round(d * 0.8);
    sized.media = content.media ? { kind: content.media, x: (d - size) / 2, y: (d - size) / 2, size, fileId: content.fileId, icon: content.icon, initials: content.initials } : null;
    if (content.main) under.push({ line: fitLine(content.main, m.nameFs, 600, labelW), fs: m.nameFs, weight: 600 });
    if (content.sub) under.push({ line: fitLine(content.sub, m.roleFs, 400, labelW), fs: m.roleFs, weight: 400 });
  }
  if (content.badge) {
    const bw = Math.min(pillWidth(content.badge.text, m.badgeFs, ORG.BADGE_PAD, 600), labelW);
    const line = fitLine(content.badge.text, m.badgeFs, 600, bw - ORG.BADGE_PAD * 2);
    const by = d - m.badgeH / 2 - 4;
    const text = makeText(line, d / 2, by + m.badgeH / 2, m.badgeFs, 600, "middle", bw - ORG.BADGE_PAD * 2);
    if (content.badge.count && !line.truncated) text.count = content.badge.count;
    sized.badge = { text, x: (d - bw) / 2, y: by, w: bw, h: m.badgeH, role: content.badge.role };
  }
  const badgeBelow = sized.badge ? sized.badge.y + sized.badge.h - d : 0;
  let y = d + Math.max(0, badgeBelow) + ORG.UNDER_GAP;
  let widest = 0;
  const texts: OrgText[] = [];
  for (const { line, fs, weight } of under) {
    const lh = Math.round(fs * 1.25);
    texts.push(makeText(line, d / 2, y + lh / 2, fs, weight, "middle", labelW));
    y += lh;
    widest = Math.max(widest, line.width);
  }
  if (sized.main) sized.sub = texts[0] ?? null;
  else {
    sized.main = texts[0] ?? null;
    sized.sub = texts[1] ?? null;
  }
  if (texts.length) {
    sized.underW = Math.ceil(widest);
    sized.underH = Math.ceil(y - d);
  } else if (badgeBelow > 0) {
    sized.underW = sized.badge?.w ?? 0;
    sized.underH = Math.ceil(badgeBelow);
  }
}

function placeRollup(sized: Sized, m: Metrics): void {
  const text = sized.content.rollup;
  if (!text) return;
  const w = pillWidth(text, m.rollupFs, ORG.ROLLUP_PAD, 600);
  const top = sized.shapeH + sized.underH + ORG.UNDER_GAP;
  const line = fitLine(text, m.rollupFs, 600, w);
  sized.rollupPill = {
    text: makeText(line, sized.shapeW / 2, top + m.rollupH / 2, m.rollupFs, 600, "middle", w),
    x: (sized.shapeW - w) / 2,
    y: top,
    w,
    h: m.rollupH,
    role: "rollup",
  };
  sized.underW = Math.max(sized.underW, w);
  sized.underH = top + m.rollupH - sized.shapeH;
}

// ── The tidy tree (top down, left to right) ───────────────────────────────────
//
// Laid out on two abstract axes: `b` along a level (x top down, y left to
// right) and `d` across the levels. A subtree's contour is the list of the
// boxes it draws, relative to its root's anchor on `b` and absolute on `d`
// (the bands are known before anything is placed), so two subtrees are
// pushed apart wherever ANY of their boxes would meet - a spine reaching
// three bands down included.

interface BRect {
  b0: number;
  b1: number;
  d0: number;
  d1: number;
  /** The sibling itself, not one of its descendants. */
  top: boolean;
}

interface Placed {
  sized: Sized;
  /** The shape's centre on `b`: relative to its parent while placing, absolute at the end. */
  b: number;
  /** The centre line of its band (its row, in a spine) on `d`. */
  d: number;
  bLo: number;
  bHi: number;
  dLo: number;
  dHi: number;
  children: Placed[];
}

function rangesOf(sized: Sized, lr: boolean): { bLo: number; bHi: number; dLo: number; dHi: number } {
  const fw = Math.max(sized.shapeW, sized.underW);
  const fh = sized.shapeH + sized.underH;
  if (!lr) return { bLo: -fw / 2, bHi: fw / 2, dLo: -sized.shapeH / 2, dHi: -sized.shapeH / 2 + fh };
  return { bLo: -sized.shapeH / 2, bHi: -sized.shapeH / 2 + fh, dLo: -fw / 2, dHi: fw / 2 };
}

/** A parent whose reports are all leaf CARDS: a row of circles (a team's bubbles) reads as a row and never folds. */
const isSpineParent = (node: Drawn, spine: boolean, lr: boolean, sizedOf: Map<Drawn, Sized>) =>
  spine &&
  !lr &&
  node.children.length >= 2 &&
  node.children.every((child) => !child.children.length && sizedOf.get(child)?.content.shape === "card");

function tidyLayout(forest: Drawn[], sizedOf: Map<Drawn, Sized>, ctx: Context, spine: boolean): Placed[] {
  const lr = ctx.orientation === "left_right";
  const m = ctx.metrics;
  const branchZones = !!ctx.block.group_by_branch;
  const all = flatten(forest);
  const placedOf = new Map<Drawn, Placed>();
  for (const node of all) {
    const sized = sizedOf.get(node)!;
    placedOf.set(node, { sized, b: 0, d: 0, ...rangesOf(sized, lr), children: [] });
  }
  const spines = new Set(all.filter((node) => isSpineParent(node, spine, lr, sizedOf)));
  for (const node of all) if (node.parent && spines.has(node.parent)) sizedOf.get(node)!.spine = true;
  // The bands: the shapes of a level on one centre line.
  const levels = Math.max(...all.map((node) => node.level)) + 1;
  const above = new Array<number>(levels).fill(0);
  const below = new Array<number>(levels).fill(0);
  for (const node of all) {
    const p = placedOf.get(node)!;
    above[node.level] = Math.max(above[node.level], -p.dLo);
    below[node.level] = Math.max(below[node.level], p.dHi);
  }
  const center = new Array<number>(levels).fill(0);
  center[0] = ORG.PAD + above[0];
  for (let level = 1; level < levels; level += 1) center[level] = center[level - 1] + below[level - 1] + m.levelGap + above[level];
  for (const node of all) placedOf.get(node)!.d = center[node.level];
  // A spine's rows run down from its band, one card under the other.
  for (const node of spines) {
    let d = center[node.level + 1];
    for (const child of node.children) {
      const p = placedOf.get(child)!;
      p.d = d;
      d += p.dHi - p.dLo + ORG.SPINE_GAP;
    }
  }

  const gapBetween = (a: BRect, r: BRect, depth: number): number => {
    if (depth === 0) return ORG.ROOT_GAP;
    if (depth === 1 && branchZones) return ORG.BRANCH_GAP;
    return a.top && r.top ? m.siblingGap : m.cousinGap;
  };
  const overlapsD = (a: BRect, r: BRect) => a.d0 < r.d1 - EPS && r.d0 < a.d1 - EPS;
  /** Where `right` must stand on `b` to clear every box of `left` (each placed at its own `at`). */
  const clearance = (left: { rects: BRect[]; at: number; owner: number }[], right: BRect[], depth: number) => {
    let need = Number.NEGATIVE_INFINITY;
    let binder = -1;
    for (const { rects, at, owner } of left) {
      for (const a of rects) {
        for (const r of right) {
          if (!overlapsD(a, r)) continue;
          const v = at + a.b1 + gapBetween(a, r, depth) - r.b0;
          if (v > need + EPS) {
            need = v;
            binder = owner;
          }
        }
      }
    }
    return { need, binder };
  };
  /** Place sibling subtrees left to right, each as close as its boxes allow, the small ones spread between the large. */
  const pack = (shapes: BRect[][], depth: number): number[] => {
    const offsets: number[] = [0];
    for (let i = 1; i < shapes.length; i += 1) {
      const left = shapes.slice(0, i).map((rects, owner) => ({ rects, at: offsets[owner], owner }));
      const { need, binder } = clearance(left, shapes[i], depth);
      offsets[i] = Number.isFinite(need) ? need : offsets[i - 1];
      // Walker: a subtree pushed right by a sibling further left spreads the
      // ones in between evenly over the room it made.
      if (binder < 0 || binder >= i - 1) continue;
      const adjacent = clearance([{ rects: shapes[i - 1], at: offsets[i - 1], owner: i - 1 }], shapes[i], depth).need;
      const delta = offsets[i] - (Number.isFinite(adjacent) ? adjacent : offsets[i - 1]);
      if (delta <= EPS) continue;
      const trial = offsets.slice();
      for (let k = binder + 1; k < i; k += 1) trial[k] += (delta * (k - binder)) / (i - binder);
      let fits = true;
      for (let q = 1; q <= i && fits; q += 1) {
        for (let s = 0; s < q && fits; s += 1) {
          const pair = clearance([{ rects: shapes[s], at: trial[s], owner: s }], shapes[q], depth).need;
          if (Number.isFinite(pair) && trial[q] < pair - EPS) fits = false;
        }
      }
      if (fits) offsets.splice(0, offsets.length, ...trial);
    }
    return offsets;
  };

  const subtree = (node: Drawn): BRect[] => {
    const p = placedOf.get(node)!;
    const own: BRect = { b0: p.bLo, b1: p.bHi, d0: p.d + p.dLo, d1: p.d + p.dHi, top: true };
    if (!node.children.length) return [own];
    const kids = node.children.map((child) => placedOf.get(child)!);
    p.children = kids;
    if (spines.has(node)) {
      // One column under the parent: the rail on its left, the cards beside it.
      const cardW = Math.max(...kids.map((kid) => kid.bHi - kid.bLo));
      const columnW = ORG.SPINE_INDENT + cardW;
      const left = -columnW / 2;
      const rects: BRect[] = [own];
      for (const kid of kids) {
        kid.b = left + ORG.SPINE_INDENT - kid.bLo;
        rects.push({ b0: left, b1: left + columnW, d0: kid.d + kid.dLo, d1: kid.d + kid.dHi, top: false });
      }
      return rects;
    }
    const shapes = node.children.map(subtree);
    const offsets = pack(shapes, node.level + 1);
    // The parent over the middle of its first and last child.
    const mid = (offsets[0] + offsets[offsets.length - 1]) / 2;
    const rects: BRect[] = [own];
    kids.forEach((kid, index) => {
      kid.b = offsets[index] - mid;
      for (const rect of shapes[index]) rects.push({ ...rect, b0: rect.b0 + kid.b, b1: rect.b1 + kid.b, top: false });
    });
    return rects;
  };

  // The roots side by side, like the children of a root the chart does not draw.
  const roots = forest.map((root) => placedOf.get(root)!);
  const rootOffsets = pack(forest.map(subtree), 0);
  roots.forEach((root, index) => {
    root.b = rootOffsets[index];
  });
  const absolute = (p: Placed, at: number) => {
    p.b = at;
    for (const kid of p.children) absolute(kid, at + kid.b);
  };
  roots.forEach((root) => absolute(root, root.b));
  let lo = Number.POSITIVE_INFINITY;
  for (const node of all) {
    const p = placedOf.get(node)!;
    lo = Math.min(lo, p.b + p.bLo);
  }
  const shift = ORG.PAD + (branchZones ? ORG.ZONE_PAD : 0) - lo;
  for (const node of all) placedOf.get(node)!.b += shift;
  return all.map((node) => placedOf.get(node)!);
}

// ── Radial ────────────────────────────────────────────────────────────────────

function leafCount(node: Drawn): number {
  return node.children.length ? node.children.reduce((sum, child) => sum + leafCount(child), 0) : 1;
}

/** Half the extent of a box of `w` x `h` along a unit direction. */
const extentAlong = (t: Point, w: number, h: number) => (Math.abs(t.x) * w) / 2 + (Math.abs(t.y) * h) / 2;

/** The clockwise tangent of a ring at `angle` (screen coordinates, y down). */
const tangentAt = (angle: number): Point => ({ x: -Math.sin(angle), y: Math.cos(angle) });

/** The label block of a circle seat (its words and roll-up, under it until placed). */
const labelBlock = (s: Sized) => ({ w: s.underW, h: Math.max(0, s.underH - ORG.UNDER_GAP) });

/**
 * Rings around the root: each seat an angular share of the leaves under it,
 * each ring as far out as its widest seat needs to keep its arc - its circle
 * and, beside it on the tangent, its label.
 */
function radialLayout(forest: Drawn[], sizedOf: Map<Drawn, Sized>): Map<Drawn, { cx: number; cy: number; angle: number }> {
  const all = flatten(forest);
  const levels = Math.max(...all.map((node) => node.level)) + 1;
  const span = new Map<Drawn, [number, number]>();
  const leaves = forest.reduce((sum, root) => sum + leafCount(root), 0);
  let start = -Math.PI / 2;
  for (const root of forest) {
    const share = (leafCount(root) / leaves) * Math.PI * 2;
    span.set(root, [start, start + share]);
    start += share;
  }
  const assign = (node: Drawn) => {
    const [a0, a1] = span.get(node)!;
    let at = a0;
    const total = leafCount(node);
    for (const child of node.children) {
      const share = ((a1 - a0) * leafCount(child)) / total;
      span.set(child, [at, at + share]);
      at += share;
      assign(child);
    }
  };
  forest.forEach(assign);
  const single = forest.length === 1;
  const angleOf = (node: Drawn) => {
    const [a0, a1] = span.get(node)!;
    return (a0 + a1) / 2;
  };
  /** How far a seat reaches from its centre, every way: its circle and the label beside it. */
  const reach = (node: Drawn): number => {
    const s = sizedOf.get(node)!;
    if (s.content.shape === "card") return Math.hypot(s.shapeW, s.shapeH + s.underH) / 2;
    const block = labelBlock(s);
    return s.shapeW / 2 + (block.h ? ORG.UNDER_GAP + Math.max(block.w, block.h) : 0);
  };
  const tangential = (node: Drawn): number => {
    const s = sizedOf.get(node)!;
    const block = labelBlock(s);
    const t = tangentAt(angleOf(node));
    return s.shapeW + (block.h ? ORG.UNDER_GAP + 2 * extentAlong(t, block.w, block.h) : 0);
  };
  const radius = new Array<number>(levels).fill(0);
  for (let level = single ? 1 : 0; level < levels; level += 1) {
    const ring = all.filter((node) => node.level === level);
    const inner = level ? all.filter((node) => node.level === level - 1) : [];
    let r = level ? radius[level - 1] + Math.max(...inner.map(reach)) + Math.max(...ring.map(reach)) + ORG.RING_GAP : 0;
    for (const node of ring) {
      const [a0, a1] = span.get(node)!;
      r = Math.max(r, (tangential(node) + ORG.SIBLING_GAP) / Math.max(1e-3, a1 - a0));
    }
    radius[level] = Math.ceil(r);
  }
  const out = new Map<Drawn, { cx: number; cy: number; angle: number }>();
  for (const node of all) {
    const angle = single && !node.parent ? -Math.PI / 2 : angleOf(node);
    const r = single && !node.parent ? 0 : radius[node.level];
    out.set(node, { cx: r * Math.cos(angle), cy: r * Math.sin(angle), angle });
  }
  return out;
}

// ── Lines ─────────────────────────────────────────────────────────────────────

const pathOf = (lines: Point[][]): string =>
  lines
    .filter((line) => line.length >= 2)
    .map((line) => line.map((p, i) => `${i ? "L" : "M"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" "))
    .join(" ");

const inflate = (r: Rect, by: number): Rect => ({ x: r.x - by, y: r.y - by, w: r.w + by * 2, h: r.h + by * 2 });
const inside = (r: Rect, p: Point) => p.x > r.x + EPS && p.x < r.x + r.w - EPS && p.y > r.y + EPS && p.y < r.y + r.h - EPS;
const rectsMeet = (a: Rect, b: Rect) => a.x < b.x + b.w - 1 && b.x < a.x + a.w - 1 && a.y < b.y + b.h - 1 && b.y < a.y + a.h - 1;

function unionRect(rects: Rect[]): Rect {
  if (!rects.length) return { x: 0, y: 0, w: 0, h: 0 };
  const x0 = Math.min(...rects.map((r) => r.x));
  const y0 = Math.min(...rects.map((r) => r.y));
  const x1 = Math.max(...rects.map((r) => r.x + r.w));
  const y1 = Math.max(...rects.map((r) => r.y + r.h));
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

const centerOf = (node: OrgNodeLayout): Point => ({ x: node.x + node.w / 2, y: node.y + node.h / 2 });

/** Where the line from a seat's centre toward `toward` leaves it: its circle, or the border of `box` (its card, or its footprint). */
function edgePoint(node: OrgNodeLayout, toward: Point, box?: Rect): Point {
  const c = centerOf(node);
  const dx = toward.x - c.x;
  const dy = toward.y - c.y;
  const length = Math.hypot(dx, dy) || 1;
  if (node.shape === "circle" && !box) return { x: c.x + (dx / length) * (node.w / 2), y: c.y + (dy / length) * (node.h / 2) };
  const r = box ?? { x: node.x, y: node.y, w: node.w, h: node.h };
  const tx = dx > 0 ? (r.x + r.w - c.x) / dx : dx < 0 ? (r.x - c.x) / dx : Infinity;
  const ty = dy > 0 ? (r.y + r.h - c.y) / dy : dy < 0 ? (r.y - c.y) / dy : Infinity;
  const t = Math.min(tx, ty);
  return { x: c.x + dx * t, y: c.y + dy * t };
}

/** A minimal binary heap of `[cost, value]`, ordered by cost then by value (deterministic). */
class Heap {
  private items: [number, number][] = [];
  get size() {
    return this.items.length;
  }
  push(item: [number, number]) {
    const items = this.items;
    items.push(item);
    let i = items.length - 1;
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (!Heap.less(items[i], items[parent])) break;
      [items[i], items[parent]] = [items[parent], items[i]];
      i = parent;
    }
  }
  pop(): [number, number] {
    const items = this.items;
    const top = items[0];
    const last = items.pop()!;
    if (items.length) {
      items[0] = last;
      let i = 0;
      for (;;) {
        const l = i * 2 + 1;
        const r = l + 1;
        let best = i;
        if (l < items.length && Heap.less(items[l], items[best])) best = l;
        if (r < items.length && Heap.less(items[r], items[best])) best = r;
        if (best === i) break;
        [items[i], items[best]] = [items[best], items[i]];
        i = best;
      }
    }
    return top;
  }
  private static less(a: [number, number], b: [number, number]) {
    return a[0] < b[0] - EPS || (Math.abs(a[0] - b[0]) <= EPS && a[1] < b[1]);
  }
}

/**
 * A dotted line from a secondary manager to its report, routed orthogonally
 * through the gaps the tree leaves: a shortest path over the lines running
 * beside every seat, a bend costing as much as 40px of line, so it never runs
 * through a seat. `null` when no route clears every seat.
 */
function routeDotted(from: OrgNodeLayout, to: OrgNodeLayout, obstacles: Rect[], bounds: Rect): Point[] | null {
  const margin = ORG.DOTTED_MARGIN;
  const fromC = centerOf(from);
  const toC = centerOf(to);
  const start: Point = { x: toC.x >= fromC.x ? from.x + from.w : from.x, y: fromC.y };
  const end: Point = { x: fromC.x >= toC.x ? to.x + to.w : to.x, y: toC.y };
  const startOut: Point = { x: start.x + (start.x > fromC.x ? margin : -margin), y: start.y };
  const endOut: Point = { x: end.x + (end.x > toC.x ? margin : -margin), y: end.y };
  const blocked = obstacles.map((r) => inflate(r, margin - 2));
  const round = (v: number) => Math.round(v * 2) / 2;
  const xs = new Set<number>([round(startOut.x), round(endOut.x), round(bounds.x), round(bounds.x + bounds.w)]);
  const ys = new Set<number>([round(startOut.y), round(endOut.y), round(bounds.y), round(bounds.y + bounds.h)]);
  for (const r of obstacles) {
    xs.add(round(r.x - margin));
    xs.add(round(r.x + r.w + margin));
    ys.add(round(r.y - margin));
    ys.add(round(r.y + r.h + margin));
  }
  const X = Array.from(xs).sort((a, b) => a - b);
  const Y = Array.from(ys).sort((a, b) => a - b);
  const W = X.length;
  const H = Y.length;
  const at = (i: number, j: number): Point => ({ x: X[i], y: Y[j] });
  const freePoint = new Uint8Array(W * H);
  for (let i = 0; i < W; i += 1) for (let j = 0; j < H; j += 1) freePoint[i * H + j] = blocked.some((r) => inside(r, at(i, j))) ? 0 : 1;
  const clear = (a: Point, b: Point) => !blocked.some((r) => segmentIntersectsRect(a, b, inflate(r, -1)));
  const si = X.indexOf(round(startOut.x)) * H + Y.indexOf(round(startOut.y));
  const goal = X.indexOf(round(endOut.x)) * H + Y.indexOf(round(endOut.y));
  // State = point * 3 + the axis it was reached along (0 none, 1 horizontal, 2 vertical).
  const dist = new Float64Array(W * H * 3).fill(Infinity);
  const prev = new Int32Array(W * H * 3).fill(-1);
  const heap = new Heap();
  dist[si * 3] = 0;
  heap.push([0, si * 3]);
  const BEND = 40;
  let found = -1;
  while (heap.size) {
    const [cost, state] = heap.pop();
    if (cost > dist[state] + EPS) continue;
    const point = Math.floor(state / 3);
    const axis = state % 3;
    if (point === goal) {
      found = state;
      break;
    }
    const i = Math.floor(point / H);
    const j = point % H;
    const here = at(i, j);
    for (const [ni, nj, nAxis] of [
      [i + 1, j, 1],
      [i - 1, j, 1],
      [i, j + 1, 2],
      [i, j - 1, 2],
    ] as const) {
      if (ni < 0 || nj < 0 || ni >= W || nj >= H) continue;
      const next = ni * H + nj;
      if (!freePoint[next]) continue;
      const there = at(ni, nj);
      if (!clear(here, there)) continue;
      const step = cost + Math.abs(there.x - here.x) + Math.abs(there.y - here.y) + (axis && axis !== nAxis ? BEND : 0);
      const target = next * 3 + nAxis;
      if (step < dist[target] - EPS) {
        dist[target] = step;
        prev[target] = state;
        heap.push([step, target]);
      }
    }
  }
  if (found < 0) return null;
  const route: Point[] = [];
  for (let state = found; state >= 0; state = prev[state]) {
    const point = Math.floor(state / 3);
    route.unshift(at(Math.floor(point / H), point % H));
  }
  const points = [start, ...route, end];
  return points.filter((p, i) => {
    if (i === 0 || i === points.length - 1) return true;
    const a = points[i - 1];
    const b = points[i + 1];
    return !((Math.abs(a.x - p.x) < EPS && Math.abs(p.x - b.x) < EPS) || (Math.abs(a.y - p.y) < EPS && Math.abs(p.y - b.y) < EPS));
  });
}

/** A radial chart's dotted line: a curve bowed toward whichever side clears every seat. */
function curvedDotted(from: OrgNodeLayout, to: OrgNodeLayout, obstacles: Rect[]): { points: Point[]; d: string } {
  const a = edgePoint(from, centerOf(to));
  const b = edgePoint(to, centerOf(from));
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const normal = { x: -(b.y - a.y) / length, y: (b.x - a.x) / length };
  for (const bow of [0, 0.25, -0.25, 0.45, -0.45]) {
    const control = { x: mid.x + normal.x * bow * length, y: mid.y + normal.y * bow * length };
    const points = Array.from({ length: 17 }, (_unused, i) => {
      const t = i / 16;
      const u = 1 - t;
      return { x: u * u * a.x + 2 * u * t * control.x + t * t * b.x, y: u * u * a.y + 2 * u * t * control.y + t * t * b.y };
    });
    if (!obstacles.some((rect) => points.slice(1).some((p, i) => segmentIntersectsRect(points[i], p, inflate(rect, -1))))) {
      return { points, d: `M ${a.x.toFixed(1)} ${a.y.toFixed(1)} Q ${control.x.toFixed(1)} ${control.y.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}` };
    }
  }
  return { points: [a, b], d: pathOf([[a, b]]) };
}

// ── The legend ────────────────────────────────────────────────────────────────

const KIND_ORDER: DeckOrgNodeKind[] = ["person", "vacant", "planned", "group", "bubble", "external"];

function legendEntries(block: DeckOrgChartBlock, nodes: OrgNodeLayout[], dotted: boolean, words: OrgWords) {
  const mode = block.legend ?? "auto";
  if (mode === "none") return [];
  if (mode === "custom") return (block.legend_items ?? []).map((item) => ({ label: plain(item.label), kind: item.kind, tone: item.tone, dotted: item.dotted }));
  // Auto: the kinds that need saying - every kind drawn but the people - and the dotted line.
  const drawn = new Set(nodes.map((node) => node.kind));
  const entries: { label: string; kind?: DeckOrgNodeKind; tone?: DeckTone; dotted?: boolean }[] = KIND_ORDER.filter(
    (kind) => kind !== "person" && drawn.has(kind),
  ).map((kind) => ({ label: words.kinds[kind], kind }));
  if (dotted) entries.push({ label: words.dotted, dotted: true });
  // A lone "Team" entry over a chart of teams says nothing the cards do not.
  return entries.length === 1 && entries[0].kind === "group" ? [] : entries;
}

// ── One pass: the seats sized and placed, the lines, zones and legend ─────────

interface Pass {
  nodes: OrgNodeLayout[];
  links: OrgLink[];
  zones: OrgZone[];
  legend: OrgLegendEntry[];
  width: number;
  height: number;
  levels: number;
  levelWidths: number[];
  spine: boolean;
}

function countFormatter(locale: string | undefined): (n: number) => string {
  try {
    const formatter = new Intl.NumberFormat(locale ?? "en", { maximumFractionDigits: 0 });
    return (n: number) => formatter.format(n);
  } catch {
    return (n: number) => String(Math.round(n));
  }
}

function ariaOf(node: OrgNodeLayout, words: OrgWords): string {
  const parts = [node.main?.full, node.sub?.full].filter((part): part is string => !!part);
  if (node.kind !== "fold" && node.kind !== "person") parts.push(words.kinds[node.kind]);
  if (node.number) parts.push(node.number.full);
  if (node.badge) parts.push(node.badge.text.full);
  if (node.rollupPill) parts.push(node.rollupPill.text.full);
  return parts.join(", ");
}

function shiftText(text: OrgText | null, dx: number, dy: number): OrgText | null {
  return text ? { ...text, x: text.x + dx, y: text.y + dy } : null;
}

function shiftPill(pill: OrgPill | null, dx: number, dy: number): OrgPill | null {
  return pill ? { ...pill, x: pill.x + dx, y: pill.y + dy, text: shiftText(pill.text, dx, dy)! } : null;
}

function translateNode(node: OrgNodeLayout, dx: number, dy: number): void {
  node.x += dx;
  node.y += dy;
  node.footprint = { ...node.footprint, x: node.footprint.x + dx, y: node.footprint.y + dy };
  node.main = shiftText(node.main, dx, dy);
  node.sub = shiftText(node.sub, dx, dy);
  node.number = shiftText(node.number, dx, dy);
  node.media = node.media ? { ...node.media, x: node.media.x + dx, y: node.media.y + dy } : null;
  node.chips = node.chips.map((chip) => shiftPill(chip, dx, dy)!);
  node.badge = shiftPill(node.badge, dx, dy);
  node.rollupPill = shiftPill(node.rollupPill, dx, dy);
}

function buildPass(forest: Drawn[], ctx: Context, spine: boolean): Pass {
  const { block, metrics: m, words } = ctx;
  const radial = ctx.orientation === "radial";
  const lr = ctx.orientation === "left_right";
  const all = flatten(forest);
  const sizedOf = new Map<Drawn, Sized>();
  for (const node of all) {
    sizedOf.set(node, {
      node,
      content: contentOf(node, ctx),
      shapeW: 0,
      shapeH: 0,
      underW: 0,
      underH: 0,
      spine: false,
      main: null,
      sub: null,
      number: null,
      media: null,
      chips: [],
      badge: null,
      rollupPill: null,
    });
  }
  // Every card of a level as wide and as tall as the widest and tallest of them.
  const levels = Math.max(...all.map((node) => node.level)) + 1;
  const width = new Array<number>(levels).fill(0);
  const height = new Array<number>(levels).fill(0);
  for (const node of all) {
    const { content } = sizedOf.get(node)!;
    if (content.shape !== "card") continue;
    width[node.level] = Math.max(width[node.level], cardNaturalWidth(content, m));
    height[node.level] = Math.max(height[node.level], cardHeight(content, m));
  }
  for (const node of all) {
    const sized = sizedOf.get(node)!;
    if (sized.content.shape === "card") {
      sized.shapeW = width[node.level];
      sized.shapeH = height[node.level];
      placeCard(sized, sized.shapeW, sized.shapeH, m);
    } else {
      placeCircle(sized, m);
    }
    placeRollup(sized, m);
  }

  const nodes: OrgNodeLayout[] = [];
  const layoutOf = new Map<Drawn, OrgNodeLayout>();
  const toLayout = (node: Drawn, cx: number, cy: number): OrgNodeLayout => {
    const s = sizedOf.get(node)!;
    const x = cx - s.shapeW / 2;
    const y = cy - s.shapeH / 2;
    const fw = Math.max(s.shapeW, s.underW);
    const layout: OrgNodeLayout = {
      id: node.id,
      sourceId: node.sourceId,
      spec: node.spec,
      kind: node.kind,
      shape: s.content.shape,
      level: node.level,
      parentId: node.parent?.id ?? null,
      childIds: node.children.map((child) => child.id),
      x,
      y,
      w: s.shapeW,
      h: s.shapeH,
      radius: s.content.shape === "circle" ? s.shapeW / 2 : m.radius,
      footprint: { x: cx - fw / 2, y, w: fw, h: s.shapeH + s.underH },
      tone: s.content.tone,
      color: s.content.color,
      emphasis: s.content.emphasis,
      main: shiftText(s.main, x, y),
      sub: shiftText(s.sub, x, y),
      number: shiftText(s.number, x, y),
      media: s.media ? { ...s.media, x: s.media.x + x, y: s.media.y + y } : null,
      chips: s.chips.map((chip) => shiftPill(chip, x, y)!),
      badge: shiftPill(s.badge, x, y),
      rollupPill: shiftPill(s.rollupPill, x, y),
      rollup: node.rollup,
      hidden: node.hidden,
      foldable: node.foldable,
      open: node.open,
      spine: s.spine,
      dotted: [],
      ariaLabel: "",
    };
    layout.ariaLabel = ariaOf(layout, words);
    return layout;
  };

  const links: OrgLink[] = [];
  if (!radial) {
    for (const p of tidyLayout(forest, sizedOf, ctx, spine)) {
      const layout = lr ? toLayout(p.sized.node, p.d, p.b) : toLayout(p.sized.node, p.b, p.d);
      layoutOf.set(p.sized.node, layout);
      nodes.push(layout);
    }
    // One bus per parent: a stub from the parent, one line along the gap, a drop to each child.
    for (const node of all) {
      if (!node.children.length) continue;
      const parent = layoutOf.get(node)!;
      const kids = node.children.map((child) => layoutOf.get(child)!);
      const lines: Point[][] = [];
      if (kids.every((kid) => kid.spine)) {
        // The spine: the stub to the gap, across to the rail, down the rail, a stub to each card.
        const out = { x: parent.x + parent.w / 2, y: parent.footprint.y + parent.footprint.h };
        const railX = Math.min(...kids.map((kid) => kid.x)) - ORG.SPINE_INDENT / 2;
        const busY = (out.y + kids[0].y) / 2;
        const last = kids[kids.length - 1];
        lines.push([out, { x: out.x, y: busY }, { x: railX, y: busY }, { x: railX, y: last.y + last.h / 2 }]);
        for (const kid of kids) lines.push([{ x: railX, y: kid.y + kid.h / 2 }, { x: kid.x, y: kid.y + kid.h / 2 }]);
      } else if (!lr) {
        const out = { x: parent.x + parent.w / 2, y: parent.footprint.y + parent.footprint.h };
        const busY = (out.y + Math.min(...kids.map((kid) => kid.y))) / 2;
        const xs = kids.map((kid) => kid.x + kid.w / 2);
        if (kids.length === 1 && Math.abs(xs[0] - out.x) < 0.5) {
          lines.push([out, { x: out.x, y: kids[0].y }]);
        } else {
          lines.push([out, { x: out.x, y: busY }]);
          lines.push([{ x: Math.min(out.x, ...xs), y: busY }, { x: Math.max(out.x, ...xs), y: busY }]);
          for (const kid of kids) lines.push([{ x: kid.x + kid.w / 2, y: busY }, { x: kid.x + kid.w / 2, y: kid.y }]);
        }
      } else {
        const out = { x: parent.x + parent.w, y: parent.y + parent.h / 2 };
        const busX = (parent.footprint.x + parent.footprint.w + Math.min(...kids.map((kid) => kid.x))) / 2;
        const ys = kids.map((kid) => kid.y + kid.h / 2);
        if (kids.length === 1 && Math.abs(ys[0] - out.y) < 0.5) {
          lines.push([out, { x: kids[0].x, y: out.y }]);
        } else {
          lines.push([out, { x: busX, y: out.y }]);
          lines.push([{ x: busX, y: Math.min(out.y, ...ys) }, { x: busX, y: Math.max(out.y, ...ys) }]);
          for (const kid of kids) lines.push([{ x: busX, y: kid.y + kid.h / 2 }, { x: kid.x, y: kid.y + kid.h / 2 }]);
        }
      }
      links.push({ key: `bus:${node.id}`, kind: "line", from: node.id, to: kids.map((kid) => kid.id), level: node.level + 1, points: lines, d: pathOf(lines) });
    }
  } else {
    const placed = radialLayout(forest, sizedOf);
    for (const node of all) {
      const { cx, cy, angle } = placed.get(node)!;
      const layout = toLayout(node, cx, cy);
      const s = sizedOf.get(node)!;
      const block = labelBlock(s);
      if (node.parent && layout.shape === "circle" && block.h > 0) {
        // The words beside the circle, upright, on the ring's clockwise tangent.
        const t = tangentAt(angle);
        const reach = layout.w / 2 + ORG.UNDER_GAP + extentAlong(t, block.w, block.h);
        const lx = cx + t.x * reach;
        const top = cy + t.y * reach - block.h / 2;
        const from = layout.y + layout.h + ORG.UNDER_GAP;
        const move = (text: OrgText | null): OrgText | null => (text && text.y > layout.y + layout.h ? { ...text, x: lx, y: top + (text.y - from) } : text);
        layout.main = move(layout.main);
        layout.sub = move(layout.sub);
        if (layout.rollupPill) {
          const pill = layout.rollupPill;
          const py = top + (pill.y - from);
          layout.rollupPill = { ...pill, x: lx - pill.w / 2, y: py, text: { ...pill.text, x: lx, y: py + pill.h / 2 } };
        }
        layout.footprint = unionRect([{ x: layout.x, y: layout.y, w: layout.w, h: layout.h }, { x: lx - block.w / 2, y: top, w: block.w, h: block.h }]);
      }
      layoutOf.set(node, layout);
      nodes.push(layout);
    }
    const bounds = unionRect(nodes.map((node) => node.footprint));
    for (const layout of nodes) translateNode(layout, ORG.PAD - bounds.x, ORG.PAD - bounds.y);
    // Spokes from the parent's edge to the child's, along the line between their centres; a card leaves from its footprint (its roll-up is under it).
    for (const node of all) {
      if (!node.children.length) continue;
      const parent = layoutOf.get(node)!;
      const lines = node.children.map((child) => {
        const kid = layoutOf.get(child)!;
        return [edgePoint(parent, centerOf(kid), parent.shape === "card" ? parent.footprint : undefined), edgePoint(kid, centerOf(parent))];
      });
      links.push({ key: `bus:${node.id}`, kind: "line", from: node.id, to: node.children.map((child) => child.id), level: node.level + 1, points: lines, d: pathOf(lines) });
    }
  }

  // The dotted lines, last, around every seat.
  if (block.show_dotted ?? true) {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const bounds = unionRect(nodes.map((node) => node.footprint));
    const box = { x: bounds.x - ORG.PAD / 2, y: bounds.y - ORG.PAD / 2, w: bounds.w + ORG.PAD, h: bounds.h + ORG.PAD };
    for (const node of nodes) {
      for (const other of node.spec?.dotted_to ?? []) {
        const from = byId.get(other);
        if (!from) continue;
        node.dotted.push(other);
        const obstacles = nodes.filter((n) => n !== node && n !== from).map((n) => n.footprint);
        let points: Point[];
        let d: string;
        if (radial) {
          ({ points, d } = curvedDotted(from, node, obstacles));
        } else {
          points = routeDotted(from, node, obstacles, box) ?? [edgePoint(from, centerOf(node)), edgePoint(node, centerOf(from))];
          d = pathOf([points]);
        }
        links.push({ key: `dotted:${other}>${node.id}`, kind: "dotted", from: other, to: [node.id], level: -1, points: [points], d });
      }
    }
  }

  // One zone per top-level branch, behind its seats.
  const zones: OrgZone[] = [];
  if (block.group_by_branch && !radial) {
    for (const root of forest) {
      root.children.forEach((branch, index) => {
        const rect = unionRect(flatten([branch]).map((member) => layoutOf.get(member)!.footprint));
        zones.push({
          key: `zone:${branch.id}`,
          root: branch.id,
          color: BRANCH_COLORS[index % BRANCH_COLORS.length],
          x: rect.x - ORG.ZONE_PAD,
          y: rect.y - ORG.ZONE_PAD,
          w: rect.w + ORG.ZONE_PAD * 2,
          h: rect.h + ORG.ZONE_PAD * 2,
        });
      });
    }
  }

  // The legend, under the drawing, wrapped to its width and centred.
  const drawn = unionRect([...nodes.map((node) => node.footprint), ...zones, ...links.flatMap((link) => link.points.flat().map((p) => ({ x: p.x, y: p.y, w: 0, h: 0 })))]);
  let widthAll = Math.ceil(drawn.x + drawn.w + ORG.PAD);
  let bottom = drawn.y + drawn.h;
  const legend: OrgLegendEntry[] = [];
  const entries = legendEntries(block, nodes, links.some((link) => link.kind === "dotted"), words);
  if (entries.length) {
    const items = entries.map((entry) => ({ entry, textW: Math.ceil(estimateTextWidth(entry.label, ORG.LEGEND_FS, 400)) }));
    const rows: (typeof items)[] = [[]];
    const room = Math.max(widthAll - ORG.PAD * 2, 600);
    let rowW = 0;
    for (const item of items) {
      const w = ORG.LEGEND_SWATCH + 10 + item.textW;
      const gap = rows[rows.length - 1].length ? ORG.LEGEND_ITEM_GAP : 0;
      if (gap && rowW + gap + w > room) {
        rows.push([]);
        rowW = 0;
      }
      rowW += (rows[rows.length - 1].length ? ORG.LEGEND_ITEM_GAP : 0) + w;
      rows[rows.length - 1].push(item);
    }
    let y = bottom + ORG.LEGEND_GAP;
    for (const row of rows) {
      const total = row.reduce((sum, item, i) => sum + ORG.LEGEND_SWATCH + 10 + item.textW + (i ? ORG.LEGEND_ITEM_GAP : 0), 0);
      widthAll = Math.max(widthAll, Math.ceil(total + ORG.PAD * 2));
      let x = (widthAll - total) / 2;
      for (const item of row) {
        const textX = x + ORG.LEGEND_SWATCH + 10;
        legend.push({
          key: `legend:${legend.length}`,
          label: item.entry.label,
          kind: item.entry.kind,
          tone: item.entry.tone,
          dotted: item.entry.dotted,
          x,
          y,
          text: makeText(fitLine(item.entry.label, ORG.LEGEND_FS, 400, item.textW + 1), textX, y + ORG.LEGEND_H / 2, ORG.LEGEND_FS, 400, "start", item.textW + 1),
        });
        x += ORG.LEGEND_SWATCH + 10 + item.textW + ORG.LEGEND_ITEM_GAP;
      }
      y += ORG.LEGEND_H;
    }
    bottom = y;
  }
  const levelWidths = new Array<number>(levels).fill(0);
  for (let level = 0; level < levels; level += 1) {
    const row = nodes.filter((node) => node.level === level && !node.spine);
    if (!row.length) continue;
    levelWidths[level] = Math.max(...row.map((node) => node.footprint.x + node.footprint.w)) - Math.min(...row.map((node) => node.footprint.x));
  }
  return {
    nodes,
    links,
    zones,
    legend,
    width: widthAll,
    height: Math.ceil(bottom + ORG.PAD),
    levels,
    levelWidths,
    spine: nodes.some((node) => node.spine),
  };
}

// ── Self-check ────────────────────────────────────────────────────────────────

/** The smallest type that names a seat (its name, its role, a team's number), in canvas px before any fit. */
function smallestType(nodes: OrgNodeLayout[]): number {
  let smallest = Number.POSITIVE_INFINITY;
  for (const node of nodes) {
    for (const text of [node.main, node.sub, node.number]) {
      if (text) smallest = Math.min(smallest, text.fontSize);
    }
  }
  return Number.isFinite(smallest) ? smallest : ORG.NAME_FS;
}

function selfCheck(pass: Pass, tree: OrgTree, block: DeckOrgChartBlock, folded: boolean): Omit<OrgFacts, "scale" | "minType" | "minAllowed" | "refused" | "wide"> {
  const byId = new Map(pass.nodes.map((node) => [node.id, node]));
  const broken = pass.nodes.filter((node) => node.main?.broken || node.sub?.broken).map((node) => node.id);
  const unequal = pass.nodes
    .filter((node) => {
      const cards = node.childIds.map((id) => byId.get(id)!).filter((kid) => kid.shape === "card");
      return cards.length > 1 && Math.max(...cards.map((c) => c.h)) - Math.min(...cards.map((c) => c.h)) > 0.5;
    })
    .map((node) => node.id);
  const crossings: string[] = [];
  for (const link of pass.links) {
    const ends = new Set([link.from, ...link.to]);
    for (const node of pass.nodes) {
      if (ends.has(node.id)) continue;
      const rect = inflate(node.footprint, -2);
      if (link.points.some((line) => line.slice(1).some((p, i) => segmentIntersectsRect(line[i], p, rect)))) crossings.push(`${link.key}|${node.id}`);
    }
  }
  const overlaps = new Set<string>();
  for (const node of pass.nodes) {
    if (node.shape !== "circle") continue;
    const outside = [node.main, node.sub].filter((text): text is OrgText => !!text && (text.y < node.y || text.y > node.y + node.h || text.x < node.x || text.x > node.x + node.w));
    for (const text of outside) {
      const w = estimateTextWidth(text.text, text.fontSize, text.weight);
      const rect: Rect = { x: text.anchor === "middle" ? text.x - w / 2 : text.x, y: text.y - text.fontSize / 2, w, h: text.fontSize };
      for (const other of pass.nodes) {
        if (other !== node && rectsMeet(rect, { x: other.x, y: other.y, w: other.w, h: other.h })) overlaps.add(`${node.id}|${other.id}`);
      }
    }
  }
  const plannedByLevel = new Map<number, OrgNodeLayout[]>();
  for (const node of pass.nodes) {
    if (node.kind === "planned") plannedByLevel.set(node.level, [...(plannedByLevel.get(node.level) ?? []), node]);
  }
  const undated = Array.from(plannedByLevel)
    .filter(([, planned]) => planned.every((node) => !node.spec?.period))
    .map(([level]) => level + 1)
    .sort((a, b) => a - b);
  return {
    seats: block.nodes.length,
    drawn: pass.nodes.length,
    folded,
    spine: pass.spine,
    broken,
    unequal,
    crossings,
    overlaps: Array.from(overlaps),
    counts: countMismatches(tree),
    undated,
  };
}

/**
 * Lay an org chart out. With a `room`, the drawing is fitted to it, never
 * magnified: leaves fold into a spine first when the widest level does not
 * fit (`spine: auto`), and a drawing whose seats' smallest type would fall
 * under `ORG.MIN_TYPE_PX` at the scale the room allows is REFUSED
 * (`refused`, with the seat count) rather than shrunk into unreadability.
 */
export function layoutOrgChart(block: DeckOrgChartBlock, options: OrgLayoutOptions = {}): OrgChartLayout {
  const words = options.words ?? ENGLISH_WORDS;
  const tree = buildTree(block);
  const forest = drawForest(tree, block, options.opened ?? new Set());
  const all = flatten(forest);
  const orientation: DeckOrgOrientation = block.orientation ?? "top_down";
  const ctx: Context = {
    block,
    metrics: metricsFor(block),
    words,
    format: countFormatter(options.locale),
    orientation,
    maxCount: Math.max(1, ...all.map((node) => teamCount(node) ?? 0)),
  };
  const spineMode = block.spine ?? "auto";
  const room = options.room && options.room.width > 0 && options.room.height > 0 ? options.room : null;
  let pass = buildPass(forest, ctx, spineMode === "always");
  // The widest level does not fit the room: its leaves fold into spines.
  if (spineMode === "auto" && room && pass.width > room.width + 0.5 && orientation === "top_down") {
    const spined = buildPass(forest, ctx, true);
    if (spined.spine && spined.width < pass.width) pass = spined;
  }
  const scale = room ? Math.min(1, room.width / pass.width, room.height / pass.height) : 1;
  const minType = Math.round(smallestType(pass.nodes) * scale * 10) / 10;
  const refused = minType < ORG.MIN_TYPE_PX - 0.05;
  let wide: OrgFacts["wide"] = null;
  if (room && pass.width > room.width + 0.5) {
    const widest = pass.levelWidths.reduce((best, w, level) => (w > pass.levelWidths[best] ? level : best), 0);
    wide = { level: widest + 1, width: Math.round(pass.levelWidths[widest]), room: Math.round(room.width) };
  }
  return {
    width: pass.width,
    height: pass.height,
    orientation,
    nodes: pass.nodes,
    links: pass.links,
    zones: pass.zones,
    legend: pass.legend,
    facts: {
      ...selfCheck(pass, tree, block, all.some((node) => node.hidden !== null)),
      scale: Math.round(scale * 1000) / 1000,
      minType,
      minAllowed: ORG.MIN_TYPE_PX,
      refused,
      wide,
    },
    refused: refused ? { seats: pass.nodes.length, minType } : null,
    levels: pass.levels,
  };
}

/** The seats around one in the drawn tree, for keyboard navigation: its manager, its first report, its neighbours. */
export function orgNeighbours(layout: OrgChartLayout, id: string): { parent: string | null; firstChild: string | null; previous: string | null; next: string | null } {
  const byId = new Map(layout.nodes.map((node) => [node.id, node]));
  const node = byId.get(id);
  if (!node) return { parent: null, firstChild: null, previous: null, next: null };
  const row = layout.nodes
    .filter((other) => other.level === node.level)
    .sort((a, b) => (layout.orientation === "left_right" ? a.y - b.y || a.x - b.x : a.x - b.x || a.y - b.y))
    .map((other) => other.id);
  const at = row.indexOf(id);
  return {
    parent: node.parentId,
    firstChild: node.childIds[0] ?? null,
    previous: at > 0 ? row[at - 1] : null,
    next: at >= 0 && at < row.length - 1 ? row[at + 1] : null,
  };
}
