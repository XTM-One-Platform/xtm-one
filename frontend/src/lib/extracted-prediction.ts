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
 * How much of a stored extracted prediction a reader is shown.
 *
 * The value is persisted WHOLE, as the record of what was scored, so the renderer
 * bounds it. A real bound, not a scroll box: a `label_set` may hold 2000 entries of
 * 200 characters (`extraction_max_values`, `extraction_max_value_chars`), ~400 KB in
 * one node that a `max-h` only hides. Copy and Download still hand over every byte.
 *
 * Kept free of React so it can be tested on its own.
 */

/** Characters of the pretty-printed value rendered into the DOM. */
export const EXTRACTED_PREVIEW_CHARS = 4000;

export interface RenderedPrediction {
  /** What to render — already pretty-printed and already bounded. */
  text: string;
  /** Characters withheld from `text`; 0 when the whole value is shown. */
  hidden: number;
}

/**
 * Pretty-print `prediction` when it is JSON, and bound what comes back.
 *
 * Non-JSON is passed through verbatim: the output kind is validated elsewhere, and
 * a reader debugging a score is better served by the raw characters than an error.
 */
export function renderExtractedPrediction(
  prediction: string,
  limit: number = EXTRACTED_PREVIEW_CHARS,
): RenderedPrediction {
  let pretty: string;
  try {
    pretty = JSON.stringify(JSON.parse(prediction.trim()), null, 2);
  } catch {
    pretty = prediction;
  }
  // `undefined` is what JSON.stringify returns for a bare `undefined`; a stored
  // prediction can never be that, but a non-string here would reach the DOM.
  if (typeof pretty !== "string") pretty = prediction;
  return {
    text: boundedSlice(pretty, limit),
    hidden: Math.max(0, pretty.length - limit),
  };
}

/**
 * `slice` cuts on UTF-16 code units, so a bound inside a surrogate pair leaves a
 * lone high surrogate: `encodeURIComponent` THROWS on it and `JSON.stringify`
 * re-emits it escaped. `components/traces/prompt-text.ts` applies the same rule.
 *
 * An unpaired surrogate already in the value is left alone: the job is not to
 * create one.
 */
function boundedSlice(value: string, limit: number): string {
  const cut = value.slice(0, limit);
  const last = cut.charCodeAt(cut.length - 1);
  // A high surrogate in final position had its partner cut away by this slice.
  return last >= 0xd800 && last <= 0xdbff ? cut.slice(0, -1) : cut;
}
