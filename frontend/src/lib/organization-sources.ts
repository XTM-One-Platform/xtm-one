// SPDX-License-Identifier: Apache-2.0

/**
 * The Sources panel of the organization profile (#1134 S2 part 2): who feeds which section.
 * One entry per source (each connected platform, then Typed, CSV, API and Agent rows) with
 * the rows it holds per section, and the link that opens the section's tab filtered on it.
 * Pure, so the panel and its tests share it.
 */

import { profileCardHref, type ProfileCard } from "./organization-profile-tabs";
import { sourceKind, sourcePlatform, type Provenance, type SourceKind } from "./organization-provenance";

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

/** A source of rows: a platform key, or a hand or agent kind. */
type SourceKey = string;

export interface SourceSummary {
  key: SourceKey;
  /** "platform" for a platform, else the hand or agent kind. */
  kind: SourceKind;
  /** Rows per section, sections without a row left out, in SOURCED_SECTIONS order. */
  sections: { section: SourcedSection; count: number }[];
  total: number;
}

type SourcedProfile = Partial<Record<SourcedSection, readonly Provenance[] | undefined>>;

/** The key a row counts under: its platform, else its kind. */
function sourceKeyOf(row: Provenance): SourceKey {
  return sourcePlatform(row.source) || sourceKind(row.source);
}

const HAND_ORDER: SourceKind[] = ["manual", "csv", "api", "agent"];

/**
 * Every source of the profile with its rows per section. The connected platforms come first, in
 * the order given, even with no row yet (the panel offers to start syncing them); a platform that
 * is not connected but still holds rows follows; then the hand and agent kinds that hold rows.
 */
export function summarizeSources(profile: SourcedProfile, connected: readonly string[]): SourceSummary[] {
  const counts = new Map<SourceKey, Map<SourcedSection, number>>();
  for (const section of SOURCED_SECTIONS) {
    for (const row of profile[section] ?? []) {
      const key = sourceKeyOf(row);
      const bySection = counts.get(key) ?? new Map<SourcedSection, number>();
      bySection.set(section, (bySection.get(section) ?? 0) + 1);
      counts.set(key, bySection);
    }
  }
  const handKeys = new Set<string>(HAND_ORDER);
  const platforms = [...new Set([...connected, ...[...counts.keys()].filter((key) => !handKeys.has(key)).sort()])];
  const keys = [...platforms, ...HAND_ORDER.filter((kind) => counts.has(kind))];
  return keys.map((key) => {
    const bySection = counts.get(key) ?? new Map<SourcedSection, number>();
    const sections = SOURCED_SECTIONS.filter((section) => bySection.has(section)).map((section) => ({ section, count: bySection.get(section) ?? 0 }));
    return {
      key,
      kind: handKeys.has(key) ? (key as SourceKind) : "platform",
      sections,
      total: sections.reduce((sum, entry) => sum + entry.count, 0),
    };
  });
}

/** The card of the profile page that lists a section. */
const SECTION_CARD: Record<SourcedSection, ProfileCard> = {
  crown_jewels: "crown_jewels",
  tech_stack: "tech_stack",
  ai_systems: "ai_systems",
  assets_exposure: "assets_exposure",
  asset_groups: "asset_groups",
};

/**
 * The link of a section chip: the section's tab, landed on its card, with the table filtered on
 * the source's label where the table has a Source filter (the label the filter lists).
 */
export function sourceSectionHref(section: SourcedSection, sourceLabel: string): string {
  const card = SECTION_CARD[section];
  const params = new URLSearchParams();
  const prefix = SOURCE_FILTER_PREFIX[section];
  if (prefix) params.set(`${prefix}f_source`, sourceLabel);
  return profileCardHref(card, params);
}
