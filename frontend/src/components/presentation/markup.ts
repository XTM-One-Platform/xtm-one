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
 * Inline markup tokenizer for deck text — the parse half of
 * `backend/app/services/presentation_markup.py`; `inline-markup.tsx` renders
 * the segments.
 *
 * The grammar is tiny and flat on purpose: `**bold**`, `_em_` (word-bounded, so
 * `snake_case` stays literal), `` `code` ``, `[[tone:text]]`, the slide link
 * `[text](#/<slide id>)` (another slide of the same deck, by its stable id),
 * `[text](https://...)` / `[text](mailto:...)` (a web page, an e-mail address
 * - any other Markdown link stays literal text) and `\n`. It is
 * tokenized into segments and rendered as React nodes — never turned into an
 * HTML string — so a deck string can never smuggle markup past the renderer,
 * whatever the backend accepted (an older row, a hand-edited spec). Anything
 * the tokenizer does not recognise is rendered as literal text, which is the
 * safe failure mode.
 *
 * The token regex mirrors `_TOKEN_RE` on the backend; the backend test suite
 * pins the two by grepping this file for the same markers.
 */
import type { DeckTone } from "@/types";

/** `[[tone:text]]` span kinds: the six tones plus the brand gradient. */
export type MarkupSpanKind = DeckTone | "gradient";

const SPAN_KINDS: ReadonlySet<string> = new Set<MarkupSpanKind>([
  "accent",
  "info",
  "success",
  "warning",
  "error",
  "muted",
  "gradient",
]);

export type MarkupSegment =
  | { kind: "text"; text: string }
  | { kind: "bold"; text: string }
  | { kind: "em"; text: string }
  | { kind: "code"; text: string }
  | { kind: "span"; text: string; tone: MarkupSpanKind }
  /**
   * `[text](#/<slide id>)`: `target` is the slide id, resolved by
   * `slide-link-context.ts`; `[text](https://...)` / `mailto:`: `target` is
   * the address (`isLinkUrl`). A slide id holds no `:`, so the two never meet.
   */
  | { kind: "link"; text: string; target: string }
  | { kind: "br" };

