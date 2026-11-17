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
 * The arithmetic and geometry of the validation views (`charts/OutcomeViews.tsx`):
 * outcomes by label - checks that succeeded, failed or wait - read as rates,
 * scores, bands, arcs, rings, orbits, gates, columns, nodes and cards, with
 * the numbers of the security posture widgets they reproduce
 * (`dev-docs/dashboard-rendering-reference.md`, OpenAEV sections 9-14: the
 * security domains, security coverage, attack path, command center, exposure
 * score and resilience gauge).
 *
 * Pure, relative and type-only imports: runs on `node --test`.
 */
import type {
  WidgetOutcome,
  WidgetOutcomeCounts,
  WidgetOutcomeEntry,
  WidgetOutcomePart,
  WidgetOutcomesData,
  WidgetOutcomeStatus,
} from "@/types";

import { rateColor, type ColorMode } from "../charts/palettes.ts";

/** The validation views, by their view name. */
const OUTCOME_VIEWS = ["outcomematrix", "pathgraph", "exposureconsole", "scoregauge", "ringgauge", "domaincards"] as const;
export type OutcomeView = (typeof OUTCOME_VIEWS)[number];
export const isOutcomeView = (view: string | undefined): view is OutcomeView => (OUTCOME_VIEWS as readonly string[]).includes(view ?? "");

// ── Colours ─────────────────────────────────────────────────────────────────

/** The semantic colours these views draw with, by theme (the reference's theme table). */
export const SEMANTIC: Record<ColorMode, { success: string; warning: string; warn: string; error: string; primary: string; secondary: string; disabled: string }> = {
  dark: { success: "#17ab1f", warning: "#ffa726", warn: "#e6700f", error: "#f14337", primary: "#42caff", secondary: "#00f0bc", disabled: "#75829A" },
  light: { success: "#1CA55E", warning: "#ed6c02", warn: "#E6700F", error: "#F14337", primary: "#001bdb", secondary: "#009474", disabled: "#6E7788" },
};

/** The "high" exposure band's colour: the design system's warning token, in both themes. */
const HIGH_EXPOSURE = "var(--color-feedback-warning-primary)";

/** `c` at `percent` % over transparent - any CSS colour, a token included. */
export const tint = (color: string, percent: number): string => `color-mix(in srgb, ${color} ${percent}%, transparent)`;

/** `c` at an opacity (0..1). */
export const alpha = (color: string, opacity: number): string => tint(color, Math.round(opacity * 1000) / 10);

/** A named colour of an action or a status, read with this theme's semantic colours; another colour is returned as it is. */
export function toneColor(name: string | undefined, mode: ColorMode): string | undefined {
  const theme = SEMANTIC[mode];
  switch (name) {
    case "accent":
      return theme.secondary;
    case "info":
      return theme.primary;
    case "warning":
      return theme.warning;
    case "danger":
      return theme.error;
    case "success":
      return theme.success;
    default:
      return undefined;
  }
}

// ── Rates and scores ────────────────────────────────────────────────────────

/** All the outcomes added up. */
export function totalsOf(items: readonly WidgetOutcomeCounts[]): WidgetOutcomeCounts {
  return items.reduce(
    (sum, item) => ({ label: "", success: sum.success + item.success, failed: sum.failed + item.failed, pending: sum.pending + item.pending }),
    { label: "", success: 0, failed: 0, pending: 0 },
  );
}

/** The exposure score: the percentage of resolved checks that failed, rounded; 0 when none is resolved. */
export function exposureScore(counts: WidgetOutcomeCounts): number {
  const resolved = counts.success + counts.failed;
  return resolved > 0 ? Math.round((counts.failed / resolved) * 100) : 0;
}

/** The resilience: the percentage of resolved checks that succeeded, rounded; 0 when none is resolved. */
export function resilience(counts: WidgetOutcomeCounts): number {
  const resolved = counts.success + counts.failed;
  return resolved > 0 ? Math.round((counts.success / resolved) * 100) : 0;
}

