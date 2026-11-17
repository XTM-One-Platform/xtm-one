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
 * Where the presenter surfaces live.
 *
 * `/presentations/:id/speaker` is the speaker view — a window of its own,
 * opened from the deck's toolbar or the `S` key, that follows the stage over
 * `speaker-sync.ts`. `/presentations/:id/notes` is the printable notes
 * handout. Every entry point builds its URL here so the two routes cannot
 * drift from `App.tsx`. The URLs are ROUTE paths (`navigate()` takes them as
 * is); the `window.open` sinks below add the URL path prefix the platform is
 * served under.
 */
import { withBasePath } from "@/lib/base-path";

export function speakerViewUrl(presentationId: string): string {
  return `/presentations/${presentationId}/speaker`;
}

function notesHandoutUrl(presentationId: string): string {
  return `/presentations/${presentationId}/notes`;
}

/** The window name: opening the speaker view twice focuses the one window.
 * @public
 */
export function speakerWindowName(presentationId: string): string {
  return `xtm-speaker-${presentationId}`;
}

/** Popup geometry: a presenter laptop's second screen or a floating window. */
const SPEAKER_WINDOW_FEATURES = "popup=yes,width=1180,height=760,menubar=no,toolbar=no,location=no,status=no";

/** The speaker windows this page opened, by deck: a second `S` focuses the
 *  open one instead of reloading it (`window.open` with the same name would
 *  navigate it again and drop its timer). */
const speakerWindows = new Map<string, Window>();

/**
 * Open (or focus) the speaker window for a deck. Returns false when the
 * browser blocked the popup, so the caller can fall back to a plain link.
 */
export function openSpeakerView(presentationId: string): boolean {
  const existing = speakerWindows.get(presentationId);
  if (existing && !existing.closed) {
    try {
      existing.focus();
    } catch {
      // Focus is best-effort: some browsers refuse it for background windows.
    }
    return true;
  }
  const opened = window.open(withBasePath(speakerViewUrl(presentationId)), speakerWindowName(presentationId), SPEAKER_WINDOW_FEATURES);
  if (!opened) return false;
  speakerWindows.set(presentationId, opened);
  try {
    opened.focus();
  } catch {
    // Focus is best-effort: some browsers refuse it for background windows.
  }
  return true;
}

/**
 * The audience window (xtm-one #10552, slice A2): slides only, the window a
 * presenter shares in a meeting tool (Teams, Zoom, Meet) while the speaker
 * view covers it on one screen.
 */
export function audienceViewUrl(presentationId: string): string {
  return `/presentations/${presentationId}/audience`;
}

/** @public */
export function audienceWindowName(presentationId: string): string {
  return `xtm-audience-${presentationId}`;
}

/** A 16:9 window by default; the page letterboxes the deck in any size. */
const AUDIENCE_WINDOW_FEATURES = "popup=yes,width=1280,height=720,menubar=no,toolbar=no,location=no,status=no";

/**
 * Open (or bring back) the audience window of a deck. Any window of the app may
 * call it - the stage, the speaker view - so an audience window another window
 * opened is found by its NAME: `window.open("", name)` hands back the open
 * window without navigating it, and only a fresh blank one is pointed at the
 * audience route. A reload would drop the share the meeting tool holds on it.
 * Returns false when the browser blocked the popup.
 */
export function openAudienceWindow(presentationId: string, features: string = AUDIENCE_WINDOW_FEATURES): boolean {
  const url = withBasePath(audienceViewUrl(presentationId));
  const opened = window.open("", audienceWindowName(presentationId), features);
  if (!opened) return false;
  let showsAudience = false;
  try {
    showsAudience = opened.location.pathname.endsWith(audienceViewUrl(presentationId));
  } catch {
    // Not readable: treat as fresh and point it at the route.
  }
  if (!showsAudience) opened.location.href = url;
  try {
    opened.focus();
  } catch {
    // Focus is best-effort.
  }
  return true;
}

/** The part of a `ScreenDetailed` (Window Management API) the placement reads. */
export interface ScreenArea {
  availLeft: number;
  availTop: number;
  availWidth: number;
  availHeight: number;
}

/**
 * Window features that place the audience window over the whole of a screen
 * OTHER than the one the speaker view is on (xtm-one #10552, slice A3), or
 * null when there is no other screen: on one screen the plain 16:9 window is
 * the one to share. The primary-first order of the list is kept, so with three
 * screens the first one that is not the presenter's wins.
 */
export function audienceFeaturesForScreens(current: ScreenArea, screens: readonly ScreenArea[]): string | null {
  const same = (a: ScreenArea) =>
    a.availLeft === current.availLeft &&
    a.availTop === current.availTop &&
    a.availWidth === current.availWidth &&
    a.availHeight === current.availHeight;
  const other = screens.find((screen) => !same(screen));
  if (!other) return null;
  return [
    "popup=yes",
    `left=${other.availLeft}`,
    `top=${other.availTop}`,
    `width=${other.availWidth}`,
    `height=${other.availHeight}`,
    "menubar=no",
    "toolbar=no",
    "location=no",
    "status=no",
  ].join(",");
}

interface ScreenDetailsLike {
  currentScreen: ScreenArea;
  screens: readonly ScreenArea[];
}

/**
 * Ask the browser where the screens are when the presenter has more than one
 * (`screen.isExtended`); null on one screen, without the Window Management API
 * (Firefox, Safari) or when the permission is refused, which keeps the plain
 * window. The browser asks the permission once, on the first use.
 */
export async function audienceFeaturesOnOtherScreen(): Promise<string | null> {
  const extended = (window.screen as Screen & { isExtended?: boolean }).isExtended;
  const getScreenDetails = (window as Window & { getScreenDetails?: () => Promise<ScreenDetailsLike> }).getScreenDetails;
  if (!extended || typeof getScreenDetails !== "function") return null;
  try {
    const details = await getScreenDetails.call(window);
    return audienceFeaturesForScreens(details.currentScreen, details.screens);
  } catch {
    return null;
  }
}

/** Open the printable notes handout in a new tab (it prints itself from there). */
export function openNotesHandout(presentationId: string): void {
  window.open(withBasePath(notesHandoutUrl(presentationId)), "_blank", "noopener");
}
