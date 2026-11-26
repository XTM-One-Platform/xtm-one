// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { ACCEPTANCE_MAX_DAYS, acceptanceMaxDay, utcDayIn } from "./case-acceptance";

describe("acceptanceMaxDay (#2490)", () => {
  it("is the UTC day 90 days ahead, the backend default", () => {
    expect(ACCEPTANCE_MAX_DAYS).toBe(90);
    expect(acceptanceMaxDay(Date.parse("2026-11-26T08:00:00Z"))).toBe("2027-02-24");
  });

  it("is the same day at the first and the last minute of a UTC day", () => {
    expect(acceptanceMaxDay(Date.parse("2026-11-26T00:01:00Z"))).toBe("2027-02-24");
    expect(acceptanceMaxDay(Date.parse("2026-11-26T23:59:00Z"))).toBe("2027-02-24");
  });

  it("counts days in UTC", () => {
    expect(utcDayIn(1, Date.parse("2026-12-31T23:30:00Z"))).toBe("2027-01-01");
  });
});
