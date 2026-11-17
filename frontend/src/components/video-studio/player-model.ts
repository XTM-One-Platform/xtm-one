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
 * The rules of the Video Studio player, out of the component so they
 * are tested on their own (`yarn test:unit`): the clock it shows, the keys it
 * answers, the speeds it offers, the volume it remembers and the side of a
 * touch screen a double tap skips towards. Its chapters are
 * `player-chapters.ts`, where it was left `player-resume.ts`. The stillness
 * before its controls and the pointer leave a playing video is the presenting
 * deck's (`deck-idle.ts`).
 */

export { IDLE_HIDE_MS } from "../presentation/deck-idle.ts";

/** The speeds the player offers, slowest first. */
export const PLAYBACK_SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2] as const;

/** What the skip buttons and `J` / `L` move, in seconds; the arrows move half of it. */
export const SKIP_SECONDS = 10;
const ARROW_SECONDS = 5;
export const VOLUME_STEP = 0.1;

/** The frame rate `,` / `.` step by when the video's is not known. */
export const DEFAULT_FPS = 30;
/** Two taps closer than this on a side of a touch screen skip; one alone waits this long to act. */
export const DOUBLE_TAP_MS = 300;
/** How long what a key did stays on the picture. */
export const FLASH_MS = 800;
/** A media URL that stopped answering is minted again at most this often; past it, the player says it failed. */
export const SOURCE_RENEWAL_COOLDOWN_MS = 20_000;
/** How long the player waits for a minted URL to play before it says it failed. */
export const SOURCE_RENEWAL_TIMEOUT_MS = 10_000;
/** How long "Resumed at" offers to start over. */
export const RESUMED_NOTICE_MS = 6000;

/** The side of a touch surface a tap at `share` of its width lands on: back (`-1`), the middle (`0`), forward (`1`). */
export function tapSide(share: number): -1 | 0 | 1 {
  return share < 1 / 3 ? -1 : share > 2 / 3 ? 1 : 0;
}

