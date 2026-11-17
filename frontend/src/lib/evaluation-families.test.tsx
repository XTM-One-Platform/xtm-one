// The family copy the server serves is translated with its own text as the key.

import { describe, expect, it } from "vitest";

import type { FamilyInfo } from "@/types";

import { FAMILY_CATALOG_COPY, familyDescription, familyLabel } from "./evaluation-families";

const PROSE: FamilyInfo = {
  id: "PROSE",
  label: "Prose",
  description: "Natural-language prose.",
  metric_keys: [],
  structured_output: false,
  extractor_output_kinds: [],
};
const FRENCH: Record<string, string> = { Prose: "Texte libre" };
const t_i18n = (id: string) => FRENCH[id] ?? id;

describe("familyLabel", () => {
  it("translates the label the server serves", () => {
    expect(familyLabel("PROSE", [PROSE], t_i18n)).toBe("Texte libre");
  });

  it("shows the id of a family the catalog does not list", () => {
    expect(familyLabel("STIX_BUNDLE", [PROSE], t_i18n)).toBe("STIX_BUNDLE");
    expect(familyLabel("PROSE", undefined, t_i18n)).toBe("PROSE");
  });
});

describe("familyDescription", () => {
  it("shows a sentence the server changed as it now reads, never an older copy", () => {
    const changed = { ...PROSE, description: "A sentence no catalog knows yet." };
    expect(familyDescription(changed, t_i18n)).toBe("A sentence no catalog knows yet.");
  });

  it("is null without a family", () => {
    expect(familyDescription(undefined, t_i18n)).toBeNull();
  });
});

describe("FAMILY_CATALOG_COPY", () => {
  it("lists each string once", () => {
    expect(new Set(FAMILY_CATALOG_COPY).size).toBe(FAMILY_CATALOG_COPY.length);
  });
});
