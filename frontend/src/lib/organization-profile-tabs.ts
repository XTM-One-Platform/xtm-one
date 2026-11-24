// SPDX-License-Identifier: Apache-2.0

/**
 * The five tabs of Settings > Organization > Profile (#12329): every card of the
 * profile page, and every field of the profile contract, sits in exactly one tab.
 * The tab is a segment of the page path (`/settings/organization/profile/<tab>`, the
 * first tab the bare `/settings/organization`; path routes, not a query, #10203), so a
 * tab can be linked to and the browser's Back returns to the previous one. A missing field, a refused Save,
 * a draft's proposals and an onboarding jump are routed to their tab by
 * `tabOfField`. `organization-profile-tabs.test.ts` fails on a field or a card
 * left without a tab.
 */

/** The tabs, in page order; the first is the default. */
export const PROFILE_TABS = ["identity", "business", "technology", "exposure", "knowledge"] as const;
export type ProfileTab = (typeof PROFILE_TABS)[number];

/**
 * The tabs Settings > Organization keeps (#1178, Sam 22 Nov 2026; #1690, Sam 24 Nov 2026): the Business, Technology
 * and Exposure cards live on CTEM > Inventory now, so Settings draws no tab bar (identity, knowledge through its
 * button); the five tabs stay the grouping of fields (drafts, unsaved markers, missing fields).
 */
export const SETTINGS_PROFILE_TABS = ["identity", "knowledge"] as const satisfies readonly ProfileTab[];

/** Whether a tab's cards live on CTEM > Inventory instead of Settings. */
export function isInventoryTab(tab: ProfileTab): boolean {
  return !(SETTINGS_PROFILE_TABS as readonly ProfileTab[]).includes(tab);
}

/** The tab names (at most three words). @i18n-keys */
export const PROFILE_TAB_LABELS = {
  identity: "Identity and risk",
  business: "Business",
  technology: "Technology",
  exposure: "Exposure",
  knowledge: "Knowledge and sources",
} as const satisfies Record<ProfileTab, string>;

/** Every card of the profile page, by its key, and the tab that shows it. */
export const PROFILE_CARD_TAB = {
  identity: "identity",
  risk_appetite: "identity",
  ticketing: "identity",
  crown_jewels: "business",
  shipped_products: "business",
  tracked_organizations: "business",
  tech_stack: "technology",
  assets_exposure: "exposure",
  asset_groups: "exposure",
  prioritization_policy: "exposure",
  outside_lens: "exposure",
  evidence_health: "exposure",
  enterprise_knowledge: "knowledge",
  scheduled_imports: "knowledge",
  context_injection: "knowledge",
} as const satisfies Record<string, ProfileTab>;
export type ProfileCard = keyof typeof PROFILE_CARD_TAB;

/**
 * The CTEM > Inventory page of each card that moved there (#1178): a link to the card lands on that tab, which
 * mounts the card with its edit (OrganizationSection embed).
 */
export const INVENTORY_CARD_PATH: Partial<Record<ProfileCard, string>> = {
  tech_stack: "/ctem/inventory/software",
  // The business inputs have their own Inventory tab (#1690).
  crown_jewels: "/ctem/inventory/business",
  shipped_products: "/ctem/inventory/business",
  tracked_organizations: "/ctem/inventory/business",
  assets_exposure: "/ctem/inventory",
  // The settings cards have their own Inventory tab (#1511), never an edit view opened by URL only.
  prioritization_policy: "/ctem/inventory/exposure",
  outside_lens: "/ctem/inventory/exposure",
  evidence_health: "/ctem/inventory/exposure",
  asset_groups: "/ctem/inventory/asset-groups",
};

/** The link to one card: its Inventory tab when it moved there, else its Settings tab, with `?card=<card>`. */
export function profileCardHref(card: ProfileCard, params: URLSearchParams = new URLSearchParams()): string {
  // The card first, then the table filters, as the links were written before the move.
  const next = new URLSearchParams({ [PROFILE_CARD_PARAM]: card });
  params.forEach((value, key) => next.set(key, value));
  return `${INVENTORY_CARD_PATH[card] ?? profileTabPath(PROFILE_CARD_TAB[card])}?${next.toString()}`;
}

/**
 * Every field of the profile (a form key or a contract path) and the card that edits it.
 * pir_names travels with pir_ids; posture_target is set on the CTEM governance page and
 * only carried by the form, so it opens the exposure tab, where the policy lives.
 */
