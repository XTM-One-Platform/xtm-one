import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { NO_DATE_RANGE, hasDateRange, resolveDateRange } from "./date-range.ts";

const NOW = Date.parse("2026-10-20T14:35:00Z");

describe("a list page's time range", () => {
  it("counts a preset back from now, with no end", () => {
    assert.deepEqual(resolveDateRange({ preset: "7d", from: "", to: "" }, NOW), { date_from: "2026-10-13T14:35:00.000Z" });
    assert.deepEqual(resolveDateRange({ preset: "1h", from: "", to: "" }, NOW), { date_from: "2026-10-20T13:35:00.000Z" });
  });

  it("runs custom days from the first local midnight to the last millisecond of the last day", () => {
    const range = resolveDateRange({ preset: "", from: "2026-10-01", to: "2026-10-03" }, NOW);
    assert.equal(range.date_from, new Date(2026, 9, 1, 0, 0, 0, 0).toISOString());
    assert.equal(range.date_to, new Date(2026, 9, 3, 23, 59, 59, 999).toISOString());
    assert.deepEqual(Object.keys(resolveDateRange({ preset: "", from: "", to: "2026-10-03" }, NOW)), ["date_to"]);
  });

  it("narrows nothing without a range, or with a preset it does not know", () => {
    assert.deepEqual(resolveDateRange(NO_DATE_RANGE, NOW), {});
    assert.deepEqual(resolveDateRange({ preset: "toString", from: "", to: "" }, NOW), {});
    assert.equal(hasDateRange(NO_DATE_RANGE), false);
    assert.equal(hasDateRange({ preset: "toString", from: "", to: "" }), false);
    assert.equal(hasDateRange({ preset: "", from: "", to: "2026-10-03" }), true);
  });
});