/** `0:05`, `12:34`, `1:02:03`: a player's clock. */
export function formatPlayerTime(seconds: number): string {
  const total = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = String(total % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** The share of a track under a pointer at `x`, from the track's left edge and width. */
export function shareAt(x: number, left: number, width: number): number {
  return width > 0 ? clamp((x - left) / width, 0, 1) : 0;
}

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Where a picture of `media`'s shape sits in a box of `container`'s, letterboxed (`object-fit: contain`); 16:9 until the shape is known. */
export function letterbox(container: { width: number; height: number }, media: { width: number; height: number }): Box {
  if (!(container.width > 0 && container.height > 0)) return { left: 0, top: 0, width: 0, height: 0 };
  const ratio = media.width > 0 && media.height > 0 ? media.width / media.height : 16 / 9;
  const width = Math.min(container.width, container.height * ratio);
  const height = width / ratio;
  return { left: (container.width - width) / 2, top: (container.height - height) / 2, width, height };
}

/** How far a caption `offset` px from its edge of the player moves to clear `clearance` px of controls there. */
export function captionLift(offset: number, clearance: number): number {
  return Math.max(0, Math.ceil(clearance - offset));
}

/** A cue placed at the top of the picture: a line counted from the top (`line:0`), or a position in its upper half. */
export function cueAtTop(cue: { line?: unknown; snapToLines?: boolean }): boolean {
  if (typeof cue.line !== "number") return false;
  return cue.snapToLines === false ? cue.line < 50 : cue.line >= 0;
}

const CUE_ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", nbsp: "\u00a0", lrm: "\u200e", rlm: "\u200f" };

/** A cue's words as a viewer reads them: its WebVTT markup (`<b>`, `<v Name>`, timestamps) and escapes undone. */
export function cueText(raw: string): string {
  return raw.replace(/<[^>]*>/g, "").replace(/&(amp|lt|gt|nbsp|lrm|rlm);/g, (_, name: string) => CUE_ENTITIES[name]);
}

/** The end of the buffered range the playhead is in, else 0 (`HTMLMediaElement.buffered`). */
export function bufferedEnd(ranges: { length: number; start(i: number): number; end(i: number): number }, time: number): number {
  for (let i = 0; i < ranges.length; i += 1) {
    if (ranges.start(i) <= time + 0.5 && time <= ranges.end(i)) return ranges.end(i);
  }
  return 0;
}

/** The offered speed next to `rate`, one step faster (`1`) or slower (`-1`); the ends hold. */
export function nextSpeed(rate: number, direction: 1 | -1): number {
  if (direction > 0) return PLAYBACK_SPEEDS.find((speed) => speed > rate + 1e-6) ?? PLAYBACK_SPEEDS[PLAYBACK_SPEEDS.length - 1];
  return [...PLAYBACK_SPEEDS].reverse().find((speed) => speed < rate - 1e-6) ?? PLAYBACK_SPEEDS[0];
}

export type PlayerAction =
  | { type: "toggle" }
  | { type: "seekBy"; seconds: number }
  | { type: "seekTo"; share: number }
  | { type: "volumeBy"; delta: number }
  | { type: "mute" }
  | { type: "captions" }
  | { type: "fullscreen" }
  | { type: "speed"; direction: 1 | -1 }
  | { type: "frame"; direction: 1 | -1 }
  | { type: "shortcuts" };

/**
 * What a key does to the player, or `null` for a key it leaves alone. A
 * modified key (Ctrl, Cmd, Alt) is always the browser's.
 */
export function playerKeyAction(event: { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean }): PlayerAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (/^[0-9]$/.test(key)) return { type: "seekTo", share: Number(key) / 10 };
  switch (key) {
    case " ":
    case "k":
      return { type: "toggle" };
    case "ArrowLeft":
      return { type: "seekBy", seconds: -ARROW_SECONDS };
    case "ArrowRight":
      return { type: "seekBy", seconds: ARROW_SECONDS };
    case "j":
      return { type: "seekBy", seconds: -SKIP_SECONDS };
    case "l":
      return { type: "seekBy", seconds: SKIP_SECONDS };
    case "ArrowUp":
      return { type: "volumeBy", delta: VOLUME_STEP };
    case "ArrowDown":
      return { type: "volumeBy", delta: -VOLUME_STEP };
    case "Home":
      return { type: "seekTo", share: 0 };
    case "End":
      return { type: "seekTo", share: 1 };
    case "m":
      return { type: "mute" };
    case "c":
      return { type: "captions" };
    case "f":
      return { type: "fullscreen" };
    case "<":
      return { type: "speed", direction: -1 };
    case ">":
      return { type: "speed", direction: 1 };
    case ",":
      return { type: "frame", direction: -1 };
    case ".":
      return { type: "frame", direction: 1 };
    case "?":
      return { type: "shortcuts" };
    default:
      return null;
  }
}

const VOLUME_KEY = "xtm-video-player-volume";

export interface PlayerVolume {
  volume: number;
  muted: boolean;
}

/** The volume the person last chose, kept across videos; full and unmuted when none is stored. */
export function readPlayerVolume(storage: Pick<Storage, "getItem"> | undefined): PlayerVolume {
  try {
    const stored = JSON.parse(storage?.getItem(VOLUME_KEY) ?? "null") as Partial<PlayerVolume> | null;
    const volume = typeof stored?.volume === "number" && Number.isFinite(stored.volume) ? clamp(stored.volume, 0, 1) : 1;
    return { volume, muted: stored?.muted === true };
  } catch {
    return { volume: 1, muted: false };
  }
}

export function writePlayerVolume(storage: Pick<Storage, "setItem"> | undefined, value: PlayerVolume): void {
  try {
    storage?.setItem(VOLUME_KEY, JSON.stringify({ volume: clamp(value.volume, 0, 1), muted: value.muted }));
  } catch {
    // Storage full or refused (a private window): the volume is simply not remembered.
  }
}