export const PROFILE_FIELD_CARD = {
  name: "identity",
  sector: "identity",
  sub_sector: "identity",
  regions: "identity",
  size: "identity",
  currency: "risk_appetite",
  loaded_hourly_rate: "risk_appetite",
  risk_appetite: "risk_appetite",
  annual_loss_threshold: "risk_appetite",
  approval_threshold: "risk_appetite",
  ticketing: "ticketing",
  pir_ids: "ticketing",
  pir_names: "ticketing",
  crown_jewels: "crown_jewels",
  shipped_products: "shipped_products",
  tech_stack: "tech_stack",
  // #1501: the AI systems are the assets of category ai_target, edited on the asset list.
  ai_systems: "assets_exposure",
  assets_exposure: "assets_exposure",
  asset_groups: "asset_groups",
  prioritization_policy: "prioritization_policy",
  accepted_risks: "prioritization_policy",
  posture_target: "prioritization_policy",
  owned_domains: "outside_lens",
  address_ranges: "outside_lens",
  attack_surface: "outside_lens",
  evidence_health: "evidence_health",
  enterprise_knowledge: "enterprise_knowledge",
  tracked_organizations: "tracked_organizations",
} as const satisfies Record<string, ProfileCard>;

/** The tab of a field, by form key or contract path ("risk_appetite.approval_threshold", "ticketing.project"); null when unknown. */
export function tabOfField(path: string): ProfileTab | null {
  const fields = PROFILE_FIELD_CARD as Record<string, ProfileCard>;
  const card = fields[path] ?? fields[path.split(".").pop() ?? ""] ?? fields[path.split(".")[0]];
  return card ? PROFILE_CARD_TAB[card] : null;
}

/** The tab a URL names; anything else is the first tab. */
export function parseProfileTab(raw: string | null | undefined): ProfileTab {
  return (PROFILE_TABS as readonly string[]).includes(raw ?? "") ? (raw as ProfileTab) : PROFILE_TABS[0];
}

const PROFILE_PATH = "/settings/organization";
const PROFILE_TAB_SEGMENT = /^\/settings\/organization\/profile\/([^/]+)\/?$/;

/** The path of a tab: the first tab is the bare section path, the others `/settings/organization/profile/<tab>`. */
export function profileTabPath(tab: ProfileTab): string {
  return tab === PROFILE_TABS[0] ? PROFILE_PATH : `${PROFILE_PATH}/profile/${tab}`;
}

/** The query parameter a link sets to land on one card of its tab (read once by the page, then dropped). */
export const PROFILE_CARD_PARAM = "card";

/** The card a URL query names; null for none or an unknown key. */
export function parseProfileCard(raw: string | null | undefined): ProfileCard | null {
  return raw && Object.prototype.hasOwnProperty.call(PROFILE_CARD_TAB, raw) ? (raw as ProfileCard) : null;
}

/**
 * The link to the card that edits a field: its tab's path with `?card=<card>`, so a call to
 * action ("Map crown jewels to assets") opens the right tab scrolled to the right card, never the
 * first tab. An unknown field opens the first tab.
 */
export function profileFieldHref(path: string): string {
  const fields = PROFILE_FIELD_CARD as Record<string, ProfileCard>;
  const card = fields[path] ?? fields[path.split(".").pop() ?? ""] ?? fields[path.split(".")[0]];
  if (!card) return PROFILE_PATH;
  return profileCardHref(card);
}

/** The tab a page path names; any other path is the first tab. */
export function profileTabOfPath(pathname: string): ProfileTab {
  return parseProfileTab(PROFILE_TAB_SEGMENT.exec(pathname)?.[1]);
}

/** The tabs holding a field whose value differs between two versions of the form. */
export function changedTabs(before: object, after: object): Set<ProfileTab> {
  const a = before as Record<string, unknown>;
  const b = after as Record<string, unknown>;
  const tabs = new Set<ProfileTab>();
  for (const key of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (JSON.stringify(a[key]) === JSON.stringify(b[key])) continue;
    const tab = tabOfField(key);
    if (tab) tabs.add(tab);
  }
  return tabs;
}

/** The tabs of a list of fields, in tab order. */
export function tabsOfFields(paths: readonly string[]): ProfileTab[] {
  const found = new Set(paths.map(tabOfField));
  return PROFILE_TABS.filter((tab) => found.has(tab));
}

/** Items grouped by the tab of their field, in tab order; an item of an unknown field goes last, under null. */
export function groupByTab<T>(items: readonly T[], field: (item: T) => string): { tab: ProfileTab | null; items: T[] }[] {
  const groups = new Map<ProfileTab | null, T[]>();
  for (const item of items) {
    const tab = tabOfField(field(item));
    groups.set(tab, [...(groups.get(tab) ?? []), item]);
  }
  return [...PROFILE_TABS, null].filter((tab) => groups.has(tab)).map((tab) => ({ tab, items: groups.get(tab)! }));
}
