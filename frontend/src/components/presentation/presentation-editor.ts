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
 * The manual editing session of a deck: ONE draft, shared by
 * the side panel (the inspector) and the stage (selection, in-place text).
 *
 * `useDeckEditor` is created by the viewer and handed to both. It owns:
 *
 *   - the DRAFT - the deck as it would be saved (title, theme, transition,
 *     template, chrome, slides) - seeded from the stored deck; a revision
 *     saved elsewhere (a collaborator, an agent) is MERGED into it, the
 *     history rebased onto it (`deck-merge.ts`);
 *   - the HISTORY - every change is one step, undo / redo walk it; edits typed
 *     into one field in quick succession coalesce into one step (a keystroke
 *     is not a step, a word is);
 *   - the SELECTION on the stage - a slide, or a block on it by spec path -
 *     and the text under edit in place (`EditingText`, what `InlineMarkup`
 *     reads through `EditModeContext`);
 *   - the OPERATIONS - a write at a path, block insert / move / duplicate /
 *     delete, slide insert / move / duplicate / delete, deck fields - all
 *     copy-on-write through `edit-paths.ts`, so the deck's React keys survive
 *     an edit and only what changed re-renders.
 *
 * Nothing here talks to the server: the autosave (`useDeckAutosave.ts`) saves
 * (`PATCH` with `expected_revision`) and calls `adoptSaved`.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";

import type {
  DeckBlock,
  DeckChrome,
  Presentation,
  PresentationSlide,
  PresentationSpec,
  PresentationTheme,
  PresentationTransition,
} from "@/types";

// The explicit extensions let Node's runner load this module for
// `presentation-editor.test.ts` (the bundler resolves them either way).
import { BLOCK_SCHEMA, blockRunCapacity } from "./block-schema.ts";
import { canonicalJson, canonicalOf } from "./canonical-json.ts";
import { mergeDrafts, pathUntouched, slideIndexAfter, slidesChangedBy, type MergeConflict } from "./deck-merge.ts";
import type { EditingText, SlideEditSession } from "./edit-context";
import { blockSlot, getAt, insertAt, joinPath, moveAt, parsePath, removeAt, setAt } from "./edit-paths.ts";
import { activeInlineEditor } from "./inline-editor-registry.ts";
import { DEFAULT_SLIDE_CAP } from "./slide-schema.ts";

export { canonicalJson };

export interface DeckDraft {
  title: string;
  theme: PresentationTheme;
  transition: PresentationTransition;
  /** The deck's template — `null` for a free deck. */
  template_container_id: string | null;
  /** The deck's chrome settings, complete: `{}` when the spec has none. */
  chrome: DeckChrome;
  slides: PresentationSlide[];
}

/** What is selected on the stage: a slide, or one block on it. */
export interface EditorSelection {
  slide: number;
  /** The block's spec path, or `null` when the slide itself is selected. */
  block: string | null;
}

/** Steps of one field typed within this window fold into one history step. */
const COALESCE_MS = 900;
const HISTORY_LIMIT = 200;

/**
 * The chrome in its normal form: a switch at its default (`true`), the logo
 * position at its default (`auto`), a blank text and an empty `fields` map
 * drop their key. The stored spec spells the defaults out (`show_footer:
 * true`), the panel's edits do not (`withChrome`), so the pristine snapshot
 * and the draft must be read through the same normaliser — otherwise flipping
 * a switch off and back on leaves the panel dirty and offers a Save that would
 * write nothing.
 */
export function normalizeChrome(chrome: DeckChrome | undefined): DeckChrome {
  const source = (chrome ?? {}) as Record<string, unknown>;
  const next: Record<string, unknown> = {};
  // Keys in a fixed order, so the JSON comparisons below read two chromes
  // that say the same thing as equal whatever order the edits came in.
  for (const key of Object.keys(source).sort()) {
    const value = source[key];
    if (value === undefined || value === null || value === true) continue;
    if (typeof value === "string" && value.trim() === "") continue;
    if (key === "logo_position" && value === "auto") continue;
    if (key === "fields") {
      const raw = (value as Record<string, string>) ?? {};
      const fields: Record<string, string> = {};
      for (const name of Object.keys(raw).sort()) {
        const text = raw[name];
        if (typeof text === "string" && text.trim() !== "") fields[name] = text;
      }
      if (Object.keys(fields).length > 0) next.fields = fields;
      continue;
    }
    next[key] = value;
  }
  return next as DeckChrome;
}

/**
 * A slide's LOOK fields in their normal form: `hero: "plain"`,
 * `hero_tone: "brand"`, `overlay: "none"` and `hero_accent_word: false` are
 * the models' defaults, which the stored spec spells out (the validator
 * materialises them on every save) and the panel's pickers do not (a picker
 * put back on the default unsets the key, like a `flag` control does). The
 * pristine snapshot and the draft are read through the same normaliser, or
 * picking `Plain` on a deck stored before the fields existed - or `Gradient`
 * then `Plain` on one stored after - leaves the panel dirty with a Save that
 * would write nothing. Same rule as `normalizeChrome`; the theme's "inherit"
 * is an absent key on both sides already.
 */
export function normalizeSlideLook<T extends PresentationSlide>(slide: T): T {
  const next = { ...(slide as unknown as Record<string, unknown>) };
  if (next.hero === "plain") delete next.hero;
  if (next.hero_tone === "brand") delete next.hero_tone;
  if (next.overlay === "none") delete next.overlay;
  if (next.hero_accent_word === false) delete next.hero_accent_word;
  if (next.theme === undefined || next.theme === null) delete next.theme;
  return normalizeBlockLooks(next) as unknown as T;
}

/**
 * The same rule for the looks of the BLOCKS on a slide, wherever they
 * sit (a column, a callout's children, a comparison side, the footer slot, a
 * dashboard panel): a quote's `variant: "card"` and a metric's
 * `variant: "card"` / `align: "start"`, a formula's `align: "center"` and a
 * config_cards' `row_style: "outlined"` are the models' defaults, which the
 * stored spec spells and the inspector's pickers unset. Copy-on-write: an
 * object is copied only on the path to a key it drops, never mutated.
 */
function normalizeBlockLooks(value: unknown): unknown {
  if (Array.isArray(value)) {
    let changed = false;
    const items = value.map((item) => {
      const normal = normalizeBlockLooks(item);
      if (normal !== item) changed = true;
      return normal;
    });
    return changed ? items : value;
  }
  if (!value || typeof value !== "object") return value;
  const record = value as Record<string, unknown>;
  let copy: Record<string, unknown> | null = null;
  const edit = () => (copy ??= { ...record });
  const metric = record.type === "metric" || record.type === "metrics_grid";
  if ((record.type === "quote" || metric) && record.variant === "card") delete edit().variant;
  // No figures is no key (`figures: None`): a stored `[]` reads as absent.
  if (record.type === "quote" && Array.isArray(record.figures) && record.figures.length === 0) delete edit().figures;
  if (metric && record.align === "start") delete edit().align;
  // A formula centres its terms unless set otherwise: the picker unsets `center`.
  if (record.type === "formula" && record.align === "center") delete edit().align;
  // ...and config cards frame their rows unless set otherwise: the picker unsets `outlined`.
  if (record.type === "config_cards" && record.row_style === "outlined") delete edit().row_style;
  // A poll's results show live unless set otherwise: the picker unsets `live`.
  if (record.type === "poll" && record.show_results === "live") delete edit().show_results;
  // ...and draw the open question unless set otherwise: the picker unsets `active`.
  if (record.type === "poll" && record.layout === "active") delete edit().layout;
  // ...with the presenter controls on hover and the quiz answer revealed by hand (#10315).
  if (record.type === "poll" && record.controls === "hover") delete edit().controls;
  if (record.type === "poll" && record.reveal === "manual") delete edit().reveal;
  // ...and a reveal number only under the mode that reads it (#10411 UX B2): a mode change drops it.
  if (record.type === "poll" && record.reveal !== "after_votes" && "reveal_after_votes" in record) {
    delete edit().reveal_after_votes;
  }
  if (record.type === "poll" && record.reveal !== "after_seconds" && "reveal_after_seconds" in record) {
    delete edit().reveal_after_seconds;
  }
  for (const [key, child] of Object.entries(record)) {
    if (!child || typeof child !== "object") continue;
    const normal = normalizeBlockLooks(child);
    if (normal !== child) edit()[key] = normal;
  }
  return copy ?? record;
}

