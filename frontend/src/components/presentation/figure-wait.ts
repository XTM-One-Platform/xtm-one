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
 * How long a deck figure may hold the ready signal.
 *
 * A figure carries `data-deck-pending` until it is complete, and the viewer's
 * browser-print path waits for every one of them (`deckSettled()` in
 * `ready.ts`). Some waits have no end of their own: a chart canvas that never
 * gets room to draw in, a lazy chunk the network holds, a Mermaid render that
 * never answers, the icon catalog still on its way. So a figure GIVES UP after
 * a bound of visible time (`visible-time.ts`): it drops `data-deck-pending`,
 * keeps what it shows - its placeholder, the drawing as it stands - and says
 * so with `data-deck-unfinished`, which the print path reads to tell the
 * reader. What it waited for may still arrive and finish the figure.
 *
 * Never on the export page (`[data-deck-static="true"]`): the exporter and the
 * layout check wait under their own server-side timeout, and a client bound
 * must not make them capture a figure half drawn.
 */
import { useEffect, useRef, useState, type RefObject } from "react";

import { FIGURE_WAIT_MS } from "./ready";
import { pageVisibleClock } from "./visible-time";

const STATIC_PAGE = '[data-deck-static="true"]';

/**
 * Calls `onGiveUp(wait)` once `ms` of visible time has passed while the wait
 * named `wait` lasted. `null` waits for nothing; a new name is a new wait,
 * counted from zero. `ref` is an element of the figure, read when the bound
 * runs out to leave the export page alone.
 */
export function useWaitBound(
  ref: RefObject<Element | null>,
  wait: string | null,
  ms: number,
  onGiveUp: (wait: string) => void,
): void {
  const giveUp = useRef(onGiveUp);
  giveUp.current = onGiveUp;
  useEffect(() => {
    if (wait === null) return;
    return pageVisibleClock().wait(ms, () => {
      if (!ref.current?.closest(STATIC_PAGE)) giveUp.current(wait);
    });
  }, [ref, wait, ms]);
}

/** Whether the figure gave up on the wait named `wait` (`useWaitBound`): `true` for as long as that wait lasts. */
export function useGiveUp(ref: RefObject<Element | null>, wait: string | null, ms: number = FIGURE_WAIT_MS): boolean {
  const [gaveUp, setGaveUp] = useState<string | null>(null);
  useWaitBound(ref, wait, ms, setGaveUp);
  return wait !== null && gaveUp === wait;
}