/** Whether outcomes have any count: without one, a view may show its sample. */
export const hasCounts = (data: WidgetOutcomesData): boolean => data.items.some((item) => item.success + item.failed + item.pending > 0);

/** An exposure score's band. @i18n-keys */
export const EXPOSURE_BANDS = {
  low: "Low exposure",
  moderate: "Moderate exposure",
  high: "High exposure",
  critical: "Critical exposure",
} as const;
export type ExposureBand = keyof typeof EXPOSURE_BANDS;

/** The band of a score, under 25, 50, 75, and its colour. */
export function exposureBand(score: number, mode: ColorMode): { band: ExposureBand; color: string } {
  const theme = SEMANTIC[mode];
  if (score < 25) return { band: "low", color: theme.success };
  if (score < 50) return { band: "moderate", color: theme.warning };
  if (score < 75) return { band: "high", color: HIGH_EXPOSURE };
  return { band: "critical", color: theme.error };
}

/** The severity bands an explanation lists, with what each means. @i18n-keys */
export const BAND_DESCRIPTIONS = {
  low: "Your controls stopped almost every validated attack.",
  moderate: "A meaningful share of attacks got through - worth reviewing.",
  high: "More than half of the validated attacks were not stopped.",
  critical: "Most validated attacks breached your controls.",
} as const;
export const BAND_RANGES: Record<ExposureBand, string> = { low: "0 - 24", moderate: "25 - 49", high: "50 - 74", critical: "75 - 100" };

// ── Arcs ────────────────────────────────────────────────────────────────────

