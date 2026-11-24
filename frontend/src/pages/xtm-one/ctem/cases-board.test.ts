// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { DAY, ageBands, daysSince, topCases } from "./cases-board";

const NOW = Date.parse("2026-11-13T12:00:00Z");

function kase(id: string, priority: CtemCaseSummary["priority"], ageDays: number | null): CtemCaseSummary {
  return {
    id,
    title: id,
    priority,
    state: "new",
    state_since: ageDays === null ? null : new Date(NOW - ageDays * DAY).toISOString(),
  } as CtemCaseSummary;
}

describe("cases board figures (S5c of #11545)", () => {
  it("reads days in state, never negative, null when unknown", () => {
    expect(daysSince(new Date(NOW - 3 * DAY).toISOString(), NOW)).toBe(3);
    expect(daysSince(new Date(NOW + DAY).toISOString(), NOW)).toBe(0);
    expect(daysSince(null, NOW)).toBeNull();
    expect(daysSince("not a date", NOW)).toBeNull();
  });

  it("puts each case in one age band, bounds inclusive, and leaves out a case with no state time", () => {
    const cases = [kase("a", "low", 0), kase("b", "low", 7), kase("c", "low", 8), kase("d", "low", 30), kase("e", "low", 90), kase("f", "low", 91), kase("g", "low", null)];
    expect(ageBands(cases, NOW)).toEqual([2, 2, 1, 1]);
  });

  it("keeps the three highest-priority cases, the oldest first within a priority", () => {
    const cases = [kase("low", "low", 50), kase("high-new", "high", 1), kase("crit", "critical", 2), kase("high-old", "high", 40)];
    expect(topCases(cases).map((c) => c.id)).toEqual(["crit", "high-old", "high-new"]);
  });
});
