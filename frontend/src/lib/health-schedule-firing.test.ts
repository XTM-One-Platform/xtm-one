import { describe, expect, it } from "vitest";

import { isScheduleFiring as isFiring } from "@/lib/health";

describe("isFiring (#735)", () => {
  const at = "2026-11-20T09:21:00Z";

  it("does not read the scheduler's first-sighting mark as a firing", () => {
    // run_due_schedules records `now` the first time it sees a schedule so it
    // never fires at boot: no cycle, no refusal. The table shows "Never".
    expect(isFiring({ schedule: "quality", fired_at: at, cycle_id: null, refusal: null })).toBe(false);
  });

  it("reads a mark that started a cycle or was refused as a firing", () => {
    expect(isFiring({ schedule: "memory", fired_at: at, cycle_id: "c1", refusal: null })).toBe(true);
    expect(isFiring({ schedule: "digest", fired_at: at, cycle_id: null, refusal: "budget_exhausted" })).toBe(true);
  });

  it("answers false when the schedule has no mark", () => {
    expect(isFiring(undefined)).toBe(false);
  });
});
