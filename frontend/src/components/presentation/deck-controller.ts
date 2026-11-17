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
 * The app-side handle on a live reveal.js deck.
 *
 * reveal's own on-slide controls (the corner arrows, the progress bar, the
 * `?` overlay, the `G` jump box) are switched off in `PresentationDeck`, so the
 * slide surface carries nothing but the slide. Everything a presenter operates
 * lives in `DeckToolbar` instead, and this hook is what the toolbar drives:
 * a reactive snapshot of the deck (position, routes, overview, blackout) plus
 * the navigation actions, re-derived from reveal's events rather than from the
 * click that requested them — a swipe, a fragment, a `sync()` after an in-place
 * edit all move the deck without the toolbar being involved.
 *
 * One controller per rendered deck. `attach` is handed to `PresentationDeck`
 * as `onDeck` and receives the instance once ready (and `null` on unmount).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RevealApi } from "reveal.js";

interface DeckPosition {
  /** Zero-based flat index of the current slide (fragments do not count). */
  index: number;
  total: number;
  hasPrev: boolean;
  hasNext: boolean;
  overview: boolean;
  /** reveal's "paused" state: the black-screen presenter mode (`B` / `.`). */
  blackout: boolean;
}

export interface DeckController extends DeckPosition {
  ready: boolean;
  api: RevealApi | null;
  attach: (api: RevealApi | null) => void;
  prev: () => void;
  next: () => void;
  first: () => void;
  last: () => void;
  goTo: (index: number) => void;
  toggleOverview: () => void;
  toggleBlackout: () => void;
}

const IDLE: DeckPosition = {
  index: 0,
  total: 0,
  hasPrev: false,
  hasNext: false,
  overview: false,
  blackout: false,
};

function readPosition(api: RevealApi): DeckPosition {
  const routes = api.availableRoutes({ includeFragments: true });
  return {
    index: api.getSlidePastCount(),
    total: api.getTotalSlides(),
    // Decks are linear (one horizontal row), but a fragment or a vertical
    // stack authored by hand still counts as "more to show".
    hasPrev: routes.left || routes.up,
    hasNext: routes.right || routes.down,
    overview: api.isOverview(),
    blackout: api.isPaused(),
  };
}

// Every reveal event after which the snapshot can differ. `sync` covers an
// in-place edit that added or removed a slide (`PresentationDeck` calls
// `sync()` on each revision), so `total` follows the agent's edits live.
const POSITION_EVENTS = [
  "ready",
  "slidechanged",
  "sync",
  "fragmentshown",
  "fragmenthidden",
  "overviewshown",
  "overviewhidden",
  "paused",
  "resumed",
] as const;

function samePosition(a: DeckPosition, b: DeckPosition): boolean {
  return (
    a.index === b.index &&
    a.total === b.total &&
    a.hasPrev === b.hasPrev &&
    a.hasNext === b.hasNext &&
    a.overview === b.overview &&
    a.blackout === b.blackout
  );
}

export function useDeckController(): DeckController {
  const [api, setApi] = useState<RevealApi | null>(null);
  const [position, setPositionState] = useState<DeckPosition>(IDLE);
  const apiRef = useRef<RevealApi | null>(null);

  // An event that leaves the snapshot as it was keeps the previous object, so
  // the host does not re-render: a `sync()` re-run on every render of a deck
  // whose host owns this controller would otherwise feed itself.
  const setPosition = useCallback((next: DeckPosition) => {
    setPositionState((previous) => (samePosition(previous, next) ? previous : next));
  }, []);

  const attach = useCallback(
    (next: RevealApi | null) => {
      apiRef.current = next;
      setApi(next);
      setPosition(next?.isReady() ? readPosition(next) : IDLE);
    },
    [setPosition],
  );

  useEffect(() => {
    if (!api) return;
    const refresh = () => {
      if (apiRef.current === api && api.isReady()) setPosition(readPosition(api));
    };
    refresh();
    for (const type of POSITION_EVENTS) api.on(type, refresh);
    return () => {
      for (const type of POSITION_EVENTS) api.off(type, refresh);
    };
  }, [api, setPosition]);

  const run = useCallback((action: (deck: RevealApi) => void) => {
    const deck = apiRef.current;
    if (deck?.isReady()) action(deck);
  }, []);

  const actions = useMemo(
    () => ({
      prev: () => run((deck) => deck.prev()),
      next: () => run((deck) => deck.next()),
      first: () => run((deck) => deck.slide(0, 0, 0)),
      last: () => run((deck) => deck.slide(Number.MAX_VALUE, 0, 0)),
      goTo: (index: number) => run((deck) => deck.slide(index, 0, 0)),
      toggleOverview: () => run((deck) => deck.toggleOverview()),
      toggleBlackout: () => run((deck) => deck.togglePause()),
    }),
    [run],
  );

  return {
    ...position,
    ready: !!api?.isReady(),
    api,
    attach,
    ...actions,
  };
}
