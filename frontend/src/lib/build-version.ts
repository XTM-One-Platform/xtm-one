// SPDX-License-Identifier: Apache-2.0

/**
 * The frontend build a page runs vs the one the server serves.
 *
 * A tab opened before an upgrade keeps running the previous bundle, which may
 * misread what the new server sends it until the tab is reloaded. The
 * backend names the build of the `index.html` it serves in a
 * `<meta name="build-id">` (`branding.frontend_build_id`) and reports its
 * current build on `GET /auth/config` (`build_id`); the page compares the two
 * and offers a reload (`useStaleBuildNotice`).
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */

/** `<meta name>` the backend writes the served build into (`BUILD_ID_META_NAME`). */
export const BUILD_ID_META_NAME = "build-id";

/** What {@link readPageBuildId} needs of a document. */
export interface MetaSource {
  querySelector(selectors: string): { getAttribute(name: string): string | null } | null;
}

/**
 * The build of the bundle this page booted, read from its own `index.html`.
 * `""` when the page has none - the Vite dev server writes no such meta - and
 * an unknown build is never compared.
 */
export function readPageBuildId(doc: MetaSource | undefined = globalThis.document): string {
  const meta = doc?.querySelector(`meta[name="${BUILD_ID_META_NAME}"]`);
  return meta?.getAttribute("content")?.trim() ?? "";
}

/**
 * Whether the server now serves another build than the one this page runs.
 * Unknown on either side (a dev page, an older backend without `build_id`, a
 * server without a built frontend) is never stale.
 */
export function isStaleBuild(pageBuildId: string, serverBuildId: string | null | undefined): boolean {
  return (
    pageBuildId !== "" &&
    typeof serverBuildId === "string" &&
    serverBuildId !== "" &&
    serverBuildId !== pageBuildId
  );
}
