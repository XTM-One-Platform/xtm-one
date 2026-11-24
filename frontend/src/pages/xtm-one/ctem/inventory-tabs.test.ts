// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { INVENTORY_CARD_PATH, PROFILE_CARD_TAB, isInventoryTab, profileFieldHref, type ProfileCard } from "@/lib/organization-profile-tabs";
import { EXPOSURE_TAB_CARDS, INVENTORY_TABS, RETIRED_TABS } from "./inventory-tabs";
import { INVENTORY_PATH } from "./inventory";

/** The tab key a CTEM > Inventory path names: the bare path is the first tab. */
function tabOfPath(path: string): string | null {
  if (path === INVENTORY_PATH) return INVENTORY_TABS[0].key;
  const prefix = `${INVENTORY_PATH}/`;
  if (!path.startsWith(prefix)) return null;
  // A retired tab (#1495: tech-stack) redirects to the tab that replaced it.
  const tab = path.slice(prefix.length);
  return RETIRED_TABS[tab] ?? tab;
}

/** #1511: no profile card that moved to CTEM > Inventory is reachable by URL only. */
describe("CTEM > Inventory tabs", () => {
  const drawn = new Set<string>(INVENTORY_TABS.map((tab) => tab.key));
  const inventoryCards = (Object.keys(PROFILE_CARD_TAB) as ProfileCard[]).filter((card) => isInventoryTab(PROFILE_CARD_TAB[card]));

  it("lands every inventory card on a tab the page draws", () => {
    for (const card of inventoryCards) {
      const path = INVENTORY_CARD_PATH[card];
      expect(path, card).toBeDefined();
      expect(drawn.has(tabOfPath(path!) ?? ""), `${card} -> ${path}`).toBe(true);
    }
  });

  it("draws the settings cards on the Exposure policy tab", () => {
    for (const card of EXPOSURE_TAB_CARDS) expect(INVENTORY_CARD_PATH[card]).toBe(`${INVENTORY_PATH}/exposure`);
    expect(profileFieldHref("owned_domains")).toBe("/ctem/inventory/exposure?card=outside_lens");
    expect(profileFieldHref("address_ranges")).toBe("/ctem/inventory/exposure?card=outside_lens");
  });
});
