// SPDX-License-Identifier: Apache-2.0

/**
 * Where an integration / bot provider's logo is served from - the pure half of
 * `providerLogoSrc()` in `constants.ts`, which calls it with the page's URL
 * path prefix and the curated `PROVIDER_LOGOS` map. Kept free of the `@/`
 * alias and of Vite globals so `yarn test:unit` (Node, no DOM) can import it.
 */
import { joinBasePath } from "./base-path.ts";

/**
 * The curated entry for `provider` when the map has one - already under the
 * prefix, returned as is - else the conventional `/logos/<provider>.svg` under
 * `basePath`, prefixed once. The slug is encoded as ONE path segment, so a
 * provider id can never add a directory, a query string or a fragment to the
 * request; and only the map's own keys are curated entries, never an inherited
 * one such as `constructor`.
 */
export function providerLogoSrcFrom(
  basePath: string,
  curated: Readonly<Record<string, string>>,
  provider: string,
): string {
  if (Object.prototype.hasOwnProperty.call(curated, provider)) return curated[provider];
  // A channel bot's logo is its platform's (`slack_bot` -> `slack.svg`); the
  // curated map already spells the bots it knows.
  const slug = provider.replace(/_bot$/, "");
  return joinBasePath(basePath, `/logos/${encodeURIComponent(slug)}.svg`);
}
