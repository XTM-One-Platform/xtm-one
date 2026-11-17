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
 * Placing citations inside prose — the arithmetic, without the JSX.
 *
 * Separate from `CitedAnswer.tsx` because these are the parts that can be
 * wrong in ways a screenshot does not reveal: a marker one sentence off still
 * looks like a citation.
 */
import type { InvestigationEvidence } from "@/types";
import { safeHttpUrl } from "../../lib/safe-url.ts";

/** Where a citation may link: a web page (`safeHttpUrl()`, the one rule for
 *  a third-party `href`: absolute http(s), no userinfo) or a `mailto:` address.
 *  An allowlist: the set of dangerous values is open-ended, and React does not
 *  sanitise `href` - a `javascript:` URL renders and executes on click with
 *  only a dev-mode warning; a relative or protocol-relative value would
 *  resolve against this app.
 *
 *  The parser already refuses these server-side, so nothing hostile should
 *  reach here. This is the second layer, and it exists because the two render
 *  paths disagreed: the woven markers went through `ChatMarkdown`'s
 *  `urlTransform` and the numbered key put the raw value into `<a href>`. A
 *  value that is persisted and rendered twice needs the check on both. */
export function safeHref(url?: string | null): string | undefined {
  const web = safeHttpUrl(url);
  if (web) return web;
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "mailto:" ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

/** A markdown link destination that a provider-supplied URL cannot break out
 *  of. CommonMark allows BALANCED parens in a bare destination, so
 *  `.../Cozy_Bear_(hacker_group)` survives — but an unbalanced `)` or a
 *  literal space ends the destination early and the remainder leaks into the
 *  prose beside a broken link. Angle brackets take both; only `<` and `>`
 *  themselves have to be encoded. */
function linkDestination(href: string): string {
  return `<${href.replace(/</g, "%3C").replace(/>/g, "%3E")}>`;
}


type NumberedCitation = InvestigationEvidence & { n: number };

const isAnchored = (e: InvestigationEvidence) =>
  typeof e.start === "number" && typeof e.end === "number";

/**
 * Number the sources, in reading order, one number per distinct source.
 *
 * Anchored and unanchored are kept apart because only the first kind can be
 * placed: a source with no span was consulted, and dropping a marker for it at
 * a plausible spot attributes a sentence to a page that may not support it.
 * They still get numbers — they belong in the key underneath.
 */
export function numberCitations(evidence: InvestigationEvidence[]): {
  anchored: NumberedCitation[];
  unanchored: NumberedCitation[];
  ordered: NumberedCitation[];
} {
  const anchoredRaw = [...evidence]
    .filter(isAnchored)
    .sort((a, b) => a.start! - b.start! || a.end! - b.end!);

  const numberOf = new Map<string, number>();
  const ordered: NumberedCitation[] = [];
  const assign = (e: InvestigationEvidence): NumberedCitation => {
    // Keyed on the address, falling back to the label for a source with none —
    // otherwise every unaddressed citation collapses into one number.
    const key = e.href || `label:${e.label}`;
    let n = numberOf.get(key);
    if (n === undefined) {
      n = numberOf.size + 1;
      numberOf.set(key, n);
      ordered.push({ ...e, n });
    }
    return { ...e, n };
  };

  // Anchored first, so numbering follows the prose rather than the order the
  // payload happened to arrive in.
  const anchored = anchoredRaw.map(assign);
  const unanchored = evidence.filter((e) => !isAnchored(e)).map(assign);
  return { anchored, unanchored, ordered };
}

/**
 * True when `index` falls inside a fenced code block.
 *
 * A marker inserted there would be printed as literal characters inside the
 * code rather than rendered as a citation — and worse, it would change code
 * the reader may be about to copy.
 *
 * Scanned line by line, tracking the length of the fence that opened the
 * block, because a simple parity count of every ``` in the string gets nested
 * fences exactly backwards. In
 *
 *     ````markdown
 *     intro
 *     ```
 *     inner
 *     ```
 *     outro
 *     ````
 *
 * the position at `inner` sits between two counted fences and reads as
 * OUTSIDE, while it is inside the outer block — so a marker lands in the
 * code. That shape is not hypothetical: `hardenNestedCodeFences` in
 * `ChatMarkdown` emits it deliberately whenever it detects nesting, and a
 * provider quoting markdown produces it on its own.
 *
 * Follows CommonMark: a fence opens at the start of a line (up to three
 * spaces of indent) and is closed only by a run of at least as many of the
 * SAME character, with nothing else on the line. `~~~` counts too — the old
 * scan ignored it entirely, so a tilde-fenced block was never protected.
 */
function insideCodeFence(text: string, index: number): boolean {
  const fence = /^ {0,3}(`{3,}|~{3,})(.*)$/;
  let open: string | null = null;
  let pos = 0;

  for (const line of text.split("\n")) {
    // The newline itself belongs to the line that precedes it.
    const lineEnd = pos + line.length;
    if (pos > index) break;

    const m = fence.exec(line);
    if (m) {
      const marks = m[1];
      if (open === null) {
        // An opener may carry an info string; a ``` fence may not contain
        // backticks in it, which is what keeps inline code from opening one.
        if (marks[0] !== "`" || !m[2].includes("`")) {
          open = marks;
          if (index > lineEnd) {
            pos = lineEnd + 1;
            continue;
          }
          // The opening line itself is not inside the block.
          return false;
        }
      } else if (
        marks[0] === open[0] &&
        marks.length >= open.length &&
        m[2].trim() === ""
      ) {
        open = null;
        if (index <= lineEnd) return false;
        pos = lineEnd + 1;
        continue;
      }
    }

    if (index <= lineEnd) return open !== null;
    pos = lineEnd + 1;
  }
  return open !== null;
}

/**
 * A citation offset as an index into *text*: the backend counts code points,
 * a JavaScript string counts UTF-16 units, and every emoji or astral
 * character before a span would otherwise move its marker one unit left.
 * Clamped to the end of the text.
 */
export function utf16Index(text: string, codePoints: number): number {
  let index = 0;
  let seen = 0;
  for (const char of text) {
    if (seen >= codePoints) break;
    index += char.length;
    seen += 1;
  }
  return index;
}

/**
 * Weave citation markers into the answer as markdown links.
 *
 * Inserting into the SOURCE markdown, rather than splitting the rendered
 * output, is what lets the answer keep being an answer: these replies arrive
 * with headings, lists and tables, and cutting the string at arbitrary offsets
 * to interleave React nodes would break every one of them.
 *
 * Insertions run back-to-front so each offset still refers to the original
 * string when it is used.
 */
export function weaveCitations(answer: string, anchored: NumberedCitation[]): string {
  const byEnd = new Map<number, NumberedCitation[]>();
  for (const cite of anchored) {
    const end = utf16Index(answer, Math.max(0, cite.end ?? 0));
    if (insideCodeFence(answer, end)) continue;
    const at = byEnd.get(end);
    if (at) at.push(cite);
    else byEnd.set(end, [cite]);
  }

  let woven = answer;
  for (const cut of [...byEnd.keys()].sort((a, b) => b - a)) {
    const marks = byEnd
      .get(cut)!
      // One marker per distinct number: a source cited twice for the same
      // span would otherwise print its number twice in a row.
      .filter((c, i, all) => all.findIndex((o) => o.n === c.n) === i)
      .map((c) => {
        const href = safeHref(c.href);
        return href ? `[[${c.n}]](${linkDestination(href)})` : `[${c.n}]`;
      })
      .join("");
    woven = woven.slice(0, cut) + marks + woven.slice(cut);
  }
  return woven;
}
