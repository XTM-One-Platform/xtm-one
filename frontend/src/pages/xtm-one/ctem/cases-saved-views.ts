// SPDX-License-Identifier: Apache-2.0

/**
 * Saved views of the CTEM Cases list (#1176, dev-docs/ctem-cases.md section 7.2):
 * a name for the list as the URL holds it (the state tab `?state=` and the
 * table parameters `?c_*`: filters, search, sort, page size, grouping; never
 * the page number). Pure: the
 * pill reads and writes them in the browser's storage.
 */

/** One saved view: its name and the query string it opens. */
export type CaseSavedView = { id: string; name: string; query: string };

/** Where the views are kept, per browser. */
export const CASE_SAVED_VIEWS_STORAGE_KEY = "xtm.ctem-cases.views";

/** At most this many views; saving one more drops the oldest. */
export const CASE_SAVED_VIEWS_MAX = 20;

/** The longest name kept. */
const CASE_SAVED_VIEW_NAME_MAX = 60;

/** The parameters a view keeps: the state tab and the table's own (`c_`), the page number aside. */
function keptParam(key: string): boolean {
  return key === "state" || (key.startsWith("c_") && key !== "c_page");
}

/** The parameters a view replaces when it opens: its own, and the page number (back to page 1). */
function listParam(key: string): boolean {
  return keptParam(key) || key === "c_page";
}

/** The query of the list as shown, kept parameters only, in a stable order. */
export function caseViewQuery(params: URLSearchParams): string {
  const kept = [...params.entries()].filter(([key]) => keptParam(key));
  kept.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return new URLSearchParams(kept).toString();
}

/** The URL parameters a view opens: the current ones without the list's, then the view's. */
export function applyCaseView(current: URLSearchParams, query: string): URLSearchParams {
  const next = new URLSearchParams([...current.entries()].filter(([key]) => !listParam(key)));
  for (const [key, value] of new URLSearchParams(query)) next.append(key, value);
  return next;
}

/** The stored views read back; anything malformed is dropped, never thrown. */
export function readCaseSavedViews(raw: string | null): CaseSavedView[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (v): v is CaseSavedView =>
          typeof v === "object" &&
          v !== null &&
          typeof (v as CaseSavedView).id === "string" &&
          typeof (v as CaseSavedView).name === "string" &&
          (v as CaseSavedView).name.trim() !== "" &&
          typeof (v as CaseSavedView).query === "string",
      )
      .slice(-CASE_SAVED_VIEWS_MAX);
  } catch {
    return [];
  }
}

/**
 * Save a view. One query, one view: a view that already opens this query is
 * renamed in place (so the pill always names the view the user opened), and
 * another view holding the same name goes. A name already used (any case) by
 * another query takes the new query in place. Past the cap the oldest goes.
 */
export function addCaseSavedView(views: readonly CaseSavedView[], name: string, query: string, id: string): CaseSavedView[] {
  const trimmed = name.trim().slice(0, CASE_SAVED_VIEW_NAME_MAX);
  if (!trimmed) return [...views];
  const sameName = (v: CaseSavedView) => v.name.toLowerCase() === trimmed.toLowerCase();
  const sameQuery = views.findIndex((v) => v.query === query);
  if (sameQuery >= 0) {
    return views
      .filter((v, i) => i === sameQuery || !sameName(v))
      .map((v) => (v.id === views[sameQuery].id ? { ...v, name: trimmed } : v));
  }
  const same = views.findIndex(sameName);
  if (same >= 0) return views.map((v, i) => (i === same ? { ...v, name: trimmed, query } : v));
  return [...views, { id, name: trimmed, query }].slice(-CASE_SAVED_VIEWS_MAX);
}

/** Delete one view by id. */
export function removeCaseSavedView(views: readonly CaseSavedView[], id: string): CaseSavedView[] {
  return views.filter((v) => v.id !== id);
}

/** The view the list shows now, if one matches its query exactly. */
export function activeCaseView(views: readonly CaseSavedView[], query: string): CaseSavedView | undefined {
  return views.find((v) => v.query === query);
}
