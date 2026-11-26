// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import {
  COORDINATION_RETENTION_DEFAULTS,
  MAX_COORDINATION_RETENTION_DAYS,
  parseCoordinationRetentionDays,
} from "./coordination-retention";

describe("parseCoordinationRetentionDays", () => {
  it("keeps a whole number within 0 to 3650, 0 included", () => {
    expect(parseCoordinationRetentionDays("0")).toBe(0);
    expect(parseCoordinationRetentionDays(" 90 ")).toBe(90);
    expect(parseCoordinationRetentionDays(String(MAX_COORDINATION_RETENTION_DAYS))).toBe(3650);
  });

  it("refuses empty, out of bounds, decimals and text, never clamped", () => {
    for (const value of ["", "3651", "-1", "1.5", "abc"]) {
      expect(parseCoordinationRetentionDays(value)).toBeNull();
    }
  });

  it("defaults closed work items and resolved shared state to 90 days", () => {
    expect(COORDINATION_RETENTION_DEFAULTS.work_item_resolved_days).toBe(90);
    expect(COORDINATION_RETENTION_DEFAULTS.shared_state_resolved_days).toBe(90);
  });

  it("defaults finished assignment runs to 90 days", () => {
    expect(COORDINATION_RETENTION_DEFAULTS.assignment_run_days).toBe(90);
  });
});
