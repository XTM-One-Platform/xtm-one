/**
 * Unit tests for the accent-line splitter (`hero_accent_word`) -
 * `yarn test:unit`.
 *
 * `InlineMarkup` renders what `splitAccentLine` hands it; what is pinned here
 * is the promise the schema and the tool make: the last LINE of a title, or
 * the last WORD of a one-line title - found across the runs, cut out of the
 * run that holds it with the wrapper kept, a code chip left whole - with the
 * trailing line breaks drawn plain after it.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { splitAccentLine } from "./accent-line.ts";
import { type MarkupSegment, parseInlineMarkup } from "./markup.ts";

const split = (value: string) => splitAccentLine(parseInlineMarkup(value));
const text = (value: string): MarkupSegment => ({ kind: "text", text: value });
const bold = (value: string): MarkupSegment => ({ kind: "bold", text: value });
const em = (value: string): MarkupSegment => ({ kind: "em", text: value });
const code = (value: string): MarkupSegment => ({ kind: "code", text: value });
const br: MarkupSegment = { kind: "br" };

test("a multi-line title accents everything after its last line break, runs intact", () => {
  assert.deepEqual(split("Uncover Threats.\nTake Action."), {
    head: [text("Uncover Threats."), br],
    accent: [text("Take Action.")],
    trailing: [],
  });
  assert.deepEqual(split("One\nTwo\n**Three** four"), {
    head: [text("One"), br, text("Two"), br],
    accent: [bold("Three"), text(" four")],
    trailing: [],
  });
});

test("a one-line title accents its last word", () => {
  assert.deepEqual(split("Uncover Threats. Take Action."), {
    head: [text("Uncover Threats. Take ")],
    accent: [text("Action.")],
    trailing: [],
  });
  // Trailing whitespace is not the boundary and rides with the word.
  assert.deepEqual(split("Take Action.  "), {
    head: [text("Take ")],
    accent: [text("Action.  ")],
    trailing: [],
  });
  // One word is the whole line.
  assert.deepEqual(split("Action."), { head: [], accent: [text("Action.")], trailing: [] });
});

test("the last word is cut out of a formatted run with its wrapper kept on both halves", () => {
  // Copilot's case: never the whole bold run.
  assert.deepEqual(split("Launch **Take Action.**"), {
    head: [text("Launch "), bold("Take ")],
    accent: [bold("Action.")],
    trailing: [],
  });
  assert.deepEqual(split("Go _far and wide_"), {
    head: [text("Go "), em("far and ")],
    accent: [em("wide")],
    trailing: [],
  });
  // A tone span splits the same way and keeps its tone.
  assert.deepEqual(split("Now [[accent:Take Action.]]"), {
    head: [text("Now "), { kind: "span", text: "Take ", tone: "accent" }],
    accent: [{ kind: "span", text: "Action.", tone: "accent" }],
    trailing: [],
  });
});

test("the last word spans the runs it is written across", () => {
  // The bold run AND the full stop after it are the word.
  assert.deepEqual(split("Uncover Threats. Take **Action**."), {
    head: [text("Uncover Threats. Take ")],
    accent: [bold("Action"), text(".")],
    trailing: [],
  });
  // A whole formatted run that IS the last word.
  assert.deepEqual(split("Launch **Action.**"), {
    head: [text("Launch ")],
    accent: [bold("Action.")],
    trailing: [],
  });
  // Whitespace after the last run rides with it, never as an empty accent.
  assert.deepEqual(split("Go **Take**   "), {
    head: [text("Go ")],
    accent: [bold("Take"), text("   ")],
    trailing: [],
  });
});

test("a code chip is one token: never split, accented whole", () => {
  assert.deepEqual(split("Run `git push origin`"), {
    head: [text("Run ")],
    accent: [code("git push origin")],
    trailing: [],
  });
  assert.deepEqual(split("`git push` now"), {
    head: [code("git push"), text(" ")],
    accent: [text("now")],
    trailing: [],
  });
});

test("trailing line breaks carry no glyph: skipped to find the line, drawn plain after it", () => {
  assert.deepEqual(split("Take Action.\n"), {
    head: [text("Take ")],
    accent: [text("Action.")],
    trailing: [br],
  });
  assert.deepEqual(split("Uncover Threats.\nTake Action.\n\n"), {
    head: [text("Uncover Threats."), br],
    accent: [text("Take Action.")],
    trailing: [br, br],
  });
  // Nothing but breaks: nothing to accent, everything drawn as it was.
  assert.deepEqual(split("\n\n"), { head: [br, br], accent: [], trailing: [] });
  assert.deepEqual(splitAccentLine([]), { head: [], accent: [], trailing: [] });
});
