// SPDX-License-Identifier: Apache-2.0

/**
 * Shared hook for filtering entity lists by search text, tags, visibility,
 * and `has:` properties.
 *
 * Replaces the identical search + tag filtering pattern duplicated
 * across Agents, Integrations, KnowledgeBases, MCPServers, and CustomTools.
 *
 * Search text (`q`), selected tags (`tags`), visibility segment (`vis`), and
 * `has:` properties (`has`) are persisted in URL search params so that
 * navigating away and pressing back restores the filter state.
 *
 * The visibility segment follows the one convention of `lib/visibility.ts`:
 * Mine | Company | Shared | All, the [DEPRECATED] parameters read as aliases.
 * When the URL names no segment, the page lands on the first one holding an
 * item (`landingVisibility`: Mine, else Company, else Shared, else All) once
 * the items are loaded, and writes it to `?vis=` so a link reproduces it.
 */

import { useState, useMemo, useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { sortByLabel } from "@/lib/select-options";
import {
  DEFAULT_VISIBILITY,
  countSegments,
  inSegment,
  landingVisibility,
  namedVisibility,
  writeVisibility,
  type Visibility,
} from "@/lib/visibility";
import {
  builtinLanding,
  companySplit,
  defaultBuiltinChoice,
  foldBuiltins,
  readBuiltinParam,
  readStoredBuiltin,
  storeBuiltin,
  writeBuiltinParam,
  type BuiltinChoice,
} from "@/lib/builtin-fold";

/** The visibility segment of a list page (`lib/visibility.ts`). */
type VisibilityFilter = Visibility;

/** Minimum shape required for filtering. */
interface Filterable {
  name: string;
  description?: string | null;
  tags?: string[] | Record<string, string> | null;
  is_company_managed: boolean;
  user_id: string | null;
  shared_group_ids?: string[] | null;
  shared_user_ids?: string[] | null;
}

/**
 * A filterable `has:` property — a named predicate over an item.
 *
 * Adding a property is purely additive: append a definition to the array
 * passed as `properties`, and both the filter checklist and the filtering
 * logic pick it up.
 */
export interface EntityProperty<T> {
  /** Token used in the `has:` filter and the `has` URL param (e.g. "web-exposure"). */
  key: string;
  /** True when the item has this property. */
  matches: (item: T) => boolean;
}

interface UseEntityFilterOptions<T> {
  /** Raw items from the query (may be undefined while loading). */
  items: T[] | undefined;

  /** Current user ID — required for visibility filtering. */
  userId?: string;

  /**
   * Extra fields to include in the text search (beyond `name`).
   * Return an array of string|null|undefined values to search through.
   * Defaults to searching `description` if the item has one.
   */
  searchFields?: (item: T) => (string | null | undefined)[];

  /**
   * Filterable `has:` properties. Selected properties are ANDed together —
   * an item must have every checked property to pass. Define the array at
   * module scope (or memoize it) so it keeps a stable identity across renders.
   */
  properties?: EntityProperty<T>[];

  /**
   * URL query parameters every filter change removes in the same navigation,
   * e.g. `["page"]` for a paged list that restarts on its first page. A second
   * setSearchParams call in the same event would start from the same query and
   * drop the filter. Define the array at module scope.
   */
  resetParams?: readonly string[];

  /**
   * The segment shown when the URL names none, instead of the landing rule
   * (`landingVisibility`). Unset on every list page; a page passes one only
   * with a product decision.
   */
  defaultVisibility?: VisibilityFilter;

  /**
   * `false` on a page that draws no visibility control (Datasets, Evaluations,
   * Variables): every row is shown and a `?vis=` or [DEPRECATED] alias in the
   * URL is ignored, since nothing on the page could undo it.
   */
  withVisibility?: boolean;

  /**
   * The built-in fold of a list mixing the platform's seeded catalogue with the
   * organisation's own items (`lib/builtin-fold.ts`): `list` names the stored
   * per-user choice, `isBuiltin` reads the API's flag (never a tag). Given, the
   * landing prefers Company when it holds company items of the organisation's
   * own, and Company hides its built-ins until the reader shows them. Define it
   * at module scope.
   */
  builtin?: { list: string; isBuiltin: (item: T) => boolean };
}

/** The built-in fold's state (`builtin` option given). */
export interface BuiltinFoldState {
  /** Built-ins shown or hidden on Company. */
  choice: BuiltinChoice;
  setChoice: (choice: BuiltinChoice) => void;
  /** Built-in rows the fold takes out of the rows shown (0 off Company or when shown). */
  hidden: number;
  /** Built-in company rows under the search, tags and properties: the Show/Hide row draws when > 0. */
  builtinInCompany: number;
  /** The search matches built-ins only: they are listed with a hint. */
  revealedBySearch: boolean;
}

interface UseEntityFilterResult<T> {
  /** Search input value. */
  search: string;
  /** Setter for search input. */
  setSearch: (v: string) => void;
  /** Currently selected tag filters. */
  selectedTags: string[];
  /** Setter for tag filters. */
  setSelectedTags: (v: string[]) => void;
  /** Active visibility filter. */
  visibilityFilter: VisibilityFilter;
  /** Setter for visibility filter. */
  setVisibilityFilter: (v: VisibilityFilter) => void;
  /** How many items each segment holds, under the search, tag and property filters. */
  visibilityCounts: Record<VisibilityFilter, number>;
  /**
   * The segment the page landed on with no segment in its URL (the landing
   * rule, or `defaultVisibility`): what "clear filters" goes back to.
   */
  landingVisibilityFilter: VisibilityFilter;
  /**
   * Clears the search, tags and `has:` properties and goes back to the landing
   * segment, in ONE URL write: separate setters called in the same event each
   * start from the same query and the last one wins. `alsoClear`
   * applies the page's own filter keys to that same write. `keepScope` keeps
   * the segment shown: on a records table the scope switcher is not a filter,
   * so clearing never lowers the total (standard v2, UX round 1 on #10920, B2).
   */
  clearAll: (alsoClear?: (next: URLSearchParams) => URLSearchParams, options?: { keepScope?: boolean }) => void;
  /**
   * Writes the page's own URL parameters (its extra filters, its page) from the
   * same latest query the hook's setters use, so a page write in the same tick
   * as a filter write never resolves against the previous render and undoes it.
   * `reset` names the parameters this write drops;
   * none by default.
   */
  writeParams: (update: (prev: URLSearchParams) => URLSearchParams, reset?: readonly string[]) => void;
  /** Currently checked `has:` property keys. */
  selectedProperties: string[];
  /** Setter for the `has:` property filters. */
  setSelectedProperties: (v: string[]) => void;
  /** All items sorted by name. */
  allItems: T[];
  /** All unique tags extracted from items, sorted. */
  allTags: string[];
  /** Items that pass search, tag, visibility, and property filters. */
  filtered: T[];
  /** The built-in fold, when the `builtin` option is given. */
  builtinFold: BuiltinFoldState | null;
}

export function useEntityFilter<T extends Filterable>({
  items,
  userId,
  searchFields,
  properties,
  resetParams,
  defaultVisibility,
  withVisibility = true,
  builtin,
}: UseEntityFilterOptions<T>): UseEntityFilterResult<T> {
  const [searchParams, setURLSearchParams] = useSearchParams();
  // The query the last filter write asked for. react-router hands a functional
  // update the params of the last render, so two writes before the next render
  // (a segment click, then a keystroke) would each start from the same stale
  // query and the second would undo the first. Every write starts from
  // this copy instead; `pending` holds the queries written and not yet rendered.
  const latest = useRef({ params: searchParams, pending: [] as string[] });
  useLayoutEffect(() => {
    const state = latest.current;
    const at = state.pending.indexOf(searchParams.toString());
    // Not one of ours (back, a link): the URL wins and nothing is pending.
    state.pending = at === -1 ? [] : state.pending.slice(at + 1);
    if (state.pending.length === 0) state.params = searchParams;
  }, [searchParams]);
  const writeLatest = useCallback(
    (update: (prev: URLSearchParams) => URLSearchParams, reset: readonly string[]) => {
      const state = latest.current;
      const next = update(new URLSearchParams(state.params));
      for (const key of reset) next.delete(key);
      if (next.toString() !== state.params.toString()) state.pending.push(next.toString());
      state.params = next;
      setURLSearchParams(next, { replace: true });
    },
    [setURLSearchParams],
  );
  // Every filter write also drops the caller's reset parameters, in one navigation.
  const setSearchParams = useCallback(
    (update: (prev: URLSearchParams) => URLSearchParams) => writeLatest(update, resetParams ?? []),
    [writeLatest, resetParams],
  );

  const [search, setSearchState] = useState(() => searchParams.get("q") ?? "");
  const [selectedTags, setSelectedTagsState] = useState<string[]>(() => {
    const raw = searchParams.get("tags");
    return raw ? raw.split(",").filter(Boolean) : [];
  });
  // A page with no visibility control shows every row, whatever an old link
  // says. Otherwise the segment the URL names, or null: the page lands on the
  // first segment holding an item once the items are loaded (below).
  const [chosenVisibility, setVisibilityFilterState] = useState<VisibilityFilter | null>(() =>
    withVisibility ? namedVisibility(searchParams) ?? defaultVisibility ?? null : "all",
  );
  // The landing segment, decided once on the first loaded list, so a later
  // search or refetch never moves the page to another segment.
  const [landed, setLanded] = useState<VisibilityFilter | null>(defaultVisibility ?? null);
  const [selectedProperties, setSelectedPropertiesState] = useState<string[]>(() => {
    const raw = searchParams.get("has");
    return raw ? raw.split(",").filter(Boolean) : [];
  });
  // The built-in choice the URL or the reader named; `null` = the stored one, else the default.
  const [namedBuiltin, setNamedBuiltin] = useState<BuiltinChoice | null>(() =>
    builtin ? readBuiltinParam(searchParams) : null,
  );

  const setSearch = useCallback(
    (v: string) => {
      setSearchState(v);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (v) next.set("q", v);
          else next.delete("q");
          return next;
        },
      );
    },
    [setSearchParams],
  );

  const setSelectedTags = useCallback(
    (v: string[]) => {
      setSelectedTagsState(v);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (v.length > 0) next.set("tags", v.join(","));
          else next.delete("tags");
          return next;
        },
      );
    },
    [setSearchParams],
  );

  const setVisibilityFilter = useCallback(
    (v: VisibilityFilter) => {
      setVisibilityFilterState(v);
      // Always written, Mine included: the link names the segment it shows.
      setSearchParams((prev) => writeVisibility(prev, v));
    },
    [setSearchParams],
  );

  const setSelectedProperties = useCallback(
    (v: string[]) => {
      setSelectedPropertiesState(v);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (v.length > 0) next.set("has", v.join(","));
          else next.delete("has");
          return next;
        },
      );
    },
    [setSearchParams],
  );

  const allItems = useMemo(() => sortByLabel(items ?? [], (item) => item.name), [items]);

  const allTags = useMemo(() => {
    const set = new Set<string>();
    for (const item of allItems) {
      if (Array.isArray(item.tags)) {
        for (const tag of item.tags) set.add(tag);
      }
    }
    return sortByLabel([...set], (tag) => tag);
  }, [allItems]);

  // Resolve the checked keys to their predicates once per change. Keys with no
  // matching definition (a stale URL, a renamed property) are ignored rather
  // than emptying the list.
  const activePredicates = useMemo(() => {
    if (selectedProperties.length === 0 || !properties) return [];
    return properties.filter((p) => selectedProperties.includes(p.key)).map((p) => p.matches);
  }, [properties, selectedProperties]);

  // The rows the search, tags and properties keep, before the visibility segment.
  const matching = useMemo(() => {
    const q = search.toLowerCase();
    return allItems.filter((item) => {
      // Text search
      if (q) {
        const nameMatch = item.name.toLowerCase().includes(q);
        const extraFields = searchFields
          ? searchFields(item)
          : [item.description];
        const extraMatch = extraFields.some((f) => f?.toLowerCase().includes(q));
        if (!nameMatch && !extraMatch) return false;
      }
      // Tag filter (categorization tags are string[]; cascading key-value
      // tag maps are not selectable here)
      if (
        selectedTags.length > 0 &&
        !selectedTags.some((t) => (Array.isArray(item.tags) ? item.tags : []).includes(t))
      ) {
        return false;
      }
      // `has:` property filters — every checked property must match.
      if (!activePredicates.every((matches) => matches(item))) return false;
      return true;
    });
  }, [allItems, search, selectedTags, searchFields, activePredicates]);

  // Counted under the other filters: an empty Mine says "look elsewhere" only
  // when the segment alone empties the list, never for a search matching nothing.
  const visibilityCounts = useMemo(() => countSegments(matching, userId), [matching, userId]);

  // The landing rule reads the segments of the whole list, before the search,
  // tag and `has:` filters: a link opened with `?q=` matching nothing would
  // otherwise land on All, and "clear filters" go back there.
  const landingCounts = useMemo(() => countSegments(allItems, userId), [allItems, userId]);

  // The built-in fold: the organisation's own company items decide the landing
  // and the default choice; the URL, then the stored choice, win over it.
  const isBuiltin = builtin?.isBuiltin;
  const builtinList = builtin?.list;
  const wholeSplit = useMemo(
    () => (isBuiltin ? companySplit(allItems, userId, isBuiltin) : { custom: 0, builtin: 0 }),
    [allItems, userId, isBuiltin],
  );
  const storedBuiltin = useMemo(
    () => (builtinList ? readStoredBuiltin(builtinList, userId) : null),
    [builtinList, userId],
  );
  const builtinChoice: BuiltinChoice = namedBuiltin ?? storedBuiltin ?? defaultBuiltinChoice(wholeSplit.custom);

  // Mine | Company | Shared | All: the first holding an item, as soon as the
  // list is loaded (with the fold: Mine, then the organisation's own Company,
  // then Company with its built-ins, then Shared and All; `builtinLanding`); Mine
  // before that (the pages draw their skeleton while loading, so no empty Mine flashes).
  const landingFilter: VisibilityFilter =
    landed ??
    (items !== undefined && userId !== undefined
      ? builtin
        ? builtinLanding(landingCounts, wholeSplit)
        : landingVisibility(landingCounts)
      : DEFAULT_VISIBILITY);
  const visibilityFilter: VisibilityFilter = chosenVisibility ?? landingFilter;
  const segmentRows = useMemo(
    () => matching.filter((item) => inSegment(item, visibilityFilter, userId)),
    [matching, visibilityFilter, userId],
  );
  const fold = useMemo(
    () =>
      isBuiltin
        ? foldBuiltins(segmentRows, visibilityFilter, builtinChoice, isBuiltin, !!search)
        : { shown: segmentRows, hidden: 0, revealedBySearch: false },
    [segmentRows, visibilityFilter, builtinChoice, isBuiltin, search],
  );
  const filtered = fold.shown;
  // Built-in company rows under the search, tags and properties: what Company's count leaves out while hidden.
  const matchingSplit = useMemo(
    () => (isBuiltin ? companySplit(matching, userId, isBuiltin) : { custom: 0, builtin: 0 }),
    [matching, userId, isBuiltin],
  );
  // A search that matches built-ins only shows them (revealedBySearch): Company then counts the rows shown.
  const foldedCounts = useMemo(
    () =>
      isBuiltin && builtinChoice === "hide" && !fold.revealedBySearch
        ? { ...visibilityCounts, company: visibilityCounts.company - matchingSplit.builtin }
        : visibilityCounts,
    [visibilityCounts, isBuiltin, builtinChoice, matchingSplit.builtin, fold.revealedBySearch],
  );
  const setBuiltinChoice = useCallback(
    (choice: BuiltinChoice) => {
      setNamedBuiltin(choice);
      if (builtinList) storeBuiltin(builtinList, userId, choice);
      setSearchParams((prev) => writeBuiltinParam(prev, choice));
    },
    [builtinList, userId, setSearchParams],
  );
  const builtinFold: BuiltinFoldState | null = builtin
    ? {
        choice: builtinChoice,
        setChoice: setBuiltinChoice,
        hidden: fold.hidden,
        builtinInCompany: matchingSplit.builtin,
        revealedBySearch: fold.revealedBySearch,
      }
    : null;

  // Once landed, the segment is fixed and written to `?vis=`, so the URL names
  // the view shown and a copied link reproduces it. Written without the
  // caller's reset parameters: landing is not a filter change, a `?page=` of
  // the link is kept. Not before the user is known: every row would count as
  // someone else's.
  const landNow = withVisibility && landed === null && items !== undefined && userId !== undefined;
  const clearAll = useCallback(
    (alsoClear?: (next: URLSearchParams) => URLSearchParams, options?: { keepScope?: boolean }) => {
      const resetScope = withVisibility && !options?.keepScope;
      setSearchState("");
      setSelectedTagsState([]);
      setSelectedPropertiesState([]);
      if (resetScope) setVisibilityFilterState(landingFilter);
      setSearchParams(
        (prev) => {
          let next = new URLSearchParams(prev);
          for (const key of ["q", "tags", "has"]) next.delete(key);
          if (resetScope) next = writeVisibility(next, landingFilter);
          return alsoClear ? alsoClear(next) : next;
        },
      );
    },
    [setSearchParams, withVisibility, landingFilter],
  );

  const writeParams = useCallback(
    (update: (prev: URLSearchParams) => URLSearchParams, reset: readonly string[] = []) => writeLatest(update, reset),
    [writeLatest],
  );

  useEffect(() => {
    if (!landNow) return;
    setLanded(landingFilter);
    if (chosenVisibility === null) {
      setVisibilityFilterState(landingFilter);
      writeLatest((prev) => writeVisibility(prev, landingFilter), []);
    }
  }, [landNow, landingFilter, chosenVisibility, writeLatest]);

  return {
    search, setSearch,
    selectedTags, setSelectedTags,
    visibilityFilter, setVisibilityFilter, visibilityCounts: foldedCounts,
    landingVisibilityFilter: landingFilter,
    clearAll,
    writeParams,
    selectedProperties, setSelectedProperties,
    allItems, allTags, filtered,
    builtinFold,
  };
}
