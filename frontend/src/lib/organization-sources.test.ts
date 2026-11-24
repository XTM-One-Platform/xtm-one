// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { sourceSectionHref } from "./organization-sources";

describe("organization sources (#1134 S2 part 2, #1276, #1178)", () => {
  it("links a primary assets chip to the Settings tab filtered on the source's label", () => {
    expect(sourceSectionHref("crown_jewels", { key: "opencrq", label: "OpenCRQ" })).toBe(
      "/ctem/inventory/business?card=crown_jewels&ocj_f_source=OpenCRQ",
    );
  });

  it("lands an Assets chip on the Inventory filtered on the source key the server filter takes (UX round 5 on #1254, B11)", () => {
    expect(sourceSectionHref("assets_exposure", { key: "csv", label: "CSV import" })).toBe("/ctem/inventory?card=assets_exposure&a_f_source=csv");
    expect(sourceSectionHref("assets_exposure", { key: "openaev", label: "OpenAEV" })).toBe("/ctem/inventory?card=assets_exposure&a_f_source=openaev");
  });

  it("gives no link to a section whose Inventory list has no Source filter, so the chip never promises one (B11)", () => {
    for (const section of ["software", "asset_groups"] as const) {
      expect(sourceSectionHref(section, { key: "openaev", label: "OpenAEV" })).toBeNull();
    }
  });
});
