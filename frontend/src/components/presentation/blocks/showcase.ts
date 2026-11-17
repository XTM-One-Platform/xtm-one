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
 * The showcase frame of an `image` block: the room the window may take.
 *
 * A framed picture is sized to the PICTURE - the window hugs it at its ratio,
 * never a letterboxed box - so with `fill` the stylesheet needs two numbers CSS
 * cannot read on its own: the width and the height of the room the figure was
 * handed (`.deck-showcase-room`, the box left once the caption is placed). The
 * hook measures them like a chart measures its canvas (`useMeasuredBox` in
 * `chart.tsx`) and publishes them as `--deck-room-w` / `--deck-room-h`, in
 * canvas px; `presentation.css` then takes the largest window of the
 * picture's ratio that fits both (`min()` over the width and the height minus
 * the chrome). Layout metrics (`clientWidth` / `clientHeight`) are what is
 * read: neither the stage transform nor the fit ratio touches them, so the
 * numbers are canvas px wherever the deck is drawn.
 *
 * A room with no height to give - a figure in a run its container hugs, where
 * `height: 100%` would resolve to nothing - publishes its width alone: the
 * stylesheet falls back to the size bucket, so the frame never collapses on a
 * height that was its own content's.
 */
import { useLayoutEffect, useState, type CSSProperties, type RefObject } from "react";

/** Below this, the room's height is its own content, not a height it was handed. */
export const SHOWCASE_MIN_ROOM_PX = 120;

interface ShowcaseRoom {
  width: number;
  height: number | null;
}

/** The inline variables the stylesheet reads, for the measured room (none before the first measure). */
export function showcaseRoomStyle(room: ShowcaseRoom | null): CSSProperties | undefined {
  if (!room) return undefined;
  const style: Record<string, string> = { "--deck-room-w": `${room.width}px` };
  if (room.height !== null) style["--deck-room-h"] = `${room.height}px`;
  return style as CSSProperties;
}

/** What one measurement of the room publishes: the width, and the height when it is a room's. */
export function readShowcaseRoom(width: number, height: number): ShowcaseRoom | null {
  const w = Math.round(width);
  const h = Math.round(height);
  if (w <= 0) return null;
  return { width: w, height: h >= SHOWCASE_MIN_ROOM_PX ? h : null };
}

/** The room's box, re-read on every resize of the room; `null` until it has a width. */
export function useShowcaseRoom(ref: RefObject<HTMLElement | null>, enabled: boolean): ShowcaseRoom | null {
  const [room, setRoom] = useState<ShowcaseRoom | null>(null);
  useLayoutEffect(() => {
    const host = ref.current;
    if (!host || !enabled) return;
    const read = () => {
      const next = readShowcaseRoom(host.clientWidth, host.clientHeight);
      setRoom((current) => {
        if (!next) return current === null ? current : null;
        if (current && current.width === next.width && current.height === next.height) return current;
        return next;
      });
    };
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(host);
    return () => observer.disconnect();
  }, [ref, enabled]);
  return enabled ? room : null;
}
