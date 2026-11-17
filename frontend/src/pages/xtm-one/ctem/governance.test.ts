// The figures of the CTEM Governance & Risk page (S2e of #11547): no value is
// invented, a missing score or target is null, an unreadable expiry is left out.

import { describe, expect, it } from "vitest";

import { BAND_ZONES, gapSeries, postureGap, risksByExpiry, stripPosition } from "./governance";

const today = new Date("2026-11-13T15:00:00Z");
const risk = (subject: string, expires_on: string) => ({ subject, scope: "", owner: "CISO", expires_on });

describe("postureGap", () => {
  it("is the target minus the score, rounded", () => {
    expect(postureGap(61.4, 75)).toBe(14);
    expect(postureGap(80, 75)).toBe(-5);
  });

  it("is null without a score or a target", () => {
    expect(postureGap(null, 75)).toBeNull();
    expect(postureGap(60, null)).toBeNull();
    expect(gapSeries([60, null], 70)).toEqual([10, null]);
  });
});

describe("BAND_ZONES", () => {
  it("covers 0 to 100 without a hole, in the ctem_scoring bands", () => {
    expect(BAND_ZONES[0].from).toBe(0);
    expect(BAND_ZONES[BAND_ZONES.length - 1].to).toBe(100);
    BAND_ZONES.slice(1).forEach((zone, i) => expect(zone.from).toBe(BAND_ZONES[i].to));
  });
});

describe("risksByExpiry", () => {
  it("orders by days until expiry, marks the expired, drops a blank or unreadable date", () => {
    const placed = risksByExpiry(
      [risk("CVE-B", "2026-12-13"), risk("CVE-A", "2026-11-10"), risk("CVE-C", ""), risk("CVE-D", "soon"), risk("  ", "2026-12-01")],
      today,
    );
    expect(placed.map((r) => [r.subject, r.days, r.expired])).toEqual([
      ["CVE-A", -3, true],
      ["CVE-B", 30, false],
    ]);
  });
});

describe("stripPosition", () => {
  it("clamps to the strip", () => {
    expect(stripPosition(-4)).toBe(0);
    expect(stripPosition(45)).toBe(0.5);
    expect(stripPosition(400)).toBe(1);
  });
});
