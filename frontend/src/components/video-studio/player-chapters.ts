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
 * A render's chapters in the Video Studio player: the scenes the
 * render recorded (`timeline.chapters`), marked on the scrubber and named
 * while they play. Pure, so `yarn test:unit` holds the rules on their own.
 */
import type { VideoChapter } from "@/types";

/** How far into a chapter "previous chapter" goes back to its start rather than to the one before. */
export const CHAPTER_RESTART_SECONDS = 3;

export interface ChapterSpan extends VideoChapter {
  index: number;
  end: number;
}

/**
 * The chapters a player draws for a video of *duration* seconds: in order,
 * inside the video, each running to the next one's start. Fewer than two is
 * none at all - one chapter is the video itself.
 */
export function chapterSpans(chapters: readonly VideoChapter[] | null | undefined, duration: number): ChapterSpan[] {
  if (!chapters || !(duration > 0)) return [];
  const inside = chapters
    .filter((chapter) => Number.isFinite(chapter.start) && chapter.start >= 0 && chapter.start < duration)
    .slice()
    .sort((a, b) => a.start - b.start)
    .filter((chapter, index, all) => index === 0 || chapter.start > all[index - 1].start);
  if (inside.length < 2) return [];
  if (inside[0].start > 0) inside[0] = { ...inside[0], start: 0 };
  return inside.map((chapter, index) => ({ ...chapter, index, end: inside[index + 1]?.start ?? duration }));
}

/** The chapter playing at *time*, or `null` when the video has none. */
export function chapterAt(spans: readonly ChapterSpan[], time: number): ChapterSpan | null {
  let found: ChapterSpan | null = null;
  for (const span of spans) {
    if (span.start <= time) found = span;
    else break;
  }
  return found;
}

/**
 * Where "next chapter" (`1`) or "previous chapter" (`-1`) seeks from *time*,
 * or `null` past the last one. Previous goes back to the start of the
 * chapter playing once it has played a moment, as a player's does.
 */
export function chapterSeek(spans: readonly ChapterSpan[], time: number, direction: 1 | -1): number | null {
  const current = chapterAt(spans, time);
  if (!current) return null;
  if (direction > 0) return spans[current.index + 1]?.start ?? null;
  if (time - current.start > CHAPTER_RESTART_SECONDS || current.index === 0) return current.start;
  return spans[current.index - 1].start;
}
