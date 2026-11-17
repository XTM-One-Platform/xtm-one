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
 * Where a viewer left a video: the Video Studio player picks it up
 * there the next time, in this browser. Kept per render (or public link) in
 * one bounded `localStorage` entry. Pure, so `yarn test:unit` holds it.
 */

const RESUME_KEY = "xtm-video-player-resume";

/** Under this, the video starts over: nothing was watched worth coming back to. */
const RESUME_MIN_SECONDS = 5;
/** Closer than this to the end, the video was watched: it starts over. */
const RESUME_END_MARGIN_SECONDS = 10;
/** How many videos are remembered; the oldest go first. */
export const RESUME_MAX_ENTRIES = 50;
/** How long a position is remembered. */
export const RESUME_TTL_MS = 30 * 24 * 60 * 60 * 1000;

type Positions = Record<string, { t: number; at: number }>;

function read(storage: Pick<Storage, "getItem"> | undefined): Positions {
  try {
    const parsed = JSON.parse(storage?.getItem(RESUME_KEY) ?? "null") as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Positions) : {};
  } catch {
    return {};
  }
}

function write(storage: Pick<Storage, "setItem"> | undefined, positions: Positions): void {
  try {
    storage?.setItem(RESUME_KEY, JSON.stringify(positions));
  } catch {
    // Storage full or refused (a private window): the position is simply not remembered.
  }
}

/** Whether *time* in a video of *duration* is worth coming back to. */
export function isResumable(time: number, duration: number): boolean {
  return (
    Number.isFinite(time) && duration > 0 && time >= RESUME_MIN_SECONDS && time <= duration - RESUME_END_MARGIN_SECONDS
  );
}

/** Where the viewer left the video *key*, in seconds, or `null` to start from the beginning. */
export function readResumePosition(
  storage: Pick<Storage, "getItem"> | undefined,
  key: string,
  duration: number,
  now = Date.now(),
): number | null {
  const entry = read(storage)[key];
  if (!entry || typeof entry.t !== "number" || typeof entry.at !== "number") return null;
  if (now - entry.at > RESUME_TTL_MS) return null;
  return isResumable(entry.t, duration) ? entry.t : null;
}

/**
 * Remember *time* for the video *key*, or forget it when it is not worth
 * coming back to (the start, the end). The newest `RESUME_MAX_ENTRIES` are kept.
 */
export function writeResumePosition(
  storage: (Pick<Storage, "getItem"> & Pick<Storage, "setItem">) | undefined,
  key: string,
  time: number,
  duration: number,
  now = Date.now(),
): void {
  const positions = read(storage);
  delete positions[key];
  if (isResumable(time, duration)) positions[key] = { t: Math.floor(time * 10) / 10, at: now };
  const kept = Object.entries(positions)
    .filter(([, entry]) => entry && typeof entry.at === "number" && now - entry.at <= RESUME_TTL_MS)
    .sort(([, a], [, b]) => b.at - a.at)
    .slice(0, RESUME_MAX_ENTRIES);
  write(storage, Object.fromEntries(kept));
}
