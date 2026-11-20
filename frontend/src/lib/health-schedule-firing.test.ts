/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

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
