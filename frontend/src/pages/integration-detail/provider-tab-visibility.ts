// SPDX-License-Identifier: Apache-2.0

/**
 * Who is shown a provider-contributed tab (`provider-tabs.tsx`). Each rule
 * mirrors the guard of the routes the tab calls:
 * a tab whose every call is refused would open on "Failed to load this
 * section".
 *
 * No runtime imports outside this folder's tree: `yarn test:unit` loads this
 * file under plain `node --test`, which does not resolve the `@/` alias.
 */
import type { Integration } from "@/types";
import { canManageEntity } from "../../lib/entity-permissions.ts";

/** Who is looking, for the tabs that are not for everyone who sees the integration. */
export interface ProviderTabViewer {
  userId: string | undefined;
  isAdmin: boolean;
}

export type ProviderTabVisibility = (integration: Integration, viewer: ProviderTabViewer) => boolean;

/** Platform admins only — the tab's routes answer anyone else "Admins only." (403). */
export const adminsOnly: ProviderTabVisibility = (_integration, viewer) => viewer.isAdmin;

/** The people who manage the integration (owner, `config` grant, admin). */
export const integrationManagersOnly: ProviderTabVisibility = (integration, viewer) =>
  canManageEntity(integration, viewer.userId, viewer.isAdmin);

/** A registry entry (tab, Overview card) gated by one of the rules above. */
export interface ViewerGatedEntry {
  key: string;
  /** Omitted = everyone who can open the integration. */
  visible?: ProviderTabVisibility;
}

/** The entries `viewer` is shown on `integration`. */
export function visibleEntries<T extends ViewerGatedEntry>(
  entries: readonly T[],
  integration: Integration,
  viewer: ProviderTabViewer,
): T[] {
  return entries.filter((entry) => !entry.visible || entry.visible(integration, viewer));
}

/** Whether `key` names an entry `viewer` is not shown, e.g. a tab opened from
 *  a link: the page says so instead of rendering an empty body. */
export function isRestrictedEntry(
  entries: readonly ViewerGatedEntry[],
  key: string,
  integration: Integration,
  viewer: ProviderTabViewer,
): boolean {
  const entry = entries.find((candidate) => candidate.key === key);
  return !!entry?.visible && !entry.visible(integration, viewer);
}
