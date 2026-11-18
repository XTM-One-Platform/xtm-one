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

// The STIX bundle review (#12282), its pure part: one entry's expected bundle against
// one or more answers, object by object, with the scorer's own rules (canonical ids,
// canonical triples). The backend groups each expected object with what names the same
// thing (a near-miss); this module derives every status, count and layout the page draws.

import type { BundleReview } from "@/types";

/** The source key of the expected bundle. */
export const EXPECTED = "expected";
/** The reference of a review that has none: the answer alone. */
export const NONE = "none";

/**
 * How an answer is named: by its evaluation when the answers come from several, else by
 * its agent, which is then what tells them apart. `detail` adds what the title leaves out.
 * `model` formats a model id.
 */
export function answerName(
  answers: ReadonlyArray<Pick<BundleReview["answers"][number], "evaluation_id" | "evaluation_name" | "subject_name" | "model_id">>,
  answer: Pick<BundleReview["answers"][number], "evaluation_name" | "subject_name" | "model_id">,
  model: (id: string) => string,
): { title: string; detail: string } {
  const oneEvaluation = new Set(answers.map((a) => a.evaluation_id)).size === 1;
  if (oneEvaluation && answer.subject_name) return { title: answer.subject_name, detail: model(answer.model_id) };
  return { title: answer.evaluation_name, detail: [answer.subject_name, model(answer.model_id)].filter(Boolean).join(" · ") };
}

export interface StixObject {
  name: string;
  cid: string;
  type: string;
  aliases: string[];
  ext: string | null;
  hashes: Record<string, string> | null;
}

/** One object, whatever name, type or canonical id each source gave it. */
export interface ObjectGroup {
  id: string;
  type: string;
  /** The object each source gave this group (the expected one's own id when the source has it). */
  by: Record<string, StixObject>;
  /** Every object each source gave this group. */
  all: Record<string, StixObject[]>;
  /** Objects a source gave this group past the first: each counts as spurious. */
  extra: Record<string, number>;
}

export interface RelationshipRow {
  id: string;
  src: string;
  tgt: string;
  /** The relationship type each source wrote. */
  kinds: Record<string, string>;
  /** The real endpoint objects each source used. */
  ends: Record<string, [StixObject, StixObject]>;
  /** Duplicates of this row in a source: each counts as spurious. */
  dup: Record<string, number>;
}

export interface ReviewModel {
  groups: ObjectGroup[];
  rels: RelationshipRow[];
  /** The object types present, in reading order. */
  types: string[];
  byId: Map<string, ObjectGroup>;
  relById: Map<string, RelationshipRow>;
}

const TYPE_RANK = [
  "report", "campaign", "threat-actor", "intrusion-set", "malware", "tool", "software", "attack-pattern",
  "vulnerability", "indicator", "identity", "location", "domain-name", "ipv4-addr", "ipv6-addr", "url", "file",
];
const typeRank = (t: string) => {
  const i = TYPE_RANK.indexOf(t);
  return i < 0 ? 99 : i;
};

/** The groups and relationship rows of a review, built once per answer. */
export function buildModel(review: Pick<BundleReview, "objects" | "groups" | "sources" | "relationships">): ReviewModel {
  const objs: StixObject[] = review.objects.map((o) => ({
    name: o.n,
    cid: o.c,
    type: o.t,
    aliases: o.a ?? [],
    ext: o.x ?? null,
    hashes: o.h ?? null,
  }));
  const count = review.groups.length ? Math.max(...review.groups) + 1 : 0;
  const slots: ObjectGroup[] = Array.from({ length: count }, (_, k) => ({ id: `g${k}`, type: "", by: {}, all: {}, extra: {} }));
  // The expected bundle first: an answer's object with the expected canonical id wins its group.
  const order = Object.keys(review.sources).sort((x, y) => (x === EXPECTED ? -1 : y === EXPECTED ? 1 : 0));
  for (const s of order) {
    for (const oi of review.sources[s]) {
      const g = slots[review.groups[oi]];
      const ob = objs[oi];
      if (!g || !ob) continue;
      (g.all[s] ??= []).push(ob);
      if (!g.by[s]) g.by[s] = ob;
      else {
        g.extra[s] = (g.extra[s] ?? 0) + 1;
        const exp = g.by[EXPECTED];
        if (exp && s !== EXPECTED && ob.cid === exp.cid) g.by[s] = ob;
      }
    }
  }
  const groups = slots.filter((g) => Object.keys(g.all).length > 0);
  for (const g of groups) g.type = (g.by[EXPECTED] ?? Object.values(g.all)[0][0]).type;
  const rels: RelationshipRow[] = review.relationships.map(([a, b, cells], k) => {
    const r: RelationshipRow = { id: `r${k}`, src: `g${a}`, tgt: `g${b}`, kinds: {}, ends: {}, dup: {} };
    for (const [s, [kind, si, ti, dup]] of Object.entries(cells)) {
      r.kinds[s] = kind;
      r.ends[s] = [objs[si], objs[ti]];
      if (dup) r.dup[s] = dup;
    }
    return r;
  });
  const types = [...new Set(groups.map((g) => g.type))].sort((x, y) => typeRank(x) - typeRank(y) || x.localeCompare(y));
  return {
    groups,
    rels,
    types,
    byId: new Map(groups.map((g) => [g.id, g])),
    relById: new Map(rels.map((r) => [r.id, r])),
  };
}

