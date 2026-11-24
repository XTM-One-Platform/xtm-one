// SPDX-License-Identifier: Apache-2.0

/**
 * The section links of the Sources and syncs table (#1134 S2 part 2, #1276): the label of each
 * section a source feeds, and the link that opens the section's tab filtered on that source.
 * A section that moved to CTEM > Inventory (#1178) opens there. Pure, so the table and its tests share it.
 */

import { profileCardHref, type ProfileCard } from "./organization-profile-tabs";

/** The profile lists that carry a row source, in the order of the tabs. */
const SOURCED_SECTIONS = ["crown_jewels", "software", "assets_exposure", "asset_groups"] as const;
export type SourcedSection = (typeof SOURCED_SECTIONS)[number];

/** The section names on a chip. @i18n-keys */
export const SOURCED_SECTION_LABELS = {
  crown_jewels: "Primary assets",
  software: "Software",
  assets_exposure: "Assets",
  asset_groups: "Asset groups",
} as const satisfies Record<SourcedSection, string>;

/** A source as a chip names it: its key (the value the Inventory's server filter takes) and its display label. */
export interface ChipSource {
  key: string;
  label: string;
}

/**
 * How each section's list filters on a source, where it can (UX round 5 on #1254, B11): the URL prefix of its
 * Source filter (`<prefix>f_source`) and whether that filter takes the source's key (the Inventory's server-side
 * Assets filter, values `csv`, `openaev`...) or its display label (the profile tables of Settings). A section with
 * no entry has no Source filter: its chip is no link, so it never promises a filter it does not apply.
 */
const SOURCE_FILTER: Partial<Record<SourcedSection, { prefix: string; value: keyof ChipSource }>> = {
  crown_jewels: { prefix: "ocj_", value: "label" },
  assets_exposure: { prefix: "a_", value: "key" },
};

/** The card of the profile page that lists a section. */
const SECTION_CARD: Record<SourcedSection, ProfileCard> = {
  crown_jewels: "crown_jewels",
  software: "software",
  assets_exposure: "assets_exposure",
  asset_groups: "asset_groups",
};

/**
 * The link of a section chip: the section's tab (on the Inventory when it moved there), landed on its card and
 * filtered on the source. `null` when the section's list cannot filter on a source (Software and Asset groups
 * of the Inventory): the chip then reads as plain text.
 */
export function sourceSectionHref(section: SourcedSection, source: ChipSource): string | null {
  const filter = SOURCE_FILTER[section];
  if (!filter) return null;
  const params = new URLSearchParams();
  params.set(`${filter.prefix}f_source`, source[filter.value]);
  return profileCardHref(SECTION_CARD[section], params);
}