// Same alternation order as the backend: bold, code, span, slide link, em,
// line break. The link's ref is `SLIDE_LINK_REF_PATTERN`
// (`presentation_markup.py`), the slide id grammar; its address
// `LINK_URL_PATTERN`.
const TOKEN_RE =
  /\*\*([^\n]+?)\*\*|`([^`\n]+)`|\[\[([a-zA-Z_]+):([^\]\n]+?)\]\]|\[([^[\]\n]+)\]\((?:#\/([A-Za-z0-9_-]{1,64})|(https:\/\/[^\s<>"'`()]+|mailto:[^\s<>"'`()@]+@[^\s<>"'`()@]+\.[^\s<>"'`()@]+))\)|(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])|\n/g;

/** A web or e-mail link's address, whole: `LINK_URL_PATTERN` (`presentation_markup.py`). */
const LINK_URL_RE = /^(?:https:\/\/[^\s<>"'`()]+|mailto:[^\s<>"'`()@]+@[^\s<>"'`()@]+\.[^\s<>"'`()@]+)$/;

/** Whether a link's `target` is an address (`https://`, `mailto:`) rather than a slide id. */
export function isLinkUrl(target: string): boolean {
  return LINK_URL_RE.test(target);
}

/** Tokenize a markup string. Never throws: unknown constructs stay literal. */
export function parseInlineMarkup(value: string): MarkupSegment[] {
  const segments: MarkupSegment[] = [];
  let cursor = 0;
  for (const match of value.matchAll(TOKEN_RE)) {
    const start = match.index ?? 0;
    if (start > cursor) segments.push({ kind: "text", text: value.slice(cursor, start) });
    cursor = start + match[0].length;
    const [whole, bold, code, spanKind, spanText, linkText, linkRef, linkUrl, em] = match;
    if (bold !== undefined) {
      segments.push({ kind: "bold", text: bold });
    } else if (code !== undefined) {
      segments.push({ kind: "code", text: code });
    } else if (linkText !== undefined) {
      segments.push({ kind: "link", text: linkText, target: linkRef ?? linkUrl });
    } else if (spanKind !== undefined) {
      const kind = spanKind.toLowerCase();
      if (SPAN_KINDS.has(kind)) {
        segments.push({ kind: "span", text: spanText, tone: kind as MarkupSpanKind });
      } else {
        // An unknown tone is not a render error: show the text, drop the wrapper.
        segments.push({ kind: "text", text: spanText });
      }
    } else if (em !== undefined) {
      segments.push({ kind: "em", text: em });
    } else if (whole === "\n") {
      segments.push({ kind: "br" });
    }
  }
  if (cursor < value.length) segments.push({ kind: "text", text: value.slice(cursor) });
  return segments;
}

/**
 * The tone of a text that is ONE `[[tone:text]]` span and nothing else — the
 * natural way to write "a green heading" — or `undefined` for any other shape:
 * plain text, several runs, a span over part of the text, or a `gradient` span
 * (a text treatment, not a colour a marker can take). A block whose `tone`
 * colours its text together with a glued icon or marker (heading, section
 * header, list item) reads this as its tone when it carries none, so the icon
 * follows the text instead of staying accent beside it. An explicit
 * block `tone` always wins.
 */
export function soleSpanTone(value: string | undefined | null): DeckTone | undefined {
  if (!value) return undefined;
  const segments = parseInlineMarkup(value);
  if (segments.length !== 1) return undefined;
  const [only] = segments;
  if (only.kind !== "span" || only.tone === "gradient") return undefined;
  return only.tone;
}

/** Markers stripped — for `alt` text, `aria-label`s and plain-text fallbacks. */
export function markupToPlainText(value: string | undefined | null): string {
  if (!value) return "";
  return parseInlineMarkup(value)
    .map((segment) => (segment.kind === "br" ? "\n" : segment.text))
    .join("");
}

/** The span kinds the grammar accepts, for a picker (`[[tone:text]]`). */
export const MARKUP_SPAN_KINDS: readonly MarkupSpanKind[] = [...SPAN_KINDS] as MarkupSpanKind[];

// The backend's refusals (`presentation_markup.parse_inline_markup`), mirrored
// so the in-place editor can flag a string BEFORE the save instead of
// relaying a 422. Same three rules, same order: anything tag-like is HTML, a
// marker left over in plain text is unbalanced, a marker inside a span is
// nested. The tokenizer above renders such strings leniently (a renderer must
// never throw on a stored row); this is the strict reading of the same text.
// `](#/` is the slide link's marker: left over, it is a link with no label or
// a ref that is no slide id.
const TAG_LIKE_RE = /<\s*\/?\s*[A-Za-z!]/;
const MARKER_RE = /\*\*|`|\[\[|\]\]|\]\(#\//;

export type MarkupProblem =
  | { kind: "html" }
  | { kind: "unbalanced"; marker: string }
  | { kind: "nested"; construct: string }
  | { kind: "unknown_tone"; tone: string };

/**
 * Why the validator would refuse `value` as inline markup, or `null` when it
 * would accept it. The messages are the editor's to word (i18n).
 */
export function markupProblem(value: string | undefined | null): MarkupProblem | null {
  if (!value) return null;
  if (TAG_LIKE_RE.test(value)) return { kind: "html" };
  let cursor = 0;
  const plain = (chunk: string): MarkupProblem | null => {
    const leftover = MARKER_RE.exec(chunk);
    return leftover ? { kind: "unbalanced", marker: leftover[0] } : null;
  };
  for (const match of value.matchAll(TOKEN_RE)) {
    const start = match.index ?? 0;
    const before = plain(value.slice(cursor, start));
    if (before) return before;
    cursor = start + match[0].length;
    const [, bold, code, spanKind, spanText, linkText, , , em] = match;
    const inner = bold ?? code ?? spanText ?? linkText ?? em;
    if (inner !== undefined && (MARKER_RE.test(inner) || inner.includes("\n"))) {
      const construct =
        bold !== undefined
          ? "**bold**"
          : code !== undefined
            ? "`code`"
            : spanKind !== undefined
              ? "[[tone:text]]"
              : linkText !== undefined
                ? "[text](link)"
                : "_emphasis_";
      return { kind: "nested", construct };
    }
    if (spanKind !== undefined && !SPAN_KINDS.has(spanKind.toLowerCase())) {
      return { kind: "unknown_tone", tone: spanKind };
    }
  }
  return plain(value.slice(cursor));
}