// ─── Comparison (the scorer's rules: canonical ids, canonical triples) ─────────

export type EntityStatus = "tp" | "fn" | "fp" | "near";
export type RelationshipStatus = EntityStatus | "cascade" | "type";
export type Verdict = "same" | "fixed" | "regressed" | "new" | "still";
/** What a row shows: a status, a verdict between two answers, or a near-miss the what-if counts. */
export type DisplayStatus = RelationshipStatus | Verdict | "sim";
export type ReviewMode = "score" | "alone" | "runs";

/** The reviewed answer (`under`) and what it is compared with (`ref`). */
export interface ReviewPair {
  ref: string;
  under: string;
}

export function modeOf(pair: ReviewPair): ReviewMode {
  return pair.ref === NONE ? "alone" : pair.ref === EXPECTED ? "score" : "runs";
}

export function compareEntity(g: ObjectGroup, x: string, y: string): EntityStatus | null {
  const a = g.by[x];
  const b = g.by[y];
  if (a && b) return a.cid === b.cid ? "tp" : "near";
  return a ? "fn" : b ? "fp" : null;
}

export interface RelationshipComparison {
  st: RelationshipStatus;
  /** The group whose absence or other canonical id explains the status. */
  cause?: string;
}

export function compareRelationship(model: ReviewModel, r: RelationshipRow, x: string, y: string): RelationshipComparison | null {
  const ta = r.kinds[x];
  const tb = r.kinds[y];
  if (ta && tb) {
    // On the real endpoint objects each source used, as the scorer's triples are.
    const [xa, xb] = r.ends[x];
    const [ya, yb] = r.ends[y];
    if (xa.cid !== ya.cid) return { st: "cascade", cause: r.src };
    if (xb.cid !== yb.cid) return { st: "cascade", cause: r.tgt };
    return { st: ta === tb ? "tp" : "type" };
  }
  const ends = [model.byId.get(r.src), model.byId.get(r.tgt)];
  if (ta) return { st: "fn", cause: ends.find((g) => g && !g.by[y])?.id };
  if (tb) return { st: "fp", cause: ends.find((g) => g && !g.by[x])?.id };
  return null;
}

function entityOk(g: ObjectGroup, s: string, whatIf: boolean): boolean {
  const st = compareEntity(g, EXPECTED, s);
  return st === "tp" || (whatIf && st === "near");
}

export function relationshipOk(model: ReviewModel, r: RelationshipRow, s: string, whatIf: boolean): boolean {
  const c = compareRelationship(model, r, EXPECTED, s);
  return !!c && (c.st === "tp" || (whatIf && c.st === "cascade"));
}

/** From answer A to answer B, each judged against the expected bundle. */
function verdict(inA: boolean, inB: boolean, okA: boolean, okB: boolean, expected: boolean): Verdict | null {
  if (!inA && !inB) return null;
  if (expected && !okA && !okB) return "still";
  if (inA && inB) return okA && okB ? "same" : okB ? "fixed" : okA ? "regressed" : "still";
  if (inA) return okA ? "regressed" : "fixed";
  return okB ? "fixed" : "new";
}

