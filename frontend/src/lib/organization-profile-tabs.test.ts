// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { FIELD_LABELS } from "./organization-profile";
import {
  PROFILE_CARD_TAB,
  PROFILE_FIELD_CARD,
  PROFILE_TABS,
  PROFILE_TAB_LABELS,
  changedTabs,
  groupByTab,
  isInventoryTab,
  parseProfileCard,
  parseProfileTab,
  profileFieldHref,
  profileTabOfPath,
  profileTabPath,
  tabOfField,
  tabsOfFields,
} from "./organization-profile-tabs";
import { EXPOSURE_SECTIONS } from "@/pages/settings/OrganizationExposureCards";

/** Coverage of the five tabs (#12329): a new field or card cannot be added without a tab. */
describe("organization profile tabs", () => {
  it("has five tabs, each named in at most three words", () => {
    expect(PROFILE_TABS).toHaveLength(5);
    for (const tab of PROFILE_TABS) expect(PROFILE_TAB_LABELS[tab].split(" ").length).toBeLessThanOrEqual(3);
  });

  it("maps every field of the profile contract to exactly one tab", () => {
    for (const path of Object.keys(FIELD_LABELS)) expect(tabOfField(path), path).not.toBeNull();
  });

  it("maps every exposure card, the tracked organizations and the agent context to a tab", () => {
    for (const card of EXPOSURE_SECTIONS) expect(tabOfField(card), card).not.toBeNull();
    expect(PROFILE_CARD_TAB.tracked_organizations).toBe("business");
    // The PIR sources live on the Prioritization policy card since #1938: no imports card on the knowledge tab.
    expect("scheduled_imports" in PROFILE_CARD_TAB).toBe(false);
    expect(PROFILE_CARD_TAB.context_injection).toBe("knowledge");
  });

  it("puts every tab to use and every field on a known card", () => {
    const used = new Set(Object.values(PROFILE_CARD_TAB));
    expect([...used].sort()).toEqual([...PROFILE_TABS].sort());
    for (const card of Object.values(PROFILE_FIELD_CARD)) expect(card in PROFILE_CARD_TAB, card).toBe(true);
  });

  it("reads a contract path or a form key", () => {
    expect(tabOfField("risk_appetite.approval_threshold")).toBe("identity");
    expect(tabOfField("approval_threshold")).toBe("identity");
    expect(tabOfField("ticketing.project")).toBe("identity");
    // The PIR list moved to the Prioritization policy of CTEM > Inventory (#1889).
    expect(tabOfField("pir_ids")).toBe("exposure");
    expect(profileFieldHref("pir_ids")).toBe("/ctem/inventory/exposure?card=prioritization_policy");
    expect(tabOfField("accepted_risks")).toBe("exposure");
    expect(tabOfField("enterprise_knowledge")).toBe("knowledge");
    expect(tabOfField("unknown_field")).toBeNull();
  });

  it("parses the tab of the URL, the first tab otherwise", () => {
    expect(parseProfileTab("technology")).toBe("technology");
    expect(parseProfileTab(null)).toBe("identity");
    expect(parseProfileTab("nope")).toBe("identity");
  });

  it("puts the tab in the path, never in the query (#10203)", () => {
    expect(profileTabPath("identity")).toBe("/settings/organization");
    expect(profileTabPath("exposure")).toBe("/settings/organization/profile/exposure");
    expect(profileTabOfPath("/settings/organization/profile/exposure")).toBe("exposure");
    expect(profileTabOfPath("/settings/organization")).toBe("identity");
    expect(profileTabOfPath("/settings/organization/profile/nope")).toBe("identity");
    for (const tab of PROFILE_TABS) expect(profileTabOfPath(profileTabPath(tab))).toBe(tab);
  });

  it("links a field to its tab and card, never to the bare first tab when the field lives elsewhere", () => {
    expect(profileFieldHref("primary_assets")).toBe("/ctem/inventory/business?card=primary_assets");
    // The technology and exposure cards live on CTEM > Inventory (#1178).
    expect(profileFieldHref("prioritization_policy")).toBe("/ctem/inventory/exposure?card=prioritization_policy");
    expect(profileFieldHref("accepted_risks")).toBe("/ctem/inventory/exposure?card=prioritization_policy");
    expect(profileFieldHref("assets_exposure")).toBe("/ctem/inventory?card=assets_exposure");
    expect(profileFieldHref("software")).toBe("/ctem/inventory/software?card=software");
    expect(profileFieldHref("tracked_organizations")).toBe("/ctem/inventory/business?card=tracked_organizations");
    expect(profileFieldHref("sector")).toBe("/settings/organization?card=identity");
    expect(profileFieldHref("risk_appetite.approval_threshold")).toBe("/settings/organization?card=risk_appetite");
    expect(profileFieldHref("nope")).toBe("/settings/organization");
    // Every field lands on its own Settings tab, or on CTEM > Inventory when its card moved there.
    for (const field of Object.keys(PROFILE_FIELD_CARD)) {
      const [path] = profileFieldHref(field).split("?");
      const tab = tabOfField(field)!;
      if (isInventoryTab(tab)) expect(path.startsWith("/ctem/inventory"), field).toBe(true);
      else expect(profileTabOfPath(path), field).toBe(tab);
    }
  });

  it("parses the card of the URL, null for none or an unknown key", () => {
    expect(parseProfileCard("primary_assets")).toBe("primary_assets");
    expect(parseProfileCard("toString")).toBeNull();
    expect(parseProfileCard("nope")).toBeNull();
    expect(parseProfileCard(null)).toBeNull();
  });

  it("names the tabs a change touches and groups items by tab in tab order", () => {
    expect([...changedTabs({ name: "a", software: [] }, { name: "a", software: [{ product: "x" }] })]).toEqual(["technology"]);
    expect(tabsOfFields(["software", "pir_ids", "size", "nope"])).toEqual(["identity", "technology", "exposure"]);
    const groups = groupByTab([{ f: "software" }, { f: "size" }, { f: "nope" }], (item) => item.f);
    expect(groups.map((group) => group.tab)).toEqual(["identity", "technology", null]);
  });
});