/** The draft of no deck at all (the hook before its deck has loaded). */
const EMPTY_DRAFT: DeckDraft = {
  title: "",
  theme: "dark",
  transition: "none",
  template_container_id: null,
  chrome: {},
  slides: [],
};

export const draftFrom = (presentation: Presentation | undefined): DeckDraft =>
  presentation
    ? {
        title: presentation.spec.title,
        theme: presentation.spec.theme,
        transition: presentation.spec.transition,
        template_container_id: presentation.template_container_id,
        chrome: normalizeChrome(structuredClone(presentation.spec.chrome ?? {})),
        // Deep copy: slide edits must never mutate the cached deck the renderer
        // is showing, or the "dirty" comparison below would always be clean.
        // Read in normal form (`normalizeSlideLook`), like the chrome.
        slides: structuredClone(presentation.spec.slides).map(normalizeSlideLook),
      }
    : EMPTY_DRAFT;

/**
 * `fresh` (a deck just read from the server, as a draft) with every piece
 * `previous` already holds in the same spelling reused as it is: the
 * session re-seeds on every autosave and every collaborator's revision, and a
 * fresh copy of every slide would redraw the whole deck each time - and make
 * the next merge compare every slide by content. A slide is matched by id (by
 * position for one without); what differs is `fresh`'s. `previous` itself
 * when nothing differs.
 */
export function shareDraft(fresh: DeckDraft, previous: DeckDraft): DeckDraft {
  const byId = new Map(previous.slides.filter((slide) => slide.id).map((slide) => [slide.id, slide]));
  const slides = fresh.slides.map((slide, index) => {
    const known = slide.id ? byId.get(slide.id) : previous.slides[index];
    return known && canonicalOf(known) === canonicalOf(slide) ? known : slide;
  });
  const sameSlides = slides.length === previous.slides.length && slides.every((slide, index) => slide === previous.slides[index]);
  const next: DeckDraft = {
    ...fresh,
    chrome: canonicalOf(fresh.chrome) === canonicalOf(previous.chrome) ? previous.chrome : fresh.chrome,
    slides: sameSlides ? previous.slides : slides,
  };
  const unchanged =
    next.title === previous.title &&
    next.theme === previous.theme &&
    next.transition === previous.transition &&
    next.template_container_id === previous.template_container_id &&
    next.chrome === previous.chrome &&
    next.slides === previous.slides;
  return unchanged ? previous : next;
}

/**
 * The spec the draft would save — the stored spec with the draft's pieces laid
 * over it, the same merge the server performs on PATCH. What the host renders
 * as the live preview.
 */
export const specWithDraft = (spec: PresentationSpec, draft: DeckDraft): PresentationSpec => ({
  ...spec,
  title: draft.title.trim() || spec.title,
  theme: draft.theme,
  transition: draft.transition,
  chrome: draft.chrome,
  slides: draft.slides,
});

