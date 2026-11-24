// SPDX-License-Identifier: Apache-2.0

/**
 * Catalog entries whose shape depends on one operator choice.
 *
 * The entry names the choice in `variant_selector` and maps each of its
 * values, in `variants`, to the facets that differ; the base entry is the
 * default. Consumers resolve once and read plain fields.
 */

import type { ProviderCatalogItem, ProviderCatalogVariant, SetupGuide } from "@/types";

type Entry = Pick<ProviderCatalogItem, "variant_selector" | "variants">;

/**
 * The selector value in effect. A2A's direction is the one selector that is
 * not a config key: it lives on its own settings row, so the caller supplies
 * it. Every other selector is read from the config.
 */
export function variantValue(
  entry: Entry | undefined,
  config: Record<string, unknown> | null | undefined,
  a2aDirection?: string,
): string | undefined {
  const selector = entry?.variant_selector;
  if (!selector) return undefined;
  if (selector === "a2a_direction") return a2aDirection;
  const value = config?.[selector];
  return typeof value === "string" ? value : undefined;
}

/** The entry as it reads for `value`: the variant's facets over the base. */
export function resolveVariant<T extends Entry>(entry: T, value: string | undefined): T {
  const variant: ProviderCatalogVariant | undefined = value ? entry.variants?.[value] : undefined;
  if (!variant) return entry;
  const set = Object.fromEntries(Object.entries(variant).filter(([, v]) => v != null));
  return { ...entry, ...set } as T;
}

/**
 * Field keys declared by variants other than `value`: what an edit must drop.
 *
 * An UNKNOWN `value` drops nothing. Every variant's keys would otherwise
 * qualify — none equals `undefined` — stripping the live variant's own fields
 * from a save that replaces the stored config wholesale. A2A reaches this
 * whenever agentic chat is off, since the direction query is then disabled and
 * a disabled query never resolves — and whenever that query fails with
 * anything but a 404 (`useA2ADirection`).
 */
export function otherVariantFieldKeys(entry: Entry | undefined, value: string | undefined): Set<string> {
  if (value === undefined) return new Set();
  return new Set(
    Object.entries(entry?.variants ?? {})
      .filter(([key]) => key !== value)
      .flatMap(([, variant]) => Object.keys(variant.extra_fields ?? {})),
  );
}

/**
 * The setup guide of `authType`: the entry's guide for that authentication
 * mode when it has one (`auth_setup_guides`), else its own `setup_guide`.
 * Pass the entry as resolved for its variant.
 */
export function setupGuideFor(
  entry: Pick<ProviderCatalogItem, "setup_guide" | "auth_setup_guides"> | undefined,
  authType: string | null | undefined,
): SetupGuide | null | undefined {
  return (authType ? entry?.auth_setup_guides?.[authType] : undefined) ?? entry?.setup_guide;
}
