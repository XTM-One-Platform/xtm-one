/**
 * A render's chapters in the player - `yarn test:unit`.
 *
 * Only chapters inside the video are drawn, in order, each running to the
 * next; one chapter is no chapters at all. The chapter playing is found from
 * the playhead, and previous / next seek like a player's.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { CHAPTER_RESTART_SECONDS, chapterAt, chapterSeek, chapterSpans } from "./player-chapters.ts";

const CHAPTERS = [
  { start: 0, type: "title", title: "From Normandy" },
  { start: 3.3, type: "clip", title: "Dawn" },
  { start: 9.7, type: "statement", title: null },
  { start: 14, type: "outro", title: "Thanks" },
];

test("each chapter runs to the next one's start, the last to the end", () => {
  const spans = chapterSpans(CHAPTERS, 18);
  assert.deepEqual(
    spans.map((s) => [s.index, s.start, s.end]),
    [
      [0, 0, 3.3],
      [1, 3.3, 9.7],
      [2, 9.7, 14],
      [3, 14, 18],
    ],
  );
});

test("a chapter outside the video, a repeated one and a lone one are not drawn", () => {
  assert.deepEqual(
    chapterSpans([...CHAPTERS, { start: 30, type: "clip", title: null }], 18).map((s) => s.start),
    [0, 3.3, 9.7, 14],
  );
  assert.deepEqual(
    chapterSpans([CHAPTERS[1], CHAPTERS[0], { ...CHAPTERS[1] }], 18).map((s) => s.start),
    [0, 3.3],
  );
  assert.deepEqual(chapterSpans([CHAPTERS[0]], 18), []);
  assert.deepEqual(chapterSpans(undefined, 18), []);
  assert.deepEqual(chapterSpans(CHAPTERS, 0), []);
  // The first chapter starts the video whatever it recorded.
  assert.equal(chapterSpans([{ start: 0.4, type: "title", title: null }, CHAPTERS[1]], 18)[0].start, 0);
});

test("the chapter playing is the last one started", () => {
  const spans = chapterSpans(CHAPTERS, 18);
  assert.equal(chapterAt(spans, 0)?.title, "From Normandy");
  assert.equal(chapterAt(spans, 9.69)?.title, "Dawn");
  assert.equal(chapterAt(spans, 9.7)?.type, "statement");
  assert.equal(chapterAt(spans, 17.9)?.title, "Thanks");
  assert.equal(chapterAt([], 4), null);
});

test("next goes to the next chapter, previous to this one's start once it played a moment", () => {
  const spans = chapterSpans(CHAPTERS, 18);
  assert.equal(chapterSeek(spans, 5, 1), 9.7);
  assert.equal(chapterSeek(spans, 15, 1), null);
  assert.equal(chapterSeek(spans, 9.7 + CHAPTER_RESTART_SECONDS + 1, -1), 9.7);
  assert.equal(chapterSeek(spans, 10, -1), 3.3);
  assert.equal(chapterSeek(spans, 1, -1), 0);
  assert.equal(chapterSeek([], 4, 1), null);
});