/** A fresh slide id in the server's own shape (12 hex chars). */
function newSlideId(): string {
  const bytes = new Uint8Array(6);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The block at `path` on `slide`, or `undefined`. */
export function blockAt(slide: PresentationSlide, path: string): DeckBlock | undefined {
  const value = getAt(slide, parsePath(path));
  return value && typeof value === "object" && "type" in (value as object) ? (value as DeckBlock) : undefined;
}

/** The text field a block opens on `Enter` (its main copy), if it has one. */
export function primaryTextField(block: DeckBlock): string | null {
  const schema = BLOCK_SCHEMA[block.type];
  const field = schema?.fields.find((f) => f.kind === "text" && f.markup && getAt(block, [f.key]) !== undefined);
  if (field) return field.key;
  const items = schema?.items;
  if (items) {
    const first = items.fields.find((f) => f.kind === "text");
    const list = getAt(block, [items.key]);
    if (first && Array.isArray(list) && list.length > 0) return `${items.key}.0.${first.key}`;
  }
  return null;
}

/**
 * The block paths of `slide` in reading order (depth first), what `Tab`
 * walks. Mirrors where the renderer nests blocks (`edit-paths.ts`).
 */
export function blockPathsOf(slide: PresentationSlide): string[] {
  const paths: string[] = [];
  const visit = (block: DeckBlock, path: string) => {
    paths.push(path);
    if (block.type === "callout") {
      block.children?.forEach((child, index) => visit(child, `${path}.children.${index}`));
    } else if (block.type === "comparison") {
      block.left.blocks.forEach((child, index) => visit(child, `${path}.left.blocks.${index}`));
      block.right.blocks.forEach((child, index) => visit(child, `${path}.right.blocks.${index}`));
    } else if (block.type === "columns") {
      block.columns.forEach((column, c) =>
        column.forEach((child, index) => visit(child, `${path}.columns.${c}.${index}`)),
      );
    }
  };
  if (slide.layout === "content") {
    if (slide.columns)
      slide.columns.forEach((column, c) => column.forEach((child, index) => visit(child, `columns.${c}.${index}`)));
    else slide.blocks?.forEach((child, index) => visit(child, `blocks.${index}`));
  } else if (slide.layout === "dashboard") {
    slide.panels.forEach((panel, index) => visit(panel.block, `panels.${index}.block`));
  }
  if ("footer_block" in slide && slide.footer_block) visit(slide.footer_block, "footer_block");
  return paths;
}

/**
 * One step of the history: the draft as it was, and what was selected then -
 * an undo puts the selection back where the person was working (the block a
 * move carried away, the block a delete removed), the way a slide editor does.
 */
interface Step {
  draft: DeckDraft;
  selection: EditorSelection | null;
}

/** The draft with its history, one state so undo / redo are pure updates. */
export interface Timeline {
  draft: DeckDraft;
  past: Step[];
  future: Step[];
}

export const timelineFrom = (presentation: Presentation | undefined): Timeline => ({
  draft: draftFrom(presentation),
  past: [],
  future: [],
});

/**
 * The timeline once a save has landed: a save is a CHECKPOINT in the history,
 * never its end. Every step stays undoable - the ones before the save
 * included - the way a slide editor with an autosave behaves: undoing past
 * the save makes the draft dirty against the saved deck again, and the next
 * save writes what the undo restored as a NEW revision (`History` keeps the
 * saved one; nothing is lost by walking back).
 *
 * The draft the save WROTE (`savedDraft`, the one the PATCH body was built
 * from) is replaced wherever it sits - as the current draft, or as the step a
 * post-save change recorded - by a fresh seed of the saved deck: the server
 * may have normalised it (ids assigned, blanks dropped), and the saved state
 * must read clean against the new base. A draft that moved on WHILE the save
 * was in flight - a field typed into during the round trip - is KEPT: nothing
 * typed is dropped, and only what came after the save reads dirty. Every
 * other step (past and future) takes the slide ids the save assigned
 * (`withSavedSlideIds`), so an undo into the pre-save deck saved again does
 * not mint other ids for the slides the first save just named. A keystroke
 * that coalesced across the save left no step holding the written draft: the
 * saved deck is then inserted where it belongs in time - as the step before
 * the current draft, or, when an undo during the round trip moved the work
 * since the save into the redo stack, as the first thing to redo - so it is
 * still something to walk to, and only once. Without `savedDraft` the caller
 * vouches for nothing and the draft is re-seeded. `base` is the saved deck as
 * a draft, when the caller already built it (the session keeps it as the base
 * the next merge reads).
 */
export function timelineAfterSave(
  current: Timeline,
  saved: Presentation,
  savedDraft: DeckDraft | undefined,
  base: DeckDraft = draftFrom(saved),
): Timeline {
  if (savedDraft === undefined) return { draft: base, past: [], future: [] };
  const carry = (draft: DeckDraft) => (draft === savedDraft ? base : withSavedSlideIds(draft, savedDraft, base));
  const carried = (steps: readonly Step[]) => steps.map((step) => ({ ...step, draft: carry(step.draft) }));
  if (current.draft === savedDraft) {
    return { draft: base, past: carried(current.past), future: carried(current.future) };
  }
  const holds = (steps: readonly Step[]) => steps.some((step) => step.draft === savedDraft);
  if (holds(current.past) || holds(current.future)) {
    return { draft: carry(current.draft), past: carried(current.past), future: carried(current.future) };
  }
  const checkpoint: Step = { draft: base, selection: null };
  // A change clears the redo stack, so a non-empty one means the person undid
  // and changed nothing since: the written draft (coalesced into the step the
  // undo moved forward) lies between the current draft and that step.
  if (current.future.length) {
    return { draft: carry(current.draft), past: carried(current.past), future: [checkpoint, ...carried(current.future)] };
  }
  return { draft: carry(current.draft), past: [...carried(current.past), checkpoint].slice(-HISTORY_LIMIT), future: [] };
}

/**
 * The timeline once a revision saved ELSEWHERE (a collaborator, an agent) is
 * merged in: the draft and every step of the history are rebased
 * from `base` (the revision they were built on) onto `theirs`, each merged
 * three ways (`mergeDrafts`). An undo then walks back the person's own
 * changes and never the collaborator's: a step is "what I had then, plus
 * what they did since". `conflicts` and `restored` are the current draft's -
 * where both changed the same thing and ours was kept, and the slides ours
 * deleted that came back with their edit.
 */
export function timelineAfterRemote(
  current: Timeline,
  base: DeckDraft,
  theirs: DeckDraft,
): { timeline: Timeline; conflicts: MergeConflict[]; restored: string[] } {
  const rebased = new Map<DeckDraft, DeckDraft>();
  const { draft, conflicts, restored } = mergeDrafts(base, current.draft, theirs);
  rebased.set(current.draft, draft);
  const rebase = (step: Step): Step => {
    let next = rebased.get(step.draft);
    if (next === undefined) {
      next = mergeDrafts(base, step.draft, theirs).draft;
      rebased.set(step.draft, next);
    }
    return next === step.draft ? step : { ...step, draft: next };
  };
  return { timeline: { draft, past: current.past.map(rebase), future: current.future.map(rebase) }, conflicts, restored };
}

/**
 * The timeline once the draft was DISCARDED back to the stored deck it was
 * seeded from ("Leave anyway" once a save failed): the dropped draft becomes the step
 * before the stored one, so `Ctrl+Z` brings the work back - a discard
 * is one more change, not the end of the history. A draft that already reads
 * as the stored deck records nothing. Not for the conflict banner's reload:
 * a history built on a revision the deck has moved past is dropped whole
 * (see `discard` in the hook).
 */
export function timelineAfterDiscard(
  current: Timeline,
  stored: Presentation,
  selection: EditorSelection | null,
  base: DeckDraft = draftFrom(stored),
): Timeline {
  if (canonicalJson(current.draft) === canonicalJson(base)) return { ...current, draft: base };
  return {
    draft: base,
    past: [...current.past.slice(-(HISTORY_LIMIT - 1)), { draft: current.draft, selection }],
    future: [],
  };
}

/** A deck at one revision: what the draft was seeded from, what a save was refused at. */
export interface DeckRevision {
  id: string | null;
  revision: number;
}

/**
 * Whether the answer to a save of deck `id` concerns the session seeded on
 * `seeded`. The viewer keeps ONE session when only the route's deck
 * changes (a duplicate opened, the Back button), so a save answered after
 * that change is about a deck the draft no longer sits on: its refusal must
 * not hold the new deck's Save, and its saved copy must not become the new
 * deck's base.
 */
export function answersSeed(id: string | null, seeded: DeckRevision): boolean {
  return id !== null && id === seeded.id;
}

/**
 * Whether the answer to a save sent as `sent` - the deck and the revision the
 * PATCH named - is about the base the session still sits on.
 * The session leaves a base only by adopting another deck during the round
 * trip: a newer revision taken over a draft emptied meanwhile (it carries
 * this save, or supersedes it), the viewer moved to another deck. An answer
 * about a base it left is moot, a refusal and a success alike: rebased on a
 * saved copy older than the revision it moved to, the session would read
 * that revision's changes as edits of its own - a conflict over a draft that
 * was clean. A save that changed nothing is answered at the revision it was
 * sent at, and still answers the base.
 */
export function answersBase(sent: DeckRevision, seeded: DeckRevision): boolean {
  return answersSeed(sent.id, seeded) && sent.revision === seeded.revision;
}

/**
 * The refusal a 409 records for a save sent as `sent`: the server
 * holds a revision past it. `null` when the refusal is moot - it answers a
 * base the session has left (`answersBase`), so it says nothing about the
 * deck the draft now sits on.
 */
export function refusalOf(sent: DeckRevision, seeded: DeckRevision): DeckRevision | null {
  return answersBase(sent, seeded) ? { id: sent.id, revision: sent.revision } : null;
}

/**
 * Whether the deck a 409 announced is still on its way: while the
 * query holds the very revision the save was refused at, the deck on screen
 * is the one the server refused, and nothing may be re-seeded on it. The
 * refetch the 409 starts (`useUpdatePresentation`) ends the wait.
 */
export function awaitsLatestRevision(refused: DeckRevision | null, stored: DeckRevision): boolean {
  return refused !== null && stored.id === refused.id && stored.revision === refused.revision;
}

/**
 * What Discard does against the deck the query holds - the conflict banner's
 * reload is the same button. `step`: back to the deck the
 * draft was seeded from, one more undoable change. `reset`: onto a revision
 * the deck moved to under the draft, with a fresh history - every step of
 * the old one was built on a revision a save must no longer write over.
 * `wait`: a 409 announced a revision the query does not hold yet, and
 * re-seeding on the refused one would only earn the next save another 409;
 * the reset happens when the newer deck lands.
 */
export function discardMode(
  stored: DeckRevision,
  seeded: DeckRevision,
  refused: DeckRevision | null,
): "step" | "reset" | "wait" {
  if (awaitsLatestRevision(refused, stored)) return "wait";
  return stored.id === seeded.id && stored.revision === seeded.revision ? "step" : "reset";
}

/**
 * Whether the deck the query holds is a revision the session has not seen: a
 * NEWER revision of the deck the draft is seeded on. A deck's
 * revision only grows, so an older copy - a read answered after the save the
 * session already adopted - is stale, never a revision arriving: adopting it
 * would take the draft back to before the save.
 */
export function revisionArrives(stored: DeckRevision, seeded: DeckRevision): boolean {
  return stored.id === seeded.id && stored.revision > seeded.revision;
}

/**
 * Whether a revision the watch would merge waits for the answer of the save
 * in flight: while a save of the base the session sits on is
 * on its way, a newer revision in the query may well be that save's own
 * commit, which the poll can bring in before the answer - merged as a
 * collaborator's, it would be folded into the very draft being saved. The
 * answer decides: it rebases the session, and the watch then compares the
 * rebased draft with the query, so a genuine collaborator's revision is
 * merged right after.
 */
export function defersToSave(inFlight: DeckRevision | null, seeded: DeckRevision): boolean {
  return inFlight !== null && answersBase(inFlight, seeded);
}

/**
 * What a revision arriving from elsewhere does to the session.
 * `merge` folds it into the draft and every step of the history
 * (`timelineAfterRemote`): nothing typed here is lost, nothing waits for a
 * reload. `reset` adopts it with a fresh history - only for a Discard asked
 * while a 409's newer deck was on its way (`askedOver`, the timeline it was
 * asked over), and only when the person did nothing since: an edit,
 * an undo or a redo is another timeline, and a text opened in place is new
 * work too. `hold` keeps the draft as it is while a text open in place cannot
 * hand its words to the draft yet (markup the save would refuse): the
 * revision is merged once it can, or once the field closes.
 */
export function revisionArrival(
  timeline: Timeline,
  askedOver: Timeline | null,
  textOpen: boolean,
  textHeld: boolean,
): "reset" | "merge" | "hold" {
  if (textHeld) return "hold";
  if (askedOver !== null && askedOver === timeline && !textOpen) return "reset";
  return "merge";
}

/**
 * `draft` with the slide ids the server assigned on the save carried in: a
 * slide the draft still holds WITHOUT an id (an older deck's, stored before
 * ids were assigned) takes the id the saved deck gave the same slide. The
 * slide is found by IDENTITY in the draft the save wrote (an untouched slide
 * keeps its object through the copy-on-write updates); a slide edited during
 * the round trip (a new object) is matched by position ONLY when the order is
 * POSITIVELY established: same count, exactly ONE slide without an identity
 * match - every other slide matched at its written place leaves that one
 * slide exactly one place to be, whereas two or more unmatched slides could
 * have swapped places among themselves (a legacy `[A, B, C]` moved to
 * `[B', A', C]` with both edited keeps only `C` in place, and a position
 * fallback would then hand A B's id and B A's) - and the written slide at
 * that place itself id-less when written (the server named it in THIS save:
 * an edit keeps a slide's id, so an id-less draft slide can never be a slide
 * that already carried one - a legacy slide deleted from the place where a
 * new, already-named slide was then inserted must not take that slide's id,
 * or the pre-delete draft undone into past the checkpoint and saved would
 * tell the server that slide was EDITED into the old one). Anything less
 * certain stays id-less rather than take a neighbour's id (two slides under
 * one id, or two slides with their ids swapped, is what breaks the links and
 * the comment anchors this exists to keep); the next save then names the
 * slide again, which is the smaller harm. Without the carry-over the next
 * save would mint ANOTHER id for the slide the first save had just named.
 * Copy-on-write: a draft with nothing to carry is returned as is.
 */
export function withSavedSlideIds(draft: DeckDraft, savedDraft: DeckDraft, base: DeckDraft): DeckDraft {
  if (base.slides.length !== savedDraft.slides.length) return draft;
  const written = draft.slides.map((slide) => savedDraft.slides.indexOf(slide));
  const orderKept =
    draft.slides.length === savedDraft.slides.length &&
    written.filter((at) => at < 0).length === 1 &&
    written.every((at, index) => at < 0 || at === index);
  const claimed = new Set(written.filter((at) => at >= 0));
  let changed = false;
  const slides = draft.slides.map((slide, index) => {
    if (slide.id) return slide;
    let at = written[index];
    if (at < 0 && orderKept && !claimed.has(index) && !savedDraft.slides[index]?.id) at = index;
    const id = at >= 0 ? base.slides[at]?.id : undefined;
    if (!id) return slide;
    changed = true;
    return { ...slide, id };
  });
  return changed ? { ...draft, slides } : draft;
}

/** The draft update that writes `value` at `path` on slide `index`; the draft itself when nothing changes. */
function writeAtUpdate(index: number, path: string, value: unknown): (draft: DeckDraft) => DeckDraft {
  return (d) => {
    const slide = d.slides[index];
    if (!slide) return d;
    const segments = parsePath(path);
    // A commit that changed nothing (a field opened and closed) is not a step.
    if (getAt(slide, segments) === value) return d;
    const next = setAt(slide, segments, value);
    if (next === slide) return d;
    return {
      ...d,
      slides: d.slides.map((s, i) => (i === index ? next : s)),
    };
  };
}

/**
 * Whether the block at `blockPath` may be removed: never the last block of a
 * run the validator requires non-empty (a slide body, a column, a comparison
 * side) - delete the parent instead; a callout's children and the footer slot
 * may be emptied. A dashboard panel's block is the panel's REQUIRED figure
 * (`panels.N.block`): the panel goes as a whole from the inspector, its
 * block alone never.
 */
export function canRemoveBlock(slide: PresentationSlide, blockPath: string): boolean {
  const slot = blockSlot(blockPath);
  if (!slot) return blockPath === "footer_block";
  if (slot.list[slot.list.length - 1] === "children") return true;
  const list = getAt(slide, slot.list);
  return Array.isArray(list) && list.length > 1;
}

/**
 * Whether the run at `listPath` may take one more block: the validator caps a
 * slide body, a column and a container (`MAX_BLOCKS_PER_*`), and an insert or
 * a duplicate past the cap would only be refused at the save.
 */
export function canGrowRun(slide: PresentationSlide, listPath: string): boolean {
  const list = getAt(slide, parsePath(listPath));
  return !Array.isArray(list) || list.length < blockRunCapacity(listPath);
}

/** A run of blocks a person can add one to by hand: the stage's "+" and the inspector's menu. */
export interface BlockRun {
  /** The list's spec path: `blocks`, `columns.1`, `blocks.2.children`... */
  list: string;
  /** The blocks it holds: a block added to it lands after them. */
  length: number;
  /** The slide's own flow, one of its columns, or a run inside a container block. */
  kind: "body" | "column" | "inside";
  /** A `column` run's column, 1-based. */
  column?: number;
  /** An `inside` run's container block. */
  container?: string;
}

/** The runs a container block holds: a callout's children, a comparison's sides, a columns block's columns. */
function containerRuns(block: DeckBlock, path: string): string[] {
  if (block.type === "callout") return [`${path}.children`];
  if (block.type === "comparison") return [`${path}.left.blocks`, `${path}.right.blocks`];
  if (block.type === "columns") return block.columns.map((_, index) => `${path}.columns.${index}`);
  return [];
}

/**
 * Where a block can be added on `slide`, in reading order: a content slide's
 * body - its flow, empty or not, or each of its columns - then the runs of the
 * container blocks the selection is in or is, outermost first, so the runs
 * inside a callout are offered once it (or a block in it) is selected. The
 * other layouts hold items, not blocks: none.
 */
export function addableRuns(slide: PresentationSlide | undefined, selectedBlock: string | null): BlockRun[] {
  if (!slide || slide.layout !== "content") return [];
  const runs: BlockRun[] = slide.columns
    ? slide.columns.map(
        (column, index): BlockRun => ({ list: `columns.${index}`, length: column.length, kind: "column", column: index + 1 }),
      )
    : [{ list: "blocks", length: slide.blocks?.length ?? 0, kind: "body" }];
  if (!selectedBlock) return runs;
  const parts = parsePath(selectedBlock);
  for (let end = 1; end <= parts.length; end += 1) {
    const path = joinPath(parts.slice(0, end));
    const block = blockAt(slide, path);
    if (!block) continue;
    for (const list of containerRuns(block, path)) {
      const value = getAt(slide, parsePath(list));
      runs.push({ list, length: Array.isArray(value) ? value.length : 0, kind: "inside", container: path });
    }
  }
  return runs;
}

/**
 * The cap a deck is held to: its template's `max_slides`, served on
 * the deck response; the platform default for a response that predates it.
 */
export function deckSlideCap(presentation: Pick<Presentation, "max_slides"> | undefined): number {
  const cap = presentation?.max_slides;
  return typeof cap === "number" && Number.isFinite(cap) && cap >= 1 ? Math.floor(cap) : DEFAULT_SLIDE_CAP;
}

/** Whether the deck may take one more slide (its cap: `deckSlideCap`). */
export function canAddSlide(draft: DeckDraft, maxSlides: number = DEFAULT_SLIDE_CAP): boolean {
  return draft.slides.length < maxSlides;
}

/** How a write joins the history. */
interface WriteOptions {
  /** Every write of one key folds into one step, however far apart in time. */
  sticky?: boolean;
  /**
   * The last write of that run (a text closed in place): the next write is a
   * step of its own, and a run that ends where it began is no step at all.
   */
  ends?: boolean;
}

/** How a "save now" goes out: `leaving` - the page is being hidden or closed, the request must outlive it. */
export interface SaveRequest {
  leaving?: boolean;
}

/** A revision saved elsewhere, merged into the draft - what the panel says and highlights. */
interface RemoteMerge {
  /** The revision merged in. */
  revision: number;
  /** Who saved it, as the server names the deck's last editor. */
  by: string | null;
  /** The slides it added or changed (stable ids). */
  changed: ReadonlySet<string>;
  /** Where both sides changed the same thing differently: ours was kept. */
  conflicts: readonly MergeConflict[];
  /** The slides the person deleted and they edited: back, with their edit (stable ids). */
  restored: readonly string[];
  /** A text open in place was closed: their revision changed what it was written into (its words are in the draft). */
  closedText: boolean;
}

export interface DeckEditor {
  presentation: Presentation;
  enabled: boolean;
  draft: DeckDraft;
  /** The draft differs from the deck it was seeded from (what the autosave writes). */
  dirty: boolean;
  /**
   * There is work a reload would lose: a dirty draft, or a text open in place
   * whose last words may not be in the draft yet. What the tab-close guard
   * reads - never `dirty` alone.
   */
  unsaved: boolean;
  /**
   * A revision newer than the one the draft is seeded on is known and not
   * merged in yet: a save would only meet a 409, so none is sent. It
   * clears when the merge happens - when the newer deck is in the query after
   * a 409, or when a text open in place can hand its words over.
   */
  outOfDate: boolean;
  /**
   * A save was refused (409) and the newer deck it announced is not in the
   * query yet: a Discard waits for it - it would otherwise re-seed on
   * the revision the server just refused.
   */
  awaitingLatest: boolean;
  baseRevision: number;
  canUndo: boolean;
  canRedo: boolean;
  selection: EditorSelection | null;
  editingText: EditingText | null;
  /** What the stage's `PresentationDeck` / `PageDocument` receive as `edit`. */
  stageSession: SlideEditSession;
  /** The last revision saved elsewhere that was merged in, `null` until one is. */
  lastRemoteMerge: RemoteMerge | null;

  /** Replace the draft through an updater; `key` coalesces bursts on one field. */
  apply: (update: (draft: DeckDraft) => DeckDraft, key?: string, options?: WriteOptions) => void;
  setDeck: (patch: Partial<Omit<DeckDraft, "slides">>, key?: string) => void;
  setSlides: (slides: PresentationSlide[]) => void;
  /** Write `value` at `path` on slide `index` (`undefined` unsets an optional field). */
  writeAt: (index: number, path: string, value: unknown, key?: string, options?: WriteOptions) => void;
  updateSlide: (index: number, slide: PresentationSlide) => void;
  insertSlide: (at: number, slide: PresentationSlide) => void;
  duplicateSlide: (index: number) => void;
  moveSlide: (from: number, to: number) => void;
  removeSlide: (index: number) => void;
  insertBlock: (slide: number, listPath: string, at: number, block: DeckBlock) => void;
  removeBlock: (slide: number, blockPath: string) => void;
  moveBlock: (slide: number, blockPath: string, delta: number) => void;
  duplicateBlock: (slide: number, blockPath: string) => void;

  select: (selection: EditorSelection | null) => void;
  startTextEdit: (slide: number, path: string, caret?: number) => void;
  commitText: (slide: number, path: string, value: string) => void;
  cancelText: () => void;

  undo: () => void;
  redo: () => void;
  /**
   * Back to the stored deck - what "Leave anyway" does after a save failed.
   * Of the deck the draft was seeded from it is one more undoable step: the
   * dropped draft is what `undo` brings back (`timelineAfterDiscard`).
   * Onto a deck that moved on under the draft it starts a fresh history: its
   * steps were built on a revision the next save must not write over. Asked
   * while a 409's newer deck is on its way, it waits for that deck
   * (`discardMode`).
   */
  reload: () => void;
  /**
   * A save sent as `sent` (the deck and the revision the PATCH named) came
   * back 409: the draft is kept and `outOfDate` holds until the newer
   * deck the refusal announced is in the query, then it is merged in and the
   * autosave writes again. A refusal of a deck or a revision the
   * session has since left is moot (`refusalOf`).
   */
  saveRefused: (sent: DeckRevision) => void;
  /**
   * After a successful save: the server's normalised deck becomes the base,
   * the history stays - a save is a checkpoint, and undoing past it makes
   * the draft dirty again for the next save (`timelineAfterSave`).
   * `sent` is the deck and the revision the PATCH named, `savedDraft` the
   * draft its body was built from; a draft edited since (during the round
   * trip) is kept and rebased, never dropped. The answer to a save of a base
   * the session has since left - another deck, or a newer revision adopted
   * meanwhile - is not its to adopt (`answersBase`).
   */
  adoptSaved: (saved: Presentation, sent: DeckRevision, savedDraft?: DeckDraft) => void;
  /**
   * A save sent as `sent` left / settled: the autosave brackets every
   * PATCH with the two, whatever its answer. While one is in flight a merge
   * waits for its answer (`defersToSave`), and `whenSaveSettles` resolves
   * once it has settled and the session has taken the answer in.
   */
  beginSave: (sent: DeckRevision) => void;
  endSave: (sent: DeckRevision) => void;
  /** Resolves at once with no save in flight, otherwise once its answer is in. */
  whenSaveSettles: () => Promise<void>;
  /** The autosave registers "save now" here; the stage's `Ctrl+S` calls it through `requestSave`. */
  onSaveRequest: MutableRefObject<((request?: SaveRequest) => void) | null>;
  /**
   * Save now: the words of a text open in place handed to the draft first,
   * the field left open - and nothing saved while the field cannot hand them
   * over (markup the validator would refuse: the field says why).
   */
  requestSave: (request?: SaveRequest) => void;
  /**
   * The slide the stage shows, written by the viewer on every render: a merge
   * that moves it (a slide inserted before it elsewhere) asks the stage to
   * follow it (`focusRequest`), so the person stays on the slide they were on.
   */
  stageIndex: MutableRefObject<number | null>;
  /**
   * The slide the stage should move to once it has rendered the draft: set by
   * a structural change (insert, duplicate, delete, move) and consumed by the
   * viewer AFTER the deck synced to the new slides. reveal navigates by index
   * over the sections it HAS, so a `goTo` issued in the same tick as the
   * change lands on the section holding that index NOW - one place off.
   */
  focusRequest: MutableRefObject<number | null>;
}

/**
 * The session, or `null` until the deck has loaded (the hook runs either way,
 * so a viewer that has the deck behind a query can call it unconditionally).
 */
export function useDeckEditor(presentation: Presentation | undefined, enabled: boolean): DeckEditor | null {
  const [timeline, setTimeline] = useState<Timeline>(() => timelineFrom(presentation));
  const { draft } = timeline;
  const [baseRevision, setBaseRevision] = useState(presentation?.revision ?? 0);
  const [outOfDate, setOutOfDate] = useState(false);
  const [selection, setSelection] = useState<EditorSelection | null>(null);
  const [editingText, setEditingText] = useState<EditingText | null>(null);
  const [lastRemoteMerge, setLastRemoteMerge] = useState<RemoteMerge | null>(null);
  const lastStep = useRef<{ key: string; at: number } | null>(null);
  const onSaveRequest = useRef<((request?: SaveRequest) => void) | null>(null);
  const focusRequest = useRef<number | null>(null);
  const stageIndex = useRef<number | null>(null);
  // Every text opened in place is one session: its live writes and its commit
  // fold into ONE history step (`sticky`), however long the typing lasts.
  const textSessions = useRef(0);
  // The deck (and revision) the draft was seeded from, kept in refs so the two
  // effects below read the value a `reset` in the same commit just wrote: a
  // deck arriving (or the route moving to another deck) re-seeds, never
  // merges.
  const seeded = useRef<DeckRevision>({
    id: presentation?.id ?? null,
    revision: presentation?.revision ?? 0,
  });
  // The same seed as a draft: the base a merge reads. The very object
  // the draft was built from, so the merge compares untouched slides by
  // reference.
  const baseDraft = useRef<DeckDraft>(timeline.draft);
  // The base revision a save was refused at (409), until a rebase:
  // while the query still holds it, the newer deck is on its way.
  const [refused, setRefused] = useState<DeckRevision | null>(null);
  // A Discard asked during that wait: the timeline it was asked over,
  // performed as a reset the moment the newer deck lands - unless the person
  // went back to work since (`revisionArrival`).
  const discardAskedOver = useRef<Timeline | null>(null);
  // The save on its way, if any: the base it was sent from, and who
  // waits for its answer (leaving the editor over the draft being saved).
  const [saveInFlight, setSaveInFlight] = useState<DeckRevision | null>(null);
  const saveInFlightRef = useRef(saveInFlight);
  saveInFlightRef.current = saveInFlight;
  const saveWaiters = useRef<(() => void)[]>([]);
  // A merge that waits one render for the words of a text open in place to
  // reach the draft (`flush` hands them over through a state update).
  const [mergeTick, setMergeTick] = useState(0);

  // "Dirty" is the draft against the deck it was SEEDED from - not against
  // whatever revision the query holds now: a collaborator's save arriving
  // while nothing was touched here must read as clean (and be adopted below),
  // not as a conflict between an untouched draft and a newer deck. Compared
  // with the keys SORTED: the query cache keeps the deck it already held for
  // every part a save left equal (TanStack's structural sharing), and the
  // stored spec and a PATCH response spell the same slide with `id` in
  // different places - a byte comparison read that as an edit that never was.
  const [pristine, setPristine] = useState(() => canonicalJson(draftFrom(presentation)));
  // Serialised once per DRAFT, not once per render: a selection change or a
  // hover re-renders the session without touching the draft, and the whole
  // spec of a forty-slide deck is not stringified for that.
  const draftJson = useMemo(() => canonicalOf(draft), [draft]);
  const dirty = draftJson !== pristine;

  // The deck as of the last render, for the merges and the history steps:
  // they run from effects and event handlers.
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  const timelineRef = useRef(timeline);
  timelineRef.current = timeline;
  const editingTextRef = useRef(editingText);
  editingTextRef.current = editingText;

  // The saved deck becomes the base the draft is read against (and the
  // revision the next save names); the timeline is the caller's decision.
  // The coalescing of a burst of typing carries over: an autosave is not a
  // gesture of the person, and does not cut their word in two undo steps.
  const rebase = useCallback((from: Presentation, base: DeckDraft = draftFrom(from)) => {
    seeded.current = { id: from.id, revision: from.revision };
    baseDraft.current = base;
    setPristine(canonicalJson(base));
    setBaseRevision(from.revision);
    setOutOfDate(false);
    setRefused(null);
    discardAskedOver.current = null;
  }, []);

  const reset = useCallback(
    (from: Presentation) => {
      const seed = timelineFrom(from);
      rebase(from, seed.draft);
      lastStep.current = null;
      setTimeline(seed);
      setEditingText(null);
      setSelection(null);
      setLastRemoteMerge(null);
    },
    [rebase],
  );

  // A save landed: the draft it wrote is re-seeded from the server's copy; one
  // edited meanwhile is kept and rebased (`timelineAfterSave`). The selection
  // stays where the person was working - a save is not a reason to lose it. A
  // save answered after the poll loaded a newer revision is merged with it
  // right after, by the revision watch below - unless the session
  // already adopted that revision (a draft emptied during the round trip):
  // the answer is then about a base it left, and moot.
  const adoptSaved = useCallback(
    (saved: Presentation, sent: DeckRevision, savedDraft?: DeckDraft) => {
      if (!answersBase(sent, seeded.current)) return;
      // What the server stored, in the very objects the save sent wherever it
      // stored them as sent: an autosave redraws nothing.
      const base = savedDraft ? shareDraft(draftFrom(saved), savedDraft) : draftFrom(saved);
      rebase(saved, base);
      setTimeline((current) => timelineAfterSave(current, saved, savedDraft, base));
    },
    [rebase],
  );

  useEffect(() => {
    if (presentation && seeded.current.id !== presentation.id) reset(presentation);
  }, [presentation, reset]);

  // A revision saved elsewhere (a collaborator, an agent) MERGED into the
  // draft and its whole history: what only they changed lands, what
  // only the person changed stays, a clash keeps the person's (they save
  // last) and is reported. The person stays where they were: the selection
  // and the stage follow their slide by id, and a text open in place stays
  // open while they type - closed only when the revision changed what it is
  // written into or moved it (`pathUntouched`; its words are in the draft by
  // now, the watch handed them over).
  const mergeRemote = useCallback(
    (from: Presentation) => {
      const before = timelineRef.current;
      const base = baseDraft.current;
      // Their revision in the base's own objects wherever it left them alone:
      // the merge compares those by reference, and they redraw nothing.
      const theirs = shareDraft(draftFrom(from), base);
      const { timeline: next, conflicts, restored } = timelineAfterRemote(before, base, theirs);
      const changed = slidesChangedBy(base, theirs);
      const follow = (index: number) => slideIndexAfter(before.draft, next.draft, index);
      // Whether their revision left `path` on the slide at `index` alone.
      const leftAlone = (index: number, path: string) => {
        const id = before.draft.slides[index]?.id;
        if (!id || !changed.has(id)) return true;
        const bySlide = (draft: DeckDraft) => draft.slides.find((slide) => slide.id === id);
        return pathUntouched(bySlide(base), bySlide(theirs), path);
      };
      const text = editingTextRef.current;
      const closedText = text !== null && !leftAlone(text.slide, text.path);
      // Closed KEEPING its words (the watch handed them over: the commit
      // writes nothing new), never cancelled - an Escape writes the opening
      // value back.
      if (closedText) activeInlineEditor()?.commit();
      rebase(from, theirs);
      setTimeline(next);
      setSelection((current) => {
        if (!current) return current;
        const at = follow(current.slide);
        if (at === null) return null;
        const block = current.block !== null && leftAlone(current.slide, current.block) ? current.block : null;
        return at === current.slide && block === current.block ? current : { slide: at, block };
      });
      setEditingText((current) => {
        if (!current || closedText) return null;
        const at = follow(current.slide);
        if (at === null) return null;
        return at === current.slide ? current : { ...current, slide: at };
      });
      const stage = stageIndex.current;
      if (stage !== null) {
        const at = follow(stage);
        if (at !== null && at !== stage) focusRequest.current = at;
      }
      setLastRemoteMerge({ revision: from.revision, by: from.updated_by_name ?? null, changed, conflicts, restored, closedText });
    },
    [rebase],
  );

  // A new revision arrived from elsewhere (the agent, a collaborator, our own
  // save answered after the poll read it). Only a NEWER revision arrives
  // (`revisionArrives`): an older copy in the query is stale and would
  // take the draft back to before a save. The answer of a save in flight
  // decides first (`defersToSave`). A text open in place hands its words to
  // the draft first (`flush`) - the merge then waits one render for them -
  // and one that cannot yet (markup the save would refuse) holds the merge
  // until it can or closes (`revisionArrival`).
  useEffect(() => {
    if (!presentation || !revisionArrives(presentation, seeded.current)) return;
    if (defersToSave(saveInFlight, seeded.current)) return;
    const open = editingText !== null ? activeInlineEditor() : null;
    const handover = open ? open.flush() : "clean";
    // A Discard that waited for this revision after a 409 is consumed
    // here: performed, or abandoned for the work started since the ask.
    const arrival = revisionArrival(timelineRef.current, discardAskedOver.current, open !== null, handover === "refused");
    if (arrival === "hold") {
      setOutOfDate(true);
      return;
    }
    discardAskedOver.current = null;
    if (arrival === "reset") reset(presentation);
    else if (handover === "pushed") setMergeTick((tick) => tick + 1);
    else mergeRemote(presentation);
    // Keyed on the two revisions - the query's, and the one the session is
    // seeded on (a save answered after the poll loaded a newer revision is
    // adopted, then merged at once) - on the save in flight, on the
    // text under edit, and, while a merge is held, on the draft: the field's
    // next valid words are what releases it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presentation?.revision, baseRevision, saveInFlight, editingText, outOfDate ? draft : null]);

  useEffect(() => {
    if (mergeTick === 0 || !presentation || !revisionArrives(presentation, seeded.current)) return;
    if (defersToSave(saveInFlightRef.current, seeded.current)) return;
    mergeRemote(presentation);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mergeTick]);

  const beginSave = useCallback((sent: DeckRevision) => {
    const flight = { id: sent.id, revision: sent.revision };
    // Read at once by `whenSaveSettles` (leaving the editor right after the
    // save is asked for); the state follows on the next render.
    saveInFlightRef.current = flight;
    setSaveInFlight(flight);
  }, []);
  const endSave = useCallback(
    (sent: DeckRevision) =>
      setSaveInFlight((current) =>
        current && current.id === sent.id && current.revision === sent.revision ? null : current,
      ),
    [],
  );
  // Resolved AFTER the render that took the answer in (and after the watch
  // above decided on it), so a waiter reads the session as it now stands.
  const whenSaveSettles = useCallback(
    () =>
      saveInFlightRef.current === null
        ? Promise.resolve()
        : new Promise<void>((resolve) => saveWaiters.current.push(resolve)),
    [],
  );
  useEffect(() => {
    if (saveInFlight !== null) return;
    for (const resolve of saveWaiters.current.splice(0)) resolve();
  }, [saveInFlight]);
  useEffect(
    () => () => {
      for (const resolve of saveWaiters.current.splice(0)) resolve();
    },
    [],
  );

  // Leaving edit mode drops the selection and any text under edit (the viewer
  // commits the text first); the draft itself stays, and the autosave writes
  // what is left of it.
  useEffect(() => {
    if (enabled) return;
    setSelection(null);
    setEditingText(null);
  }, [enabled]);

  const apply = useCallback((update: (draft: DeckDraft) => DeckDraft, key?: string, options?: WriteOptions) => {
    // The coalescing decision is taken OUTSIDE the updater (an updater must be
    // pure; React may run it twice): one field typed within the window folds
    // into the step already on the stack - a sticky key whatever the pause.
    // The selection recorded is the one BEFORE the change (`apply` runs from
    // an event handler): what an undo of that change puts back.
    const now = Date.now();
    const last = lastStep.current;
    const coalesce =
      key !== undefined && last?.key === key && (options?.sticky === true || now - last.at < COALESCE_MS);
    lastStep.current = key !== undefined && !options?.ends ? { key, at: now } : null;
    const selectedBefore = selectionRef.current;
    setTimeline((current) => {
      const next = update(current.draft);
      if (next === current.draft) return current;
      if (coalesce) {
        // A run that ENDS where it began - a text typed in place and Escaped,
        // or typed and taken back before it was closed - is no step at all.
        // Only at its end: a step dropped mid-run would leave the run's next
        // write folding into the draft before it, with no step to undo to.
        const below = current.past[current.past.length - 1];
        if (options?.ends && below && canonicalOf(below.draft) === canonicalOf(next)) {
          return { draft: below.draft, past: current.past.slice(0, -1), future: [] };
        }
        return { ...current, draft: next, future: [] };
      }
      return {
        draft: next,
        past: [...current.past.slice(-(HISTORY_LIMIT - 1)), { draft: current.draft, selection: selectedBefore }],
        future: [],
      };
    });
  }, []);

  // The selection a step recorded, if it still names something in the draft
  // the step restores and sits on the slide the person is looking at (the
  // current selection's slide); otherwise the current selection is kept where
  // it still exists, or falls back to its slide.
  const restoreSelection = useCallback((recorded: EditorSelection | null, draft: DeckDraft) => {
    const current = selectionRef.current;
    const exists = (candidate: EditorSelection | null) =>
      candidate !== null &&
      candidate.slide < draft.slides.length &&
      (candidate.block === null || getAt(draft.slides[candidate.slide], parsePath(candidate.block)) !== undefined);
    const sameSlide = current === null || recorded === null || recorded.slide === current.slide;
    if (sameSlide && exists(recorded)) setSelection(recorded);
    else if (exists(current)) setSelection(current);
    else setSelection(current ? { slide: Math.min(current.slide, draft.slides.length - 1), block: null } : null);
  }, []);

  const undo = useCallback(() => {
    const current = timelineRef.current;
    const previous = current.past[current.past.length - 1];
    if (!previous) return;
    lastStep.current = null;
    setEditingText(null);
    setTimeline({
      draft: previous.draft,
      past: current.past.slice(0, -1),
      future: [{ draft: current.draft, selection: selectionRef.current }, ...current.future],
    });
    restoreSelection(previous.selection, previous.draft);
  }, [restoreSelection]);

  const redo = useCallback(() => {
    const current = timelineRef.current;
    const next = current.future[0];
    if (!next) return;
    lastStep.current = null;
    setEditingText(null);
    setTimeline({
      draft: next.draft,
      past: [...current.past, { draft: current.draft, selection: selectionRef.current }],
      future: current.future.slice(1),
    });
    restoreSelection(next.selection, next.draft);
  }, [restoreSelection]);

  // Discard ("Leave anyway" once a save failed): back to the stored deck as
  // the base, the dropped draft kept as the step before it. Onto a
  // deck that moved on under the draft it is a reset: every step of the
  // history was built on a revision that is no longer the base - an undo into
  // one of them, saved with the new `expected_revision`, would hand the
  // server the old slides over the collaborator's with no warning. After a
  // 409 the deck the query holds may BE the refused revision for the
  // length of the refetch: the discard then waits for the newer one and
  // remembers the timeline it was asked over - work started since abandons
  // it (`revisionArrival`).
  const discard = useCallback(() => {
    if (!presentation) return;
    const mode = discardMode(presentation, seeded.current, refused);
    if (mode === "wait") {
      discardAskedOver.current = timelineRef.current;
      setEditingText(null);
      setSelection(null);
      return;
    }
    if (mode === "reset") {
      reset(presentation);
      return;
    }
    const base = draftFrom(presentation);
    rebase(presentation, base);
    lastStep.current = null;
    const selectedBefore = selectionRef.current;
    setTimeline((current) => timelineAfterDiscard(current, presentation, selectedBefore, base));
    setEditingText(null);
    setSelection(null);
  }, [presentation, refused, rebase, reset]);

  const saveRefused = useCallback((sent: DeckRevision) => {
    const refusal = refusalOf(sent, seeded.current);
    if (!refusal) return;
    setRefused(refusal);
    setOutOfDate(true);
  }, []);

  // Save now (the stage's Ctrl+S): the words of a text open in place go to
  // the draft FIRST and the save waits for them to land (a state update,
  // flushed after the handler that asked) - saving the draft as it was would
  // send the words minus the ones being typed. The field stays open. One the
  // field cannot hand over saves nothing: its explicit commit is refused and
  // says why, in the inspector's words.
  const requestSave = useCallback((request?: SaveRequest) => {
    const open = activeInlineEditor();
    const handover = open ? open.flush() : "clean";
    if (handover === "refused") {
      open?.commit();
      return;
    }
    if (handover === "pushed") window.setTimeout(() => onSaveRequest.current?.(request), 0);
    else onSaveRequest.current?.(request);
  }, []);

  const setDeck = useCallback(
    (patch: Partial<Omit<DeckDraft, "slides">>, key?: string) => apply((d) => ({ ...d, ...patch }), key),
    [apply],
  );
  const setSlides = useCallback((slides: PresentationSlide[]) => apply((d) => ({ ...d, slides })), [apply]);

  // `key` names the field a burst of typing belongs to, slide-relative the
  // way the callers know it (`title`, `notes`, `blocks.1.items.0.text`); the
  // step it coalesces into is per field ON ONE SLIDE, so the same path typed
  // into on the next slide within the window is another step, never the tail
  // of this one.
  const writeAt = useCallback(
    (index: number, path: string, value: unknown, key?: string, options?: WriteOptions) =>
      apply(writeAtUpdate(index, path, value), key === undefined ? undefined : `slides.${index}.${key}`, options),
    [apply],
  );

  const updateSlide = useCallback(
    (index: number, slide: PresentationSlide) =>
      apply((d) => ({
        ...d,
        slides: d.slides.map((s, i) => (i === index ? slide : s)),
      })),
    [apply],
  );

  // The caps are read off the draft as rendered: a deck or a run at its cap
  // takes nothing more, and the selection stays where it was (the UI disables
  // the same actions from the same predicates, so this is the belt). The
  // slide cap is the deck's own: its template's `max_slides`.
  const slideCap = deckSlideCap(presentation);
  const insertSlide = useCallback(
    (at: number, slide: PresentationSlide) => {
      if (!canAddSlide(timelineRef.current.draft, slideCap)) return;
      apply((d) =>
        canAddSlide(d, slideCap) ? insertAt(d, ["slides"], at, { ...slide, id: slide.id ?? newSlideId() }) : d,
      );
      setSelection({ slide: at, block: null });
    },
    [apply, slideCap],
  );

  const duplicateSlide = useCallback(
    (index: number) => {
      if (!canAddSlide(timelineRef.current.draft, slideCap)) return;
      apply((d) => {
        const source = d.slides[index];
        if (!source || !canAddSlide(d, slideCap)) return d;
        return insertAt(d, ["slides"], index + 1, {
          ...structuredClone(source),
          id: newSlideId(),
        });
      });
      setSelection({ slide: index + 1, block: null });
    },
    [apply, slideCap],
  );

  const moveSlide = useCallback(
    (from: number, to: number) => {
      apply((d) => moveAt(d, ["slides"], from, to));
      // The selection follows its slide: the moved one to its destination,
      // the ones it passed over one place the other way (a block selected on
      // a displaced slide stays selected on that slide).
      setSelection((current) => {
        if (!current) return current;
        const at = current.slide;
        if (at === from) return { ...current, slide: to };
        if (from < at && at <= to) return { ...current, slide: at - 1 };
        if (to <= at && at < from) return { ...current, slide: at + 1 };
        return current;
      });
    },
    [apply],
  );

  const removeSlide = useCallback(
    (index: number) => {
      apply((d) => (d.slides.length <= 1 ? d : removeAt(d, ["slides", index])));
      setSelection(null);
    },
    [apply],
  );

  const insertBlock = useCallback(
    (slideIndex: number, listPath: string, at: number, block: DeckBlock) => {
      const current = timelineRef.current.draft.slides[slideIndex];
      if (!current || !canGrowRun(current, listPath)) return;
      apply((d) => {
        const slide = d.slides[slideIndex];
        if (!slide || !canGrowRun(slide, listPath)) return d;
        const next = insertAt(slide, parsePath(listPath), at, block);
        return {
          ...d,
          slides: d.slides.map((s, i) => (i === slideIndex ? next : s)),
        };
      });
      setSelection({ slide: slideIndex, block: `${listPath}.${at}` });
    },
    [apply],
  );

  const removeBlock = useCallback(
    (slideIndex: number, blockPath: string) => {
      apply((d) => {
        const slide = d.slides[slideIndex];
        // A lone slot (footer_block) is unset; a list item is spliced out - but
        // never the last block of a run the validator requires non-empty.
        if (!slide || !canRemoveBlock(slide, blockPath)) return d;
        const next = removeAt(slide, parsePath(blockPath));
        return {
          ...d,
          slides: d.slides.map((s, i) => (i === slideIndex ? next : s)),
        };
      });
      setSelection({ slide: slideIndex, block: null });
    },
    [apply],
  );

  const moveBlock = useCallback(
    (slideIndex: number, blockPath: string, delta: number) => {
      const slot = blockSlot(blockPath);
      if (!slot) return;
      const to = slot.index + delta;
      apply((d) => {
        const slide = d.slides[slideIndex];
        if (!slide) return d;
        const next = moveAt(slide, slot.list, slot.index, to);
        if (next === slide) return d;
        return {
          ...d,
          slides: d.slides.map((s, i) => (i === slideIndex ? next : s)),
        };
      });
      setSelection((current) => {
        if (!current || current.block !== blockPath) return current;
        const list = getAt(draft.slides[slideIndex], slot.list);
        if (!Array.isArray(list) || to < 0 || to >= list.length) return current;
        return { slide: slideIndex, block: joinPath([...slot.list, to]) };
      });
    },
    [apply, draft.slides],
  );

  const duplicateBlock = useCallback(
    (slideIndex: number, blockPath: string) => {
      const slot = blockSlot(blockPath);
      if (!slot) return;
      const current = timelineRef.current.draft.slides[slideIndex];
      if (!current || !canGrowRun(current, joinPath(slot.list))) return;
      apply((d) => {
        const slide = d.slides[slideIndex];
        if (!slide || !canGrowRun(slide, joinPath(slot.list))) return d;
        const block = getAt(slide, parsePath(blockPath));
        if (!block) return d;
        const next = insertAt(slide, slot.list, slot.index + 1, structuredClone(block));
        return {
          ...d,
          slides: d.slides.map((s, i) => (i === slideIndex ? next : s)),
        };
      });
      setSelection({
        slide: slideIndex,
        block: joinPath([...slot.list, slot.index + 1]),
      });
    },
    [apply],
  );

  const select = useCallback((next: EditorSelection | null) => {
    setSelection(next);
    // A new selection ends any text under edit elsewhere (its editor commits
    // on blur first).
    setEditingText((current) => (current && (!next || next.slide !== current.slide) ? null : current));
  }, []);

  const startTextEdit = useCallback((slide: number, path: string, caret?: number) => {
    textSessions.current += 1;
    setEditingText({ slide, path, caret, session: textSessions.current });
  }, []);

  // The history key of the text under edit at `slide` / `path`, if it is the
  // one open: its live writes and its commit are ONE step (`sticky`).
  // Not slide-relative like a field's key: a merge may move the slide while
  // the text is open, and the session is still one step.
  const textKey = useCallback((slide: number, path: string): string | undefined => {
    const open = editingTextRef.current;
    return open && open.slide === slide && open.path === path ? `inline-text-${open.session ?? 0}` : undefined;
  }, []);

  // The words typed in place, handed to the draft while the field is open:
  // the autosave writes them without the field being closed. Only
  // the text under edit writes, and only markup the save would accept (the
  // field checks before it hands over).
  const liveText = useCallback(
    (slide: number, path: string, value: string) => {
      const key = textKey(slide, path);
      if (key !== undefined) apply(writeAtUpdate(slide, path, value), key, { sticky: true });
    },
    [apply, textKey],
  );

  const commitText = useCallback(
    (slide: number, path: string, value: string) => {
      const key = textKey(slide, path);
      setEditingText((current) => (current && current.slide === slide && current.path === path ? null : current));
      // An emptied REQUIRED field would fail the save; an emptied optional one
      // is dropped. The inspector and the 422 both name it; here the string is
      // written as typed, empty included, so the person sees what they did.
      // The last write of the text's session (an Escape writes back the value
      // it opened with this way).
      apply(writeAtUpdate(slide, path, value), key, { sticky: true, ends: true });
    },
    [apply, textKey],
  );

  const cancelText = useCallback(() => setEditingText(null), []);

  // Keep the selection on a slide that still exists after an external reload.
  useEffect(() => {
    setSelection((current) => {
      if (!current) return current;
      if (current.slide >= draft.slides.length) return null;
      if (current.block && !blockAt(draft.slides[current.slide], current.block)) {
        return { slide: current.slide, block: null };
      }
      return current;
    });
  }, [draft.slides]);

  const stageSession = useMemo<SlideEditSession>(
    () => ({
      enabled,
      editingText,
      onLiveText: liveText,
      onCommitText: commitText,
      onCancelText: cancelText,
    }),
    [enabled, editingText, liveText, commitText, cancelText],
  );

  if (!presentation) return null;

  return {
    presentation,
    enabled,
    draft,
    dirty,
    unsaved: dirty || editingText !== null,
    outOfDate,
    awaitingLatest: awaitsLatestRevision(refused, presentation),
    baseRevision,
    selection,
    editingText,
    stageSession,
    lastRemoteMerge,
    apply,
    setDeck,
    setSlides,
    writeAt,
    updateSlide,
    insertSlide,
    duplicateSlide,
    moveSlide,
    removeSlide,
    insertBlock,
    removeBlock,
    moveBlock,
    duplicateBlock,
    select,
    startTextEdit,
    commitText,
    cancelText,
    undo,
    redo,
    reload: discard,
    saveRefused,
    adoptSaved,
    beginSave,
    endSave,
    whenSaveSettles,
    onSaveRequest,
    requestSave,
    stageIndex,
    focusRequest,
    canUndo: timeline.past.length > 0,
    canRedo: timeline.future.length > 0,
  };
}
