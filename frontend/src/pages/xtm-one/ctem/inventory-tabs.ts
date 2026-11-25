// SPDX-License-Identifier: Apache-2.0

/**
 * The tabs of CTEM > Inventory (#1178, #1511). A tab is a segment of the page path (`/ctem/inventory/<tab>`, the
 * first tab the bare path). Sources is a page of its own reached by the header button, not a drawn tab (#1367).
 */
import type { ProfileCard } from "@/lib/organization-profile-tabs";

export type InventoryTab = "assets" | "asset-groups" | "software" | "business" | "exposure" | "sources";

/** The tab the Software tab replaced (#1495): an old link lands on Software. */
export const RETIRED_TABS: Record<string, InventoryTab> = { "tech-stack": "software" };

/**
 * The profile cards that are settings, not rows (#1178 slice 2b): owned domains, address ranges and the outside lens
 * pass, the prioritization policy and the evidence health pass. They have a tab of their own (#1511): read by
 * everyone, each card's own Edit for administrators, so no card is reachable by URL only.
 */
export const EXPOSURE_TAB_CARDS = ["outside_lens", "prioritization_policy", "evidence_health"] as const satisfies readonly ProfileCard[];

/**
 * The business inputs of the CTEM loop (#1690, Sam 24 Nov 2026): primary assets and business services, shipped
 * products and tracked organizations moved from Settings > Organization to a tab of their own, read by everyone,
 * each card's own Edit for administrators, as the Exposure policy tab.
 */
export const BUSINESS_TAB_CARDS = ["primary_assets", "shipped_products", "tracked_organizations"] as const satisfies readonly ProfileCard[];

/** The tabs the page draws, in order (English labels, translated by DetailTabs). */
// AI targets are assets of category AI target, on the Assets tab: no tab of their own.
export const INVENTORY_TABS = [
  { key: "assets", label: "Assets" },
  { key: "asset-groups", label: "Asset groups" },
  { key: "software", label: "Software" },
  { key: "business", label: "Business" },
  { key: "exposure", label: "Exposure policy" },
] as const satisfies readonly { key: InventoryTab; label: string }[];
