// SPDX-License-Identifier: Apache-2.0

/**
 * How many rows each visibility segment of a server-paginated list holds
 * under the list's other filters: what `<VisibilityFilter counts>`
 * shows on those pages, as `useEntityFilter` does for the lists filtered in
 * the browser.
 *
 * Every paginated list answers `GET <list>/segment-counts` with
 * `{mine, shared, company, all}` (`backend/app/services/visibility.py`), taking
 * the list's filters, never the segment. The query lives under the list's own
 * cache key (`root`), so whatever refreshes the list (a mutation, the activity
 * stream) refreshes its counts too; `undefined` while loading, so a segment
 * shows no number rather than a wrong 0.
 *
 * The counts are an optional side read: a failure never reaches the
 * page's error boundary (`throwOnError: false`) and answers `null`, so the
 * numbers stay hidden and the landing settles on its fallback instead of
 * waiting. When the URL names no segment the page is about to land from the
 * counts, so they are read again on mount and only counts fetched since the
 * mount are answered: the landing never follows cached numbers up to
 * `staleTime` old.
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "@/lib/api";
import { namedVisibility, type Visibility } from "@/lib/visibility";

type SegmentCounts = Record<Visibility, number>;

/** The counts of a list: `undefined` while loading, `null` when the read failed. */
type SegmentCountsResult = SegmentCounts | null | undefined;

type FilterValue = string | number | boolean | null | undefined;

interface SegmentCountsOptions {
  enabled?: boolean;
  /** The list's own polling, so the numbers never lag behind the rows. */
  refetchInterval?: number | false;
}

/** The query string of the filters: unset, empty and `false` values left out, keys sorted for a stable cache key. */
function segmentCountsQuery(filters: Readonly<Record<string, FilterValue>>): string {
  const params = new URLSearchParams();
  for (const key of Object.keys(filters).sort()) {
    const value = filters[key];
    if (value === undefined || value === null || value === "" || value === false) continue;
    params.set(key, String(value));
  }
  return params.toString();
}

export function useSegmentCounts(
  /** The list's cache key prefix (e.g. `["work-items"]`): its invalidations reach the counts. */
  root: readonly unknown[],
  /** The list's API path (e.g. `/work-items`); `/segment-counts` is appended. */
  listPath: string,
  filters: Readonly<Record<string, FilterValue>>,
  { enabled = true, refetchInterval }: SegmentCountsOptions = {},
): SegmentCountsResult {
  const qs = segmentCountsQuery(filters);
  const [searchParams] = useSearchParams();
  // No segment in the URL: the page lands from these counts (`useVisibilityParam`).
  const landing = namedVisibility(searchParams) === null;
  const { data, isError, isFetchedAfterMount } = useQuery<SegmentCounts>({
    queryKey: [...root, "segment-counts", listPath, qs],
    queryFn: () => api.get(`${listPath}/segment-counts${qs ? `?${qs}` : ""}`),
    enabled,
    refetchInterval,
    refetchOnMount: landing ? "always" : true,
    placeholderData: keepPreviousData,
    throwOnError: false,
  });
  if (!enabled) return undefined;
  if (isError) return null;
  if (landing && !isFetchedAfterMount) return undefined;
  return data;
}

/**
 * The counts of a page that lists two feeds together (Executions: assignment
 * runs and flow runs), segment by segment; `undefined` until every feed shown
 * has answered, `null` when one of them failed. A feed not shown is passed as
 * `false`.
 */
export function addSegmentCounts(...feeds: readonly (SegmentCountsResult | false)[]): SegmentCountsResult {
  const shown = feeds.filter((feed) => feed !== false);
  if (shown.some((feed) => feed === null)) return null;
  if (shown.length === 0 || shown.some((feed) => feed === undefined)) return undefined;
  const total: SegmentCounts = { mine: 0, shared: 0, company: 0, all: 0 };
  for (const feed of shown as SegmentCounts[]) {
    for (const key of Object.keys(total) as Visibility[]) total[key] += feed[key];
  }
  return total;
}
