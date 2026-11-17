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
  parseProfileTab,
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

  it("maps every exposure card, the tracked organizations, the imports and the agent context to a tab", () => {
    for (const card of EXPOSURE_SECTIONS) expect(tabOfField(card), card).not.toBeNull();
    expect(PROFILE_CARD_TAB.tracked_organizations).toBe("business");
    expect(PROFILE_CARD_TAB.scheduled_imports).toBe("knowledge");
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

  it("names the tabs a change touches and groups items by tab in tab order", () => {
    expect([...changedTabs({ name: "a", tech_stack: [] }, { name: "a", tech_stack: [{ product: "x" }] })]).toEqual(["technology"]);
    expect(tabsOfFields(["tech_stack", "pir_ids", "nope"])).toEqual(["identity", "technology"]);
    const groups = groupByTab([{ f: "tech_stack" }, { f: "size" }, { f: "nope" }], (item) => item.f);
    expect(groups.map((group) => group.tab)).toEqual(["identity", "technology", null]);
  });
});
