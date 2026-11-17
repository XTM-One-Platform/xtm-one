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
 * The view of an agent graph page (layout, segment, filters, forces, Fleets
 * layer), kept in the URL query and in a per-user, per-page preference.
 *
 * The URL is the source of truth while it names the view; a page opened with
 * no view key in its URL shows the stored preference; every change writes
 * both. The rules and the (de)serialisation are in `lib/agent-graph-view-state`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "@/hooks/useAuth";
import {
  graphViewStorageKey,
  resolveGraphView,
  serializeView,
  writeViewToUrl,
  type GraphViewState,
} from "@/lib/agent-graph-view-state";

function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null; // storage disabled (private mode, policy): the URL alone holds the view
  }
}

// A slider drag changes the view on every move: the view follows at once, the
// URL and the preference are written once the moves pause, so a drag never
// routes the page on each frame.
const PERSIST_DELAY_MS = 150;

function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // quota or storage disabled: the URL still holds the view
  }
}

/**
 * `page` names the page's preference: "map" for the Agentic Flow map,
 * "fleet:<id>" for a fleet's Graph tab, so each keeps its own view.
 */
export function usePersistedGraphView(
  page: string,
  defaults: GraphViewState,
): [GraphViewState, (change: Partial<GraphViewState>) => void] {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const storageKey = graphViewStorageKey(user?.id, page);
  // Read once per page key: the preference only matters when the URL names
  // no view, and every change writes the URL before the preference.
  const stored = useRef<{ key: string; value: string | null } | null>(null);
  if (stored.current?.key !== storageKey) stored.current = { key: storageKey, value: readStored(storageKey) };
  const storedValue = stored.current.value;

  const resolved = useMemo(() => resolveGraphView(searchParams, storedValue, defaults), [searchParams, storedValue, defaults]);
  const search = searchParams.toString();
  const searchRef = useRef(search);
  searchRef.current = search;
  // The view changed but not yet shown by the URL. Keyed by page: switching
  // fleet within the delay never shows the previous fleet's view. Once written,
  // it is kept until the URL moves from the query it was written over: the
  // router may commit the new query after this hook's own state, and a render
  // in between would show the previous view, so a quick second click would
  // toggle that previous view instead of the one on screen.
  const [pendingEntry, setPending] = useState<{ key: string; view: GraphViewState; writtenOver?: string } | null>(null);
  const pending =
    pendingEntry?.key === storageKey && (pendingEntry.writtenOver === undefined || pendingEntry.writtenOver === search)
      ? pendingEntry.view
      : null;
  const view = pending ?? resolved;
  const viewRef = useRef(view);
  viewRef.current = view;
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const persist = useCallback(
    (next: GraphViewState) => {
      const value = serializeView(next);
      writeStored(storageKey, value);
      if (stored.current) stored.current.value = value;
      setSearchParams((prev) => writeViewToUrl(prev, next, defaults), { replace: true });
      setPending({ key: storageKey, view: next, writtenOver: searchRef.current });
    },
    [storageKey, setSearchParams, defaults],
  );

  const update = useCallback(
    (change: Partial<GraphViewState>) => {
      // A layout set through the toolbar is a choice: it wins on the next visit.
      const next = { ...viewRef.current, ...change, ...(change.layout !== undefined ? { layoutChosen: true } : {}) };
      viewRef.current = next;
      setPending({ key: storageKey, view: next });
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        timer.current = null;
        persist(next);
      }, PERSIST_DELAY_MS);
    },
    [persist, storageKey],
  );

  // Leaving the page within the delay still keeps the last change.
  const pendingRef = useRef(pendingEntry);
  pendingRef.current = pendingEntry;
  useEffect(
    () => () => {
      if (timer.current) {
        clearTimeout(timer.current);
        timer.current = null;
        // Under the key it was made on: a page switch keeps the previous page's change.
        if (pendingRef.current) writeStored(pendingRef.current.key, serializeView(pendingRef.current.view));
      }
    },
    [storageKey],
  );

  return [view, update];
}
