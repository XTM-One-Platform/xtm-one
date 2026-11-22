// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { sourceSectionHref } from "./organization-sources";

describe("organization sources (#1134 S2 part 2, #1276)", () => {
  it("links a chip to its tab and card, filtered on the source where the table has a Source filter", () => {
    expect(sourceSectionHref("crown_jewels", "OpenCRQ")).toBe("/settings/organization/profile/business?card=crown_jewels&ocj_f_source=OpenCRQ");
    expect(sourceSectionHref("tech_stack", "Entered by hand")).toBe("/settings/organization/profile/technology?card=tech_stack&ots_f_source=Entered+by+hand");
    expect(sourceSectionHref("assets_exposure", "OpenCTI")).toBe("/settings/organization/profile/exposure?card=assets_exposure");
  });
});
