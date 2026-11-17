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
 * One line of plain text out of Markdown an agent wrote: what a list
 * row shows of a description or a result, the full text being rendered with
 * `ChatMarkdown` where there is room for it (a dialog).
 *
 * A light line-by-line strip, never a Markdown parse: a page of 200 rows calls
 * it once per row, and the result is cached by text. Emphasis, code, link and
 * heading markers are removed, a fenced code block is left out, and the blocks
 * (paragraphs, list items, headings, table rows) are joined with " · ".
 */

const SEPARATOR = " · ";
/** Longest text read: a summary is one line, the rest is never shown. */
const MAX_INPUT = 4000;
const CACHE_LIMIT = 500;
const cache = new Map<string, string>();

const FENCE = /^\s*(```|~~~)/;
const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?/;
const HEADING = /^\s{0,3}#{1,6}\s+/;
const QUOTE = /^\s*(?:>\s?)+/;
const TABLE_ROW = /^\s*\|/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const RULE = /^\s*([-*_])(?:\s*\1){2,}\s*$/;

function inline(text: string): string {
  return text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/`+([^`]*?)`+/g, "$1")
    .replace(/(\*\*|__)(.+?)\1/g, "$2")
    .replace(/(^|[^\w*])\*(?!\s)([^*]+?)\*(?!\w)/g, "$1$2")
    .replace(/(^|[^\w_])_(?!\s)([^_]+?)_(?!\w)/g, "$1$2")
    .replace(/~~(.+?)~~/g, "$1")
    // An autolink is text, not a tag: a scheme autolink (<https://...>,
    // <mailto:...>) or a CommonMark email autolink (<a@b.c>). Anything else in
    // angle brackets that is not a tag (<a@b/c>) is kept as the author wrote it.
    .replace(
      /<((?:[a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^\s<>]+)|(?:[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*))>/g,
      "$1",
    )
    .replace(/<\/?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^>]*)?\/?>/g, "")
    // An unbalanced marker left by a cut or a typo is not text either.
    .replace(/\*\*|`/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function strip(markdown: string): string {
  const parts: string[] = [];
  let current = "";
  let inFence = false;
  const flush = () => {
    if (current) parts.push(current);
    current = "";
  };
  for (const raw of markdown.slice(0, MAX_INPUT).split(/\r?\n/)) {
    if (FENCE.test(raw)) {
      flush();
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    if (!raw.trim() || RULE.test(raw) || TABLE_RULE.test(raw)) {
      flush();
      continue;
    }
    if (TABLE_ROW.test(raw)) {
      flush();
      const cells = raw.trim().replace(/^\||\|$/g, "").split("|").map((cell) => inline(cell)).filter(Boolean);
      if (cells.length) parts.push(cells.join(SEPARATOR));
      continue;
    }
    const heading = HEADING.test(raw);
    const listItem = LIST_ITEM.test(raw);
    const text = inline(raw.replace(QUOTE, "").replace(HEADING, "").replace(LIST_ITEM, ""));
    if (!text) continue;
    if (heading) {
      flush();
      parts.push(text);
    } else if (listItem) {
      flush();
      current = text;
    } else {
      current = current ? `${current} ${text}` : text;
    }
  }
  flush();
  return parts.join(SEPARATOR);
}

/** The text of *markdown* on one line, without markup ("" for none). */
export function markdownSummary(markdown: string | null | undefined): string {
  if (!markdown) return "";
  const hit = cache.get(markdown);
  if (hit !== undefined) return hit;
  const summary = strip(markdown);
  if (cache.size >= CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
  cache.set(markdown, summary);
  return summary;
}
