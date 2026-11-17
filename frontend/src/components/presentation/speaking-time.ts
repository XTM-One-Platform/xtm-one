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
 * Speaking-time estimate for speaker notes — the frontend half of
 * `backend/app/services/presentation_notes.py`.
 *
 * The editor panel (word count + time per slide), the speaker view (per-slide
 * estimate, deck total, pacing) and the notes handout all read the same
 * numbers the agent is told in its tool results, so "about 34 minutes spoken"
 * means the same thing on every surface. `WORDS_PER_MINUTE` is pinned to the
 * backend constant by the backend test suite.
 */
import type { PresentationSlide } from "@/types";

import { markupToPlainText } from "./markup";

/** Conversational presenting pace: the middle of the 120-150 wpm read-aloud band.
 * @public
 */
export const WORDS_PER_MINUTE = 130;

/**
 * Spoken pace of a script written WITHOUT spaces between words (Japanese,
 * Chinese): about 300 characters a minute. A word rule reads a whole Japanese
 * sentence as one word, so those characters are counted one by one and
 * converted to word-equivalents at `WORDS_PER_MINUTE / CJK_CHARS_PER_MINUTE`
 * each. Same figure and same character class as the backend (pinned there).
 * @public
 */
export const CJK_CHARS_PER_MINUTE = 300;

const WORD_RE = /[\p{L}\p{N}]+(?:['\u2019-][\p{L}\p{N}]+)*/gu;
// Han (the unified ideographs, extension A, the compatibility block), Hiragana,
// Katakana and half-width Katakana. Hangul is left out on purpose: Korean is
// written with spaces and the word rule above reads it.
const CJK_RE = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/gu;

/**
 * Spoken word-equivalents in a notes string (markup markers do not count): a
 * space-delimited word is one, a CJK character `WORDS_PER_MINUTE /
 * CJK_CHARS_PER_MINUTE` of one, rounded up per string.
 */
export function countWords(notes: string | null | undefined): number {
  if (!notes) return 0;
  const text = markupToPlainText(notes);
  const cjk = text.match(CJK_RE)?.length ?? 0;
  const words = text.replace(CJK_RE, " ").match(WORD_RE)?.length ?? 0;
  return cjk ? words + Math.ceil((cjk * WORDS_PER_MINUTE) / CJK_CHARS_PER_MINUTE) : words;
}

/** Seconds it takes to say `notes` at `WORDS_PER_MINUTE` (0 for no notes). */
export function estimateSpeakingSeconds(notes: string | null | undefined): number {
  const words = countWords(notes);
  if (!words) return 0;
  return Math.max(1, Math.ceil((words * 60) / WORDS_PER_MINUTE));
}

/** Whole-deck estimate: the sum over every slide that carries notes. */
export function deckSpeakingSeconds(slides: ReadonlyArray<Pick<PresentationSlide, "notes">>): number {
  return slides.reduce((total, slide) => total + estimateSpeakingSeconds(slide.notes), 0);
}

/** Estimated seconds of speech from slide 0 up to (not including) `index`. */
export function speakingSecondsBefore(
  slides: ReadonlyArray<Pick<PresentationSlide, "notes">>,
  index: number,
): number {
  return deckSpeakingSeconds(slides.slice(0, Math.max(0, index)));
}

/** Layouts whose slide is structural — notes welcome, their absence not a gap.
 * @public
 */
export const NOTES_OPTIONAL_LAYOUTS: ReadonlySet<string> = new Set(["title", "section", "agenda", "closing"]);

/** True for a content slide the presenter has nothing written to say over. */
export function slideMissesNotes(slide: Pick<PresentationSlide, "layout" | "notes">): boolean {
  if (NOTES_OPTIONAL_LAYOUTS.has(slide.layout)) return false;
  return !slide.notes || slide.notes.trim().length === 0;
}
