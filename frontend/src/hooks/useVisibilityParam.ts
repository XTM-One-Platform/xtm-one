// SPDX-License-Identifier: Apache-2.0

/**
 * The visibility segment of a server-filtered list page, held in the URL
 * (dev:visibility-filters-consistency, `lib/visibility.ts`).
 *
 * Reads `?vis=` (the [DEPRECATED] `ownership`, `scope` and `visibility` read
 * once as aliases) and writes the choice back to `?vis=`, Mine included, with
 * every alias and the page number removed. When the URL names no segment, a
 * page passing `landing` (its `useSegmentCounts`) waits for the counts and
 * lands on the first segment holding a row (`resolveLanding`):
 * the third value is `true` until then, the page shows a skeleton and sends
 * no list request; the landed segment is written to `?vis=` (replace, the
 * page number kept). Counts that failed to load land on `fallback` with no
 * number shown; the counts are read again on mount whenever the URL
 * names no segment (`useSegmentCounts`), so the landing never follows cached
 * numbers. Without `landing` it is Mine at once, as before. The
 * pages that filter in the browser use `useEntityFilter`; the ones whose API
 * filters (Operations, Files, Background tasks) use this hook and send the
 * segment to the API as `vis=`.
 */

import { useCallback, useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import {
  DEFAULT_VISIBILITY,
  VISIBILITY_SEGMENTS,
  namedVisibility,
  resolveLanding,
  writeVisibility,
  type Visibility,
} from "@/lib/visibility";

/** The URL parameters a segment change resets besides the aliases (pagination). */
const RESET_ON_CHANGE: readonly string[] = ["page"];

/** The landing of a page whose API filters: its per-segment counts, and whether it lands at all. */
export interface VisibilityLanding {
  /** `useSegmentCounts` of the list, `undefined` while loading, `null` when the read failed. */
  counts: Partial<Record<Visibility, number>> | null | undefined;
  /** `false` on a list that opens on *fallback* (locked on a fleet or a workspace). */
  enabled?: boolean;
}

export function useVisibilityParam(
  offered: readonly Visibility[] = VISIBILITY_SEGMENTS,
  fallback: Visibility = DEFAULT_VISIBILITY,
  landing?: VisibilityLanding,
): [Visibility, (value: Visibility) => void, boolean] {
  const [searchParams, setSearchParams] = useSearchParams();
  const named = namedVisibility(searchParams, offered);
  const { visibility, pending, land } = resolveLanding(named, landing?.counts, {
    offered,
    fallback,
    enabled: landing !== undefined && landing.enabled !== false,
  });
  // The landed segment goes to the URL once, so a link or a reload reopens it
  // and a later search or filter never moves it.
  useEffect(() => {
    if (land) setSearchParams((prev) => writeVisibility(prev, visibility), { replace: true });
  }, [land, visibility, setSearchParams]);
  const setVisibility = useCallback(
    (value: Visibility) =>
      setSearchParams(
        (prev) => {
          const next = writeVisibility(prev, value);
          for (const key of RESET_ON_CHANGE) next.delete(key);
          return next;
        },
        { replace: true },
      ),
    [setSearchParams],
  );
  return [visibility, setVisibility, pending];
}

/** What the API is sent for a segment: nothing for All (every row the caller can read). */
export function visibilityQuery(value: Visibility): Visibility | undefined {
  return value === "all" ? undefined : value;
}
