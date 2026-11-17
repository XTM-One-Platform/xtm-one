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
 * Three-way merge of a deck draft with a revision saved elsewhere.
 *
 * The editor autosaves, so two people (or a person and an agent) editing one
 * deck write revisions a few seconds apart. A save is conditional on the
 * revision it extends (`expected_revision`), and the session that finds a
 * newer revision in the query MERGES it instead of asking the person to
 * reload and lose their work: `base` is the revision the draft was seeded
 * from, `ours` the draft, `theirs` the revision that arrived.
 *
 *   - What only one side changed is taken from that side.
 *   - Slides are matched by their STABLE id (assigned by the server, minted by
 *     the editor for a slide it inserts): an insert, a delete, a move and an
 *     edit on either side all survive. A slide one side deleted and the other
 *     edited STAYS, edited - an edit is work, a deletion is cheap to redo -
 *     and is reported: a conflict when ours edited it, `restored` when theirs
 *     did. The order follows the side that reordered.
 *   - Inside a slide, objects merge key by key; a list (blocks, items, rows)
 *     is aligned with a diff3 over its elements, so a block added on one side
 *     and a word typed in another block on the other side both land, and a
 *     block both sides edited merges field by field.
 *   - Where both changed the same thing differently, OURS is kept: the local
 *     draft is about to be saved after theirs, so it is the last writer, and
 *     the person is told (`MergeConflict`). A list whose two editions cannot be
 *     aligned without guessing keeps ours whole, never a mix of the two -
 *     a guess could duplicate or drop a block.
 *
 * Copy-on-write like every draft update: what comes out unchanged is the very
 * object that went in (the side it came from), so the deck's React keys and
 * memoised slides survive a merge and the next merge compares by reference.
 */
import type { PresentationSlide } from "@/types";

import { canonicalOf } from "./canonical-json.ts";
import { getAt, parsePath } from "./edit-paths.ts";
import type { DeckDraft } from "./presentation-editor.ts";

/** Something both sides changed differently: the local edition was kept. */
export interface MergeConflict {
  /** The slide it is on (its stable id), `null` for a deck setting. */
  slideId: string | null;
  /** Where, as a spec path inside the slide (or the deck field). */
  path: string;
}

interface DraftMerge {
  draft: DeckDraft;
  conflicts: MergeConflict[];
  /** The slides ours deleted and theirs edited: back in the deck, with their edit. */
  restored: string[];
}

/**
 * A list longer than this on both sides (elements x elements) is not aligned:
 * the alignment is quadratic, and a list that long changed on both sides at
 * once is no edit a person makes by hand. Ours is kept, as a conflict.
 */
const MAX_ALIGNMENT_CELLS = 250_000;

interface MergeContext {
  conflicts: MergeConflict[];
  restored: string[];
  slideId: string | null;
}

type Json = unknown;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

function same(a: Json, b: Json): boolean {
  if (a === b) return true;
  if (a === null || b === null || a === undefined || b === undefined) return false;
  if (typeof a !== "object" || typeof b !== "object") return false;
  return canonicalOf(a) === canonicalOf(b);
}

const joinPath = (path: string, key: string | number): string => (path ? `${path}.${key}` : String(key));

function conflict(ctx: MergeContext, path: string): void {
  ctx.conflicts.push({ slideId: ctx.slideId, path });
}

/** The merge of one value: what changed on one side wins, both sides recurse, a clash keeps ours. */
function mergeValue(base: Json, ours: Json, theirs: Json, path: string, ctx: MergeContext): Json {
  if (ours === theirs || same(ours, theirs)) return ours;
  if (same(base, theirs)) return ours;
  if (same(base, ours)) return theirs;
  if (isRecord(base) && isRecord(ours) && isRecord(theirs)) return mergeRecord(base, ours, theirs, path, ctx);
  if (Array.isArray(base) && Array.isArray(ours) && Array.isArray(theirs)) {
    return mergeList(base, ours, theirs, path, ctx);
  }
  conflict(ctx, path);
  return ours;
}

function mergeRecord(
  base: Record<string, unknown>,
  ours: Record<string, unknown>,
  theirs: Record<string, unknown>,
  path: string,
  ctx: MergeContext,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  let allOurs = true;
  let allTheirs = true;
  const keys = new Set([...Object.keys(ours), ...Object.keys(theirs), ...Object.keys(base)]);
  for (const key of keys) {
    const value = mergeValue(base[key], ours[key], theirs[key], joinPath(path, key), ctx);
    if (value !== ours[key]) allOurs = false;
    if (value !== theirs[key]) allTheirs = false;
    if (value !== undefined) out[key] = value;
  }
  if (allOurs) return ours;
  if (allTheirs) return theirs;
  return out;
}

// ── Lists: diff3 over the elements ──────────────────────────────────────────