export function entityVerdict(g: ObjectGroup, a: string, b: string, whatIf: boolean): Verdict | null {
  return verdict(!!g.by[a], !!g.by[b], entityOk(g, a, whatIf), entityOk(g, b, whatIf), !!g.by[EXPECTED]);
}

function relationshipVerdict(model: ReviewModel, r: RelationshipRow, a: string, b: string, whatIf: boolean): Verdict | null {
  return verdict(!!r.kinds[a], !!r.kinds[b], relationshipOk(model, r, a, whatIf), relationshipOk(model, r, b, whatIf), !!r.kinds[EXPECTED]);
}

// ─── Counts ─────────────────────────────────────────────────────────────────

export interface Counts {
  tp: number;
  fp: number;
  fn: number;
}

function add(c: Counts, st: RelationshipStatus, sim: boolean) {
  if (st === "tp" || (sim && (st === "near" || st === "cascade"))) c.tp++;
  else if (st === "fn") c.fn++;
  else if (st === "fp") c.fp++;
  else {
    // Another canonical id or another type: one missing plus one spurious.
    c.fn++;
    c.fp++;
  }
}

export function f1(c: Counts): number {
  const d = 2 * c.tp + c.fp + c.fn;
  return d === 0 ? 100 : (200 * c.tp) / d;
}

export function entityCounts(model: ReviewModel, s: string, sim: boolean, list: ObjectGroup[] = model.groups): Counts {
  const c: Counts = { tp: 0, fp: 0, fn: 0 };
  for (const g of list) {
    const st = compareEntity(g, EXPECTED, s);
    if (st) add(c, st, sim);
    c.fp += g.extra[s] ?? 0;
  }
  return c;
}

export function relationshipCounts(model: ReviewModel, s: string, sim: boolean): Counts {
  const c: Counts = { tp: 0, fp: 0, fn: 0 };
  for (const r of model.rels) {
    const x = compareRelationship(model, r, EXPECTED, s);
    if (x) add(c, x.st, sim);
    c.fp += r.dup[s] ?? 0;
  }
  return c;
}

export function typeCounts(model: ReviewModel, s: string, sim: boolean, type: string): Counts {
  return entityCounts(model, s, sim, model.groups.filter((g) => g.type === type));
}

// ─── The pair on screen ───────────────────────────────────────────────────────

export interface EntityItem {
  g: ObjectGroup;
  /** The reference's object (none when the answer is reviewed alone). */
  a: StixObject | null;
  /** The reviewed answer's object. */
  b: StixObject | null;
  st: EntityStatus | Verdict;
}

export interface RelationshipItem {
  r: RelationshipRow;
  inA: boolean;
  inB: boolean;
  st: RelationshipStatus | Verdict;
  /** The status between the two compared sources. */
  pairSt: RelationshipStatus;
  cause?: string;
}

export interface Derived {
  ents: Map<string, EntityItem>;
  rels: Map<string, RelationshipItem>;
  /** Entity items in reading order (type, then group). */
  entItems: EntityItem[];
  relItems: RelationshipItem[];
}

/** One item per object and relationship present on either side of the pair. */
export function derive(model: ReviewModel, pair: ReviewPair, whatIf: boolean): Derived {
  const mode = modeOf(pair);
  const { ref: A, under: B } = pair;
  const ents = new Map<string, EntityItem>();
  const rels = new Map<string, RelationshipItem>();
  for (const g of model.groups) {
    const a = mode === "alone" ? null : g.by[A] ?? null;
    const b = g.by[B] ?? null;
    if (!a && !b) continue;
    const st = mode === "runs" ? entityVerdict(g, A, B, whatIf) : mode === "alone" ? compareEntity(g, EXPECTED, B) : compareEntity(g, A, B);
    if (st) ents.set(g.id, { g, a, b, st });
  }
  for (const r of model.rels) {
    const inA = mode !== "alone" && !!r.kinds[A];
    const inB = !!r.kinds[B];
    if (!inA && !inB) continue;
    if (mode === "runs") {
      const c = compareRelationship(model, r, A, B);
      const v = relationshipVerdict(model, r, A, B, whatIf);
      if (c && v) rels.set(r.id, { r, inA, inB, st: v, pairSt: c.st, cause: c.cause });
    } else {
      const c = compareRelationship(model, r, EXPECTED, B);
      if (c) rels.set(r.id, { r, inA, inB, st: c.st, pairSt: c.st, cause: c.cause });
    }
  }
  const entItems = model.types.flatMap((t) => model.groups.filter((g) => g.type === t && ents.has(g.id)).map((g) => ents.get(g.id) as EntityItem));
  const relItems = model.rels.filter((r) => rels.has(r.id)).map((r) => rels.get(r.id) as RelationshipItem);
  return { ents, rels, entItems, relItems };
}