/** The point at `deg` degrees (0 to the right, clockwise) on a circle. */
function polar(cx: number, cy: number, r: number, deg: number): { x: number; y: number } {
  const rad = (deg * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

/** An SVG arc from `a0` to `a1` degrees, clockwise. */
export function arcPath(cx: number, cy: number, r: number, a0: number, a1: number): string {
  const s = polar(cx, cy, r, a0);
  const e = polar(cx, cy, r, a1);
  return `M ${s.x} ${s.y} A ${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${e.x} ${e.y}`;
}

// ── Score gauge (the exposure score) ────────────────────────────────────────

/** A 270° gauge from the bottom left, clockwise over the top, to the bottom right. */
export const SCORE_GAUGE = { size: 200, radius: 82, start: -225, sweep: 270, stroke: 12, sweepSize: 260, breakdown: 4 } as const;

/** The gauge's track, its value arc and the needle at its tip, for a progress (0..1). */
export function scoreGaugeArcs(progress: number): { track: string; value: string; tip: { x: number; y: number } } {
  const { size, radius, start, sweep } = SCORE_GAUGE;
  const c = size / 2;
  const end = start + sweep * Math.max(progress, 0.001);
  return { track: arcPath(c, c, radius, start, start + sweep), value: arcPath(c, c, radius, start, end), tip: polar(c, c, radius, end) };
}

/** The breakdown under a score gauge: the first labels, each with its own exposure. */
export function scoreBreakdown(items: readonly WidgetOutcome[], mode: ColorMode): { item: WidgetOutcome; exposure: number; color: string }[] {
  return items.slice(0, SCORE_GAUGE.breakdown).map((item) => {
    const exposure = exposureScore(item);
    return { item, exposure, color: exposureBand(exposure, mode).color };
  });
}

// ── Exposure console (the command center) ───────────────────────────────────

/** The orb: a ring of the score around a glass sphere, platforms orbiting it. */
export const ORB = { size: 256, ring: 84, orbit: 102, node: 13, maxOrbit: 8, orbitSeconds: 90 } as const;

/** The score ring: a full circle from the top, filled by its dash offset. */
export function orbRing(progress: number): { path: string; circumference: number; offset: number; marker: { x: number; y: number }; rotation: number } {
  const c = ORB.size / 2;
  const clamped = Math.min(Math.max(progress, 0), 1);
  const circumference = 2 * Math.PI * ORB.ring;
  return {
    path: arcPath(c, c, ORB.ring, -90, 269.99),
    circumference,
    offset: circumference * (1 - clamped),
    marker: polar(c, c, ORB.ring, -90),
    rotation: clamped * 360,
  };
}

/** Where each of `count` orbiting members sits, the first at the top. */
export function orbitPositions(count: number): { x: number; y: number }[] {
  const c = ORB.size / 2;
  const n = Math.max(count, 1);
  return Array.from({ length: count }, (_, i) => polar(c, c, ORB.orbit, (360 / n) * i - 90));
}

/** A type as a member's badge: as it is up to 4 characters, else the initials of its `_` parts. */
export function abbreviate(type: string | undefined): string {
  if (!type) return "?";
  if (type.length <= 4) return type;
  return type
    .split("_")
    .map((part) => part[0])
    .join("")
    .slice(0, 4);
}

/** A member's name under its node: cut at 21 characters past 22. */
export const orbitName = (name: string): string => (name.length > 22 ? `${name.slice(0, 21).trimEnd()}…` : name);

/** The attack flow: an adversary, gates between, what got through. */
export const FLOW = {
  width: 680,
  height: 240,
  cy: 104,
  adversaryX: 52,
  assetsX: 624,
  gatesStart: 158,
  gatesEnd: 528,
  gateHalf: 52,
  captionY: 38,
  valueY: 178,
  gaugeY: 188,
  percentY: 208,
} as const;

/** The colours of gates a label gives none, in turn. */
const GATE_COLORS = ["#38bdf8", "#818cf8", "#c084fc", "#22d3ee", "#a855f7"] as const;

/** A gate's success rate colour: green from three quarters, amber from a half, deep orange from a quarter, red. */
export function gateRateColor(rate: number, resolved: number, mode: ColorMode): string {
  const theme = SEMANTIC[mode];
  if (resolved <= 0) return theme.disabled;
  if (rate >= 0.75) return theme.success;
  if (rate >= 0.5) return theme.warning;
  if (rate >= 0.25) return "#ff7043";
  return theme.error;
}

/** A label cut to what fits `maxWidth` at `fontSize`, with an ellipsis. */
export function truncateLabel(text: string, fontSize: number, maxWidth: number): string {
  const maxChars = Math.max(3, Math.floor(maxWidth / (fontSize * 0.62)));
  return text.length > maxChars ? `${text.slice(0, maxChars - 1).trimEnd()}…` : text;
}

/** How many particles stand for `value` of `total` attempts: none for none, 1 to `max`. */
export function particlesFor(value: number, total: number, max = 3): number {
  if (value <= 0) return 0;
  return Math.max(1, Math.min(max, Math.round((value / Math.max(total, 1)) * 8)));
}

export interface FlowGate {
  item: WidgetOutcome;
  index: number;
  x: number;
  column: number;
  labelWidth: number;
  gaugeWidth: number;
  color: string;
  resolved: number;
  rate: number;
  rateColor: string;
  fill: number;
  gaugeFill: number;
  stopped: number;
}

/** The attack flow's gates: one per label with any check, spaced evenly between the adversary and what got through. */
export function flowGates(items: readonly WidgetOutcome[], mode: ColorMode): { gates: FlowGate[]; attempts: number; pending: number; breached: number; breachParticles: number } {
  const attempts = items.reduce((sum, item) => sum + item.success + item.failed + item.pending, 0);
  const pending = items.reduce((sum, item) => sum + item.pending, 0);
  const breached = items.reduce((sum, item) => sum + item.failed, 0);
  const visible = items.filter((item) => item.success + item.failed + item.pending > 0);
  const n = Math.max(visible.length, 1);
  const column = (FLOW.gatesEnd - FLOW.gatesStart) / n;
  const labelWidth = Math.max(column - 10, 24);
  const gaugeWidth = Math.max(Math.min(column - 24, 56), 32);
  const gates = visible.map((item, index) => {
    const resolved = item.success + item.failed;
    const rate = resolved > 0 ? item.success / resolved : 0;
    return {
      item,
      index,
      x: FLOW.gatesStart + ((FLOW.gatesEnd - FLOW.gatesStart) * (index + 0.5)) / n,
      column,
      labelWidth,
      gaugeWidth,
      color: item.color ?? GATE_COLORS[index % GATE_COLORS.length],
      resolved,
      rate,
      rateColor: gateRateColor(rate, resolved, mode),
      fill: Math.max(rate * FLOW.gateHalf * 2, resolved > 0 ? 3 : 0),
      gaugeFill: resolved > 0 ? Math.max(gaugeWidth * rate, rate > 0 ? 4 : 0) : 0,
      stopped: particlesFor(item.success, attempts),
    };
  });
  return { gates, attempts, pending, breached, breachParticles: particlesFor(breached, attempts) };
}

// ── Ring gauge (the resilience gauge) ───────────────────────────────────────

export const RING = { size: 132, radius: 58, stroke: 5, gapDegrees: 3 } as const;

/** The resilience's colour: green from 75, amber from 50, orange from 25, red. */
export function resilienceAccent(value: number, mode: ColorMode): string {
  const theme = SEMANTIC[mode];
  if (value >= 75) return theme.success;
  if (value >= 50) return theme.warning;
  if (value >= 25) return theme.warn;
  return theme.error;
}

/** The colour of each status. */
export function statusColor(status: WidgetOutcomeStatus, mode: ColorMode): string {
  const theme = SEMANTIC[mode];
  return status === "success" ? theme.success : status === "failed" ? theme.error : theme.disabled;
}

/** The ring's segments - successes, failures, pending checks, those with a count - as dashes from the top, with gaps. */
export function ringSegments(counts: WidgetOutcomeCounts, mode: ColorMode): { status: WidgetOutcomeStatus; color: string; length: number; offset: number; dasharray: string }[] {
  const total = counts.success + counts.failed + counts.pending;
  if (total === 0) return [];
  const circumference = 2 * Math.PI * RING.radius;
  const gap = (RING.gapDegrees / 360) * circumference;
  let at = 0;
  return (["success", "failed", "pending"] as const)
    .filter((status) => counts[status] > 0)
    .map((status) => {
      const share = counts[status] / total;
      const start = at;
      at += share;
      const length = Math.max(share * circumference - gap, 0.5);
      return { status, color: statusColor(status, mode), length, offset: -(start * circumference), dasharray: `${length} ${circumference - length}` };
    });
}

// ── Technique matrix (the security coverage) ────────────────────────────────

export type CoverageFilter = "all" | "covered" | "gaps";

/** The success-rate legend, from the best band. */
export const SUCCESS_LEGEND = [
  { label: "≤ 100%", color: "rgba(9, 78, 11 ,0.4)" },
  { label: "≤ 75%", color: "rgba(184, 135, 10, 0.4)" },
  { label: "≤ 50%", color: "rgba(136, 65, 6, 0.4)" },
  { label: "≤ 25%", color: "rgba(136, 17, 6, 0.4)" },
] as const;

/** A technique's accent by its success rate (slate when untested). */
export const coverageAccent = (rate: number | null, mode: ColorMode): string => rateColor(rate, "coverage", mode);

/** The width of a column of the matrix, in pixels. */
export const MATRIX_COLUMN = 170;

export interface MatrixCell {
  id: string;
  code: string;
  label: string;
  subIds: string[];
  success: number;
  failure: number;
  total: number;
  /** It has any outcome: covered. */
  present: boolean;
  /** Tested: it has resolved checks. */
  tested: boolean;
  rate: number | null;
  /** Its successes among its resolved checks. */
  successCount: number;
}

export interface MatrixModel {
  chains: { id: string; label: string }[];
  chain: string | null;
  columns: { id: string; label: string; cells: MatrixCell[] }[];
  total: number;
  covered: number;
  gaps: number;
  percent: number;
  /** The scope leaves nothing to show. */
  empty: boolean;
}

const byLabel = (a: { label: string }, b: { label: string }): number => (a.label < b.label ? -1 : a.label > b.label ? 1 : 0);

function cellOf(entry: WidgetOutcomeEntry, outcome: WidgetOutcome | undefined): MatrixCell {
  const success = outcome?.success ?? 0;
  const failure = outcome?.failed ?? 0;
  const total = success + failure;
  const rate = total === 0 ? null : success / total;
  const tested = total > 0 && rate !== null;
  return {
    id: entry.id,
    code: entry.code ?? outcome?.code ?? "",
    label: entry.label,
    subIds: entry.subIds ?? outcome?.subIds ?? [],
    success,
    failure,
    total,
    present: outcome !== undefined,
    tested,
    rate,
    successCount: rate ? Math.round(rate * total) : 0,
  };
}

/**
 * The matrix of one chain: a column per group, in order, holding every label of
 * that group (the reference's, else the outcomes'), sorted by label; the chain's
 * coverage (labels with an outcome among those of the chain). In the covered or
 * gaps scope, an empty column is hidden.
 */
export function matrixModel(data: WidgetOutcomesData, chainId: string | null, filter: CoverageFilter): MatrixModel {
  const byId = new Map<string, WidgetOutcome>();
  for (const item of data.items) byId.set(item.id ?? item.label, item);
  // Without a reference, the outcomes are the labels; without chains, their groups are one.
  const entries: WidgetOutcomeEntry[] =
    data.reference ?? data.items.map((item) => ({ id: item.id ?? item.label, label: item.label, code: item.code, phases: item.phases ?? (item.group ? [item.group] : []), subIds: item.subIds }));
  const chains =
    data.chains && data.chains.length > 0
      ? data.chains
      : [{ id: "", label: "", groups: [...new Set(data.items.flatMap((item) => item.phases ?? (item.group ? [item.group] : [])))].map((id) => ({ id, label: id })) }];
  const chain = chains.find((c) => c.id === chainId) ?? chains.find((c) => c.default) ?? chains[0];
  const groups = chain?.groups ?? [];
  const inChain = new Set(groups.map((g) => g.id));
  const members = entries.filter((entry) => (entry.phases ?? []).some((phase) => inChain.has(phase)));
  const covered = members.filter((entry) => byId.has(entry.id)).length;
  const total = members.length;
  const columns = groups
    .map((group) => ({
      id: group.id,
      label: group.label,
      cells: entries
        .filter((entry) => (entry.phases ?? []).includes(group.id))
        .sort(byLabel)
        .map((entry) => cellOf(entry, byId.get(entry.id)))
        .filter((cell) => filter === "all" || (filter === "covered") === cell.present),
    }))
    .filter((column) => filter === "all" || column.cells.length > 0);
  return {
    chains: chains.map((c) => ({ id: c.id, label: c.label })),
    chain: chain?.id ?? null,
    columns,
    total,
    covered,
    gaps: total - covered,
    percent: Math.round((total > 0 ? covered / total : 0) * 100),
    empty: (filter === "covered" && covered === 0) || (filter === "gaps" && total - covered === 0),
  };
}

// ── Path graph (the attack path) ────────────────────────────────────────────

export const PATH = { padding: 50, xGap: 200, yGap: 150, nodeWidth: 150, arrow: 15 } as const;

export interface PathNode {
  id: string;
  kind: "group" | "outcome" | "date";
  label: string;
  x: number;
  y: number;
  item?: WidgetOutcome;
  /** An outcome's success rate as a whole percentage, floored. */
  rate?: number | null;
}

/** A node's place: `x` index across groups, `y` index down (none for a group's header, at the top). */
export const pathPosition = (x: number, y: number | null) => ({ x: PATH.padding + PATH.xGap * x, y: y === null ? 0 : PATH.padding + PATH.yGap * y });

/** An outcome node's id: its outcome under one group. */
const pathNodeId = (item: WidgetOutcome, group: string): string => `${item.id ?? item.label}-${group}`;

/** An outcome's success rate as its node shows it: a whole percentage, floored, out of 1. */
export function pathRate(item: WidgetOutcomeCounts): number | null {
  const total = item.success + item.failed;
  return total > 0 ? Math.floor((item.success * 100) / total) / 100 : null;
}

/** The groups a path lays out, in order: its first chain's, else the outcomes' groups as they come. */
function pathGroups(data: WidgetOutcomesData): { id: string; label: string }[] {
  const chain = data.chains?.[0];
  if (chain) return chain.groups;
  const seen = new Map<string, string>();
  for (const item of data.items) for (const group of item.phases ?? (item.group ? [item.group] : [])) if (!seen.has(group)) seen.set(group, group);
  return [...seen].map(([id, label]) => ({ id, label }));
}

const groupsOf = (item: WidgetOutcome): string[] => item.phases ?? (item.group ? [item.group] : []);

/**
 * The nodes of a path: a header per group at the top, its outcomes below it in
 * order, then the run's start under the first column and its end under the last
 * (the second when there are two groups or fewer).
 */
export function pathNodes(data: WidgetOutcomesData): { nodes: PathNode[]; maxY: number; maxX: number } {
  const groups = pathGroups(data);
  const nodes: PathNode[] = [];
  let maxY = 0;
  groups.forEach((group, x) => {
    nodes.push({ id: group.id, kind: "group", label: group.label, ...pathPosition(x, null) });
    data.items
      .filter((item) => groupsOf(item).includes(group.id))
      .forEach((item, y) => {
        maxY = Math.max(maxY, y);
        nodes.push({ id: pathNodeId(item, group.id), kind: "outcome", label: item.label, item, rate: pathRate(item), ...pathPosition(x, y) });
      });
  });
  const maxX = groups.length > 2 ? groups.length - 1 : 1;
  return { nodes, maxY, maxX };
}

/** The edges a hovered outcome node shows: to every node of each outcome it leads to. */
export function pathEdges(data: WidgetOutcomesData, hovered: string | null): { id: string; source: string; target: string }[] {
  if (!hovered) return [];
  const edges: { id: string; source: string; target: string }[] = [];
  for (const item of data.items) {
    for (const group of groupsOf(item)) {
      const source = pathNodeId(item, group);
      if (source !== hovered) continue;
      for (const childId of item.links ?? []) {
        const child = data.items.find((candidate) => candidate.id === childId);
        for (const target of child ? groupsOf(child).map((g) => pathNodeId(child, g)) : []) {
          if (target !== source) edges.push({ id: `${source}-${target}-edge`, source, target });
        }
      }
    }
  }
  return edges;
}

/** An edge between two nodes of one row, the source right of the target: an arc above them; otherwise `null` (a bezier). */
export function rowArc(sourceX: number, sourceY: number, targetX: number, targetY: number): string | null {
  if (sourceY > targetY - 10 && sourceY < targetY + 10 && sourceX > targetX) {
    return `M ${sourceX} ${sourceY - 10} A ${(sourceX - targetX) * 0.55} 35 0 1 0 ${targetX} ${targetY - 10}`;
  }
  return null;
}

// ── Domain cards (the security domains) ─────────────────────────────────────

/** The domains' colours: an average's bands, a status's, and no data. */
const DOMAIN_COLORS: Record<ColorMode, { success: string; failed: string; pending: string; empty: string }> = {
  dark: { success: "rgb(2,129,8)", failed: "rgb(220, 81, 72)", pending: "rgba(248,243,243,0.74)", empty: "rgba(128,127,127,0.37)" },
  light: { success: "rgb(2,129,8)", failed: "rgb(220, 81, 72)", pending: "rgba(248,243,243,0.37)", empty: "rgba(128,127,127,0.37)" },
};

/** A percentage of a whole, or -1 when there is none. */
const percentOf = (part: number, whole: number): number => (whole <= 0 ? -1 : (part / whole) * 100);

/** An average's colour (a percentage; below 0, none): failed to 25, warning to 50, intermediate to 75, success. */
export const averageColor = (average: number, mode: ColorMode): string => rateColor(average < 0 ? null : average / 100, "average", mode);

interface DomainRow {
  label: string;
  key: string;
  status: WidgetOutcomeStatus;
  percentage: number;
  color: string;
}

interface DomainPart {
  label: string;
  icon?: string;
  key: string;
  color: string;
  rows: DomainRow[];
}

export interface DomainCard {
  id: string;
  label: string;
  icon?: string;
  hasData: boolean;
  /** Its successes among its resolved checks, a percentage, or -1. */
  score: number;
  color: string;
  parts: DomainPart[];
}

function domainPart(part: WidgetOutcomePart, mode: ColorMode): DomainPart {
  const rows = part.rows ?? [];
  const whole = part.total ?? rows.reduce((sum, row) => sum + row.value, 0);
  const colors = DOMAIN_COLORS[mode];
  return {
    label: part.label,
    icon: part.icon,
    key: part.key ?? part.label,
    color: averageColor(percentOf(part.success, part.success + part.failed), mode),
    rows: rows.map((row) => ({
      label: row.label,
      key: row.key ?? row.status,
      status: row.status,
      percentage: percentOf(row.value, whole),
      color: row.status === "success" ? colors.success : row.status === "failed" ? colors.failed : colors.pending,
    })),
  };
}

/** The cards: every listed domain in its order (the reference's, else the outcomes'), each with its outcomes or none. */
export function domainCards(data: WidgetOutcomesData, mode: ColorMode): DomainCard[] {
  const byLabelMap = new Map(data.items.map((item) => [item.label, item]));
  const listed = data.reference ?? data.items.map((item) => ({ id: item.id ?? item.label, label: item.label, icon: item.icon }));
  return listed.map((entry) => {
    const item = byLabelMap.get(entry.label);
    const parts = item?.parts ?? [];
    const hasData = parts.length > 0;
    const success = parts.reduce((sum, part) => sum + part.success, 0);
    const resolved = parts.reduce((sum, part) => sum + part.success + part.failed, 0);
    const score = percentOf(success, resolved);
    const placeholders = ("parts" in entry ? entry.parts : undefined) ?? [];
    return {
      id: entry.id,
      label: entry.label,
      icon: item?.icon ?? entry.icon,
      hasData,
      score,
      color: averageColor(score, mode),
      parts: hasData
        ? parts.map((part) => domainPart(part, mode))
        : placeholders.map((part) => ({ label: part.label, icon: part.icon, key: part.label, color: DOMAIN_COLORS[mode].empty, rows: [] })),
    };
  });
}

// ── Samples ─────────────────────────────────────────────────────────────────

/** What a view with no count yet shows, greyed: three expectation types' outcomes, as their buckets come, and one type's statuses. */
export const SAMPLE_OUTCOMES: WidgetOutcomesData = {
  items: [
    { label: "Prevention", key: "PREVENTION", success: 58, failed: 22, pending: 6, parts: [], color: "#0fbcff", successLabel: "prevented" },
    { label: "Detection", key: "DETECTION", success: 44, failed: 38, pending: 9, parts: [], color: "#0fbcff", successLabel: "detected" },
    { label: "Vulnerability", key: "VULNERABILITY", success: 23, failed: 17, pending: 4, parts: [], color: "#0fbcff", successLabel: "not vulnerable" },
  ],
};
/** The same as gates, in attack order: what is vulnerable, then what is prevented, then detected. */
export const SAMPLE_GATES: WidgetOutcomesData = { items: [SAMPLE_OUTCOMES.items[2], SAMPLE_OUTCOMES.items[0], SAMPLE_OUTCOMES.items[1]] };
export const SAMPLE_STATUSES: WidgetOutcomeCounts = { label: "", success: 64, failed: 27, pending: 9 };
