// The Cases freshness line (dev-docs/ctem-cases.md s.4.3): the outage line
// shows only while the outage is newer than the last applied recompute.
import { describe, expect, it } from "vitest";
import { missingProducts, outageIsCurrent } from "./cases-freshness";

describe("cases freshness", () => {
  it("names the products that did not answer, once each, in a stable order", () => {
    expect(missingProducts({ updated_at: null, missing: ["openaev", "opencti", "openaev"], missing_at: null })).toEqual(["OpenAEV", "OpenCTI"]);
    expect(missingProducts(undefined)).toEqual([]);
  });

  it("shows the outage only when it is newer than the last applied recompute", () => {
    const at = (t: string) => `2026-11-22T${t}:00Z`;
    expect(outageIsCurrent({ updated_at: at("13:30"), missing: ["openaev"], missing_at: at("13:40") })).toBe(true);
    expect(outageIsCurrent({ updated_at: at("13:50"), missing: ["openaev"], missing_at: at("13:40") })).toBe(false);
    expect(outageIsCurrent({ updated_at: null, missing: ["openaev"], missing_at: at("13:40") })).toBe(true);
    expect(outageIsCurrent({ updated_at: at("13:30"), missing: [], missing_at: null })).toBe(false);
  });
});
