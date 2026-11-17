// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";

/**
 * Below this viewport width the Operations toolbars switch to their compact
 * layout: visibility pills show their icon only, a long segmented
 * filter collapses into a select and the cleanup action into an icon button,
 * so each toolbar holds on two rows at 1024 px with no fleet chosen.
 */
export const COMPACT_TOOLBAR_QUERY = "(max-width: 1439px)";

/** Whether a media query matches, kept live. `fallback` where `matchMedia` is missing (jsdom). */
export function useMediaQuery(query: string, fallback = false): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return fallback;
    return window.matchMedia(query).matches;
  });
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    if (typeof mql.addEventListener === "function") {
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    }
    mql.addListener(onChange);
    return () => mql.removeListener(onChange);
  }, [query]);
  return matches;
}

/** True when the Operations toolbars should use their compact layout. */
export function useCompactToolbar(): boolean {
  return useMediaQuery(COMPACT_TOOLBAR_QUERY, false);
}

/**
 * Below this viewport width the library list toolbars (Agents, Knowledge, Templates, Presentations) show their
 * visibility segments with icon and count only: at 1024 the search, the visibility segments and the
 * page's filter pills need about 900 px where 780 are free, so the row wrapped. One query for the four pages.
 */
export const LIST_COMPACT_TOOLBAR_QUERY = "(max-width: 1279px)";

/** True when a library list toolbar should draw its visibility segments icon-only. */
export function useCompactListToolbar(): boolean {
  return useMediaQuery(LIST_COMPACT_TOOLBAR_QUERY, false);
}
