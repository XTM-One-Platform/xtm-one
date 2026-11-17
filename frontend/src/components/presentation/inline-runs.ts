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
 * The runs model behind the in-place text editor.
 *
 * The inline markup grammar is FLAT: a character is plain, bold, emphasised,
 * code, inside one `[[tone:...]]` span or inside one `[text](#/<slide id>)`
 * slide link - never two of those at once - and a line break is its own
 * token. So a string under edit is a list of RUNS, each
 * a stretch of characters sharing one style, and every editing operation is
 * arithmetic on that list: split at two offsets, restyle what lies between,
 * merge neighbours that now agree. The WYSIWYG surface (`InlineTextEditor`)
 * turns runs into DOM and DOM back into runs; the serializer here turns runs
 * into the markup the validator accepts, and only that: a run whose text
 * cannot be written in its style (a backtick inside code, a `]` inside a span,
 * an underscore or a glued letter around emphasis) is written PLAIN rather
 * than as markup the backend would refuse. What the user typed is never
 * lost; at worst a decoration is.
 *
 * Invariants a normalised run list keeps:
 *   - a newline character is always a plain run of its own (`"\n"`);
 *   - no empty run, no two adjacent runs of the same style and tone;
 *   - a styled run carries no leading or trailing whitespace (moved out to
 *     the plain neighbours), so `** a **` is never written.
 *
 * Pure: `inline-runs.test.ts` exercises it under Node's runner.
 */
// The explicit extension is what lets Node's runner load this module for
// `inline-runs.test.ts` (the bundler resolves it either way).
import { isLinkUrl, type MarkupProblem, type MarkupSpanKind, markupProblem, parseInlineMarkup } from "./markup.ts";

export type RunStyle = "plain" | "bold" | "em" | "code" | "span" | "link";

export interface TextRun {
  text: string;
  style: RunStyle;
  /** The span kind, for `style === "span"` only. */
  tone?: MarkupSpanKind;
  /** What a `link` run points at - a slide id or an address - for `style === "link"` only. */
  target?: string;
}

const WORD_CHAR = /[A-Za-z0-9_]/;
/** A slide link's target: `SLIDE_LINK_REF_PATTERN` (`presentation_markup.py`). */
const SLIDE_REF = /^[A-Za-z0-9_-]{1,64}$/;

function sameStyle(a: TextRun, b: TextRun): boolean {
  if (a.style !== b.style) return false;
  if (a.style === "span") return a.tone === b.tone;
  if (a.style === "link") return a.target === b.target;
  return true;
}

/** Neighbours of one style joined (a newline stays its own plain run so the
 *  serializer and the DOM builder can treat it as a token). */
function mergeRuns(runs: readonly TextRun[]): TextRun[] {
  const merged: TextRun[] = [];
  for (const run of runs) {
    const previous = merged[merged.length - 1];
    if (previous && sameStyle(previous, run) && run.text !== "\n" && previous.text !== "\n") {
      previous.text += run.text;
    } else {
      merged.push({ ...run });
    }
  }
  return merged;
}

/**
 * The normal form (see the invariants above). Idempotent.
 */
export function normalizeRuns(runs: readonly TextRun[]): TextRun[] {
  // 1. Newlines out of styled runs; empty runs dropped.
  const split: TextRun[] = [];
  for (const run of runs) {
    if (!run.text) continue;
    if (run.style === "plain") {
      split.push({ text: run.text, style: "plain" });
      continue;
    }
    const pieces = run.text.split("\n");
    pieces.forEach((piece, index) => {
      if (index > 0) split.push({ text: "\n", style: "plain" });
      if (piece) split.push({ ...run, text: piece });
    });
  }
  // 2. Edge whitespace out of styled runs - after a first merge, so the
  //    whitespace INSIDE a styled stretch that arrived in two pieces stays.
  const hoisted: TextRun[] = [];
  for (const run of mergeRuns(split)) {
    if (run.style === "plain") {
      hoisted.push(run);
      continue;
    }
    const leading = run.text.match(/^\s+/)?.[0] ?? "";
    const trailing = run.text.match(/\s+$/)?.[0] ?? "";
    const core = run.text.slice(leading.length, run.text.length - trailing.length);
    if (!core) {
      hoisted.push({ text: run.text, style: "plain" });
      continue;
    }
    if (leading) hoisted.push({ text: leading, style: "plain" });
    hoisted.push({ ...run, text: core });
    if (trailing) hoisted.push({ text: trailing, style: "plain" });
  }
  // 3. The hoisting can leave two plain runs side by side.
  return mergeRuns(hoisted);
}

