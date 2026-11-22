// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { sourceSectionHref } from "./organization-sources";

describe("organization sources (#1134 S2 part 2, #1276, #1178)", () => {
  it("links a chip to its tab and card, on the Inventory for the sections that moved there, filtered on the source where the table has a Source filter", () => {
    expect(sourceSectionHref("crown_jewels", "OpenCRQ")).toBe("/settings/organization/profile/business?card=crown_jewels&ocj_f_source=OpenCRQ");
    expect(sourceSectionHref("tech_stack", "Entered by hand")).toBe("/ctem/inventory/tech-stack?card=tech_stack&ots_f_source=Entered+by+hand");
    expect(sourceSectionHref("assets_exposure", "OpenCTI")).toBe("/ctem/inventory?card=assets_exposure");
  });
});
