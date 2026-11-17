/**
 * Unit tests for the three-way merge of a deck draft with a revision saved
 * elsewhere - `yarn test:unit`.
 *
 * `base` is the revision the draft started from, `ours` the local draft,
 * `theirs` the revision a collaborator (or an agent) saved meanwhile. What is
 * pinned: what one side alone changed survives; slides are matched by id
 * through inserts, deletes and moves; a list merges by element; a clash keeps
 * ours and is reported; nothing is duplicated; unchanged pieces keep their
 * identity.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { DeckBlock, PresentationSlide } from "@/types";

import { mergeDrafts, pathUntouched, slideIndexAfter, slidesChangedBy } from "./deck-merge.ts";
import type { DeckDraft } from "./presentation-editor.ts";

function title(id: string, text: string): PresentationSlide {
  return { id, layout: "title", title: text } as PresentationSlide;
}

function content(id: string, heading: string, blocks: DeckBlock[]): PresentationSlide {
  return { id, layout: "content", title: heading, blocks } as unknown as PresentationSlide;
}

const text = (value: string): DeckBlock => ({ type: "text", content: value }) as DeckBlock;
const quote = (value: string, attribution = "Someone"): DeckBlock =>
  ({ type: "quote", content: value, attribution }) as unknown as DeckBlock;

function draft(slides: PresentationSlide[], overrides: Partial<DeckDraft> = {}): DeckDraft {
  return {
    title: "Deck",
    theme: "dark",
    transition: "none",
    template_container_id: null,
    chrome: {},
    slides,
    ...overrides,
  };
}

const ids = (value: DeckDraft) => value.slides.map((slide) => slide.id);
const titles = (value: DeckDraft) => value.slides.map((slide) => (slide as { title?: string }).title);

const withSlide = (value: DeckDraft, index: number, slide: PresentationSlide): DeckDraft => ({
  ...value,
  slides: value.slides.map((current, at) => (at === index ? slide : current)),
});

const base = draft([title("a", "One"), title("b", "Two"), title("c", "Three")]);

test("what only one side changed is taken from that side, and an untouched draft becomes theirs", () => {
  const theirs = withSlide(base, 1, title("b", "Two, theirs"));
  assert.equal(mergeDrafts(base, base, theirs).draft, theirs);

  const ours = withSlide(base, 0, title("a", "One, ours"));
  const merged = mergeDrafts(base, ours, base);
  assert.equal(merged.draft, ours);
  assert.deepEqual(merged.conflicts, []);
});

test("both sides editing different slides keeps both edits", () => {
  const ours = withSlide(base, 0, title("a", "One, ours"));
  const theirs = withSlide(base, 2, title("c", "Three, theirs"));
  const { draft: merged, conflicts } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(titles(merged), ["One, ours", "Two", "Three, theirs"]);
  assert.deepEqual(conflicts, []);
  // The pieces nobody else touched are the very objects that went in.
  assert.equal(merged.slides[0], ours.slides[0]);
  assert.equal(merged.slides[2], theirs.slides[2]);
});

test("the same field changed differently on both sides keeps ours and names the slide", () => {
  const ours = withSlide(base, 1, title("b", "Two, ours"));
  const theirs = withSlide(base, 1, title("b", "Two, theirs"));
  const { draft: merged, conflicts } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(titles(merged), ["One", "Two, ours", "Three"]);
  assert.deepEqual(conflicts, [{ slideId: "b", path: "title" }]);
});

test("the same change made on both sides is no conflict", () => {
  const ours = withSlide(base, 1, title("b", "Same"));
  const theirs = withSlide(base, 1, title("b", "Same"));
  const { conflicts } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(conflicts, []);
});

test("a slide inserted on their side lands after the slide it followed, next to an edit of ours", () => {
  const theirs = draft([title("a", "One"), title("x", "Inserted"), title("b", "Two"), title("c", "Three")]);
  const ours = withSlide(base, 2, title("c", "Three, ours"));
  const { draft: merged, conflicts } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(ids(merged), ["a", "x", "b", "c"]);
  assert.deepEqual(titles(merged), ["One", "Inserted", "Two", "Three, ours"]);
  assert.deepEqual(conflicts, []);
});

test("slides inserted on both sides are all kept", () => {
  const ours = draft([title("a", "One"), title("o", "Ours"), title("b", "Two"), title("c", "Three")]);
  const theirs = draft([title("a", "One"), title("b", "Two"), title("c", "Three"), title("t", "Theirs")]);
  const { draft: merged } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(ids(merged), ["a", "o", "b", "c", "t"]);
});

test("a slide deleted on their side goes, unless ours edited it: then it stays and is reported", () => {
  const theirs = draft([title("a", "One"), title("c", "Three")]);
  assert.deepEqual(ids(mergeDrafts(base, withSlide(base, 0, title("a", "One, ours")), theirs).draft), ["a", "c"]);

  const ours = withSlide(base, 1, title("b", "Two, ours"));
  const { draft: merged, conflicts } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(ids(merged), ["a", "b", "c"]);
  assert.equal((merged.slides[1] as { title: string }).title, "Two, ours");
  assert.deepEqual(conflicts, [{ slideId: "b", path: "" }]);
});

test("a slide deleted on our side that they edited comes back with their edit, and is reported", () => {
  const ours = draft([title("a", "One"), title("c", "Three")]);
  const theirs = withSlide(base, 1, title("b", "Two, theirs"));
  const { draft: merged, conflicts, restored } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(ids(merged), ["a", "b", "c"]);
  assert.equal((merged.slides[1] as { title: string }).title, "Two, theirs");
  assert.deepEqual(conflicts, []);
  assert.deepEqual(restored, ["b"]);

  // A slide they left alone stays deleted, and nothing is said.
  const untouched = mergeDrafts(base, ours, withSlide(base, 0, title("a", "One, theirs")));
  assert.deepEqual(ids(untouched.draft), ["a", "c"]);
  assert.deepEqual(untouched.restored, []);
});

test("a move on their side and an edit of ours both survive", () => {
  const theirs = draft([title("c", "Three"), title("a", "One"), title("b", "Two")]);
  const ours = withSlide(base, 1, title("b", "Two, ours"));
  const { draft: merged, conflicts } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(ids(merged), ["c", "a", "b"]);
  assert.equal((merged.slides[2] as { title: string }).title, "Two, ours");
  assert.deepEqual(conflicts, []);
});

test("two different reorders keep ours and say so", () => {
  const ours = draft([title("b", "Two"), title("a", "One"), title("c", "Three")]);
  const theirs = draft([title("a", "One"), title("c", "Three"), title("b", "Two")]);
  const { draft: merged, conflicts } = mergeDrafts(base, ours, theirs);
  assert.deepEqual(ids(merged), ["b", "a", "c"]);
  assert.deepEqual(conflicts, [{ slideId: null, path: "slides" }]);
});

const blocksBase = draft([content("s", "Slide", [text("First"), text("Second"), text("Third")])]);
const blocksOf = (value: DeckDraft) => (value.slides[0] as { blocks: DeckBlock[] }).blocks;
const withBlocks = (blocks: DeckBlock[]) => draft([content("s", "Slide", blocks)]);

test("edits to different blocks of one slide both land", () => {
  const ours = withBlocks([text("First, ours"), text("Second"), text("Third")]);
  const theirs = withBlocks([text("First"), text("Second"), text("Third, theirs")]);
  const { draft: merged, conflicts } = mergeDrafts(blocksBase, ours, theirs);
  assert.deepEqual(
    blocksOf(merged).map((block) => (block as { content: string }).content),
    ["First, ours", "Second", "Third, theirs"],
  );
  assert.deepEqual(conflicts, []);
});

test("a block added on their side and a word typed in another block on ours both land", () => {
  const ours = withBlocks([text("First"), text("Second, ours"), text("Third")]);
  const theirs = withBlocks([text("First"), text("Second"), text("Third"), quote("Added")]);
  const { draft: merged, conflicts } = mergeDrafts(blocksBase, ours, theirs);
  assert.deepEqual(
    blocksOf(merged).map((block) => (block as { content: string }).content),
    ["First", "Second, ours", "Third", "Added"],
  );
  assert.deepEqual(conflicts, []);
});

test("one block edited on both sides merges field by field", () => {
  const was = withBlocks([quote("Words", "Ada")]);
  const ours = withBlocks([quote("Words, ours", "Ada")]);
  const theirs = withBlocks([quote("Words", "Ada Lovelace")]);
  const { draft: merged, conflicts } = mergeDrafts(was, ours, theirs);
  assert.deepEqual(blocksOf(merged), [quote("Words, ours", "Ada Lovelace")]);
  assert.deepEqual(conflicts, []);
});

test("a block moved on their side and edited on ours keeps our list whole, never a copy of the block", () => {
  const ours = withBlocks([text("First, ours"), text("Second"), text("Third")]);
  const theirs = withBlocks([text("Second"), text("Third"), text("First")]);
  const { draft: merged, conflicts } = mergeDrafts(blocksBase, ours, theirs);
  assert.equal(blocksOf(merged), blocksOf(ours));
  assert.deepEqual(conflicts, [{ slideId: "s", path: "blocks" }]);
});

test("the same block added at the same place on both sides is added once", () => {
  const ours = withBlocks([text("First"), text("Second"), text("Third"), quote("Same")]);
  const theirs = withBlocks([text("First"), text("Second"), text("Third"), quote("Same")]);
  const { draft: merged } = mergeDrafts(blocksBase, ours, theirs);
  assert.equal(blocksOf(merged).length, 4);
});

test("deck settings merge one by one: a clash keeps ours, an edit of theirs alone lands", () => {
  const ours = { ...base, title: "Ours" };
  const theirs = { ...base, title: "Theirs", theme: "light" as const };
  const { draft: merged, conflicts } = mergeDrafts(base, ours, theirs);
  assert.equal(merged.title, "Ours");
  assert.equal(merged.theme, "light");
  assert.deepEqual(conflicts, [{ slideId: null, path: "title" }]);
});

test("an older deck whose slides carry no ids is merged by content", () => {
  const legacy = draft([{ layout: "title", title: "One" } as PresentationSlide, { layout: "title", title: "Two" } as PresentationSlide]);
  const ours = withSlide(legacy, 0, { layout: "title", title: "One, ours" } as PresentationSlide);
  const theirs = withSlide(legacy, 1, { layout: "title", title: "Two, theirs" } as PresentationSlide);
  const { draft: merged, conflicts } = mergeDrafts(legacy, ours, theirs);
  assert.deepEqual(titles(merged), ["One, ours", "Two, theirs"]);
  assert.deepEqual(conflicts, []);
});

test("a text open in place is left alone by a change elsewhere on its slide, never by one that moves or rewrites it", () => {
  const slideOf = (value: DeckDraft) => value.slides[0];
  const listed = (items: string[]) => ({ type: "list", items: items.map((value) => ({ text: value })) }) as unknown as DeckBlock;
  const was = draft([content("s", "Slide", [text("First"), listed(["One", "Two"]), text("Third")])]);
  const changedTo = (heading: string, blocks: DeckBlock[]) => slideOf(draft([content("s", heading, blocks)]));

  // Another block changed, or the slide's title: the paragraph and the bullet stay open.
  const elsewhere = changedTo("Slide, theirs", [text("First"), listed(["One", "Two"]), text("Third, theirs")]);
  assert.equal(pathUntouched(slideOf(was), elsewhere, "blocks.0.content"), true);
  assert.equal(pathUntouched(slideOf(was), elsewhere, "blocks.1.items.1.text"), true);
  assert.equal(pathUntouched(slideOf(was), elsewhere, "blocks.1"), true);
  assert.equal(pathUntouched(slideOf(was), elsewhere, "title"), false);

  // The text itself, or another bullet of its list item - the item it sits in changed.
  const same = changedTo("Slide", [text("First, theirs"), listed(["One", "Two"]), text("Third")]);
  assert.equal(pathUntouched(slideOf(was), same, "blocks.0.content"), false);
  const sibling = changedTo("Slide", [text("First"), listed(["One, theirs", "Two"]), text("Third")]);
  assert.equal(pathUntouched(slideOf(was), sibling, "blocks.1.items.1.text"), true);
  assert.equal(pathUntouched(slideOf(was), sibling, "blocks.1"), false);

  // A block inserted before it, a bullet removed before it: the index moved.
  const inserted = changedTo("Slide", [quote("New"), text("First"), listed(["One", "Two"]), text("Third")]);
  assert.equal(pathUntouched(slideOf(was), inserted, "blocks.2.content"), false);
  assert.equal(pathUntouched(slideOf(was), inserted, "title"), true);
  const removed = changedTo("Slide", [text("First"), listed(["Two"]), text("Third")]);
  assert.equal(pathUntouched(slideOf(was), removed, "blocks.1.items.0.text"), false);

  // Their slide gone: nothing is left to type into.
  assert.equal(pathUntouched(slideOf(was), undefined, "title"), false);
});

test("the slides a collaborator changed are named by id, and a slide is followed to where it went", () => {
  const theirs = draft([title("x", "New"), title("a", "One"), title("b", "Two, theirs"), title("c", "Three")]);
  assert.deepEqual([...slidesChangedBy(base, theirs)].sort(), ["b", "x"]);
  assert.equal(slideIndexAfter(base, theirs, 0), 1);
  assert.equal(slideIndexAfter(base, theirs, 2), 3);
  assert.equal(slideIndexAfter(base, draft([title("a", "One")]), 1), null);
});
