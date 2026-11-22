// SPDX-License-Identifier: Apache-2.0

/**
 * The section links of the Sources and syncs table (#1134 S2 part 2, #1276): the label of each
 * section a source feeds, and the link that opens the section's tab filtered on that source.
 * A section that moved to CTEM > Inventory (#1178) opens there. Pure, so the table and its tests share it.
 */

import { profileCardHref, type ProfileCard } from "./organization-profile-tabs";

/** The profile lists that carry a row source, in the order of the tabs. */
const SOURCED_SECTIONS = ["crown_jewels", "tech_stack", "ai_systems", "assets_exposure", "asset_groups"] as const;
export type SourcedSection = (typeof SOURCED_SECTIONS)[number];

/** The section names on a chip. @i18n-keys */
export const SOURCED_SECTION_LABELS = {
  crown_jewels: "Crown jewels",
  tech_stack: "Tech stack",
  ai_systems: "AI systems",
  assets_exposure: "Assets",
  asset_groups: "Asset groups",
} as const satisfies Record<SourcedSection, string>;

/** The URL prefix of each section's table that has a Source filter (`<prefix>f_source`). */
const SOURCE_FILTER_PREFIX: Partial<Record<SourcedSection, string>> = {
  crown_jewels: "ocj_",
  tech_stack: "ots_",
  ai_systems: "oai_",
};

/** The card of the profile page that lists a section. */
const SECTION_CARD: Record<SourcedSection, ProfileCard> = {
  crown_jewels: "crown_jewels",
  tech_stack: "tech_stack",
  ai_systems: "ai_systems",
  assets_exposure: "assets_exposure",
  asset_groups: "asset_groups",
};

/**
 * The link of a section chip: the section's tab (on the Inventory when it moved there), landed on
 * its card, with the table filtered on the source's label where the table has a Source filter.
 */
export function sourceSectionHref(section: SourcedSection, sourceLabel: string): string {
  const card = SECTION_CARD[section];
  const params = new URLSearchParams();
  const prefix = SOURCE_FILTER_PREFIX[section];
  if (prefix) params.set(`${prefix}f_source`, sourceLabel);
  return profileCardHref(card, params);
}
