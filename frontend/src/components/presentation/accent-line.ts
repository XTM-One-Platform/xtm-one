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
 * Where a title's ACCENT LINE begins (`hero_accent_word`) - the pure
 * half of `InlineMarkup`'s `accentLastLine`, over the tokenizer's segments so
 * it is testable without a DOM and the renderer stays a thin wrapper.
 *
 * The rule the schema, the tool description and the inspector promise: the
 * last LINE (everything after the last line break) is drawn in the accent
 * tone, and a one-line title accents its last WORD. The word is found across
 * the runs, so `Take **Action**.` accents `Action.` (the bold run and the full
 * stop), and a run is SPLIT at the word boundary with its wrapper kept on both
 * halves, so `Launch **Take Action.**` accents `Action.` in bold and leaves
 * `Take ` bold and plain - never the whole run. A `code` run is the one
 * exception: it is drawn as a chip (padding, background), and splitting it
 * would show two chips, so it is the word as a whole. Trailing line breaks
 * carry no glyph: they are skipped when the last line is found and drawn
 * plain after the accent, as they always were.
 */
import type { MarkupSegment } from "./markup";

interface AccentSplit {
  /** Drawn plain, before the accent. */
  head: MarkupSegment[];
  /** Drawn in the accent tone; empty when there is nothing to accent. */
  accent: MarkupSegment[];
  /** Drawn plain after the accent: the trailing line breaks. */
  trailing: MarkupSegment[];
}

/** A run whose text is split at a word boundary keeps its kind (and tone). */
function withText(segment: MarkupSegment, text: string): MarkupSegment {
  return { ...segment, text } as MarkupSegment;
}

export function splitAccentLine(segments: MarkupSegment[]): AccentSplit {
  let end = segments.length;
  while (end > 0 && segments[end - 1].kind === "br") end -= 1;
  const trailing = segments.slice(end);
  const body = segments.slice(0, end);
  if (body.length === 0) return { head: segments, accent: [], trailing: [] };

  const lastBreak = body.map((segment) => segment.kind).lastIndexOf("br");
  if (lastBreak >= 0) {
    return { head: body.slice(0, lastBreak + 1), accent: body.slice(lastBreak + 1), trailing };
  }

  // One line: walk back from its end until a run holds a space. Everything
  // after that space - the rest of that run and every run behind it - is the
  // last word.
  for (let index = body.length - 1; index >= 0; index -= 1) {
    const segment = body[index];
    if (segment.kind === "br") continue; // none on one line; narrows the type
    if (segment.kind === "code") continue; // a chip is one token
    const text = segment.text;
    // Trailing whitespace at the very end of the title is not a boundary.
    const searchable = index === body.length - 1 ? text.trimEnd() : text;
    const cut = searchable.lastIndexOf(" ");
    if (cut < 0) continue;
    const before = text.slice(0, cut + 1);
    const word = text.slice(cut + 1);
    // `accent` is never empty: on the last run `word` holds the glyphs after
    // the cut (the trim only skipped what follows them), and an earlier run
    // has at least the last run behind it.
    return {
      head: [...body.slice(0, index), ...(before ? [withText(segment, before)] : [])],
      accent: [...(word ? [withText(segment, word)] : []), ...body.slice(index + 1)],
      trailing,
    };
  }
  // No space anywhere: the whole line is one word.
  return { head: [], accent: body, trailing };
}
