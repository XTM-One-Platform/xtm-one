/**
 * Unit tests for the pure half of the editing session - `yarn test:unit`.
 *
 * The hook itself needs a renderer; what is pinned here is what it computes
 * from plain data: the draft seeded from a deck, the canonical JSON the dirty
 * flag compares, the timeline a save leaves behind - a draft edited WHILE
 * the save was in flight is kept, never dropped with the response - and what
 * a Discard does after a 409.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import type { Presentation, PresentationSlide } from "@/types";

import { BLOCK_SCHEMA, enumWritten, itemsWritten, type SchemaField } from "./block-schema.ts";
import { parsePath, setAt } from "./edit-paths.ts";
import {
  addableRuns,
  answersBase,
  answersSeed,
  awaitsLatestRevision,
  canonicalJson,
  defersToSave,
  discardMode,
  draftFrom,
  normalizeSlideLook,
  refusalOf,
  revisionArrival,
  revisionArrives,
  shareDraft,
  timelineAfterDiscard,
  timelineAfterRemote,
  timelineAfterSave,
  timelineFrom,
  withSavedSlideIds,
  type DeckDraft,
  type Timeline,
} from "./presentation-editor.ts";

function slide(id: string, title: string): PresentationSlide {
  return { id, layout: "title", title } as PresentationSlide;
}

function deck(revision: number, slides: PresentationSlide[], title = "Deck"): Presentation {
  return {
    id: "deck-1",
    revision,
    template_container_id: null,
    spec: { title, theme: "dark", aspect_ratio: "16:9", transition: "none", slides },
  } as unknown as Presentation;
}

const stored = deck(1, [slide("a", "One"), slide("b", "Two")]);

test("a draft is seeded from the deck and the canonical JSON ignores key order", () => {
  const draft = draftFrom(stored);
  assert.equal(draft.title, "Deck");
  assert.deepEqual(
    draft.slides.map((s) => s.id),
    ["a", "b"],
  );
  // A deep copy: editing the draft never reaches the cached deck.
  assert.notEqual(draft.slides, stored.spec.slides);
  assert.equal(canonicalJson({ b: 1, a: { d: 2, c: 3 } }), canonicalJson({ a: { c: 3, d: 2 }, b: 1 }));
  assert.notEqual(canonicalJson({ a: 1 }), canonicalJson({ a: 2 }));
});

test("a slide's look is read in normal form: the model defaults drop their key, a choice stays", () => {
  // What the validator stores on a title slide the agent left plain.
  const materialised = {
    id: "a",
    layout: "title",
    title: "Cover",
    hero: "plain",
    hero_tone: "brand",
    overlay: "none",
    hero_accent_word: false,
  } as unknown as PresentationSlide;
  assert.deepEqual(normalizeSlideLook(materialised), { id: "a", layout: "title", title: "Cover" });
  // A look the person chose is kept whole; a theme of the slide's own too.
  const chosen = {
    id: "b",
    layout: "closing",
    title: "Thanks",
    theme: "dark",
    hero: "gradient",
    hero_tone: "accent",
    overlay: "soft",
    hero_accent_word: true,
  } as unknown as PresentationSlide;
  assert.deepEqual(normalizeSlideLook(chosen), chosen);
  assert.notEqual(normalizeSlideLook(chosen), chosen); // a copy, never the input
  // The pristine snapshot goes through it, so a deck stored AFTER the fields
  // existed (defaults spelled out) reads clean against a draft whose pickers
  // unset a default - and a deck stored before them reads the same.
  const spelled = deck(1, [materialised, slide("b", "Two")]);
  const bare = deck(1, [{ id: "a", layout: "title", title: "Cover" } as PresentationSlide, slide("b", "Two")]);
  assert.equal(canonicalJson(draftFrom(spelled)), canonicalJson(draftFrom(bare)));
});

test("block looks are read in normal form wherever the block sits, without touching the input", () => {
  // What the validator stores for a card quote and a card metric, in a column and a footer.
  const materialised = {
    id: "q",
    layout: "content",
    columns: [
      [{ type: "quote", content: "Words.", variant: "card" }],
      [{ type: "metrics_grid", items: [{ value: "1", label: "One" }], variant: "card", align: "start" }],
    ],
    footer_block: { type: "metric", value: "2", label: "Two", variant: "card", align: "start" },
  } as unknown as PresentationSlide;
  const before = JSON.stringify(materialised);
  assert.deepEqual(normalizeSlideLook(materialised), {
    id: "q",
    layout: "content",
    columns: [
      [{ type: "quote", content: "Words." }],
      [{ type: "metrics_grid", items: [{ value: "1", label: "One" }] }],
    ],
    footer_block: { type: "metric", value: "2", label: "Two" },
  });
  assert.equal(JSON.stringify(materialised), before, "the stored slide is never mutated");
  // A look the person chose stays whole.
  const chosen = {
    id: "s",
    layout: "content",
    blocks: [
      { type: "quote", content: "Words.", variant: "statement", size: "hero" },
      { type: "metric", value: "3", label: "Three", variant: "plain", align: "center" },
    ],
  } as unknown as PresentationSlide;
  assert.deepEqual(normalizeSlideLook(chosen), chosen);
});

test("a metric look picked away and back reads clean against the stored deck", () => {
  const metric = { type: "metric", value: "1", label: "One", variant: "card", align: "start" };
  const grid = { type: "metrics_grid", items: [{ value: "1", label: "One" }], variant: "card", align: "start" };
  const saved = deck(1, [{ id: "m", layout: "content", blocks: [metric, grid] } as unknown as PresentationSlide]);
  const pristine = canonicalJson(draftFrom(saved));
  const pick = (draft: DeckDraft, path: string, field: SchemaField, option: string): DeckDraft => ({
    ...draft,
    slides: draft.slides.map((s, i) => (i === 0 ? setAt(s, parsePath(path), enumWritten(field, option)) : s)),
  });
  for (const [index, type] of [
    [0, "metric"],
    [1, "metrics_grid"],
  ] as const) {
    for (const [key, other, fallback] of [
      ["variant", "plain", "card"],
      ["align", "center", "start"],
    ] as const) {
      const field = BLOCK_SCHEMA[type].fields.find((f) => f.key === key)!;
      const away = pick(draftFrom(saved), `blocks.${index}.${key}`, field, other);
      assert.notEqual(canonicalJson(away), pristine, `${type}.${key} = ${other} is an edit`);
      assert.equal(canonicalJson(pick(away, `blocks.${index}.${key}`, field, fallback)), pristine, `${type}.${key}`);
    }
  }
});

test("a figure added then removed reads clean, and a stored empty list reads as none", () => {
  const figures = BLOCK_SCHEMA.quote.items!;
  const saved = deck(1, [{ id: "q", layout: "content", blocks: [{ type: "quote", content: "Q" }] } as unknown as PresentationSlide]);
  const pristine = canonicalJson(draftFrom(saved));
  const write = (draft: DeckDraft, list: unknown[]): DeckDraft => ({
    ...draft,
    slides: draft.slides.map((s, i) => (i === 0 ? setAt(s, parsePath("blocks.0.figures"), itemsWritten(figures, list)) : s)),
  });
  const added = write(draftFrom(saved), [figures.create!()]);
  assert.notEqual(canonicalJson(added), pristine, "a figure added is an edit");
  assert.equal(canonicalJson(write(added, [])), pristine, "removed again, the quote reads as saved");
  // An agent that wrote `figures: []` stores the same quote as one with no figures.
  const spelled = deck(1, [{ id: "q", layout: "content", blocks: [{ type: "quote", content: "Q", figures: [] }] } as unknown as PresentationSlide]);
  assert.equal(canonicalJson(draftFrom(spelled)), pristine);
});

test("every look a picker writes back unset is one the draft reads in normal form", () => {
  // A picker that unsets its default against a pristine that kept the stored
  // key would read dirty at once: the two lists must be the same.
  for (const [type, schema] of Object.entries(BLOCK_SCHEMA)) {
    for (const field of schema.fields) {
      if (field.kind !== "enum" || field.defaultValue === undefined) continue;
      const stored = { id: "s", layout: "content", blocks: [{ type, [field.key]: field.defaultValue }] };
      const normal = normalizeSlideLook(stored as unknown as PresentationSlide) as unknown as typeof stored;
      assert.equal(field.key in normal.blocks[0], false, `${type}.${field.key}`);
    }
  }
});

test("a poll's reveal number reads only under the mode that uses it (#10411 UX B2)", () => {
  const poll = (extra: Record<string, unknown>) =>
    normalizeSlideLook({ id: "s", layout: "content", blocks: [{ type: "poll", ...extra }] } as unknown as PresentationSlide) as unknown as {
      blocks: Record<string, unknown>[];
    };
  assert.equal(poll({ reveal: "after_votes", reveal_after_votes: 3 }).blocks[0].reveal_after_votes, 3);
  const switched = poll({ reveal: "after_seconds", reveal_after_votes: 3, reveal_after_seconds: 10 }).blocks[0];
  assert.equal("reveal_after_votes" in switched, false);
  assert.equal(switched.reveal_after_seconds, 10);
  assert.equal("reveal_after_seconds" in poll({ reveal_after_seconds: 20 }).blocks[0], false);
});

test("a save re-seeds the draft it wrote from the server's copy and keeps the history", () => {
  const seed = draftFrom(stored);
  const written: DeckDraft = { ...seed, title: "Renamed" };
  const timeline = { draft: written, past: [{ draft: seed, selection: { slide: 0, block: null } }], future: [] };
  // The server stored the rename and normalised the deck (a fresh id on the second slide).
  const saved = deck(2, [slide("a", "One"), slide("fresh", "Two")], "Renamed");
  const after = timelineAfterSave(timeline, saved, written);
  // The current draft is the saved deck as stored: clean against the new base.
  assert.equal(canonicalJson(after.draft), canonicalJson(draftFrom(saved)));
  // A save is a checkpoint, not the end of the history: the pre-save step is
  // still there, with its selection, so Ctrl+Z after Save brings the deck
  // back to before the rename (dirty again, for a new revision).
  assert.equal(after.past.length, 1);
  assert.equal(after.past[0].draft.title, "Deck");
  assert.deepEqual(after.past[0].selection, { slide: 0, block: null });
  assert.deepEqual(after.future, []);
});

test("a redo stack survives a save too, its drafts carrying the saved ids", () => {
  const legacy = deck(1, [
    { layout: "title", title: "One" } as PresentationSlide,
    { layout: "title", title: "Two" } as PresentationSlide,
  ]);
  const seed = draftFrom(legacy);
  const renamed: DeckDraft = { ...seed, title: "Renamed" };
  const renamedTwice: DeckDraft = { ...renamed, title: "Renamed twice" };
  // Renamed twice, undone once, then saved: `renamed` is written, `renamedTwice` is the redo.
  const timeline = {
    draft: renamed,
    past: [{ draft: seed, selection: null }],
    future: [{ draft: renamedTwice, selection: null }],
  };
  const saved = deck(2, [slide("id-one", "One"), slide("id-two", "Two")], "Renamed");
  const after = timelineAfterSave(timeline, saved, renamed);
  assert.equal(after.future.length, 1);
  assert.equal(after.future[0].draft.title, "Renamed twice");
  assert.deepEqual(
    after.future[0].draft.slides.map((s) => s.id),
    ["id-one", "id-two"],
    "a redo saved later must not mint other ids for the slides this save named",
  );
  assert.deepEqual(
    after.past[0].draft.slides.map((s) => s.id),
    ["id-one", "id-two"],
  );
});

test("an undo during an in-flight save puts the saved deck in the redo stack, once", () => {
  const seed = draftFrom(stored);
  const renamed: DeckDraft = { ...seed, title: "Renamed" };
  // Save clicked on `renamed`, then Ctrl+Z while the PATCH was in flight: the
  // written draft sits in the redo stack when the response lands.
  const undone = { draft: seed, past: [], future: [{ draft: renamed, selection: null }] };
  const saved = deck(2, [slide("a", "One"), slide("b", "Two")], "Renamed");
  const after = timelineAfterSave(undone, saved, renamed);
  assert.deepEqual(after.past, [], "the saved deck is the FUTURE, never a step behind the older draft");
  assert.equal(after.future.length, 1, "and it is there once");
  assert.equal(canonicalJson(after.future[0].draft), canonicalJson(draftFrom(saved)));
  assert.equal(after.draft.title, "Deck");
  // The written draft coalesced into a later keystroke, then undone: the
  // saved deck lies between the current draft and the step to redo.
  const renamedTwice: DeckDraft = { ...renamed, title: "Renamed twice" };
  const coalescedThenUndone = { draft: seed, past: [], future: [{ draft: renamedTwice, selection: null }] };
  const between = timelineAfterSave(coalescedThenUndone, saved, renamed);
  assert.deepEqual(between.past, []);
  assert.deepEqual(
    between.future.map((step) => step.draft.title),
    ["Renamed", "Renamed twice"],
    "redo reaches the saved deck first, then the keystroke that followed it",
  );
});

test("through a checkpoint, every past and future draft keeps its own slides' ids and never a neighbour's", () => {
  // A legacy deck (no ids) edited in three steps: `B` moved to the front, `N`
  // inserted (a fresh id, as `insertSlide` mints one), `C` deleted - then saved.
  const legacy = deck(1, [
    { layout: "title", title: "A" } as PresentationSlide,
    { layout: "title", title: "B" } as PresentationSlide,
    { layout: "title", title: "C" } as PresentationSlide,
  ]);
  const seed = draftFrom(legacy);
  const [a, b, c] = seed.slides;
  const moved: DeckDraft = { ...seed, slides: [b, a, c] };
  const n = { id: "n-new", layout: "section", title: "N" } as PresentationSlide;
  const inserted: DeckDraft = { ...seed, slides: [b, n, a, c] };
  const deleted: DeckDraft = { ...seed, slides: [b, n, a] };
  // The server names B and A; N keeps the id it came with.
  const saved = deck(2, [slide("id-b", "B"), slide("n-new", "N"), slide("id-a", "A")]);
  const timeline = {
    draft: deleted,
    past: [
      { draft: seed, selection: null },
      { draft: moved, selection: null },
      { draft: inserted, selection: null },
    ],
    future: [],
  };
  const after = timelineAfterSave(timeline, saved, deleted);
  const ids = (draft: DeckDraft) => draft.slides.map((s) => s.id);
  assert.deepEqual(ids(after.draft), ["id-b", "n-new", "id-a"]);
  // Undo three times: before the delete, before the insert, before the move -
  // each slide under the id the save gave IT, in its own order of the time;
  // C, which the server never named, stays id-less rather than take a
  // neighbour's.
  assert.deepEqual(ids(after.past[2].draft), ["id-b", "n-new", "id-a", undefined], "before the delete");
  assert.deepEqual(ids(after.past[1].draft), ["id-b", "id-a", undefined], "before the insert");
  assert.deepEqual(ids(after.past[0].draft), ["id-a", "id-b", undefined], "before the move");
  for (const step of after.past) {
    const named = ids(step.draft).filter((id): id is string => !!id);
    assert.equal(new Set(named).size, named.length, "no two slides of one draft share an id");
  }
  // The trap the position fallback must not fall into: a legacy slide (B)
  // deleted from a place where a new, already-named slide (N) was then
  // inserted - same count, same order, the place unclaimed - is ANOTHER slide
  // than the one written there, since an edit keeps a slide's id and N had
  // its before the save. B stays id-less; saving the pre-delete draft undone
  // into past the checkpoint must not tell the server "N was edited into B".
  const swapped: DeckDraft = { ...seed, slides: [a, n, c] };
  const savedSwap = deck(2, [slide("id-a", "A"), slide("n-new", "N"), slide("id-c", "C")]);
  assert.deepEqual(withSavedSlideIds(seed, swapped, draftFrom(savedSwap)).slides.map((s) => s.id), [
    "id-a",
    undefined,
    "id-c",
  ]);
  // Every moved slide also edited during the round trip: `[A, B, C]` written
  // as `[B', A', C]` keeps only C in place, so the order of A and B is NOT
  // established - a position fallback would swap their ids. Both stay
  // id-less; only C, matched by identity, takes its id.
  const bothMovedAndEdited: DeckDraft = { ...seed, slides: [{ ...b, title: "B'" }, { ...a, title: "A'" }, c] };
  const savedBoth = deck(2, [slide("id-b", "B'"), slide("id-a", "A'"), slide("id-c", "C")]);
  assert.deepEqual(withSavedSlideIds(seed, bothMovedAndEdited, draftFrom(savedBoth)).slides.map((s) => s.id), [
    undefined,
    undefined,
    "id-c",
  ]);
  // ONE slide edited in place (the in-flight case the fallback exists for)
  // is still named: the other two, matched where they were written, leave it
  // exactly one place to be.
  const oneEdited: DeckDraft = { ...seed, slides: [a, { ...b, title: "B'" }, c] };
  const savedOne = deck(2, [slide("id-a", "A"), slide("id-b", "B'"), slide("id-c", "C")]);
  assert.deepEqual(withSavedSlideIds(seed, oneEdited, draftFrom(savedOne)).slides.map((s) => s.id), [
    "id-a",
    "id-b",
    "id-c",
  ]);
});

test("a discard keeps the dropped draft as the step before the stored deck", () => {
  const seed = draftFrom(stored);
  const edited: DeckDraft = { ...seed, title: "Renamed" };
  const timeline = { draft: edited, past: [{ draft: seed, selection: null }], future: [{ draft: seed, selection: null }] };
  const after = timelineAfterDiscard(timeline, stored, { slide: 1, block: "blocks.0" });
  assert.equal(canonicalJson(after.draft), canonicalJson(seed));
  assert.equal(after.past.length, 2);
  assert.equal(after.past[1].draft, edited, "Ctrl+Z after Discard brings the work back");
  assert.deepEqual(after.past[1].selection, { slide: 1, block: "blocks.0" });
  assert.deepEqual(after.future, [], "a discard is a new change: the redo stack goes");
  // A draft that already reads as the stored deck records no step.
  const clean = timelineAfterDiscard({ draft: seed, past: [], future: [] }, stored, null);
  assert.equal(clean.past.length, 0);
});

test("after a 409, Discard waits for the revision that won and lands on it, never on the refused one", () => {
  const seeded = { id: "deck-1", revision: 3 };
  // No refusal: Discard steps back to the seeded deck, the reload of a deck
  // that moved on under the draft starts over on it.
  assert.equal(discardMode({ id: "deck-1", revision: 3 }, seeded, null), "step");
  assert.equal(discardMode({ id: "deck-1", revision: 4 }, seeded, null), "reset");
  assert.equal(discardMode({ id: "deck-2", revision: 3 }, seeded, null), "reset");
  // A save sent at the seeded revision comes back 409: the server is past it.
  const sent = { id: "deck-1", revision: 3 };
  const refused = refusalOf(sent, seeded);
  assert.deepEqual(refused, { id: "deck-1", revision: 3 });
  // Until the refetch lands, the query holds the very revision the server
  // refused: nothing may be re-seeded on it.
  assert.equal(awaitsLatestRevision(refused, { id: "deck-1", revision: 3 }), true);
  assert.equal(discardMode({ id: "deck-1", revision: 3 }, seeded, refused), "wait");
  // The newer deck is in: the wait is over and the reload lands on it with a
  // fresh history - even before a rebase has cleared the refusal.
  assert.equal(awaitsLatestRevision(refused, { id: "deck-1", revision: 5 }), false);
  assert.equal(discardMode({ id: "deck-1", revision: 5 }, seeded, refused), "reset");
  // The poll brought the newer deck while the save was in flight: the
  // refusal of the old base finds the query already past it - no wait.
  assert.equal(awaitsLatestRevision(refusalOf(sent, seeded), { id: "deck-1", revision: 4 }), false);
  // A refusal of a base the session has since left (a revision adopted over a
  // draft emptied during the round trip) is moot: waiting on it would wait
  // for a revision the query already holds, forever.
  assert.equal(refusalOf(sent, { id: "deck-1", revision: 4 }), null);
  assert.equal(discardMode({ id: "deck-1", revision: 4 }, { id: "deck-1", revision: 4 }, refusalOf(sent, { id: "deck-1", revision: 4 })), "step");
  // Another deck at the same revision number is not the refused one.
  assert.equal(awaitsLatestRevision(refused, { id: "deck-2", revision: 3 }), false);
  // A 409 answered after the viewer moved to another deck (the page is reused
  // when only the route's deck changes: a duplicate opened, the Back button)
  // is about the deck it was SENT for: recorded against the deck now on
  // screen, it would hold that deck's Save and Discard for as long as the two
  // revision numbers agree - and a refetch of the other deck never ends it.
  assert.equal(refusalOf(sent, { id: "deck-2", revision: 3 }), null);
  // So is a save that succeeded: its copy is never the base of another deck.
  assert.equal(answersSeed("deck-1", seeded), true);
  assert.equal(answersSeed("deck-2", seeded), false);
  assert.equal(answersSeed("deck-1", { id: null, revision: 0 }), false);
});

test("a Discard deferred by a 409 lands on the revision that won - unless work started since the ask", () => {
  const seed = draftFrom(stored);
  const once: DeckDraft = { ...seed, title: "Renamed" };
  const twice: DeckDraft = { ...once, title: "Renamed twice" };
  // Two edits, then the editor was left over them ("Leave anyway" once a save failed)
  // while the 409's newer deck was still on its way: the Discard waits, and
  // remembers the timeline it was asked over.
  const asked: Timeline = {
    draft: twice,
    past: [
      { draft: seed, selection: null },
      { draft: once, selection: null },
    ],
    future: [],
  };
  // Nothing done since: the newer revision replaces the draft.
  assert.equal(revisionArrival(asked, asked, false, false), "reset");
  // A text opened in place since (the editor reopened before the refetch
  // landed) is new work: a reset would unmount the field from under the
  // caret - the revision is merged into the draft instead.
  assert.equal(revisionArrival(asked, asked, true, false), "merge");
  // An edit since the ask is new work, never swept by it...
  const edited: Timeline = { ...asked, draft: { ...twice, title: "Renamed again" } };
  assert.equal(revisionArrival(edited, asked, false, false), "merge");
  // ...and so is an undo (which never goes through an edit): the draft it
  // walked back to is kept, and merged.
  const undone: Timeline = { draft: once, past: [{ draft: seed, selection: null }], future: [{ draft: twice, selection: null }] };
  assert.equal(revisionArrival(undone, asked, false, false), "merge");
  // No Discard asked: a revision saved elsewhere is merged, whatever the
  // draft holds - nothing waits for a reload...
  assert.equal(revisionArrival(asked, null, false, false), "merge");
  assert.equal(revisionArrival(asked, null, true, false), "merge");
  // ...except while a text open in place cannot hand its words to the draft
  // yet (markup the save would refuse): held until it can, or closes.
  assert.equal(revisionArrival(asked, null, true, true), "hold");
  assert.equal(revisionArrival(asked, asked, true, true), "hold");
});

test("a draft edited while the save was in flight is kept and rebased, never dropped", () => {
  const seed = draftFrom(stored);
  const written: DeckDraft = { ...seed, title: "Renamed" };
  // Typed during the round trip: a new draft object, the written one on the stack
  // (under the pre-save step that led to it).
  const typed: DeckDraft = {
    ...written,
    slides: [written.slides[0], { ...written.slides[1], title: "Two, then some" }],
  };
  const timeline = {
    draft: typed,
    past: [
      { draft: seed, selection: null },
      { draft: written, selection: { slide: 1, block: null } },
    ],
    future: [],
  };
  // The server stored the rename and normalised the deck (a fresh id on the second slide).
  const saved = deck(2, [slide("a", "One"), slide("fresh", "Two")], "Renamed");
  const after = timelineAfterSave(timeline, saved, written);
  assert.equal(after.draft, typed, "the draft that moved on is the one the person keeps working in");
  assert.equal(after.draft.slides[1].title, "Two, then some");
  // What came after the save reads dirty against the new base; the saved part does not.
  assert.notEqual(canonicalJson(after.draft), canonicalJson(draftFrom(saved)));
  assert.equal(canonicalJson({ ...after.draft, slides: draftFrom(saved).slides }), canonicalJson(draftFrom(saved)));
  // The whole history survives the save: one undo lands on the saved deck AS
  // STORED (clean against the new base, the selection of that step kept), the
  // next on the pre-save step - a save is a checkpoint.
  assert.equal(after.past.length, 2);
  assert.equal(canonicalJson(after.past[1].draft), canonicalJson(draftFrom(saved)));
  assert.deepEqual(after.past[1].selection, { slide: 1, block: null });
  assert.equal(after.past[0].draft.title, "Deck");
  assert.deepEqual(after.future, []);
});

test("a kept draft takes the slide ids the save assigned, so the next save does not mint others", () => {
  // An older deck: its slides carry no id. The save assigns them.
  const legacy = deck(1, [
    { layout: "title", title: "One" } as PresentationSlide,
    { layout: "title", title: "Two" } as PresentationSlide,
  ]);
  const seed = draftFrom(legacy);
  const written: DeckDraft = { ...seed, title: "Renamed" };
  // Typed into the second slide during the round trip (copy-on-write: the
  // first slide keeps its object, the second is a new one).
  const typed: DeckDraft = { ...written, slides: [written.slides[0], { ...written.slides[1], title: "Two, then some" }] };
  const timeline = { draft: typed, past: [{ draft: written, selection: null }], future: [] };
  const saved = deck(2, [slide("id-one", "One"), slide("id-two", "Two")], "Renamed");
  const after = timelineAfterSave(timeline, saved, written);
  assert.deepEqual(
    after.draft.slides.map((s) => s.id),
    ["id-one", "id-two"],
  );
  assert.equal(after.draft.slides[1].title, "Two, then some", "the words typed during the save are kept");
  // A slide that already has an id keeps it; a draft with nothing to carry is returned as is.
  const withIds = draftFrom(stored);
  assert.equal(withSavedSlideIds(withIds, withIds, draftFrom(deck(2, stored.spec.slides))), withIds);
  // A slide inserted during the save (the counts differ, no identity) is left
  // to the next save to name: nothing is guessed from a position that moved.
  const grown: DeckDraft = { ...written, slides: [{ layout: "section", title: "New" } as PresentationSlide, ...written.slides] };
  assert.deepEqual(
    withSavedSlideIds(grown, written, draftFrom(saved)).slides.map((s) => s.id),
    [undefined, "id-one", "id-two"],
  );
  // A slide MOVED and edited during the save (same count, identity lost, the
  // order changed): it stays id-less rather than take the id of the slide
  // that used to sit at its place - two slides under one id would break the
  // links and the comment anchors the ids exist for.
  const movedAndEdited: DeckDraft = {
    ...written,
    slides: [{ ...written.slides[1], title: "Two, moved up and edited" }, written.slides[0]],
  };
  assert.deepEqual(
    withSavedSlideIds(movedAndEdited, written, draftFrom(saved)).slides.map((s) => s.id),
    [undefined, "id-one"],
  );
});

test("a keystroke coalesced across the save still leaves the saved deck to undo to", () => {
  const seed = draftFrom(stored);
  const written: DeckDraft = { ...seed, title: "Renamed" };
  // Coalesced: the post-save keystroke replaced the draft without a new step,
  // so no step holds the written draft.
  const typed: DeckDraft = { ...written, title: "Renamed again" };
  const timeline = { draft: typed, past: [{ draft: seed, selection: null }], future: [] };
  const saved = deck(2, stored.spec.slides, "Renamed");
  const after = timelineAfterSave(timeline, saved, written);
  assert.equal(after.draft, typed);
  // The saved deck is inserted as the step before the current draft, the
  // pre-save step kept under it.
  assert.equal(after.past.length, 2);
  assert.equal(canonicalJson(after.past[1].draft), canonicalJson(draftFrom(saved)));
  assert.equal(after.past[0].draft, seed);
});

test("without the draft it wrote, a save can only re-seed", () => {
  const timeline = { draft: { ...draftFrom(stored), title: "Renamed" }, past: [], future: [] };
  const saved = deck(2, stored.spec.slides, "Renamed");
  assert.deepEqual(timelineAfterSave(timeline, saved, undefined), timelineFrom(saved));
});

test("a save's answer is the session's only while it still sits on the base the save extended", () => {
  // Sent at 3, committed as 4.
  const sent = { id: "deck-1", revision: 3 };
  // The usual case: the session is still seeded on 3, the answer becomes the
  // base - and so does the answer of a save that changed nothing (still 3).
  assert.equal(answersBase(sent, { id: "deck-1", revision: 3 }), true);
  // The draft was emptied during the round trip (an undo) and the poll
  // brought a collaborator's 5, which the session adopted. The late answer
  // (4) is about a base it left: rebased on it, the session would read the
  // collaborator's changes as edits of its own and raise the banner over a
  // draft that was clean.
  assert.equal(answersBase(sent, { id: "deck-1", revision: 5 }), false);
  // The poll brought the save's OWN revision first (the draft was clean):
  // adopted again, the answer would only add an undo step that goes nowhere.
  assert.equal(answersBase(sent, { id: "deck-1", revision: 4 }), false);
  // Another deck: the viewer moved on during the round trip.
  assert.equal(answersBase(sent, { id: "deck-2", revision: 3 }), false);
  // A refusal is judged by the same rule.
  assert.deepEqual(refusalOf(sent, { id: "deck-1", revision: 3 }), sent);
  assert.equal(refusalOf(sent, { id: "deck-1", revision: 5 }), null);
});

test("only a newer revision of the seeded deck arrives", () => {
  const seeded = { id: "deck-1", revision: 4 };
  assert.equal(revisionArrives({ id: "deck-1", revision: 5 }, seeded), true);
  // The revision the session sits on: nothing arrives.
  assert.equal(revisionArrives({ id: "deck-1", revision: 4 }, seeded), false);
  // A read answered after the save the session adopted brings the pre-save
  // revision back: stale, never adopted - a reset would take the draft back
  // to before the save and wipe its history.
  assert.equal(revisionArrives({ id: "deck-1", revision: 3 }, seeded), false);
  // Another deck is the route's change, re-seeded by its own effect.
  assert.equal(revisionArrives({ id: "deck-2", revision: 9 }, seeded), false);
});

test("a merge waits for the answer of a save of the seeded base, and nothing else does", () => {
  const seeded = { id: "deck-1", revision: 3 };
  // No save on its way: the revision watch decides at once.
  assert.equal(defersToSave(null, seeded), false);
  // A save sent at 3 is on its way: the 4 the poll brings may be its own
  // commit - the merge waits for the answer, which rebases the session
  // before the watch compares again.
  assert.equal(defersToSave({ id: "deck-1", revision: 3 }, seeded), true);
  // A save of a base the session has left (a clean draft already merged onto
  // a newer revision, another deck) says nothing about the draft now.
  assert.equal(defersToSave({ id: "deck-1", revision: 2 }, seeded), false);
  assert.equal(defersToSave({ id: "deck-2", revision: 3 }, seeded), false);
});

test("a deck read back from the server reuses the draft's own pieces wherever it stored them as sent", () => {
  const sent = draftFrom(stored);
  // Stored exactly as sent: the very draft, nothing to redraw.
  assert.equal(shareDraft(draftFrom(stored), sent), sent);
  // One slide stored differently (the server named a legacy slide, or a
  // collaborator edited it): that slide is the server's, the others the draft's.
  const answer = draftFrom(deck(2, [slide("a", "One"), slide("b", "Two, theirs")]));
  const shared = shareDraft(answer, sent);
  assert.notEqual(shared, sent);
  assert.equal(shared.slides[0], sent.slides[0]);
  assert.equal(shared.slides[1], answer.slides[1]);
  assert.equal(shared.chrome, sent.chrome);
});

test("a revision saved elsewhere rebases the draft AND its history, so an undo never takes the collaborator's change back", () => {
  const seed = draftFrom(stored);
  // The person renamed the deck, then edited slide b.
  const renamed: DeckDraft = { ...seed, title: "Renamed" };
  const edited: DeckDraft = { ...renamed, slides: [renamed.slides[0], slide("b", "Two, mine")] };
  const current: Timeline = {
    draft: edited,
    past: [
      { draft: seed, selection: null },
      { draft: renamed, selection: null },
    ],
    future: [],
  };
  // A collaborator retitled slide a meanwhile.
  const theirs = draftFrom(deck(2, [slide("a", "One, theirs"), slide("b", "Two")]));
  const { timeline, conflicts } = timelineAfterRemote(current, seed, theirs);
  assert.deepEqual(conflicts, []);
  const titlesOf = (value: DeckDraft) => value.slides.map((s) => (s as { title: string }).title);
  assert.deepEqual(titlesOf(timeline.draft), ["One, theirs", "Two, mine"]);
  assert.equal(timeline.draft.title, "Renamed");
  // Every step carries their change: undoing the person's own edits walks
  // back to "theirs plus nothing of mine", never to before their save.
  assert.deepEqual(titlesOf(timeline.past[1].draft), ["One, theirs", "Two"]);
  assert.equal(timeline.past[1].draft.title, "Renamed");
  assert.equal(timeline.past[0].draft, theirs);
});

test("a block can be added to the end of the body, of each column, and inside the selected container", () => {
  const text = (content: string) => ({ type: "text", content });
  const flow = { id: "a", layout: "content", title: "Flow", blocks: [text("One"), text("Two")] } as PresentationSlide;
  assert.deepEqual(addableRuns(flow, null), [{ list: "blocks", length: 2, kind: "body" }]);
  // An empty body still takes a first block.
  const empty = { id: "b", layout: "content", title: "Empty", blocks: [] } as unknown as PresentationSlide;
  assert.deepEqual(addableRuns(empty, null), [{ list: "blocks", length: 0, kind: "body" }]);
  const columns = {
    id: "c",
    layout: "content",
    title: "Columns",
    columns: [
      [{ type: "callout", title: "Note", children: [text("Inside")] }],
      [{ type: "comparison", left: { label: "L", blocks: [text("l")] }, right: { label: "R", blocks: [text("r")] } }],
    ],
  } as unknown as PresentationSlide;
  assert.deepEqual(
    addableRuns(columns, null).map((run) => [run.list, run.length, run.kind, run.column]),
    [
      ["columns.0", 1, "column", 1],
      ["columns.1", 1, "column", 2],
    ],
  );
  // A container selected - or a block inside it - offers its own runs after the body's.
  for (const selected of ["columns.0.0", "columns.0.0.children.0"]) {
    const inside = addableRuns(columns, selected).filter((run) => run.kind === "inside");
    assert.deepEqual(inside, [{ list: "columns.0.0.children", length: 1, kind: "inside", container: "columns.0.0" }], selected);
  }
  assert.deepEqual(
    addableRuns(columns, "columns.1.0")
      .filter((run) => run.kind === "inside")
      .map((run) => run.list),
    ["columns.1.0.left.blocks", "columns.1.0.right.blocks"],
  );
  // Layouts that hold items, not blocks, take none.
  assert.deepEqual(addableRuns(slide("t", "Title"), null), []);
  assert.deepEqual(addableRuns(undefined, null), []);
});
