/**
 * Unit tests for the editor's spec paths - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  blockPathOf,
  blockPlaces,
  blockSlot,
  childPath,
  getAt,
  insertAt,
  isWithin,
  joinPath,
  moveAt,
  parsePath,
  removeAt,
  setAt,
} from "./edit-paths.ts";

test("a path round-trips through parse and join, digits read as indexes", () => {
  assert.deepEqual(parsePath("blocks.2.items.0.text"), ["blocks", 2, "items", 0, "text"]);
  assert.equal(joinPath(["blocks", 2, "items", 0, "text"]), "blocks.2.items.0.text");
  assert.deepEqual(parsePath(""), []);
  assert.equal(childPath("blocks.2", "items", 0, "text"), "blocks.2.items.0.text");
  assert.equal(childPath("", "title"), "title");
  assert.equal(childPath("blocks.2"), "blocks.2");
});

test("setAt writes copy-on-write and leaves every other branch its identity", () => {
  const slide = {
    layout: "content",
    blocks: [
      { type: "text", content: "a" },
      { type: "list", items: [{ text: "one" }, { text: "two" }] },
    ],
  };
  const next = setAt(slide, ["blocks", 1, "items", 0, "text"], "uno");
  assert.equal(getAt(next, ["blocks", 1, "items", 0, "text"]), "uno");
  assert.equal(getAt(slide, ["blocks", 1, "items", 0, "text"]), "one", "the original is untouched");
  assert.notEqual(next, slide);
  assert.notEqual(next.blocks, slide.blocks);
  assert.equal(next.blocks[0], slide.blocks[0], "the sibling block keeps its identity");
  assert.equal((next.blocks[1] as { items: unknown[] }).items[1], (slide.blocks[1] as { items: unknown[] }).items[1]);
});

test("setAt with undefined deletes the key, which is how an optional field is unset", () => {
  const block = { type: "text", content: "a", tone: "accent" };
  const next = setAt(block, ["tone"], undefined) as Record<string, unknown>;
  assert.equal("tone" in next, false);
  assert.equal(next.content, "a");
});

test("removeAt splices an array item, insertAt and moveAt reorder it", () => {
  const slide = { blocks: [{ id: "a" }, { id: "b" }, { id: "c" }] };
  assert.deepEqual(removeAt(slide, ["blocks", 1]).blocks.map((b) => b.id), ["a", "c"]);
  assert.deepEqual(insertAt(slide, ["blocks"], 1, { id: "x" }).blocks.map((b) => b.id), ["a", "x", "b", "c"]);
  assert.deepEqual(insertAt(slide, ["blocks"], 99, { id: "x" }).blocks.map((b) => b.id), ["a", "b", "c", "x"]);
  assert.deepEqual(moveAt(slide, ["blocks"], 0, 2).blocks.map((b) => b.id), ["b", "c", "a"]);
  assert.deepEqual(moveAt(slide, ["blocks"], 2, 0).blocks.map((b) => b.id), ["c", "a", "b"]);
  assert.equal(moveAt(slide, ["blocks"], 0, 5), slide, "an out-of-range move is a no-op");
});

test("isWithin compares whole segments, never prefixes of a number", () => {
  assert.equal(isWithin("blocks.2.children.1", "blocks.2"), true);
  assert.equal(isWithin("blocks.2", "blocks.2"), true);
  assert.equal(isWithin("blocks.20", "blocks.2"), false);
});

test("blockPathOf finds the innermost block a text path lives in", () => {
  assert.equal(blockPathOf("blocks.2.items.0.text"), "blocks.2");
  assert.equal(blockPathOf("blocks.2.children.1.content"), "blocks.2.children.1");
  assert.equal(blockPathOf("blocks.3.left.blocks.0.content"), "blocks.3.left.blocks.0");
  assert.equal(blockPathOf("blocks.0.columns.1.0.content"), "blocks.0.columns.1.0");
  assert.equal(blockPathOf("columns.0.1.items.2.text"), "columns.0.1");
  assert.equal(blockPathOf("footer_block.title"), "footer_block");
  assert.equal(blockPathOf("panels.1.block.caption"), "panels.1.block");
  assert.equal(blockPathOf("panels.1.title"), null, "a panel title is slide-level text");
  assert.equal(blockPathOf("title"), null);
  assert.equal(blockPathOf("items.0.title"), null, "an agenda item is slide-level text");
});

test("blockSlot names the list a block sits in, or nothing for a lone slot", () => {
  assert.deepEqual(blockSlot("blocks.2"), { list: ["blocks"], index: 2 });
  assert.deepEqual(blockSlot("columns.0.1"), { list: ["columns", 0], index: 1 });
  assert.deepEqual(blockSlot("blocks.3.left.blocks.0"), { list: ["blocks", 3, "left", "blocks"], index: 0 });
  assert.equal(blockSlot("footer_block"), null);
  assert.equal(blockSlot("panels.1.block"), null);
});

test("blockPlaces reads a block path as 1-based steps a person reads", () => {
  assert.deepEqual(blockPlaces("blocks.0"), [{ kind: "block", n: 1 }]);
  assert.deepEqual(blockPlaces("columns.0.0"), [{ kind: "column", n: 1 }, { kind: "block", n: 1 }]);
  assert.deepEqual(blockPlaces("blocks.3.left.blocks.0"), [
    { kind: "block", n: 4 },
    { kind: "left" },
    { kind: "block", n: 1 },
  ]);
  assert.deepEqual(blockPlaces("blocks.1.children.2"), [{ kind: "block", n: 2 }, { kind: "block", n: 3 }]);
  assert.deepEqual(blockPlaces("panels.2.block"), [{ kind: "panel", n: 3 }]);
  assert.deepEqual(blockPlaces("footer_block"), [{ kind: "footer" }]);
});
