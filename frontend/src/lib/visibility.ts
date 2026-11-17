// SPDX-License-Identifier: Apache-2.0

/**
 * The one visibility convention of every list page (dev:visibility-filters-consistency).
 *
 * Segments, in this order and with this wording: Mine | Company | Shared | All.
 *
 * - `mine`: owned by the signed-in user, not published to the company;
 * - `company`: company-managed, published to the whole company;
 * - `shared`: shared with the user directly or through a group, neither the
 *   user's own nor company-managed;
 * - `all`: the union of the three.
 *
 * A list page opened with no segment in its URL lands on the first segment
 * that holds items, in that order (`landingVisibility`): Mine, else Company,
 * else Shared, else All. It keeps the segment in `?vis=` so a link reopens the
 * same view. The parameters the
 * pages used before are still read, once, as aliases: they are [DEPRECATED]
 * and rewritten to `?vis=` on the next change.
 *
 * Pure module (no React, no DOM): unit-tested on `node --test`.
 */

export type Visibility = "mine" | "shared" | "company" | "all";

/** The segments in display order. A page hides one it cannot hold; it never reorders or renames them. */
export const VISIBILITY_SEGMENTS: readonly Visibility[] = ["mine", "company", "shared", "all"];

/**
 * The segment a page shows when the URL names none and it has no counts to
 * land with (a list whose API filters, before its counts are known).
 */
export const DEFAULT_VISIBILITY: Visibility = "mine";

/** The URL parameter holding the segment. */
const VISIBILITY_PARAM = "vis";

/**
 * [DEPRECATED] parameters read as aliases of `?vis=` when it is absent:
 * `visibility` (the Videos tab), `ownership` (Operations, Files), `owner`
 * (Agents, Prompts, Skills, Knowledge, Files), `scope` (Background tasks).
 * Never written; removed from the URL on the next change.
 */
const DEPRECATED_VISIBILITY_PARAMS: readonly string[] = ["visibility", "ownership", "owner", "scope"];

/** The [DEPRECATED] parameters whose `shared` meant "not mine" (see `LEGACY_OWNERSHIP_UNION`). */
const LEGACY_UNION_PARAMS: readonly string[] = ["ownership", "owner"];

/** [DEPRECATED] values the aliases carried, mapped onto the segments. */
const ALIAS_VALUES: Readonly<Record<string, Visibility>> = {
  company_managed: "company",
  own: "mine",
  everyone: "all",
};

/**
 * [DEPRECATED] `?ownership=` and `?owner=` values whose old meaning no segment
 * holds alone. On Operations, `shared` and its alias `company_managed` meant
 * "not mine": shared AND company-managed rows; `?owner=shared` ("Shared with
 * me") meant the same on the library pages and Files. Shared and Company are now split, so an old
 * link opens on All, which holds every row it showed, rather than on one
 * segment that would hide the other half.
 */
const LEGACY_OWNERSHIP_UNION: Readonly<Record<string, Visibility>> = {
  shared: "all",
  company_managed: "all",
};

/** The segment a raw value names, or `null` when it names none. */
export function toVisibility(raw: string | null | undefined): Visibility | null {
  if (!raw) return null;
  const value = raw.trim().toLowerCase();
  if ((VISIBILITY_SEGMENTS as readonly string[]).includes(value)) return value as Visibility;
  return ALIAS_VALUES[value] ?? null;
}

/**
 * The segment a URL names: `?vis=` first, then the [DEPRECATED] aliases; `null`
 * when it names none, names an unknown value, or names a segment the page
 * does not offer. A named segment always wins over the landing rule.
 */
export function namedVisibility(
  params: URLSearchParams,
  offered: readonly Visibility[] = VISIBILITY_SEGMENTS,
): Visibility | null {
  for (const key of [VISIBILITY_PARAM, ...DEPRECATED_VISIBILITY_PARAMS]) {
    const raw = params.get(key)?.trim().toLowerCase();
    const value = (LEGACY_UNION_PARAMS.includes(key) && raw ? LEGACY_OWNERSHIP_UNION[raw] : undefined) ?? toVisibility(raw);
    if (value) return offered.includes(value) ? value : null;
  }
  return null;
}

/**
 * The segment a URL asks for (`namedVisibility`), else `fallback` (Mine).
 */
export function readVisibility(
  params: URLSearchParams,
  offered: readonly Visibility[] = VISIBILITY_SEGMENTS,
  fallback: Visibility = DEFAULT_VISIBILITY,
): Visibility {
  return namedVisibility(params, offered) ?? fallback;
}

/**
 * The segment a page lands on when nothing names one: the first of Mine,
 * Company, Shared (display order) the page offers that holds an item, else
 * All (the page's own empty state). A count the page does not know is read
 * as empty. A page that does not offer All lands on its first segment.
 */
