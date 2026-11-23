// SPDX-License-Identifier: Apache-2.0

/**
 * The tabs of CTEM > Inventory (#1178, #1511). A tab is a segment of the page path (`/ctem/inventory/<tab>`, the
 * first tab the bare path). Sources is a page of its own reached by the header button, not a drawn tab (#1367).
 */
import type { ProfileCard } from "@/lib/organization-profile-tabs";

export type InventoryTab = "assets" | "asset-groups" | "software" | "exposure" | "sources";

/** The tab the Software tab replaced (#1495): an old link lands on Software. */
export const RETIRED_TABS: Record<string, InventoryTab> = { "tech-stack": "software" };

/**
 * The profile cards that are settings, not rows (#1178 slice 2b): owned domains, address ranges and the outside lens
 * pass, the prioritization policy and the evidence health pass. They have a tab of their own (#1511): read by
 * everyone, each card's own Edit for administrators, so no card is reachable by URL only.
 */
export const EXPOSURE_TAB_CARDS = ["outside_lens", "prioritization_policy", "evidence_health"] as const satisfies readonly ProfileCard[];

/** The tabs the page draws, in order (English labels, translated by DetailTabs). */
// No AI systems tab since #1501: an AI system is an asset of category AI target, on the Assets tab.
export const INVENTORY_TABS = [
  { key: "assets", label: "Assets" },
  { key: "asset-groups", label: "Asset groups" },
  { key: "software", label: "Software" },
  { key: "exposure", label: "Exposure policy" },
] as const satisfies readonly { key: InventoryTab; label: string }[];
