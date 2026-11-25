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

/** The base URL of each connected module, as its platform registration reports it. */
export type ModuleBaseUrls = Partial<Record<PivotModule, string | null | undefined>>;

/** The fields of a platform registration (`GET /platform/registrations`) the pivots read. */
export interface ModuleRegistration {
  platform_identifier: string;
  platform_url?: string | null;
  status?: string;
}

/**
 * The base URLs read off the platform registrations (#1986): the active
 * registration of each module, the canonical identifier preferred over a legacy
 * one (openbas, opengrc) whatever the order, as the CTEM posture picks it. An
 * inactive or URL-less registration gives none. The registrations are a light
 * read the pages already hold, never the posture build.
 */
export function moduleBaseUrlsOf(registrations: readonly ModuleRegistration[] | null | undefined): ModuleBaseUrls {
  const out: ModuleBaseUrls = {};
  const canonical = new Set<PivotModule>();
  for (const registration of Array.isArray(registrations) ? registrations : []) {
    if (registration.status !== "active" || !registration.platform_url) continue;
    const module = pivotModuleOf(registration.platform_identifier);
    if (!module || canonical.has(module)) continue;
    out[module] = registration.platform_url;
    if (registration.platform_identifier.toLowerCase() === module) canonical.add(module);
  }
  return out;
}