export function landingVisibility(
  counts: Partial<Record<Visibility, number>>,
  offered: readonly Visibility[] = VISIBILITY_SEGMENTS,
): Visibility {
  const segments = VISIBILITY_SEGMENTS.filter((segment) => offered.includes(segment));
  const holding = segments.find((segment) => segment !== "all" && (counts[segment] ?? 0) > 0);
  if (holding) return holding;
  return segments.includes("all") ? "all" : (segments[0] ?? "all");
}

/** Where a server-filtered list stands on its segment (`resolveLanding`). */
export interface LandingState {
  /** The segment to show and to send to the API. */
  visibility: Visibility;
  /** The URL names no segment and the counts are not in yet: show a skeleton, send no list request. */
  pending: boolean;
  /** The segment was just landed on: write it to `?vis=` (replace). */
  land: boolean;
}

/**
 * The segment of a list whose API filters: the one the URL
 * names (*named*, an empty Mine included) always wins; otherwise the page
 * waits for its per-segment counts (`GET <list>/segment-counts`) and lands
 * with `landingVisibility`, so an empty Mine never shows first. A page that
 * does not land (*enabled* false: a list locked on a fleet or a workspace)
 * shows *fallback* at once, as before. Counts that failed to load (`null`,
 * an optional side read) land on *fallback*, so the page never waits
 * on them nor shows an error in their place.
 */
export function resolveLanding(
  named: Visibility | null,
  counts: Partial<Record<Visibility, number>> | null | undefined,
  {
    offered = VISIBILITY_SEGMENTS,
    fallback = DEFAULT_VISIBILITY,
    enabled = true,
  }: { offered?: readonly Visibility[]; fallback?: Visibility; enabled?: boolean } = {},
): LandingState {
  if (named) return { visibility: named, pending: false, land: false };
  if (!enabled) return { visibility: fallback, pending: false, land: false };
  if (counts === null) return { visibility: fallback, pending: false, land: true };
  if (!counts) return { visibility: fallback, pending: true, land: false };
  return { visibility: landingVisibility(counts, offered), pending: false, land: true };
}

/** *params* with *value* written to `?vis=` and every [DEPRECATED] alias removed. */
export function writeVisibility(params: URLSearchParams, value: Visibility): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of DEPRECATED_VISIBILITY_PARAMS) next.delete(key);
  next.set(VISIBILITY_PARAM, value);
  return next;
}

/**
 * The URL update that selects *value*: `?vis=` written (Mine included, so a
 * link names the segment it shows) and every [DEPRECATED] alias removed. For
 * a hook that writes a `Record<string, string | null>` (`useLibraryParams`).
 */
export function visibilityUpdate(value: Visibility): Record<string, string | null> {
  const updates: Record<string, string | null> = { [VISIBILITY_PARAM]: value };
  for (const key of DEPRECATED_VISIBILITY_PARAMS) updates[key] = null;
  return updates;
}

/** The ownership fields every shareable entity carries. */
export interface VisibilityFields {
  user_id: string | null;
  is_company_managed: boolean;
  /** The groups it is shared with; `undefined` when the list does not serve them. */
  shared_group_ids?: string[] | null;
  /** The users it is shared with directly; `undefined` when the list does not serve them. */
  shared_user_ids?: string[] | null;
}

/**
 * The segment an item belongs to for *userId*, or `null` for a row in none of
 * them (an admin's list can hold a stranger's private row: it is in All only).
 * A row the list serves without share lists and that is neither the user's
 * nor the company's is read as shared: the server only lists it because it is.
 * A row served with both share lists empty is in none (a direct share counts).
 */
export function segmentOf(item: VisibilityFields, userId: string | undefined): Exclude<Visibility, "all"> | null {
  if (item.is_company_managed) return "company";
  if (userId && item.user_id === userId) return "mine";
  if (
    (item.shared_group_ids !== undefined || item.shared_user_ids !== undefined) &&
    !item.shared_group_ids?.length &&
    !item.shared_user_ids?.length
  ) {
    return null;
  }
  return "shared";
}

/** Whether *item* is in *segment* for *userId*. */
export function inSegment(item: VisibilityFields, segment: Visibility, userId: string | undefined): boolean {
  return segment === "all" || segmentOf(item, userId) === segment;
}

/** How many of *items* each segment holds (All counts every item). */
export function countSegments(
  items: readonly VisibilityFields[],
  userId: string | undefined,
): Record<Visibility, number> {
  const counts: Record<Visibility, number> = { mine: 0, company: 0, shared: 0, all: items.length };
  for (const item of items) {
    const segment = segmentOf(item, userId);
    if (segment) counts[segment] += 1;
  }
  return counts;
}
