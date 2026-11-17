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
 * Escape closes the deck's own popup first.
 *
 * The shortcut sheet is a Radix dialog opened over a deck that can itself live
 * in a Radix dialog (the template preview, the generator's candidate). Radix
 * listens for Escape in the CAPTURE phase on the document, and only for the
 * highest layer - but a layer becomes "highest" a few renders after its DOM is
 * on screen (its node is set, its mount effect registers it, every layer
 * re-renders on the update event). An Escape pressed in that gap reaches only
 * the host dialog, which closes the whole preview and the sheet with it.
 *
 * `useEscapeClosesFirst` closes that gap: from the commit that shows the popup
 * (a layout effect, before the browser paints it) a capture listener on the
 * WINDOW - which runs before any listener on the document - answers Escape by
 * closing the popup and consuming the key, so neither the host dialog nor
 * reveal's own Escape binding (fullscreen, overview) acts on it too.
 */
import { useLayoutEffect, useRef } from "react";

export function useEscapeClosesFirst(open: boolean, close: () => void): void {
  const closeRef = useRef(close);
  useLayoutEffect(() => {
    closeRef.current = close;
  });

  useLayoutEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      // preventDefault: a Radix layer that still sees the key does not dismiss.
      // stopPropagation: no document listener (the host dialog, reveal) sees it.
      event.preventDefault();
      event.stopPropagation();
      closeRef.current();
    };
    window.addEventListener("keydown", onKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", onKeyDown, { capture: true });
  }, [open]);
}
