import { describe, expect, it } from "vitest";

import { ORGANIZATION_CONTEXT_MODES, organizationContextCategoriesEffective, organizationContextEffective } from "./constants";

/** The mirror of backend services/organization_context_injection.resolve (#11533). */
function expected(platform: boolean, agent: string, assignment: string): boolean {
  for (const mode of [assignment, agent]) {
    if (mode !== "inherit") return mode === "always";
  }
  return platform;
}

describe("organizationContextEffective", () => {
  for (const platform of [true, false]) {
    for (const agent of ORGANIZATION_CONTEXT_MODES) {
      for (const assignment of ORGANIZATION_CONTEXT_MODES) {
        it(`platform ${platform}, agent ${agent}, assignment ${assignment}`, () => {
          expect(organizationContextEffective(platform, agent, assignment)).toBe(expected(platform, agent, assignment));
        });
      }
      it(`chat: platform ${platform}, agent ${agent}`, () => {
        expect(organizationContextEffective(platform, agent)).toBe(expected(platform, agent, "inherit"));
      });
    }
    it(`general chat follows the platform (${platform})`, () => {
      expect(organizationContextEffective(platform)).toBe(platform);
    });
  }
});

/** The mirror of backend services/organization_context_categories.resolve_categories (#11533). */
describe("organizationContextCategoriesEffective", () => {
  it("inherits the platform list by default (none)", () => {
    expect(organizationContextCategoriesEffective([])).toEqual([]);
    expect(organizationContextCategoriesEffective(["pirs"], null, null)).toEqual(["pirs"]);
  });
  it("lets the most specific explicit list win, an empty one included", () => {
    expect(organizationContextCategoriesEffective(["pirs"], ["tech_stack"])).toEqual(["tech_stack"]);
    expect(organizationContextCategoriesEffective(["pirs"], ["tech_stack"], [])).toEqual([]);
    expect(organizationContextCategoriesEffective(["pirs"], [], ["primary_assets"])).toEqual(["primary_assets"]);
  });
});
