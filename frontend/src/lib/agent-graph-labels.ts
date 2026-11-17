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
 * Names and badges of the agent graph's nodes (Agentic Flow map, fleet views):
 * what a node's drawn name reads, and where a badge sits so that it never
 * covers that name.
 */
import { wrapLabel, type LabelBox, type Point } from "./agent-graph-layout";

const BRACKETED_PREFIX = /^\[([^\]]+)\]\s*(.+)$/u;

const squash = (s: string) => s.trim().replace(/\s+/gu, " ");
const sameWords = (a: string, b: string) => squash(a).toLocaleLowerCase() === squash(b).toLocaleLowerCase();

/** What a node of the agent graph draws as its name. */
export interface AgentLabel {
  /** The office a bracketed prefix names ("Digital Security Office"), else "". */
  prefix: string;
  /** The words after the prefix ("Orchestrator"), the whole name without one. */
  base: string;
  /** The drawn name: the prefix folded in without its brackets, "Digital Security Office Orchestrator". */
  label: string;
  /**
   * A member of the fleet being viewed, drawn without its office: wrapped on
   * two lines rather than cut, the role being all its name says.
   */
  own?: boolean;
}

/**
 * The name drawn under an agent's node: a
 * bracketed office prefix is folded in without its brackets, so
 * "[Digital Security Office] Orchestrator" reads "Digital Security Office
 * Orchestrator" and two offices' orchestrators never draw the same name, on
 * every page and layout. The stored name is never changed: it stays the
 * node's accessible name and tooltip. A name without a prefix is drawn whole.
 */
export function agentLabel(name: string): AgentLabel {
  const match = BRACKETED_PREFIX.exec(name);
  const prefix = match ? squash(match[1]) : "";
  const base = match ? squash(match[2]) : "";
  if (!prefix || !base) return { prefix: "", base: name, label: name };
  return { prefix, base, label: `${prefix} ${base}` };
}

/** The fleet an agent node belongs to, as the graph knows it. */
export interface NodeFleet {
  id: string;
  name?: string | null;
}

/**
 * The name of a node of the fleet being viewed, without its office: the
 * bracket prefix of the stored name ("[Digital Security Office] Assistant"
 * reads "Assistant"), else the fleet's name when the stored name starts with
 * it and a space ("CEO Office Analyst" in CEO Office reads "Analyst"); `null`
 * when the name carries neither, so nothing is shortened.
 */
function shortOwnName(name: string, fleetName: string | null | undefined): string | null {
  const parts = agentLabel(name);
  if (parts.prefix) return parts.base;
  const office = squash(fleetName ?? "");
  const whole = squash(name);
  if (!office || whole.length <= office.length + 1) return null;
  const head = whole.slice(0, office.length + 1);
  return sameWords(head, office) && whole[office.length] === " " ? whole.slice(office.length + 1) : null;
}

/**
 * The name drawn under one node of the graph. A node of the fleet the graph
 * shows (`viewedFleetId`) reads its name without the office, since every own
 * node would repeat it ("Assistant", "Compliance Orchestrator"); a node of
 * another fleet, a lone agent and every node of a graph with no viewed fleet
 * keep the full name `agentLabel` draws ("CEO Office Assistant"). The short
 * form gives way to the full one when it is in `taken` (the labels of the
 * other nodes), so no two nodes draw the same name. The stored name is never
 * changed: it stays the hover card and the search text.
 */
export function nodeLabel(
  name: string,
  fleet: NodeFleet | null | undefined,
  viewedFleetId: string | null | undefined,
  taken: ReadonlySet<string> = new Set(),
): AgentLabel {
  const full = agentLabel(name);
  if (!viewedFleetId || !fleet || fleet.id !== viewedFleetId) return full;
  const short = shortOwnName(name, fleet.name);
  if (!short || taken.has(short)) return full;
  return { prefix: "", base: short, label: short, own: true };
}

/**
 * The drawn names of every node of one graph, by node id, under the rule of
 * `nodeLabel`: own-fleet nodes short, the rest full, and a short name two
 * own nodes would share, or equal to another node's name, drawn full on each
 * of them. No two nodes draw the same name unless their full names already
 * match.
 */