/** What a row shows: the what-if turns a near-miss (and the relationships it broke) into a match. */
export function displayStatus(st: RelationshipStatus | Verdict, mode: ReviewMode, whatIf: boolean): DisplayStatus {
  if (mode === "runs") return st;
  return whatIf && (st === "near" || st === "cascade") ? "sim" : st;
}

/** The status filters each mode offers. */
export const FILTER_KEYS: Record<ReviewMode, string[]> = {
  score: ["tp", "fn", "fp", "near"],
  alone: ["tp", "fp", "near"],
  runs: ["fixed", "regressed", "new", "still", "same"],
};

/** The filter a status belongs to. */
export function filterKey(st: string, mode: ReviewMode): string {
  if (mode === "runs") return st;
  return ["near", "cascade", "type", "sim"].includes(st) ? "near" : st;
}

export interface ReviewFilters {
  type: string | null;
  hidden: ReadonlySet<string>;
}

export function entityVisible(it: EntityItem | undefined, f: ReviewFilters, mode: ReviewMode): boolean {
  return !!it && (!f.type || it.g.type === f.type) && !f.hidden.has(filterKey(it.st, mode));
}

export function relationshipVisible(model: ReviewModel, it: RelationshipItem | undefined, f: ReviewFilters, mode: ReviewMode): boolean {
  if (!it) return false;
  const typeOk = !f.type || model.byId.get(it.r.src)?.type === f.type || model.byId.get(it.r.tgt)?.type === f.type;
  return typeOk && !f.hidden.has(filterKey(it.st, mode));
}

// ─── Why two names of one group are not the same canonical id ────────────────

export type NearKind = "type" | "same-name" | "attack-id" | "defanged" | "alias" | "case" | "length" | "wording";

function refang(s: string): string {
  return String(s).replace(/\[\.\]|\(\.\)/g, ".").replace(/hxxp/gi, "http").replace(/\[:\]/g, ":");
}

const nameKey = (s: string) => refang(s).toLowerCase().replace(/[^a-z0-9]/g, "");

export function nearKind(a: StixObject, b: StixObject): NearKind {
  if (a.type !== b.type) return "type";
  if (a.name === b.name) return "same-name";
  if (a.ext && a.ext === b.ext) return "attack-id";
  if (refang(a.name) === refang(b.name)) return "defanged";
  const na = nameKey(a.name);
  const nb = nameKey(b.name);
  if ([...a.aliases, ...b.aliases].some((x) => nameKey(x) === na || nameKey(x) === nb)) return "alias";
  if (na === nb) return "case";
  if (na.includes(nb) || nb.includes(na)) return "length";
  return "wording";
}

// ─── Text helpers ──────────────────────────────────────────────────────────

/** The name a source gave a group, else the expected one, else any. */
export function endName(g: ObjectGroup, s: string): string {
  return (g.by[s] ?? g.by[EXPECTED] ?? Object.values(g.all)[0][0]).name;
}

/** A source's relationship as `source → type → target`, or null when it has none. */
export function tripleText(r: RelationshipRow, s: string): string | null {
  const kind = r.kinds[s];
  if (!kind) return null;
  return `${r.ends[s][0].name} → ${kind} → ${r.ends[s][1].name}`;
}

/** The common prefix and suffix of two strings, and what differs between them. */
export function wordDiff(a: string, b: string): { a: [string, string, string]; b: [string, string, string] } {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  let j = 0;
  while (j < a.length - i && j < b.length - i && a[a.length - 1 - j] === b[b.length - 1 - j]) j++;
  const cut = (s: string): [string, string, string] => [s.slice(0, i), s.slice(i, s.length - j), s.slice(s.length - j)];
  return { a: cut(a), b: cut(b) };
}

/** A 0-100 score with one decimal; a dash when there is none. */
export function formatScore(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? "—" : v.toFixed(1);
}