/** A run of `base[baseStart, baseEnd)` one side replaced with `side[sideStart, sideEnd)`. */
interface Hunk {
  side: "ours" | "theirs";
  baseStart: number;
  baseEnd: number;
  sideStart: number;
  sideEnd: number;
}

/** One longest common subsequence of two key lists, as matched index pairs. */
function commonPairs(a: readonly string[], b: readonly string[]): [number, number][] {
  const width = b.length + 1;
  const table = new Int32Array((a.length + 1) * width);
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        a[i] === b[j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  const pairs: [number, number][] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      pairs.push([i, j]);
      i += 1;
      j += 1;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) i += 1;
    else j += 1;
  }
  return pairs;
}

function hunksOf(pairs: readonly [number, number][], baseLength: number, sideLength: number, side: Hunk["side"]): Hunk[] {
  const hunks: Hunk[] = [];
  let baseAt = 0;
  let sideAt = 0;
  for (const [b, s] of [...pairs, [baseLength, sideLength] as [number, number]]) {
    if (b > baseAt || s > sideAt) hunks.push({ side, baseStart: baseAt, baseEnd: b, sideStart: sideAt, sideEnd: s });
    baseAt = b + 1;
    sideAt = s + 1;
  }
  return hunks;
}

/** Whether two lists of the same length can be merged position by position: the same kind of thing at each place. */
function correspond(base: readonly Json[], ours: readonly Json[], theirs: readonly Json[]): boolean {
  if (base.length !== ours.length || base.length !== theirs.length) return false;
  return base.every((item, index) => {
    const mine = ours[index];
    const other = theirs[index];
    if (isRecord(item) && isRecord(mine) && isRecord(other)) return item.type === mine.type && item.type === other.type;
    return !isRecord(item) && !isRecord(mine) && !isRecord(other);
  });
}

function mergeList(base: Json[], ours: Json[], theirs: Json[], path: string, ctx: MergeContext): Json[] {
  if (base.length * Math.max(ours.length, theirs.length) > MAX_ALIGNMENT_CELLS) {
    conflict(ctx, path);
    return ours;
  }
  const baseKeys = base.map(canonicalOf);
  const oursKeys = ours.map(canonicalOf);
  const theirsKeys = theirs.map(canonicalOf);
  const hunks = [
    ...hunksOf(commonPairs(baseKeys, oursKeys), base.length, ours.length, "ours"),
    ...hunksOf(commonPairs(baseKeys, theirsKeys), base.length, theirs.length, "theirs"),
  ].sort((x, y) => x.baseStart - y.baseStart || x.baseEnd - x.baseStart - (y.baseEnd - y.baseStart));

  // Outside its hunks a side is aligned with the base element for element:
  // the offset between the two is what its last hunk added or removed.
  const sideIndex = (side: Hunk["side"], baseIndex: number, before: readonly Hunk[]): number => {
    let offset = 0;
    for (const hunk of before) {
      if (hunk.side === side && hunk.baseEnd <= baseIndex) offset = hunk.sideEnd - hunk.baseEnd;
    }
    return baseIndex + offset;
  };

  const out: Json[] = [];
  let cursor = 0;
  const seen: Hunk[] = [];
  const copyStable = (until: number) => {
    // Identical in all three editions (aligned by content): ours' element, its identity kept.
    for (let at = cursor; at < until; at += 1) out.push(ours[sideIndex("ours", at, seen)]);
    cursor = Math.max(cursor, until);
  };
  const sideOf = (name: Hunk["side"]) => (name === "ours" ? ours : theirs);

  let next = 0;
  while (next < hunks.length) {
    const first = hunks[next];
    const group = [first];
    let regionStart = first.baseStart;
    let regionEnd = first.baseEnd;
    next += 1;
    // A hunk starting INSIDE the region overlaps it; one starting at its end
    // (an insertion right after it) or at its start when it is itself an
    // insertion is a neighbour, applied in turn.
    while (next < hunks.length && hunks[next].baseStart < regionEnd) {
      regionEnd = Math.max(regionEnd, hunks[next].baseEnd);
      group.push(hunks[next]);
      next += 1;
    }
    regionStart = Math.min(...group.map((hunk) => hunk.baseStart));
    copyStable(regionStart);

    const sides = new Set(group.map((hunk) => hunk.side));
    if (sides.size === 1) {
      const only = group[0];
      // Two insertions at one place with the same content are one insertion.
      const previous = seen[seen.length - 1];
      const duplicate =
        previous &&
        previous.side !== only.side &&
        previous.baseStart === only.baseStart &&
        previous.baseEnd === only.baseEnd &&
        only.baseStart === only.baseEnd &&
        same(
          sideOf(previous.side).slice(previous.sideStart, previous.sideEnd),
          sideOf(only.side).slice(only.sideStart, only.sideEnd),
        );
      if (!duplicate) out.push(...sideOf(only.side).slice(only.sideStart, only.sideEnd));
    } else {
      const slice = (name: Hunk["side"]) => {
        const mine = group.filter((hunk) => hunk.side === name);
        const start = Math.min(...mine.map((hunk) => hunk.sideStart)) + (regionStart - Math.min(...mine.map((hunk) => hunk.baseStart)));
        const end = Math.max(...mine.map((hunk) => hunk.sideEnd)) + (regionEnd - Math.max(...mine.map((hunk) => hunk.baseEnd)));
        return sideOf(name).slice(start, end);
      };
      const mine = slice("ours");
      const other = slice("theirs");
      const was = base.slice(regionStart, regionEnd);
      if (same(mine, other)) out.push(...mine);
      else if (correspond(was, mine, other)) {
        mine.forEach((item, index) => out.push(mergeValue(was[index], item, other[index], joinPath(path, regionStart + index), ctx)));
      } else {
        // Two editions of one stretch that cannot be aligned without a guess:
        // ours, whole, rather than a mix that could repeat or drop an element.
        conflict(ctx, path);
        return ours;
      }
    }
    seen.push(...group);
    cursor = regionEnd;
  }
  copyStable(base.length);
  return out.length === ours.length && out.every((item, index) => item === ours[index]) ? ours : out;
}