export function nodeLabels(
  nodes: readonly { id: string; name: string; fleet?: NodeFleet | null }[],
  viewedFleetId: string | null | undefined,
): Map<string, AgentLabel> {
  const full = new Map(nodes.map((n) => [n.id, agentLabel(n.name).label]));
  const short = new Map<string, string>();
  if (viewedFleetId) {
    for (const n of nodes) {
      const s = n.fleet?.id === viewedFleetId ? shortOwnName(n.name, n.fleet.name) : null;
      if (s) short.set(n.id, s);
    }
  }
  // Drop every short name that meets another drawn name, until none does:
  // a short name dropped draws full, which may meet another short one.
  for (;;) {
    const counts = new Map<string, number>();
    for (const n of nodes) {
      const drawn = short.get(n.id) ?? full.get(n.id) ?? n.name;
      counts.set(drawn, (counts.get(drawn) ?? 0) + 1);
    }
    const clashing = [...short].filter(([, s]) => (counts.get(s) ?? 0) > 1).map(([id]) => id);
    if (clashing.length === 0) break;
    clashing.forEach((id) => short.delete(id));
  }
  // Every short name left is drawn by its node alone.
  return new Map(nodes.map((n) => [n.id, short.has(n.id) ? nodeLabel(n.name, n.fleet, viewedFleetId) : agentLabel(n.name)]));
}

/**
 * The drawn name on at most `maxLines` lines of `maxChars` characters:
 * one line when it fits; otherwise the office on the first lines (cut with an
 * ellipsis when it must be) and the role on the last, never a break inside
 * either part's reading ("Digital Security Office / Orchestrator", not
 * "Digital Security / Office Orchestrator"); a cut name still tells
 * "Digital Secu… / Orchestrator" from "Digital Secu… / Analyst" where a plain
 * cut would read "Digital Security…" for both.
 */
export function agentLabelLines(label: AgentLabel, maxChars: number, maxLines = 2): string[] {
  if (maxChars < 1 || maxLines < 1) return [];
  if ([...label.label].length <= maxChars) return [label.label];
  if (!label.prefix || maxLines < 2) return wrapLabel(label.label, maxChars, maxLines);
  const role = wrapLabel(label.base, maxChars, maxLines - 1);
  return [...wrapLabel(label.prefix, maxChars, maxLines - role.length), ...role];
}

/**
 * Each name without the leading words it shares with another name of the
 * list (#10147 UX r4 B1: five corporate fleet members start with "Lab sample
 * corporate" and, cut to two lines, read the same "Lab sample…"), so the
 * words that tell two nodes apart are the ones drawn. At least one word of
 * each name is kept, a name that shares no leading word stays whole, and a
 * name whose short form would equal another drawn name stays whole too.
 * Words compare case-insensitively and are kept as written.
 */
export function distinctLabels(labels: ReadonlyMap<string, string>): Map<string, string> {
  const split = [...labels].map(([id, label]) => [id, squash(label).split(" ").filter(Boolean)] as const);
  const lower = split.map(([, words]) => words.map((w) => w.toLocaleLowerCase()));
  const shortOf = new Map<string, string>();
  split.forEach(([id, words], i) => {
    let shared = 0;
    lower.forEach((other, j) => {
      if (j === i) return;
      let k = 0;
      while (k < words.length - 1 && k < other.length && lower[i][k] === other[k]) k++;
      shared = Math.max(shared, k);
    });
    shortOf.set(id, words.slice(shared).join(" "));
  });
  // A short form two nodes would draw, or equal to a whole name kept, gives
  // way to the whole name on each of them, until none does.
  for (;;) {
    const counts = new Map<string, number>();
    for (const [id] of split) {
      const drawn = (shortOf.get(id) ?? labels.get(id) ?? "").toLocaleLowerCase();
      counts.set(drawn, (counts.get(drawn) ?? 0) + 1);
    }
    const clashing = split
      .map(([id]) => id)
      .filter((id) => shortOf.get(id) !== squash(labels.get(id) ?? "") && (counts.get((shortOf.get(id) ?? "").toLocaleLowerCase()) ?? 0) > 1);
    if (clashing.length === 0) break;
    clashing.forEach((id) => shortOf.set(id, squash(labels.get(id) ?? "")));
  }
  return shortOf;
}

/**
 * The home group drawn under a ghost's name: left out when the drawn
 * name already says it (the agent's office prefix is that group), drawn
 * otherwise.
 */
