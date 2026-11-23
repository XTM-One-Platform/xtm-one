// The evidence health pass on by default (#1344): the front reads the backend's resolve_pass rule.

import { describe, expect, it } from "vitest";

import { DEFAULT_EVIDENCE_HEALTH, evidenceHealthRuns } from "./organization-profile";
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

  it("opens its card on CTEM > Inventory, with the outside lens card", () => {
    expect(profileFieldHref("evidence_health")).toBe("/ctem/inventory?card=evidence_health");
  });
});
