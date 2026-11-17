/** `yarn test:unit`: CTEM fact values on the compact scale, exact on hover (#10476). */
import { describe, expect, it } from "vitest";

import { exactMetricValue, formatMetricValue } from "@/lib/utils";

describe("formatMetricValue", () => {
  it("rounds small figures and compacts from 1,000", () => {
    expect(formatMetricValue(5.751765893037336)).toBe("6");
    expect(formatMetricValue(999)).toBe("999");
    expect(formatMetricValue(620965)).toBe("621K");
    expect(formatMetricValue("1500")).toBe("1.5K");
    expect(formatMetricValue(999_950)).toBe("1M");
  });

  it("keeps booleans, text and missing values as before", () => {
    expect(formatMetricValue(true)).toBe("Yes");
    expect(formatMetricValue("n/a")).toBe("n/a");
    expect(formatMetricValue(null)).toBe("--");
  });
});

describe("exactMetricValue", () => {
  it("gives the exact figure only when the drawn one is compact", () => {
    expect(exactMetricValue(620965)).toBe("620,965");
    expect(exactMetricValue(999)).toBeUndefined();
    expect(exactMetricValue(true)).toBeUndefined();
    expect(exactMetricValue("n/a")).toBeUndefined();
    expect(exactMetricValue(null)).toBeUndefined();
  });
});
