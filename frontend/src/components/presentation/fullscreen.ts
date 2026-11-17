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
 * Fullscreen state of one element, kept in sync with the browser.
 *
 * The Fullscreen API is asynchronous and can be left from OUTSIDE the app —
 * `Esc`, a browser gesture, another element taking over — so the only reliable
 * source of truth is `document.fullscreenElement` read on `fullscreenchange`.
 * Setting local state from the `requestFullscreen()` promise alone leaves the
 * toolbar showing "exit fullscreen" for a deck that already left it.
 *
 * `Present` must always present something, so the hook has two modes:
 *
 *   - `native`: the browser's fullscreen, requested on the element.
 *   - `viewport`: the in-app fallback, taken whenever the browser will not
 *     grant the native one — the API reports itself available and then
 *     REFUSES the request (an Electron webview, a kiosk or enterprise policy:
 *     "Permissions check failed"), or is not available at all. The element is
 *     moved into the browser's top layer through the Popover API (`popover=
 *     "manual"`, no light dismiss, no Escape of its own), which is where the
 *     native fullscreen paints too: above every `z-index`, clear of any
 *     transformed ancestor (a dialog's centring translate would pin a plain
 *     `position: fixed` stage to the dialog) — and sized to the viewport by
 *     the stage CSS keyed on `data-deck-viewport`. Where even the Popover API
 *     is missing the same CSS still fixes it over the page.
 *
 * `supported` is false where the API is unavailable or disallowed up front —
 * most notably inside an iframe without `allow="fullscreen"`, which is how the
 * chat is embedded in OpenCTI / OpenAEV. The toolbar then prefers the
 * standalone viewer (a tab that CAN go fullscreen) over presenting inside a
 * small embedded frame, and only falls back to the viewport mode when it has
 * no viewer to offer.
 *
 * `keepPresenting(open)` runs something that makes the browser drop its
 * fullscreen as a side effect (Chrome ends element fullscreen when the page
 * opens or focuses another window, such as the speaker view) without leaving
 * the presentation: a native exit that follows within `KEEP_PRESENTING_MS`
 * moves the element to the viewport mode instead of back to the page.
 */
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

/** @public */
export type FullscreenMode = "native" | "viewport";

export interface ElementFullscreen {
  isFullscreen: boolean;
  /** How the element is presented right now, `null` when it is not. */
  mode: FullscreenMode | null;
  /** Whether the browser's own fullscreen is available here at all. */
  supported: boolean;
  enter: () => void;
  exit: () => void;
  toggle: () => void;
  /** Run `action` (opening or focusing another window) and stay presented:
   *  a native fullscreen the browser ends because of it falls back to the
   *  viewport mode. */
  keepPresenting: (action: () => void) => void;
}

/** How long after `keepPresenting` a native exit is read as caused by it.
 * @public
 */
export const KEEP_PRESENTING_MS = 1500;

/**
 * DOM marker of the viewport mode. Set SYNCHRONOUSLY on enter (not from a
 * render), so a key handler racing the re-render — `F` then `Esc` in quick
 * succession — reads the truth, exactly as `document.fullscreenElement` is
 * the truth for the native mode. The stage CSS keys on it.
 * @public
 */
export const VIEWPORT_FULLSCREEN_ATTR = "data-deck-viewport";

export function isFullscreenSupported(): boolean {
  return (
    typeof document !== "undefined" &&
    !!document.fullscreenEnabled &&
    typeof document.documentElement.requestFullscreen === "function"
  );
}

/**
 * The element being presented, in either mode — the live DOM read a key
 * handler needs (a dialog hosting a deck must not close on the `Esc` that
 * was meant to leave the presentation).
 */
export function presentedElement(): Element | null {
  if (typeof document === "undefined") return null;
  return document.fullscreenElement ?? document.querySelector(`[${VIEWPORT_FULLSCREEN_ATTR}]`);
}

export function useElementFullscreen(ref: RefObject<HTMLElement | null>): ElementFullscreen {
  const [mode, setMode] = useState<FullscreenMode | null>(null);
  const [supported] = useState(isFullscreenSupported);
  // Whether this element is natively fullscreen, read synchronously by the
  // `fullscreenchange` listener (a state snapshot would lag a render behind).
  const nativeRef = useRef(false);
  // Until when a native exit is the side effect of `keepPresenting`.
  const keepUntilRef = useRef(0);

  const enterViewport = useCallback(() => {
    const element = ref.current;
    if (!element) return;
    element.setAttribute(VIEWPORT_FULLSCREEN_ATTR, "");
    if (typeof element.showPopover === "function") {
      // Attribute and show in the same task: a `[popover]` is `display: none`
      // until open, and a rendering step between the two would flash the deck
      // away and hand reveal a zero-size layout.
      element.setAttribute("popover", "manual");
      try {
        element.showPopover();
      } catch {
        // Already open, or the element cannot be a popover here: the fixed
        // positioning the attribute above selects still covers the page.
      }
    }
    setMode("viewport");
  }, [ref]);

  useEffect(() => {
    const sync = () => {
      const native = !!document.fullscreenElement && document.fullscreenElement === ref.current;
      const wasNative = nativeRef.current;
      nativeRef.current = native;
      if (wasNative && !native && Date.now() < keepUntilRef.current) {
        // The browser left fullscreen because another window opened: the
        // deck stays presented, in the app's viewport.
        keepUntilRef.current = 0;
        enterViewport();
        return;
      }
      // The viewport mode is not the browser's and is untouched by its events.
      setMode((current) => (native ? "native" : current === "native" ? null : current));
    };
    document.addEventListener("fullscreenchange", sync);
    document.addEventListener("fullscreenerror", sync);
    return () => {
      document.removeEventListener("fullscreenchange", sync);
      document.removeEventListener("fullscreenerror", sync);
    };
  }, [ref, enterViewport]);

  const exitViewport = useCallback(() => {
    const element = ref.current;
    if (element) {
      if (element.hasAttribute("popover")) {
        try {
          if (element.matches(":popover-open")) element.hidePopover();
        } catch {
          // Not open (or no `:popover-open` support): nothing to hide.
        }
        element.removeAttribute("popover");
      }
      element.removeAttribute(VIEWPORT_FULLSCREEN_ATTR);
    }
    setMode((current) => (current === "viewport" ? null : current));
  }, [ref]);

  const exit = useCallback(() => {
    // Whatever is fullscreen (this element or anything else) is left; the
    // `fullscreenchange` listener above records the outcome.
    keepUntilRef.current = 0;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined);
    exitViewport();
  }, [exitViewport]);

  const enter = useCallback(() => {
    const element = ref.current;
    if (!element) return;
    if (!supported) {
      enterViewport();
      return;
    }
    // The Promise is stored before `.catch` because optional chaining covers
    // only the CALL: where `requestFullscreen` is undefined, `?.()` yields
    // undefined and chaining onto it throws. The API rejects on its own
    // whenever the gesture is not user-activated or the permission is denied
    // — the deck presents anyway, in the viewport mode, instead of a button
    // that silently does nothing.
    const request = element.requestFullscreen?.({ navigationUI: "hide" });
    if (!request) {
      enterViewport();
      return;
    }
    request.catch(() => enterViewport());
  }, [ref, supported, enterViewport]);

  const toggle = useCallback(() => {
    if (document.fullscreenElement || ref.current?.hasAttribute(VIEWPORT_FULLSCREEN_ATTR)) exit();
    else enter();
  }, [ref, enter, exit]);

  const keepPresenting = useCallback((action: () => void) => {
    if (nativeRef.current) keepUntilRef.current = Date.now() + KEEP_PRESENTING_MS;
    action();
  }, []);

  return { isFullscreen: mode !== null, mode, supported, enter, exit, toggle, keepPresenting };
}