// ── Slides: matched by their stable id ──────────────────────────────────────

/** The slides' ids, or `null` when one has none or two share one (an older deck): merged by content then. */
function slideIds(slides: readonly PresentationSlide[]): string[] | null {
  const ids = slides.map((slide) => slide.id);
  if (ids.some((id) => typeof id !== "string" || id === "")) return null;
  return new Set(ids).size === ids.length ? (ids as string[]) : null;
}

const sameOrder = (a: readonly string[], b: readonly string[]): boolean =>
  a.length === b.length && a.every((id, index) => id === b[index]);

function mergeSlides(
  base: PresentationSlide[],
  ours: PresentationSlide[],
  theirs: PresentationSlide[],
  ctx: MergeContext,
): PresentationSlide[] {
  if (ours === theirs) return ours;
  const baseIds = slideIds(base);
  const oursIds = slideIds(ours);
  const theirsIds = slideIds(theirs);
  if (!baseIds || !oursIds || !theirsIds) return mergeList(base, ours, theirs, "slides", ctx) as PresentationSlide[];

  const byId = (slides: PresentationSlide[]) => new Map(slides.map((slide) => [slide.id as string, slide]));
  const inBase = byId(base);
  const inOurs = byId(ours);
  const inTheirs = byId(theirs);

  // Which slides the merged deck holds, and in what content.
  const kept = new Map<string, PresentationSlide>();
  for (const id of new Set([...oursIds, ...theirsIds])) {
    const was = inBase.get(id);
    const mine = inOurs.get(id);
    const other = inTheirs.get(id);
    const slideCtx: MergeContext = { ...ctx, slideId: id };
    if (mine && other) {
      kept.set(id, was ? (mergeValue(was, mine, other, "", slideCtx) as PresentationSlide) : mine);
      if (!was && !same(mine, other)) conflict(slideCtx, "");
    } else if (mine) {
      // Deleted on their side: gone, unless ours edited it - then it stays.
      if (!was) kept.set(id, mine);
      else if (!same(was, mine)) {
        conflict(slideCtx, "");
        kept.set(id, mine);
      }
    } else if (other) {
      // Deleted on ours: gone - unless they edited it; then it comes back with
      // their edit, and the person is told.
      if (!was) kept.set(id, other);
      else if (!same(was, other)) {
        ctx.restored.push(id);
        kept.set(id, other);
      }
    }
  }

  // The order: the side that reordered (ours when both did); the slides the
  // other side alone holds go in after the slide they followed there.
  const reordered = (ids: readonly string[], present: Map<string, PresentationSlide>) =>
    !sameOrder(
      ids.filter((id) => inBase.has(id)),
      baseIds.filter((id) => present.has(id)),
    );
  const oursMoved = reordered(oursIds, inOurs);
  const theirsMoved = reordered(theirsIds, inTheirs);
  if (oursMoved && theirsMoved) {
    const common = (ids: readonly string[]) => ids.filter((id) => inOurs.has(id) && inTheirs.has(id));
    if (!sameOrder(common(oursIds), common(theirsIds))) conflict(ctx, "slides");
  }
  const [primary, secondary] = theirsMoved && !oursMoved ? [theirsIds, oursIds] : [oursIds, theirsIds];
  const order = primary.filter((id) => kept.has(id));
  const placed = new Set(order);
  secondary.forEach((id, index) => {
    if (placed.has(id) || !kept.has(id)) return;
    let at = 0;
    for (let k = index - 1; k >= 0; k -= 1) {
      if (placed.has(secondary[k])) {
        at = order.indexOf(secondary[k]) + 1;
        break;
      }
    }
    order.splice(at, 0, id);
    placed.add(id);
  });

  const slides = order.map((id) => kept.get(id) as PresentationSlide);
  if (slides.length === ours.length && slides.every((slide, index) => slide === ours[index])) return ours;
  if (slides.length === theirs.length && slides.every((slide, index) => slide === theirs[index])) return theirs;
  return slides;
}

