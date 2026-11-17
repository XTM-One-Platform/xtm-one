/**
 * Unit tests for the runs model of the in-place text editor - `yarn
 * test:unit`. The DOM half (`InlineTextEditor`) is exercised on a live deck by
 * `tests_e2e/presentation-editor.spec.ts`; pinned here is the arithmetic both
 * halves rely on and the one promise the serializer makes: what it writes,
 * the validator accepts and the tokenizer reads back.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { markupProblem, parseInlineMarkup } from "./markup.ts";
import {
  applyStyle,
  canonicalRuns,
  inlineCommit,
  moveRange,
  normalizeRuns,
  rangeHasStyle,
  replaceRange,
  runsFromMarkup,
  runsToMarkup,
  runsToPlainText,
  styleAt,
  type TextRun,
} from "./inline-runs.ts";

const roundTrip = (markup: string) => runsToMarkup(runsFromMarkup(markup));

test("every construct of the grammar round-trips unchanged", () => {
  for (const markup of [
    "plain text",
    "**bold** and _em_ and `code` and [[success:green]]",
    "line one\nline two",
    "a [[gradient:brand]] word",
    "**bold** then [[warning:risk]] then `x`",
  ]) {
    assert.equal(roundTrip(markup), markup);
    assert.equal(markupProblem(roundTrip(markup)), null);
  }
});

test("runsFromMarkup keeps a newline as its own plain run and merges neighbours", () => {
  assert.deepEqual(runsFromMarkup("ab\ncd"), [
    { text: "ab", style: "plain" },
    { text: "\n", style: "plain" },
    { text: "cd", style: "plain" },
  ]);
  assert.deepEqual(normalizeRuns([{ text: "a", style: "bold" }, { text: "b", style: "bold" }]), [
    { text: "ab", style: "bold" },
  ]);
});

test("edge whitespace leaves a styled run so `** a **` is never written", () => {
  const runs: TextRun[] = [{ text: "x ", style: "plain" }, { text: " a ", style: "bold" }, { text: "y", style: "plain" }];
  assert.equal(runsToMarkup(runs), "x  **a** y");
  assert.deepEqual(runsFromMarkup(runsToMarkup(runs)).filter((r) => r.style === "bold"), [{ text: "a", style: "bold" }]);
});

test("a run the grammar cannot carry is written plain, never as markup the validator refuses", () => {
  // Emphasis glued to a letter (the tokenizer's word boundary) and emphasis
  // holding an underscore: both fall back to the words alone.
  assert.equal(runsToMarkup([{ text: "snake", style: "plain" }, { text: "case", style: "em" }]), "snakecase");
  assert.equal(runsToMarkup([{ text: "a_b", style: "em" }]), "a_b");
  // A backtick inside code, a `]` inside a span, `**` inside bold.
  assert.equal(runsToMarkup([{ text: "a`b", style: "code" }]), "a`b");
  assert.equal(runsToMarkup([{ text: "a]b", style: "span", tone: "accent" }]), "a]b");
  assert.equal(runsToMarkup([{ text: "a**b", style: "bold" }]), "a**b");
  // A `*` glued to the marker would move it.
  assert.equal(runsToMarkup([{ text: "a*", style: "plain" }, { text: "b", style: "bold" }]), "a*b");
  // What IS written still reads back as itself.
  for (const runs of [
    [{ text: "see ", style: "plain" }, { text: "here", style: "em" }, { text: ".", style: "plain" }],
    [{ text: "run ", style: "plain" }, { text: "make check", style: "code" }],
    [{ text: "at ", style: "plain" }, { text: "risk", style: "span", tone: "error" }, { text: "!", style: "plain" }],
  ] as TextRun[][]) {
    const markup = runsToMarkup(runs);
    assert.deepEqual(runsFromMarkup(markup), normalizeRuns(runs), markup);
    assert.equal(markupProblem(markup), null, markup);
  }
});

test("applyStyle restyles exactly the range, splitting and merging runs", () => {
  const runs = runsFromMarkup("hello world");
  const bold = applyStyle(runs, 6, 11, "bold");
  assert.equal(runsToMarkup(bold), "hello **world**");
  // Emphasis over `hello wo` would glue `_` to `rld`, which the tokenizer
  // does not read as emphasis: the runs hold it, the markup cannot, and the
  // canonical form (what the editor shows after the action) says so.
  const partly = applyStyle(bold, 0, 8, "em");
  assert.deepEqual(partly, [
    { text: "hello wo", style: "em" },
    { text: "rld", style: "bold" },
  ]);
  assert.equal(runsToMarkup(partly), "hello wo**rld**");
  assert.deepEqual(canonicalRuns(partly), [
    { text: "hello wo", style: "plain" },
    { text: "rld", style: "bold" },
  ]);
  assert.equal(runsToPlainText(partly), "hello world");
  // Emphasis over a whole word is carried.
  assert.equal(runsToMarkup(applyStyle(runs, 0, 5, "em")), "_hello_ world");
  // Back to plain over the whole string.
  assert.equal(runsToMarkup(applyStyle(partly, 0, 11, "plain")), "hello world");
  // A newline inside the range stays a line break.
  const lines = applyStyle(runsFromMarkup("ab\ncd"), 0, 5, "bold");
  assert.equal(runsToMarkup(lines), "**ab**\n**cd**");
  // A span takes its tone.
  assert.equal(runsToMarkup(applyStyle(runs, 0, 5, "span", "info")), "[[info:hello]] world");
});

test("replaceRange swaps a stretch for plain text - how Enter inserts a line break", () => {
  const runs = runsFromMarkup("**hello** world");
  assert.equal(runsToMarkup(replaceRange(runs, 5, 5, "\n")), "**hello**\n world");
  assert.equal(runsToMarkup(replaceRange(runs, 2, 8, "\n")), "**he**\nrld");
  assert.equal(runsToMarkup(replaceRange(runs, 0, 11, "")), "");
  assert.equal(runsToMarkup(replaceRange(runs, 11, 11, "!")), "**hello** world!");
});

test("rangeHasStyle answers for the whole range and ignores newlines", () => {
  const runs = runsFromMarkup("**ab**\n**cd** e");
  assert.equal(rangeHasStyle(runs, 0, 5, "bold"), true);
  assert.equal(rangeHasStyle(runs, 0, 7, "bold"), false);
  assert.equal(rangeHasStyle(runs, 2, 3, "bold"), false, "a newline alone has no style");
  assert.equal(rangeHasStyle(runsFromMarkup("[[info:a]][[error:b]]"), 0, 2, "span"), true);
  assert.equal(rangeHasStyle(runsFromMarkup("[[info:a]][[error:b]]"), 0, 2, "span", "info"), false);
  assert.equal(styleAt(runs, 1)?.style, "bold");
  assert.equal(styleAt(runs, 7)?.style, "plain");
});

test("markupProblem mirrors the validator: html, unbalanced markers, nesting, unknown tones", () => {
  assert.equal(markupProblem("fine **text** here"), null);
  assert.deepEqual(markupProblem("a <b>tag</b>"), { kind: "html" });
  // The same regex as the backend: a `<` followed by a letter (spaces
  // allowed) reads as a tag, a `<` before a digit does not.
  assert.deepEqual(markupProblem("a < b"), { kind: "html" });
  assert.deepEqual(markupProblem("x <3 y and 1 < 2"), null);
  assert.deepEqual(markupProblem("open **bold"), { kind: "unbalanced", marker: "**" });
  assert.deepEqual(markupProblem("a ` stray"), { kind: "unbalanced", marker: "`" });
  assert.deepEqual(markupProblem("[[accent:x"), { kind: "unbalanced", marker: "[[" });
  assert.deepEqual(markupProblem("**a`b**"), { kind: "nested", construct: "**bold**" });
  assert.deepEqual(markupProblem("[[purple:x]]"), { kind: "unknown_tone", tone: "purple" });
  // The lenient tokenizer still renders every one of them as text.
  assert.ok(parseInlineMarkup("open **bold").length > 0);
});

test("an in-place text is done only with markup the validator accepts; a blur keeps the words and flags them", () => {
  // Done with a string the save would refuse: refused, and said why.
  assert.deepEqual(inlineCommit("a ` stray", "done", false), {
    commit: false,
    problem: { kind: "unbalanced", marker: "`" },
  });
  assert.deepEqual(inlineCommit("open **bold", "done", false).problem, { kind: "unbalanced", marker: "**" });
  assert.deepEqual(inlineCommit("see [[x", "done", false).problem, { kind: "unbalanced", marker: "[[" });
  assert.deepEqual(inlineCommit("a <b", "done", false).problem, { kind: "html" });
  // Leaving the field (a blur) commits the words as typed, flagged the same way.
  assert.deepEqual(inlineCommit("a ` stray", "commit", false), {
    commit: true,
    problem: { kind: "unbalanced", marker: "`" },
  });
  // A string the validator accepts ends the edit either way, with nothing to say.
  for (const how of ["done", "commit"] as const) {
    assert.deepEqual(inlineCommit("fine **text** and `code`", how, false), { commit: true, problem: null });
    assert.deepEqual(inlineCommit("", how, false), { commit: true, problem: null });
  }
  // A plain field (a metric value) is never markup: a backtick is a character.
  assert.deepEqual(inlineCommit("5` or <3", "done", true), { commit: true, problem: null });
});

test("a drag inside the field moves its text once, as plain text, and a drop on itself moves nothing", () => {
  // "one two three": `one ` 0-4, `two` 4-7 (bold), ` three` 7-13.
  const runs = runsFromMarkup("one **two** three");
  // Backwards: `three` dropped before `one`.
  const back = moveRange(runs, 8, 13, 0);
  assert.ok(back);
  assert.equal(runsToMarkup(back.runs), "threeone **two** ");
  assert.deepEqual([back.start, back.end], [0, 5]);
  // Forwards: the drop offset is read in the text as it stood, the stretch included.
  const forward = moveRange(runs, 0, 4, 13);
  assert.ok(forward);
  assert.equal(runsToMarkup(forward.runs), "**two** threeone ");
  assert.deepEqual([forward.start, forward.end], [9, 13]);
  // The moved stretch lands plain, like a paste of what was cut...
  const bold = moveRange(runs, 4, 7, 13);
  assert.ok(bold);
  assert.equal(runsToMarkup(bold.runs), "one  threetwo");
  // ...and the text around it keeps its style.
  const beside = moveRange(runs, 11, 13, 4);
  assert.ok(beside);
  assert.equal(runsToMarkup(beside.runs), "one ee**two** thr");
  // Every character exactly once: nothing duplicated, nothing lost.
  for (const moved of [back, forward, bold, beside]) {
    assert.deepEqual([...runsToPlainText(moved.runs)].sort(), [...runsToPlainText(runs)].sort());
  }
  // A drop on the stretch - at either edge or inside it - moves nothing, nor
  // does an empty drag or a drop past the end of the text.
  assert.equal(moveRange(runs, 4, 7, 4), null);
  assert.equal(moveRange(runs, 4, 7, 5), null);
  assert.equal(moveRange(runs, 4, 7, 7), null);
  assert.equal(moveRange(runs, 5, 5, 0), null);
  assert.equal(moveRange(runs, 0, 3, 14), null);
});