export function ghostGroupLine(label: AgentLabel, group: string): string {
  return label.prefix && sameWords(label.prefix, group) ? "" : group;
}

/**
 * Whether an agent answers a search on the map: every word of the
 * query is found in its stored name, its drawn name or its fleet's name, so
 * "security orchestrator", "CEO Office" or "Analyst" all find their agents.
 */
export function agentMatchesQuery(query: string, name: string, fleetName?: string | null): boolean {
  const words = squash(query).toLocaleLowerCase().split(" ").filter(Boolean);
  if (words.length === 0) return true;
  const haystack = [name, agentLabel(name).label, fleetName ?? ""].join(" ").toLocaleLowerCase();
  return words.every((w) => haystack.includes(w));
}

/** Room left between a badge and the ring it stands off, in drawing units. */
export const BADGE_GAP = 1.5;

/** Least room on screen between a row under a node and the label above it, in px. */
export const ROW_GAP_PX = 2;

/**
 * Top of a row drawn under a node: `top`, or lower when one of
 * `avoid` (the node's name, its ghost line) reaches below it, a gap under
 * the lowest of them, so that the row never covers the node's own name.
 * The gap is BADGE_GAP drawing units, and never less than ROW_GAP_PX on
 * screen at `pxPerUnit`: a small zoom no longer lets the row touch
 * the name.
 */
export function topBelow(top: number, avoid: readonly LabelBox[], pxPerUnit?: number): number {
  const gap = pxPerUnit && pxPerUnit > 0 ? Math.max(BADGE_GAP, ROW_GAP_PX / pxPerUnit) : BADGE_GAP;
  return avoid.reduce((acc, q) => Math.max(acc, q.y + q.h / 2 + gap), top);
}

/** Below this, a direction component counts as zero (the ray runs along an axis). */
const AXIS_EPSILON = 1e-9;

/**
 * Distance along the ray `centre + d * (cos, sin)`, from `d` on, at which a
 * badge of half extents (`halfW`, `halfH`) leaves the box `q` widened by
 * BADGE_GAP on every side; `d` itself when the badge does not meet it.
 */
function clearanceAlong(
  centre: Point,
  cos: number,
  sin: number,
  d: number,
  halfW: number,
  halfH: number,
  q: LabelBox,
): number {
  const reachX = q.w / 2 + halfW + BADGE_GAP;
  const reachY = q.h / 2 + halfH + BADGE_GAP;
  const dx = centre.x + cos * d - q.x;
  const dy = centre.y + sin * d - q.y;
  if (Math.abs(dx) >= reachX || Math.abs(dy) >= reachY) return d;
  // Inside the widened box: the ray leaves it through the nearest far side.
  let exit = Number.POSITIVE_INFINITY;
  if (Math.abs(cos) > AXIS_EPSILON) exit = Math.min(exit, (Math.sign(cos) * reachX - dx) / cos);
  if (Math.abs(sin) > AXIS_EPSILON) exit = Math.min(exit, (Math.sign(sin) * reachY - dy) / sin);
  return Number.isFinite(exit) ? d + exit : d;
}

/**
 * Centre of a badge of half extents (`halfW`, `halfH`) laid along `angle`
 * (radians, y down) around a node at `centre`: clear of the outermost
 * ring drawn around the node (`ringRadius`, the focus ring included) by
 * BADGE_GAP, then moved further out along the same angle exactly as far as it
 * takes for its box to stay BADGE_GAP away from every box of `avoid` (the
 * node's name, its sub-label), however wide the name is at the current zoom.
 * A badge never sits on its own node's name.
 */
export function badgeCentre(
  centre: Point,
  ringRadius: number,
  angle: number,
  halfW: number,
  halfH: number,
  avoid: readonly LabelBox[] = [],
): Point {
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  let d = ringRadius + BADGE_GAP + Math.abs(cos) * halfW + Math.abs(sin) * halfH;
  // Leaving one box can enter another (the name, then the sub-label under
  // it): one pass per box settles every case, the distance only grows.
  for (let pass = 0; pass <= avoid.length; pass++) {
    const next = avoid.reduce((acc, q) => clearanceAlong(centre, cos, sin, acc, halfW, halfH, q), d);
    if (next === d) break;
    d = next;
  }
  return { x: centre.x + cos * d, y: centre.y + sin * d };
}
