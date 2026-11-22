// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import { sourceSectionHref, summarizeSources } from "./organization-sources";

describe("organization sources (#1134 S2 part 2)", () => {
  it("lists the connected platforms first, then the platforms holding rows, then typed, CSV, API and agent rows", () => {
    const sources = summarizeSources(
      {
        crown_jewels: [{ source: "opencrq" }, { source: "opencrq" }, { source: "manual" }, {}],
        tech_stack: [{ source: "openaev" }, { source: "csv" }, { source: "agent:a1" }],
        assets_exposure: [{ source: "opencti" }],
      },
      ["opencrq", "openaev"],
    );
    expect(sources.map((source) => source.key)).toEqual(["opencrq", "openaev", "opencti", "manual", "csv", "agent"]);
    expect(sources[0]).toEqual({ key: "opencrq", kind: "platform", sections: [{ section: "crown_jewels", count: 2 }], total: 2 });
    expect(sources.find((source) => source.key === "manual")?.sections).toEqual([{ section: "crown_jewels", count: 2 }]);
    expect(sources.find((source) => source.key === "agent")?.kind).toBe("agent");
  });

  it("keeps a connected platform with no row, so the panel can offer its first sync", () => {
    expect(summarizeSources({}, ["opencti"])).toEqual([{ key: "opencti", kind: "platform", sections: [], total: 0 }]);
  });

  it("answers nothing for an empty profile with no platform", () => {
    expect(summarizeSources({ crown_jewels: [], tech_stack: undefined }, [])).toEqual([]);
  });

  it("links a chip to its tab and card, filtered on the source where the table has a Source filter", () => {
    expect(sourceSectionHref("crown_jewels", "OpenCRQ")).toBe("/settings/organization/profile/business?card=crown_jewels&ocj_f_source=OpenCRQ");
    expect(sourceSectionHref("tech_stack", "Typed")).toBe("/settings/organization/profile/technology?card=tech_stack&ots_f_source=Typed");
    expect(sourceSectionHref("assets_exposure", "OpenCTI")).toBe("/settings/organization/profile/exposure?card=assets_exposure");
  });
});
