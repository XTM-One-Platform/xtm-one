// SPDX-License-Identifier: Apache-2.0

/**
 * The "Open in <Module>" pivot (#1357): the address of an entity in the
 * connected module instance it came from, built from the module's base URL
 * and the entity's own id in that module, or `null` when either is unknown or
 * the base URL is not plain http(s). A pivot is never guessed: no id, no link.
 *
 * Routes, read from each module's own router:
 * - OpenCTI: `/dashboard/id/{id}`, the resolver that opens any STIX entity's page.
 * - OpenAEV: `/admin/findings/{id}`, `/admin/assets/{id}` (elements/ListNavigationHandler).
 * - OpenCRQ: no entity route is known yet, so no pivot is drawn.
 */
import { safeHttpUrl } from "./safe-url.ts";

export type PivotModule = "opencti" | "openaev" | "opencrq";

/** The kind of entity the pivot opens: an OpenCTI entity of any type, an OpenAEV finding or asset. */
export type PivotKind = "entity" | "finding" | "asset";

const ROUTES: Record<PivotModule, Partial<Record<PivotKind, (id: string) => string>>> = {
  opencti: { entity: (id) => `/dashboard/id/${id}` },
  openaev: { finding: (id) => `/admin/findings/${id}`, asset: (id) => `/admin/assets/${id}` },
  opencrq: {},
};

/** The module a source key names (the pre-rename identifiers included), or null for a non-module source. */
export function pivotModuleOf(source: string | null | undefined): PivotModule | null {
  switch ((source ?? "").toLowerCase()) {
    case "opencti":
      return "opencti";
    case "openaev":
    case "openbas":
      return "openaev";
    case "opencrq":
    case "opengrc":
      return "opencrq";
    default:
      return null;
  }
}

/** The pivot URL, or null when the base URL, the id or the route is missing. */
export function modulePivotUrl(
  module: PivotModule | null | undefined,
  baseUrl: string | null | undefined,
  id: string | null | undefined,
  kind: PivotKind = "entity",
): string | null {
  if (!module || !id || !id.trim()) return null;
  const route = ROUTES[module]?.[kind];
  const base = safeHttpUrl(baseUrl?.trim());
  if (!route || !base) return null;
  return `${base.replace(/\/+$/, "")}${route(encodeURIComponent(id.trim()))}`;
}

/** The base URL of each connected module, as the CTEM posture reports it. */
export type ModuleBaseUrls = Partial<Record<PivotModule, string | null | undefined>>;

/** The base URLs read off a posture's `products` (an absent module gives none). */
export function moduleBaseUrlsOf(
  products: Partial<Record<string, { present?: boolean; url?: string | null } | undefined>> | null | undefined,
): ModuleBaseUrls {
  const out: ModuleBaseUrls = {};
  for (const module of ["opencti", "openaev", "opencrq"] as const) {
    const product = products?.[module];
    if (product?.present && product.url) out[module] = product.url;
  }
  return out;
}
