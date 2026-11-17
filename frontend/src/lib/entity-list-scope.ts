// SPDX-License-Identifier: Apache-2.0

/**
 * The library set vs. every row the viewer may open.
 *
 * The entity list endpoints and the run feeds take `?scope=`: `visible` (the
 * default) is the library set - own, shared and company-managed rows - for
 * every viewer, admins included; `all` is every row the viewer may open by id,
 * the whole platform for an admin. The library pages and the pickers read the
 * default, the detail pages read `all` for what an entity links to. A list
 * hook sends `all` only for an admin (`useEntityListScope()`), so anyone else
 * shares one cached copy of their library set between the two.
 */

import type { EntityListScope } from "@/types";

/** The scope a list hook sends: `all` only for an admin who asked for it. */
export function effectiveListScope(
  requested: EntityListScope | undefined,
  isAdmin: boolean,
): "all" | undefined {
  return requested === "all" && isAdmin ? "all" : undefined;
}

/** The query key of a list read: the library key as is, the scope appended for `all`. */
export function entityListKey(base: readonly unknown[], scope: "all" | undefined): unknown[] {
  return scope ? [...base, { scope }] : [...base];
}

/** The request path of a list read: `scope=all` appended for `all`. */
export function entityListPath(path: string, scope: "all" | undefined): string {
  if (!scope) return path;
  return `${path}${path.includes("?") ? "&" : "?"}scope=${scope}`;
}

/**
 * A picker's options: the library set the viewer picks from, plus the rows the
 * entity is already bound to that only the admin scope holds. An admin editing
 * a colleague's agent sees, keeps and can unbind every binding, while what can
 * be added stays the library set. `undefined` while the options are not known:
 * the library set loading, or a bound row it lacks while the admin scope loads,
 * since a picker that clamps its selection to its options would drop that
 * binding. For anyone but an admin both lists are one cached query.
 */
export function withBoundEntities<T extends { id: string }>(
  visible: T[] | undefined,
  all: T[] | undefined,
  boundIds: readonly string[] | null | undefined,
): T[] | undefined {
  if (!visible) return undefined;
  const listed = new Set(visible.map((item) => item.id));
  const missing = new Set((boundIds ?? []).filter((id) => !!id && !listed.has(id)));
  if (!missing.size) return visible;
  if (!all) return undefined;
  const extra = all.filter((item) => missing.has(item.id));
  return extra.length ? [...visible, ...extra] : visible;
}