/**
 * The runs as they will be READ BACK after a save: serialized and parsed
 * again, so a decoration the grammar cannot carry (see `runsToMarkup`) is
 * gone here too. The editor renders this form after every formatting action,
 * so what is on screen is what the save keeps.
 */
export function canonicalRuns(runs: readonly TextRun[]): TextRun[] {
  return runsFromMarkup(runsToMarkup(runs));
}

/** Runs of a markup string (the tokenizer's segments, one run each). */
export function runsFromMarkup(value: string | undefined | null): TextRun[] {
  if (!value) return [];
  const runs: TextRun[] = parseInlineMarkup(value).map((segment) => {
    switch (segment.kind) {
      case "bold":
        return { text: segment.text, style: "bold" };
      case "em":
        return { text: segment.text, style: "em" };
      case "code":
        return { text: segment.text, style: "code" };
      case "span":
        return { text: segment.text, style: "span", tone: segment.tone };
      case "link":
        return { text: segment.text, style: "link", target: segment.target };
      case "br":
        return { text: "\n", style: "plain" };
      default:
        return { text: segment.text, style: "plain" };
    }
  });
  return normalizeRuns(runs);
}

/** The characters, markers stripped. */
export function runsToPlainText(runs: readonly TextRun[]): string {
  return runs.map((run) => run.text).join("");
}

