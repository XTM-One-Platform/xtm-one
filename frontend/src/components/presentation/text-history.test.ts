/**
 * The session history of a text edited in place - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { TextRun } from "./inline-runs.ts";
import {
  TEXT_COALESCE_MS,
  TEXT_HISTORY_LIMIT,
  recordSnapshot,
  redoSnapshot,
  textHistoryFrom,
  undoSnapshot,
  type TextSnapshot,
} from "./text-history.ts";

const plain = (text: string, caret = text.length): TextSnapshot => ({
  runs: [{ text, style: "plain" }],
  start: caret,
  end: caret,
});

test("typing within the window coalesces into one step; a pause starts another", () => {
  let history = textHistoryFrom(plain(""));
  history = recordSnapshot(history, plain("h"), "insertText", 1000);
  history = recordSnapshot(history, plain("he"), "insertText", 1100);
  history = recordSnapshot(history, plain("hel"), "insertText", 1200);
  assert.equal(history.past.length, 1, "one burst, one step");
  assert.equal(history.present.runs[0].text, "hel");
  // After the window: a new step.
  history = recordSnapshot(history, plain("hello"), "insertText", 1200 + TEXT_COALESCE_MS + 1);
  assert.equal(history.past.length, 2);
  // Undo takes the second burst back, then the first - to the empty field.
  const once = undoSnapshot(history);
  assert.ok(once);
  assert.equal(once.present.runs[0].text, "hel");
  const twice = undoSnapshot(once);
  assert.ok(twice);
  assert.equal(twice.present.runs[0].text, "");
  assert.equal(undoSnapshot(twice), null, "nothing left to undo");
  // Redo walks forward again.
  const forward = redoSnapshot(twice);
  assert.ok(forward);
  assert.equal(forward.present.runs[0].text, "hel");
  assert.equal(redoSnapshot(redoSnapshot(forward)!), null);
});

test("deleting is its own kind: a delete after typing is a separate step", () => {
  let history = textHistoryFrom(plain("hello"));
  history = recordSnapshot(history, plain("hello w"), "insertText", 1000);
  history = recordSnapshot(history, plain("hello "), "deleteContentBackward", 1100);
  assert.equal(history.past.length, 2);
  assert.equal(undoSnapshot(history)!.present.runs[0].text, "hello w");
});

test("a formatting, a line break and a paste are never coalesced", () => {
  const bolded: TextSnapshot = {
    runs: [
      { text: "hello ", style: "plain" },
      { text: "world", style: "bold" },
    ],
    start: 6,
    end: 11,
  };
  let history = textHistoryFrom(plain("hello world"));
  history = recordSnapshot(history, bolded, "format", 1000);
  history = recordSnapshot(history, { ...bolded, runs: [{ text: "hello world", style: "plain" }] }, "format", 1010);
  assert.equal(history.past.length, 2, "two formattings ten milliseconds apart are two steps");
  history = recordSnapshot(history, plain("hello world pasted"), "insertFromPaste", 1020);
  history = recordSnapshot(history, plain("hello world pasted again"), "insertFromPaste", 1030);
  assert.equal(history.past.length, 4);
  // Undo restores the bold run AND the selection it was applied to.
  const back = undoSnapshot(undoSnapshot(undoSnapshot(history)!)!)!;
  assert.deepEqual(back.present, bolded);
});

test("a cut, a drag out of the field and the browser's own line break are steps of their own too", () => {
  // Two Ctrl+X within the window: two things to undo, like two pastes.
  let history = textHistoryFrom(plain("one two three"));
  history = recordSnapshot(history, plain("one two ", 8), "deleteByCut", 1000, { start: 8, end: 13 });
  history = recordSnapshot(history, plain("one ", 4), "deleteByCut", 1010, { start: 4, end: 8 });
  assert.equal(history.past.length, 2, "two cuts are two steps");
  assert.equal(undoSnapshot(history)!.present.runs[0].text, "one two ");
  // A selection dragged out of the field.
  history = recordSnapshot(history, plain("", 0), "deleteByDrag", 1020, { start: 0, end: 4 });
  assert.equal(history.past.length, 3);
  // A Shift+Enter the editor let through (`insertLineBreak`) and a paragraph
  // break: each its own step, even back to back.
  let breaking = textHistoryFrom(plain("ab", 1));
  breaking = recordSnapshot(breaking, plain("a\nb", 2), "insertLineBreak", 1000, { start: 1, end: 1 });
  breaking = recordSnapshot(breaking, plain("a\n\nb", 3), "insertLineBreak", 1010, { start: 2, end: 2 });
  breaking = recordSnapshot(breaking, plain("a\n\n\nb", 4), "insertParagraph", 1020, { start: 3, end: 3 });
  assert.equal(breaking.past.length, 3);
  assert.equal(undoSnapshot(breaking)!.present.runs[0].text, "a\n\nb");
});

test("a change that leaves the runs as they were records nothing; a new change drops the redo stack", () => {
  let history = textHistoryFrom(plain("abc"));
  history = recordSnapshot(history, plain("abc", 1), "insertText", 1000);
  assert.equal(history.past.length, 0, "a moved caret is not a step");
  history = recordSnapshot(history, plain("abcd"), "insertText", 1000);
  const undone = undoSnapshot(history)!;
  assert.equal(undone.future.length, 1);
  const branched = recordSnapshot(undone, plain("abx"), "insertText", 5000);
  assert.deepEqual(branched.future, [], "typing after an undo is a new branch");
  assert.equal(branched.past.length, 1);
  assert.equal(redoSnapshot(branched), null);
});

test("a burst breaks when the next keystroke does not begin where the last one left the caret", () => {
  // `ab` typed at the end, the caret moved to the start, `X` typed there
  // within the window: two things to undo, not one.
  let history = textHistoryFrom(plain(""));
  history = recordSnapshot(history, plain("a"), "insertText", 1000, { start: 0, end: 0 });
  history = recordSnapshot(history, plain("ab"), "insertText", 1100, { start: 1, end: 1 });
  assert.equal(history.past.length, 1, "contiguous typing is one burst");
  history = recordSnapshot(history, plain("Xab", 1), "insertText", 1200, { start: 0, end: 0 });
  assert.equal(history.past.length, 2, "typing somewhere else is a step of its own");
  const once = undoSnapshot(history)!;
  assert.equal(once.present.runs[0].text, "ab");
  assert.deepEqual([once.present.start, once.present.end], [0, 0], "undone to where the second edit began");
  const twice = undoSnapshot(once)!;
  assert.equal(twice.present.runs[0].text, "");
  // Successive backspaces begin where the previous one left the caret: one burst.
  let deleting = textHistoryFrom(plain("abc"));
  deleting = recordSnapshot(deleting, plain("ab"), "deleteContentBackward", 1000, { start: 3, end: 3 });
  deleting = recordSnapshot(deleting, plain("a"), "deleteContentBackward", 1100, { start: 2, end: 2 });
  assert.equal(deleting.past.length, 1);
  // A selection replaced by a keystroke does not continue the burst either.
  let replacing = textHistoryFrom(plain(""));
  replacing = recordSnapshot(replacing, plain("abc"), "insertText", 1000, { start: 0, end: 0 });
  replacing = recordSnapshot(replacing, plain("aXc", 2), "insertText", 1100, { start: 1, end: 2 });
  assert.equal(replacing.past.length, 2);
});

test("an undo restores the selection the change began at, not the one the previous snapshot had", () => {
  // The field opened with the caret at the end; the person clicked before
  // `a` and typed `X`: undoing puts the caret back at 0, where they were.
  let history = textHistoryFrom(plain("abc"));
  history = recordSnapshot(history, plain("Xabc", 1), "insertText", 1000, { start: 0, end: 0 });
  const undone = undoSnapshot(history)!;
  assert.deepEqual([undone.present.start, undone.present.end], [0, 0]);
  // A word selected, then formatted: undone, the word is selected again.
  const bolded: TextSnapshot = { runs: [{ text: "abc", style: "bold" }], start: 0, end: 3 };
  let formatting = textHistoryFrom(plain("abc"));
  formatting = recordSnapshot(formatting, bolded, "format", 1000, { start: 0, end: 3 });
  assert.deepEqual([undoSnapshot(formatting)!.present.start, undoSnapshot(formatting)!.present.end], [0, 3]);
  // A caret moved with nothing typed is not a step, but it is remembered.
  let moved = textHistoryFrom(plain("abc"));
  moved = recordSnapshot(moved, plain("abc", 1), "insertText", 1000, { start: 1, end: 1 });
  assert.equal(moved.past.length, 0);
  assert.deepEqual([moved.present.start, moved.present.end], [1, 1]);
  assert.equal(moved.lastKind, null, "nothing was recorded");
});

test("the history is bounded", () => {
  let history = textHistoryFrom(plain(""));
  for (let index = 0; index < TEXT_HISTORY_LIMIT + 50; index += 1) {
    history = recordSnapshot(history, plain("x".repeat(index + 1)), "insertText", index * (TEXT_COALESCE_MS + 1));
  }
  assert.equal(history.past.length, TEXT_HISTORY_LIMIT);
});

test("a tone is part of the run's identity", () => {
  const info: TextRun[] = [{ text: "a", style: "span", tone: "info" }];
  const error: TextRun[] = [{ text: "a", style: "span", tone: "error" }];
  let history = textHistoryFrom({ runs: info, start: 1, end: 1 });
  history = recordSnapshot(history, { runs: error, start: 1, end: 1 }, "format", 1000);
  assert.equal(history.past.length, 1);
});