// ── The deck ────────────────────────────────────────────────────────────────

/**
 * `ours` (the local draft, seeded from `base`) with what `theirs` (a newer
 * revision of the same deck) changed since `base` merged in. See the module
 * note for the rules; `conflicts` lists where ours was kept over theirs.
 */
export function mergeDrafts(base: DeckDraft, ours: DeckDraft, theirs: DeckDraft): DraftMerge {
  const ctx: MergeContext = { conflicts: [], restored: [], slideId: null };
  if (ours === base) return { draft: theirs, conflicts: [], restored: [] };
  const title = mergeValue(base.title, ours.title, theirs.title, "title", ctx) as string;
  const theme = mergeValue(base.theme, ours.theme, theirs.theme, "theme", ctx) as DeckDraft["theme"];
  const transition = mergeValue(base.transition, ours.transition, theirs.transition, "transition", ctx) as DeckDraft["transition"];
  const template = mergeValue(
    base.template_container_id,
    ours.template_container_id,
    theirs.template_container_id,
    "template_container_id",
    ctx,
  ) as string | null;
  const chrome = mergeValue(base.chrome, ours.chrome, theirs.chrome, "chrome", ctx) as DeckDraft["chrome"];
  const slides = mergeSlides(base.slides, ours.slides, theirs.slides, ctx);
  const merged: DeckDraft = {
    title,
    theme,
    transition,
    template_container_id: template,
    chrome,
    slides,
  };
  const unchanged = (draft: DeckDraft) =>
    draft.title === title &&
    draft.theme === theme &&
    draft.transition === transition &&
    draft.template_container_id === template &&
    draft.chrome === chrome &&
    draft.slides === slides;
  const reported = { conflicts: ctx.conflicts, restored: ctx.restored };
  if (unchanged(ours)) return { draft: ours, ...reported };
  if (unchanged(theirs)) return { draft: theirs, ...reported };
  return { draft: merged, ...reported };
}

/**
 * Whether `theirs` left the place `path` names alone, against `base` - the
 * same slide in both. A text open in place on a slide a collaborator changed
 * stays open when it does: every list on the way to it kept its
 * length (an insert or a removal before it would move its index, and the
 * field writes by index), and what holds the text is unchanged - the deepest
 * list element on the way (`blocks.2.items.0` for `blocks.2.items.0.text`),
 * or the field itself for a slide's own field (`title`). The same reading
 * keeps a selected block (`blocks.2`) selected.
 */
export function pathUntouched(base: PresentationSlide | undefined, theirs: PresentationSlide | undefined, path: string): boolean {
  if (!base || !theirs) return false;
  const segments = parsePath(path);
  let holder = segments.length;
  for (let index = 0; index < segments.length; index += 1) {
    if (typeof segments[index] !== "number") continue;
    const list = segments.slice(0, index);
    const was = getAt(base, list);
    const now = getAt(theirs, list);
    if (!Array.isArray(was) || !Array.isArray(now) || was.length !== now.length) return false;
    holder = index + 1;
  }
  return same(getAt(base, segments.slice(0, holder)), getAt(theirs, segments.slice(0, holder)));
}

/** The ids of the slides `theirs` added or changed since `base` (what a collaborator just did). */
export function slidesChangedBy(base: DeckDraft, theirs: DeckDraft): Set<string> {
  const before = new Map(base.slides.map((slide) => [slide.id, slide]));
  const changed = new Set<string>();
  for (const slide of theirs.slides) {
    if (!slide.id) continue;
    const was = before.get(slide.id);
    if (!was || !same(was, slide)) changed.add(slide.id);
  }
  return changed;
}

/**
 * Where the slide at `index` of `before` sits in `after`, by its id; `null`
 * when it is gone. A deck whose slides carry no ids keeps the index (clamped).
 */
export function slideIndexAfter(before: DeckDraft, after: DeckDraft, index: number): number | null {
  const slide = before.slides[index];
  if (!slide) return null;
  if (!slide.id) return after.slides.length === 0 ? null : Math.min(index, after.slides.length - 1);
  const at = after.slides.findIndex((candidate) => candidate.id === slide.id);
  return at < 0 ? null : at;
}