/** Whether `text` can be written in `style` and read back as the same run. */
function expressible(run: TextRun, before: string, after: string): boolean {
  const { text, style } = run;
  if (text.includes("\n")) return false;
  switch (style) {
    case "bold":
      // A `*` glued to either marker would move the marker the tokenizer
      // reads (`a***b**` is plain `a` + bold `*b`, not plain `a*` + bold `b`).
      return (
        !text.includes("**") &&
        !/`|\[\[|\]\]/.test(text) &&
        !before.endsWith("*") &&
        !after.startsWith("*")
      );
    case "code":
      return !text.includes("`") && !/\*\*|\[\[|\]\]/.test(text);
    case "span":
      return !!run.tone && !text.includes("]") && !/\*\*|`|\[\[/.test(text);
    case "link":
      // A `[` glued before the link would open `[[`, which the grammar reads
      // as a span marker.
      return (
        !!run.target &&
        (SLIDE_REF.test(run.target) || isLinkUrl(run.target)) &&
        !/[[\]]/.test(text) &&
        !/\*\*|`/.test(text) &&
        !before.endsWith("[")
      );
    case "em": {
      if (text.includes("_") || /\*\*|`|\[\[|\]\]/.test(text)) return false;
      const previous = before.slice(-1);
      const next = after.slice(0, 1);
      return !(previous && WORD_CHAR.test(previous)) && !(next && WORD_CHAR.test(next));
    }
    default:
      return true;
  }
}

/**
 * The markup for `runs` - what the editor writes back to the spec. Every run
 * the grammar can carry is written as its markers; one it cannot is written
 * plain (see the module note). The result is what `runsFromMarkup` would read
 * back as the same runs, up to those degradations.
 */
export function runsToMarkup(runs: readonly TextRun[]): string {
  const normalized = normalizeRuns(runs);
  let out = "";
  normalized.forEach((run, index) => {
    const after = normalized
      .slice(index + 1)
      .map((r) => r.text)
      .join("");
    if (run.style === "plain" || !expressible(run, out, after)) {
      out += run.text;
      return;
    }
    switch (run.style) {
      case "bold":
        out += `**${run.text}**`;
        break;
      case "em":
        out += `_${run.text}_`;
        break;
      case "code":
        out += `\`${run.text}\``;
        break;
      case "span":
        out += `[[${run.tone}:${run.text}]]`;
        break;
      case "link":
        out += isLinkUrl(run.target ?? "") ? `[${run.text}](${run.target})` : `[${run.text}](#/${run.target})`;
        break;
    }
  });
  return out;
}

/** Split `runs` so that `offset` falls on a run boundary. */
function splitAt(runs: TextRun[], offset: number): TextRun[] {
  const out: TextRun[] = [];
  let cursor = 0;
  for (const run of runs) {
    const end = cursor + run.text.length;
    if (offset > cursor && offset < end) {
      const at = offset - cursor;
      out.push({ ...run, text: run.text.slice(0, at) }, { ...run, text: run.text.slice(at) });
    } else {
      out.push({ ...run });
    }
    cursor = end;
  }
  return out;
}

/**
 * The slide every character of `[start, end)` (newlines aside) links to, or
 * `null` when part of the range is no link or links elsewhere.
 */
export function rangeLinkTarget(runs: readonly TextRun[], start: number, end: number): string | null {
  let cursor = 0;
  let target: string | null = null;
  for (const run of runs) {
    const runStart = cursor;
    const runEnd = cursor + run.text.length;
    cursor = runEnd;
    if (runEnd <= start || runStart >= end) continue;
    if (run.text === "\n") continue;
    if (run.style !== "link" || !run.target || (target !== null && run.target !== target)) return null;
    target = run.target;
  }
  return target;
}

/**
 * `runs` with `[start, end)` linked to slide `target` (`null` takes the link
 * off, back to plain). Newlines inside the range stay plain - a link label is
 * one line. Returns a normalised list.
 */
export function applyLink(runs: readonly TextRun[], start: number, end: number, target: string | null): TextRun[] {
  if (end <= start) return normalizeRuns(runs);
  const split = splitAt(splitAt([...runs], start), end);
  let cursor = 0;
  const relinked = split.map((run): TextRun => {
    const runStart = cursor;
    cursor += run.text.length;
    if (runStart < start || runStart >= end || run.text === "\n") return run;
    return target ? { text: run.text, style: "link", target } : { text: run.text, style: "plain" };
  });
  return normalizeRuns(relinked);
}

/** Whether every character of `[start, end)` (newlines aside) has `style`. */
export function rangeHasStyle(
  runs: readonly TextRun[],
  start: number,
  end: number,
  style: RunStyle,
  tone?: MarkupSpanKind,
): boolean {
  let cursor = 0;
  let seen = false;
  for (const run of runs) {
    const runStart = cursor;
    const runEnd = cursor + run.text.length;
    cursor = runEnd;
    if (runEnd <= start || runStart >= end) continue;
    if (run.text === "\n") continue;
    seen = true;
    if (run.style !== style || (style === "span" && tone !== undefined && run.tone !== tone)) return false;
  }
  return seen;
}

/**
 * `runs` with `[start, end)` set to `style` (or back to plain when `style` is
 * `"plain"`). Newlines inside the range stay plain. Returns a normalised list.
 */
export function applyStyle(
  runs: readonly TextRun[],
  start: number,
  end: number,
  style: RunStyle,
  tone?: MarkupSpanKind,
): TextRun[] {
  if (end <= start) return normalizeRuns(runs);
  const split = splitAt(splitAt([...runs], start), end);
  let cursor = 0;
  const restyled = split.map((run) => {
    const runStart = cursor;
    cursor += run.text.length;
    if (runStart < start || runStart >= end || run.text === "\n") return run;
    return style === "span" ? { text: run.text, style, tone } : { text: run.text, style };
  });
  return normalizeRuns(restyled);
}

/**
 * `runs` with `[start, end)` replaced by plain `text` (an inserted line
 * break, a typed replacement). Returns a normalised list.
 */
export function replaceRange(runs: readonly TextRun[], start: number, end: number, text: string): TextRun[] {
  const from = Math.max(0, Math.min(start, end));
  const to = Math.max(from, end);
  const split = splitAt(splitAt([...runs], from), to);
  const out: TextRun[] = [];
  let cursor = 0;
  let inserted = false;
  for (const run of split) {
    const runStart = cursor;
    cursor += run.text.length;
    if (runStart >= from && runStart < to) continue; // inside the replaced range
    if (!inserted && runStart >= to) {
      if (text) out.push({ text, style: "plain" });
      inserted = true;
    }
    out.push(run);
  }
  if (!inserted && text) out.push({ text, style: "plain" });
  return normalizeRuns(out);
}

/**
 * `runs` with the text of `[start, end)` moved to `to` - a drag inside the
 * field dropped elsewhere in it. `to` is an offset in the text as it
 * stands, the dragged stretch included. The stretch lands there as PLAIN
 * text, like a paste of what was cut, and once: it leaves its place and
 * appears at the drop point, never twice, never lost. A drop on the stretch
 * itself moves nothing (`null`). Returns the normalised runs and where the
 * moved text now sits.
 */
export function moveRange(
  runs: readonly TextRun[],
  start: number,
  end: number,
  to: number,
): { runs: TextRun[]; start: number; end: number } | null {
  const text = runsToPlainText(runs);
  const from = Math.max(0, Math.min(start, end));
  const until = Math.min(text.length, Math.max(start, end));
  if (until <= from || to < 0 || to > text.length || (to >= from && to <= until)) return null;
  const moved = text.slice(from, until);
  const at = to > until ? to - moved.length : to;
  return {
    runs: replaceRange(replaceRange(runs, from, until, ""), at, at, moved),
    start: at,
    end: at + moved.length,
  };
}

/**
 * How an in-place edit may end with `value`. `done` is the person
 * saying the text is finished - Ctrl / Cmd + Enter, Tab, Enter in a one-row
 * field, the bar's Done, the commit a save asks for; `commit` is a blur, the
 * focus gone elsewhere. Markup the validator would refuse is flagged either
 * way (`problem`) and refused as `done`: the field stays open with the text
 * as typed, and no save relays a 422 for it. A blur keeps the words
 * (`commit: true`): a field held open without the focus would either take
 * the focus back or lose the text at the next click. A plain field is never
 * markup.
 */
export function inlineCommit(
  value: string,
  how: "done" | "commit",
  plain: boolean,
): { commit: boolean; problem: MarkupProblem | null } {
  const problem = plain ? null : markupProblem(value);
  return { commit: problem === null || how === "commit", problem };
}

/**
 * The slide link around `offset` - the caret inside its label or at either
 * edge of it - as its bounds and target, or `null` when the caret is in no
 * link: what "Remove link" takes off with nothing selected.
 */
export function linkAround(
  runs: readonly TextRun[],
  offset: number,
): { start: number; end: number; target: string } | null {
  let cursor = 0;
  for (const run of runs) {
    const end = cursor + run.text.length;
    if (run.style === "link" && run.target && offset >= cursor && offset <= end) {
      return { start: cursor, end, target: run.target };
    }
    cursor = end;
  }
  return null;
}

/** The style at `offset` (of the character before it, as a caret reads). */
export function styleAt(runs: readonly TextRun[], offset: number): TextRun | null {
  let cursor = 0;
  for (const run of runs) {
    const end = cursor + run.text.length;
    if (offset > cursor && offset <= end) return run;
    cursor = end;
  }
  return null;
}
