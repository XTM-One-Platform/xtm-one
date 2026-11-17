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
 * The content box of a figure's host, in canvas px, re-read on every resize -
 * the ONE way a drawing that lays itself out for its room (a chart's canvas,
 * an architecture's stage) learns how much room it has.
 *
 * The deck is scaled by a CSS transform, so `clientWidth` / `clientHeight`
 * are canvas px whatever the screen. A host measured at zero (a slide reveal
 * keeps at `display: none`) reports `null` until it is shown.
 */
import { useLayoutEffect, useRef, useState, type RefObject } from "react";

export interface Box {
  width: number;
  height: number;
}

export function useMeasuredBox(ref: RefObject<HTMLElement | null>): Box | null {
  const [box, setBox] = useState<Box | null>(null);
  useLayoutEffect(() => {
    const host = ref.current;
    if (!host) return;
    const read = () => {
      const width = Math.round(host.clientWidth);
      const height = Math.round(host.clientHeight);
      setBox((current) => {
        if (width <= 0 || height <= 0) return current === null ? current : null;
        if (current && current.width === width && current.height === height) return current;
        return { width, height };
      });
    };
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(host);
    return () => observer.disconnect();
  }, [ref]);
  return box;
}

/**
 * How many times a settled box may turn back (grow then shrink, or the other
 * way, in width or height) within `BOX_SETTLE_WINDOW_MS` before it is frozen.
 * A deck arriving moves the room many times in one direction (shown, fonts,
 * pictures) and turns it back once or twice (the slide's fit); a room that
 * keeps going back and forth is one the figure's own size feeds back into.
 * @public
 */
export const BOX_SETTLE_CHANGES = 12;
/** The window `BOX_SETTLE_CHANGES` is counted over, in ms. @public */
export const BOX_SETTLE_WINDOW_MS = 1000;

/** What `useSettledBox` reads: the box, and whether it was frozen before it settled. */
export interface SettledBox {
  box: Box | null;
  /** The room kept changing past the bound: `box` is the last one taken, frozen until `settleKey` changes. */
  unsettled: boolean;
}

/**
 * `useMeasuredBox` for a figure that lays itself out for its room AND sizes
 * the room from that layout (an architecture's stage is as tall as its drawing's
 * ratio): a loop the browser can run forever when the slide's shrink-to-fit
 * hands the room back changed (#11580). The box is read the same way, but once
 * it has turned back more than `BOX_SETTLE_CHANGES` times within
 * `BOX_SETTLE_WINDOW_MS` it is frozen at the last box taken - the drawing stops
 * moving, the slide's fit settles on it - and `unsettled` says so, for the
 * layout check to name the figure. A new `settleKey` (the figure's input: an
 * edit, its assets) counts again from zero. Being shown or hidden is not a change,
 * and a burst of changes in one direction (a cold render) is not an oscillation.
 */
export function useSettledBox(ref: RefObject<HTMLElement | null>, settleKey: unknown): SettledBox {
  const [box, setBox] = useState<Box | null>(null);
  const [unsettled, setUnsettled] = useState(false);
  const last = useRef<Box | null>(null);
  useLayoutEffect(() => {
    const host = ref.current;
    if (!host) return;
    let frozen = false;
    let changes: number[] = [];
    let lastStep: Box | null = null;
    setUnsettled(false);
    const read = () => {
      if (frozen) return;
      const width = Math.round(host.clientWidth);
      const height = Math.round(host.clientHeight);
      const next = width > 0 && height > 0 ? { width, height } : null;
      const prev = last.current;
      if (next === null ? prev === null : prev !== null && prev.width === width && prev.height === height) return;
      if (prev && next) {
        const step = { width: Math.sign(next.width - prev.width), height: Math.sign(next.height - prev.height) };
        const turnsBack =
          lastStep !== null && (step.width * lastStep.width < 0 || step.height * lastStep.height < 0);
        lastStep = step;
        if (turnsBack) {
          const now = performance.now();
          changes = changes.filter((at) => now - at < BOX_SETTLE_WINDOW_MS);
          changes.push(now);
          if (changes.length > BOX_SETTLE_CHANGES) {
            frozen = true;
            setUnsettled(true);
            return;
          }
        }
      }
      last.current = next;
      setBox(next);
    };
    read();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(read);
    observer.observe(host);
    return () => observer.disconnect();
  }, [ref, settleKey]);
  return { box, unsettled };
}
