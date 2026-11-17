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
 * The URL state of a library paged by the server: the Video
 * Studio's Videos and Media tabs, the presentation library.
 *
 * `useLibraryParams` keeps the search (sent to the server once the typing
 * settles), one filter and the page in the URL, so a link or a reload keeps
 * them; `useClampPage` goes back to the last page when the one shown empties.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useDebounce } from "@/hooks/useDebounce";

export const LIBRARY_PARAM_SEARCH = "q";
const LIBRARY_PARAM_PAGE = "page";

/** A value the URL holds when it is one of *choices*, else *fallback*. */
export function readChoice<F extends string>(value: string | null, choices: readonly F[], fallback: F): F {
  return value && choices.includes(value as F) ? (value as F) : fallback;
}

/**
 * A library's search, one filter and the page, kept in the URL. A new search
 * or filter starts again from the first page. A search the URL changes by
 * itself - a link to the same page, the browser's back - replaces what the box
 * holds: only what the person types is written back. `update` writes
 * any other parameter of the library the same way (`params` reads them).
 * The filter is optional: a library whose only filter is the visibility
 * segment reads it with `readVisibility(params)` and writes it with
 * `update(visibilityUpdate(value))` (`lib/visibility.ts`).
 */
export function useLibraryParams<F extends string = string>(
  filterParam = "",
  filters: readonly F[] = [],
  fallback: F = "" as F,
) {
  const [params, setParams] = useSearchParams();
  const urlSearch = params.get(LIBRARY_PARAM_SEARCH) ?? "";
  const [searchDraft, setSearchDraft] = useState(urlSearch);
  const search = useDebounce(searchDraft, 250).trim();
  /** The URL search and the settled search the box last agreed on. */
  const synced = useRef({ url: urlSearch, search });
  const filter = readChoice(filterParam ? params.get(filterParam) : null, filters, fallback);
  const page = Math.max(0, Math.floor(Number(params.get(LIBRARY_PARAM_PAGE))) || 0);

  const update = useCallback(
    (updates: Record<string, string | null>) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [key, value] of Object.entries(updates)) {
            if (value) next.set(key, value);
            else next.delete(key);
          }
          if (!(LIBRARY_PARAM_PAGE in updates)) next.delete(LIBRARY_PARAM_PAGE);
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
  const setParam = useCallback((key: string, value: string | null) => update({ [key]: value }), [update]);
  useEffect(() => {
    const last = synced.current;
    if (urlSearch !== last.url) {
      synced.current = { url: urlSearch, search: urlSearch.trim() };
      setSearchDraft(urlSearch);
      return;
    }
    if (search === last.search) return;
    synced.current = { url: search, search };
    if (search !== urlSearch) setParam(LIBRARY_PARAM_SEARCH, search || null);
  }, [search, urlSearch, setParam]);
  const setFilter = useCallback(
    (value: F) => setParam(filterParam, value === fallback ? null : value),
    [setParam, filterParam, fallback],
  );
  const setPage = useCallback(
    (value: number) => setParam(LIBRARY_PARAM_PAGE, value > 0 ? String(value) : null),
    [setParam],
  );

  return { searchDraft, setSearchDraft, search, filter, setFilter, page, setPage, params, update };
}

/** Back to the last page when the one shown emptied (its last row deleted, the library shrank). */
export function useClampPage(
  page: number,
  pageSize: number,
  total: number | undefined,
  setPage: (page: number) => void,
) {
  useEffect(() => {
    if (total !== undefined && page > 0 && page * pageSize >= total) setPage(Math.max(0, Math.ceil(total / pageSize) - 1));
  }, [page, pageSize, total, setPage]);
}
