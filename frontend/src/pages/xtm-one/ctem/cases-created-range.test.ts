// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import { caseCreatedRanges } from "./cases-created-range";

const NOW = Date.parse("2026-11-22T12:00:00Z");
const daysAgo = (d: number) => new Date(NOW - d * 24 * 3600 * 1000).toISOString();

describe("caseCreatedRanges (#1176)", () => {
  it("puts a recent case in every range it falls in", () => {
    expect(caseCreatedRanges({ created_at: daysAgo(3), first_seen_at: null }, NOW)).toEqual(["7d", "30d", "90d"]);
    expect(caseCreatedRanges({ created_at: daysAgo(20), first_seen_at: null }, NOW)).toEqual(["30d", "90d"]);
    expect(caseCreatedRanges({ created_at: daysAgo(60), first_seen_at: null }, NOW)).toEqual(["90d"]);
  });

  it("puts a case older than 90 days in the older range alone", () => {
    expect(caseCreatedRanges({ created_at: daysAgo(120), first_seen_at: null }, NOW)).toEqual(["older"]);
  });

  it("falls back to the oldest issue, and matches nothing with no date", () => {
    expect(caseCreatedRanges({ created_at: null, first_seen_at: daysAgo(1) }, NOW)).toEqual(["7d", "30d", "90d"]);
    expect(caseCreatedRanges({ created_at: null, first_seen_at: null }, NOW)).toEqual([]);
  });
});