// ─── Mentions in the report text ─────────────────────────────────────────────

export type MentionHow = "name" | "alias" | "id" | "hash" | "defang" | "plural";

export interface Mention {
  text: string;
  gid: string;
  how: MentionHow;
  /** The sources whose object carries the matched text. */
  srcs: string[];
}

type PageSegment = string | Mention;

export interface LocatedText {
  pages: PageSegment[][];
  /** Each group's mentions, with their page (1-based). */
  mentions: Map<string, { page: number; how: MentionHow }[]>;
}

const isWord = (ch: string | undefined) => !!ch && /[\p{L}\p{N}_]/u.test(ch);
/** At most this many mentions of one name per page. */
const MAX_HITS = 8;

interface Candidate {
  text: string;
  gid: string;
  srcs: Set<string>;
  how: MentionHow;
  exp: boolean;
}

/** Each object's mentions in the text: name, alias, ATT&CK id, hash, refanged or defanged value. */
export function locateMentions(model: ReviewModel, pages: string[]): LocatedText {
  const byKey = new Map<string, Candidate>();
  const cands: Candidate[] = [];
  for (const g of model.groups) {
    for (const [s, list] of Object.entries(g.all)) {
      for (const ob of list) {
        const addC = (t: string, how: MentionHow) => {
          if (!t || t.length < 3) return;
          const k = `${g.id}|${t.toLowerCase()}`;
          let c = byKey.get(k);
          if (!c) {
            c = { text: t, gid: g.id, srcs: new Set(), how, exp: !!g.by[EXPECTED] };
            byKey.set(k, c);
            cands.push(c);
          }
          c.srcs.add(s);
        };
        addC(ob.name, "name");
        ob.aliases.forEach((a) => addC(a, "alias"));
        if (ob.ext) addC(ob.ext, "id");
        if (ob.hashes) Object.values(ob.hashes).forEach((h) => addC(String(h), "hash"));
        if (refang(ob.name) !== ob.name) addC(refang(ob.name), "defang");
        if (/domain|url|addr/.test(ob.type) && ob.name.includes(".")) addC(ob.name.replace(/\./g, "[.]"), "defang");
      }
    }
  }
  // Longest first, so "APT29 Group" wins over "APT29"; the expected object first on a tie.
  const order = [...cands].sort((x, y) => y.text.length - x.text.length || Number(y.exp) - Number(x.exp));
  const mentions = new Map<string, { page: number; how: MentionHow }[]>();
  const located = pages.map((text, pi) => {
    const low = text.toLowerCase();
    const taken: { start: number; end: number; c: Candidate; how: MentionHow }[] = [];
    for (const c of order) {
      const needle = c.text.toLowerCase();
      let from = 0;
      let hits = 0;
      while (hits < MAX_HITS) {
        const at = low.indexOf(needle, from);
        if (at < 0) break;
        from = at + 1;
        let end = at + needle.length;
        let how = c.how;
        if (isWord(text[at - 1])) continue;
        if (isWord(text[end])) {
          if ((text[end] === "s" || text[end] === "S") && !isWord(text[end + 1])) {
            end++;
            how = "plural";
          } else continue;
        }
        if (taken.some((t) => at < t.end && end > t.start)) continue;
        taken.push({ start: at, end, c, how });
        hits++;
      }
    }
    taken.sort((x, y) => x.start - y.start);
    const out: PageSegment[] = [];
    let last = 0;
    for (const t of taken) {
      if (t.start > last) out.push(text.slice(last, t.start));
      out.push({ text: text.slice(t.start, t.end), gid: t.c.gid, how: t.how, srcs: [...t.c.srcs] });
      last = t.end;
      const list = mentions.get(t.c.gid) ?? [];
      list.push({ page: pi + 1, how: t.how });
      mentions.set(t.c.gid, list);
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  });
  return { pages: located, mentions };
}

// ─── Layouts ─────────────────────────────────────────────────────────────────

export interface Point {
  x: number;
  y: number;
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The layered graph: report and campaign, who, how, then targets, software and infrastructure. */
const GRAPH_COLUMN: Record<string, number> = {
  report: 0, campaign: 0, "threat-actor": 1, "intrusion-set": 1, malware: 2, tool: 2, "attack-pattern": 2, indicator: 2,
};
export const GRAPH_X = [85, 292, 512, 745];
export const GRAPH_WIDTH = 880;
export const GRAPH_NODE = { w: 162, h: 48 };

export function graphLayout(ids: { id: string; type: string }[]): { pos: Map<string, Point>; height: number } {
  const cols: string[][] = [[], [], [], []];
  for (const g of ids) cols[GRAPH_COLUMN[g.type] ?? 3].push(g.id);
  const height = Math.max(560, Math.max(...cols.map((c) => c.length)) * 58 + 80);
  const pos = new Map<string, Point>();
  cols.forEach((list, ci) => {
    const gap = Math.min(70, (height - 80) / Math.max(1, list.length - 1));
    const y0 = height / 2 - ((list.length - 1) * gap) / 2 + 12;
    list.forEach((id, i) => pos.set(id, { x: GRAPH_X[ci], y: y0 + i * gap }));
  });
  return { pos, height };
}

/**
 * A force layout of every group of the entry, computed once per entry: repulsion between
 * every pair, springs along relationships, a pull to the centre, cooling steps. The number
 * of iterations shrinks as the graph grows, so ~300 objects stay well under a frame budget
 * of a few dozen milliseconds.
 */
export function forceLayout(model: ReviewModel): Map<string, Point> {
  const n = model.groups.length;
  const idx = new Map(model.groups.map((g, i) => [g.id, i]));
  const pts = model.groups.map((_, i) => ({ x: Math.cos(i * 2.39996) * (60 + i * 14), y: Math.sin(i * 2.39996) * (60 + i * 14), vx: 0, vy: 0 }));
  const links: [number, number][] = [];
  for (const r of model.rels) {
    const a = idx.get(r.src);
    const b = idx.get(r.tgt);
    if (a != null && b != null && a !== b) links.push([a, b]);
  }
  const iterations = n > 200 ? 160 : n > 100 ? 260 : 400;
  for (let it = 0; it < iterations; it++) {
    const cool = 1 - it / iterations;
    for (let i = 0; i < n; i++) {
      const p = pts[i];
      for (let j = i + 1; j < n; j++) {
        const q = pts[j];
        let dx = p.x - q.x;
        let dy = p.y - q.y;
        const d2 = Math.max(dx * dx + dy * dy, 25);
        const f = 14000 / d2;
        const d = Math.sqrt(d2);
        dx /= d;
        dy /= d;
        p.vx += dx * f;
        p.vy += dy * f;
        q.vx -= dx * f;
        q.vy -= dy * f;
      }
    }
    for (const [a, b] of links) {
      const dx = pts[b].x - pts[a].x;
      const dy = pts[b].y - pts[a].y;
      const d = Math.max(Math.hypot(dx, dy), 1);
      const f = (d - 150) * 0.05;
      pts[a].vx += (dx / d) * f;
      pts[a].vy += (dy / d) * f;
      pts[b].vx -= (dx / d) * f;
      pts[b].vy -= (dy / d) * f;
    }
    for (const p of pts) {
      p.vx -= p.x * 0.012;
      p.vy -= p.y * 0.012;
      const sp = Math.hypot(p.vx, p.vy) || 1;
      const step = Math.min(sp, 30 * cool + 1);
      p.x += (p.vx / sp) * step;
      p.y += (p.vy / sp) * step;
      p.vx *= 0.5;
      p.vy *= 0.5;
    }
  }
  return new Map(model.groups.map((g, i) => [g.id, { x: pts[i].x, y: pts[i].y }]));
}

/** The view box around these points, with room for the labels under each node. */
export function fitBox(points: Point[]): Box {
  if (!points.length) return { x: -200, y: -150, w: 400, h: 300 };
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const x0 = Math.min(...xs) - 90;
  const y0 = Math.min(...ys) - 60;
  return { x: x0, y: y0, w: Math.max(...xs) - x0 + 90, h: Math.max(...ys) - y0 + 80 };
}

/** The box zoomed by `factor` around a point (its centre by default). */
export function zoomBox(box: Box, factor: number, cx = box.x + box.w / 2, cy = box.y + box.h / 2): Box {
  return { x: cx - (cx - box.x) * factor, y: cy - (cy - box.y) * factor, w: box.w * factor, h: box.h * factor };
}
