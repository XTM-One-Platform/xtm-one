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
 * The app-side handle on a page DOCUMENT — the counterpart of
 * `useDeckController` for the portrait formats.
 *
 * An A4 / letter presentation is read, not presented: its pages are stacked in
 * a scrolling surface (`PageDocument`) and the reader scrolls through them, so
 * there is no reveal instance to ask "which slide is on screen". The
 * controller derives the current page from the scroll position instead —
 * the page under the reading line, a third of the way down the viewport — and
 * offers the same navigation verbs the deck toolbar has (`prev` / `next` /
 * `first` / `last` / `goTo`), each a scroll to the page's top, plus the zoom
 * the document viewer adds.
 *
 * Zoom is RELATIVE TO FIT-WIDTH: `1` is "the page fills the surface width",
 * the presets step above and below it. An absolute scale would be meaningless
 * here — the canvas is 1920px wide, so "100%" would mean a page wider than most
 * screens.
 *
 * One controller per rendered document. `attach` is handed to `PageDocument`
 * and receives the scroll surface once mounted (and `null` on unmount).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

/** Zoom presets, relative to fit-width (`1`).
 * @public
 */
export const PAGE_ZOOM_STEPS: readonly number[] = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 3];

/** Where, down the viewport, a page is considered "the one being read". */
const READING_LINE = 0.35;

interface PagePosition {
  /** Zero-based index of the page under the reading line. */
  index: number;
  total: number;
  hasPrev: boolean;
  hasNext: boolean;
}

export interface PageController extends PagePosition {
  ready: boolean;
  /** The scroll surface, once mounted. */
  surface: HTMLElement | null;
  attach: (surface: HTMLElement | null) => void;
  /** Zoom factor relative to fit-width (`1`). */
  zoom: number;
  canZoomIn: boolean;
  canZoomOut: boolean;
  zoomIn: () => void;
  zoomOut: () => void;
  fitWidth: () => void;
  setZoom: (zoom: number) => void;
  prev: () => void;
  next: () => void;
  first: () => void;
  last: () => void;
  goTo: (index: number) => void;
}

const IDLE: PagePosition = { index: 0, total: 0, hasPrev: false, hasNext: false };

/** The page sheets of a surface, in document order. */
function sheetsOf(surface: HTMLElement): HTMLElement[] {
  return Array.from(surface.querySelectorAll<HTMLElement>("[data-page-index]"));
}

function readPosition(surface: HTMLElement): PagePosition {
  const sheets = sheetsOf(surface);
  const total = sheets.length;
  if (total === 0) return IDLE;
  const surfaceTop = surface.getBoundingClientRect().top;
  const line = surfaceTop + surface.clientHeight * READING_LINE;
  // The last sheet whose top is above the reading line; the first sheet when
  // none is (the document is scrolled to its very top).
  let index = 0;
  for (let i = 0; i < sheets.length; i += 1) {
    if (sheets[i].getBoundingClientRect().top <= line) index = i;
    else break;
  }
  // Scrolled to the very end: the last page is the one being read even when
  // it is too short to reach the reading line.
  if (surface.scrollTop + surface.clientHeight >= surface.scrollHeight - 1) index = total - 1;
  return { index, total, hasPrev: index > 0, hasNext: index < total - 1 };
}

export function usePageController(): PageController {
  const [surface, setSurface] = useState<HTMLElement | null>(null);
  const [position, setPosition] = useState<PagePosition>(IDLE);
  const [zoom, setZoomState] = useState(1);
  const surfaceRef = useRef<HTMLElement | null>(null);

  const attach = useCallback((next: HTMLElement | null) => {
    surfaceRef.current = next;
    setSurface(next);
    setPosition(next ? readPosition(next) : IDLE);
  }, []);

  // Follow the scroll (coalesced per frame) and the document's own changes: a
  // revision that adds a page, a zoom that re-flows every sheet.
  useEffect(() => {
    if (!surface) return;
    let frame = 0;
    const refresh = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        if (surfaceRef.current === surface) setPosition(readPosition(surface));
      });
    };
    refresh();
    surface.addEventListener("scroll", refresh, { passive: true });
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => refresh());
    observer?.observe(surface);
    const mutations = new MutationObserver(refresh);
    mutations.observe(surface, { childList: true, subtree: true, attributes: true });
    return () => {
      surface.removeEventListener("scroll", refresh);
      observer?.disconnect();
      mutations.disconnect();
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [surface]);

  const scrollTo = useCallback((index: number) => {
    const current = surfaceRef.current;
    if (!current) return;
    const sheets = sheetsOf(current);
    if (sheets.length === 0) return;
    const target = sheets[Math.max(0, Math.min(sheets.length - 1, index))];
    // Relative to the surface, whatever the page's own scroll is; the gutter
    // above the first sheet is kept so a jump lands like the initial view.
    const gutter = sheets[0].offsetTop;
    current.scrollTo({ top: target.offsetTop - gutter, behavior: "smooth" });
  }, []);

  const setZoom = useCallback((next: number) => {
    const min = PAGE_ZOOM_STEPS[0];
    const max = PAGE_ZOOM_STEPS[PAGE_ZOOM_STEPS.length - 1];
    setZoomState(Math.min(max, Math.max(min, next)));
  }, []);

  const actions = useMemo(
    () => ({
      prev: () => {
        const current = surfaceRef.current;
        if (current) scrollTo(readPosition(current).index - 1);
      },
      next: () => {
        const current = surfaceRef.current;
        if (current) scrollTo(readPosition(current).index + 1);
      },
      first: () => scrollTo(0),
      last: () => scrollTo(Number.MAX_SAFE_INTEGER),
      goTo: scrollTo,
      zoomIn: () =>
        setZoomState((current) => PAGE_ZOOM_STEPS.find((step) => step > current + 1e-6) ?? current),
      zoomOut: () =>
        setZoomState(
          (current) =>
            [...PAGE_ZOOM_STEPS].reverse().find((step) => step < current - 1e-6) ?? current,
        ),
      fitWidth: () => setZoomState(1),
    }),
    [scrollTo],
  );

  return {
    ...position,
    ready: surface !== null,
    surface,
    attach,
    zoom,
    canZoomIn: zoom < PAGE_ZOOM_STEPS[PAGE_ZOOM_STEPS.length - 1] - 1e-6,
    canZoomOut: zoom > PAGE_ZOOM_STEPS[0] + 1e-6,
    setZoom,
    ...actions,
  };
}
