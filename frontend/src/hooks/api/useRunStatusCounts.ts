// SPDX-License-Identifier: Apache-2.0

/**
 * How many runs each status holds under the Executions feed's other filters:
 * the numbers on the Status pill of Operations > Executions (#9378 slice 8f).
 *
 * Both run feeds answer `GET <feed>/status-counts` with `{counts: {status: n}}`
 * (the flow feed already keyed in the Executions vocabulary), taking the feed's
 * filters but never `status`. The query lives under the feed's own cache key,
 * so what refreshes the list refreshes the counts. A side read: a failure
 * never reaches the error boundary and only hides the numbers.
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export type RunStatusCounts = Record<string, number>;

type FilterValue = string | number | boolean | null | undefined;

interface Options {
  enabled?: boolean;
  refetchInterval?: number | false;
}

/** The query string of the filters: unset, empty and `false` values left out, keys sorted. */
function statusCountsQuery(filters: Readonly<Record<string, FilterValue>>): string {
  const params = new URLSearchParams();
  for (const key of Object.keys(filters).sort()) {
    const value = filters[key];
    if (value === undefined || value === null || value === "" || value === false) continue;
    params.set(key, String(value));
  }
  return params.toString();
}

export function useRunStatusCounts(
  root: readonly unknown[],
  /** The feed's API path (`/assignments/runs`, `/flows/runs`); `/status-counts` is appended. */
  feedPath: string,
  filters: Readonly<Record<string, FilterValue>>,
  { enabled = true, refetchInterval }: Options = {},
): RunStatusCounts | undefined {
  const qs = statusCountsQuery(filters);
  const { data } = useQuery<{ counts: RunStatusCounts }>({
    queryKey: [...root, "status-counts", feedPath, qs],
    queryFn: () => api.get(`${feedPath}/status-counts${qs ? `?${qs}` : ""}`),
    enabled,
    refetchInterval,
    placeholderData: keepPreviousData,
    throwOnError: false,
  });
  return enabled ? data?.counts : undefined;
}

/** The counts of the feeds shown, added status by status; `undefined` until one answered. */
export function addRunStatusCounts(...feeds: readonly (RunStatusCounts | undefined | false)[]): RunStatusCounts | undefined {
  const shown = feeds.filter((feed): feed is RunStatusCounts => !!feed);
  if (shown.length === 0) return undefined;
  const total: RunStatusCounts = {};
  for (const feed of shown) {
    for (const [status, n] of Object.entries(feed)) total[status] = (total[status] ?? 0) + n;
  }
  return total;
}
