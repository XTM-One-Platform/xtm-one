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
 * The editor's playhead: the time on the stage, in output seconds.
 *
 * Playing moves it every animation frame; only what shows it (the stage, the
 * clock, the timeline's playhead) re-renders, through `usePlayheadTime` - the
 * rest of the editor reads it when it needs it (`get`), so a minute of
 * playback is not a minute of re-rendering the inspector.
 */
import { useSyncExternalStore } from "react";

export interface Playhead {
  get(): number;
  set(seconds: number): void;
  subscribe(listener: () => void): () => void;
}

export function createPlayhead(initial = 0): Playhead {
  let time = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => time,
    set(seconds) {
      const next = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
      if (next === time) return;
      time = next;
      for (const listener of listeners) listener();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export function usePlayheadTime(playhead: Playhead): number {
  return useSyncExternalStore(playhead.subscribe, playhead.get, playhead.get);
}

/**
 * How the timeline draws a time across its tracks, in CSS pixels that are each a whole number of
 * device pixels. A 1px line is 1.25 or 1.5 device pixels on a 125 % or 150 % screen: the
 * browser snaps it one pixel wide, then two, by turns as it moves, and a playing playhead blinks;
 * a head of an even width is never centred on a line one pixel wide. `line` is one device pixel
 * (two from a 2x screen), `side` how far the head reaches on either side of it, `depth` its height.
 */
export interface PlayheadMarks {
  line: number;
  side: number;
  depth: number;
}

export function playheadMarks(pixelRatio: number): PlayheadMarks {
  const ratio = Number.isFinite(pixelRatio) && pixelRatio > 0 ? pixelRatio : 1;
  return { line: Math.max(1, Math.floor(ratio)) / ratio, side: Math.round(5 * ratio) / ratio, depth: Math.round(7 * ratio) / ratio };
}

const readPixelRatio = () => (typeof window === "undefined" ? 1 : window.devicePixelRatio || 1);

function subscribePixelRatio(listener: () => void): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
  let query: MediaQueryList | null = null;
  const changed = () => {
    watch();
    listener();
  };
  // A query true at the current ratio only: it changes the moment the ratio does.
  const watch = () => {
    query?.removeEventListener("change", changed);
    query = window.matchMedia(`(resolution: ${readPixelRatio()}dppx)`);
    query.addEventListener("change", changed);
  };
  watch();
  return () => query?.removeEventListener("change", changed);
}

/** Device pixels per CSS pixel, followed when the window moves to another screen or the page is zoomed. */
export function useDevicePixelRatio(): number {
  return useSyncExternalStore(subscribePixelRatio, readPixelRatio, () => 1);
}

/**
 * A time the stage shows instead of the playhead's while the pointer skims a
 * figure's scene in the timeline: its build plays under the pointer,
 * and the stage is back on the playhead when the pointer leaves. `null`: no skim.
 */
export interface Skim {
  get(): number | null;
  set(seconds: number | null): void;
  subscribe(listener: () => void): () => void;
}

export function createSkim(): Skim {
  let time: number | null = null;
  const listeners = new Set<() => void>();
  return {
    get: () => time,
    set(seconds) {
      const next = seconds === null || !Number.isFinite(seconds) ? null : Math.max(0, seconds);
      if (next === time) return;
      time = next;
      for (const listener of listeners) listener();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

export function useSkimTime(skim: Skim): number | null {
  return useSyncExternalStore(skim.subscribe, skim.get, skim.get);
}
