// The evidence health pass on by default (#1344): the front reads the backend's resolve_pass rule.

import { describe, expect, it } from "vitest";

import {
  DEFAULT_EVIDENCE_HEALTH,
  evidenceHealthIntegrationChoice,
  evidenceHealthRuns,
  evidenceHealthSavedIntegration,
} from "./organization-profile";
import { profileFieldHref } from "./organization-profile-tabs";

const pass = (over: Partial<typeof DEFAULT_EVIDENCE_HEALTH> = {}) => ({ ...DEFAULT_EVIDENCE_HEALTH, ...over });

describe("evidenceHealthRuns", () => {
  it("runs on the single connected OpenCRQ integration when no administrator chose", () => {
    expect(DEFAULT_EVIDENCE_HEALTH.enabled).toBeNull();
    expect(evidenceHealthRuns(pass(), ["a"])).toEqual({ runs: true, integration: "a" });
    // The legacy stored default (off, none declared) reads as unset.
    expect(evidenceHealthRuns(pass({ enabled: false }), ["a"])).toEqual({ runs: true, integration: "a" });
  });

  it("stays off when turned off, when several are connected and none declared, or when none is", () => {
    expect(evidenceHealthRuns(pass({ enabled: false, integration: "a" }), ["a"]).runs).toBe(false);
    expect(evidenceHealthRuns(pass(), ["a", "b"]).runs).toBe(false);
    expect(evidenceHealthRuns(pass(), []).runs).toBe(false);
  });

  it("reads a declared integration as declared, never another one", () => {
    expect(evidenceHealthRuns(pass({ enabled: true, integration: "b" }), ["a", "b"])).toEqual({ runs: true, integration: "b" });
    expect(evidenceHealthRuns(pass({ enabled: true, integration: "c" }), ["a"]).runs).toBe(false);
  });

  it("holds an off saved on the default setting: the save names the single connected integration (UX round 1 on #1356, B4)", () => {
    const saved = evidenceHealthSavedIntegration("", false, ["a"]);
    expect(saved).toBe("a");
    expect(evidenceHealthRuns(pass({ enabled: false, integration: saved }), ["a"]).runs).toBe(false);
    // On, or several connected: the default stays undeclared; a pick is kept as picked.
    expect(evidenceHealthSavedIntegration("", true, ["a"])).toBe("");
    expect(evidenceHealthSavedIntegration("", false, ["a", "b"])).toBe("");
    expect(evidenceHealthSavedIntegration("b", false, ["a", "b"])).toBe("b");
  });

  it("names the integration the card reads: declared, the single connected one, several with none picked, or none (#1948)", () => {
    expect(evidenceHealthIntegrationChoice(pass({ integration: "b" }), ["a", "b"])).toBe("declared");
    expect(evidenceHealthIntegrationChoice(pass(), ["a"])).toBe("single");
    expect(evidenceHealthIntegrationChoice(pass(), ["a", "b"])).toBe("several");
    expect(evidenceHealthIntegrationChoice(pass(), [])).toBe("none");
    // A duplicate id is one integration.
    expect(evidenceHealthIntegrationChoice(pass(), ["a", "a"])).toBe("single");
  });

  it("opens its card on CTEM > Inventory, with the outside lens card", () => {
    expect(profileFieldHref("evidence_health")).toBe("/ctem/inventory/exposure?card=evidence_health");
  });
});
