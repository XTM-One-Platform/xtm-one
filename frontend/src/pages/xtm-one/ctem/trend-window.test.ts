// D3-2 of the CTEM open-source program (#9618): the period of the stage score
// trend, read from the recorded days with no model.

import { createIntl } from "react-intl";
import { describe, expect, it } from "vitest";

import { TREND_PERIODS, formatTrendDay, historyWindow, periodChange, seriesChange } from "./trend-window";

const today = new Date("2026-11-07T09:00:00Z");
const day = (date: string, score: number | null = 50) => ({ date, posture_score: score });

describe("periodChange", () => {
  it("is the live score against the first recorded day, one figure for the hero and the band", () => {
    expect(periodChange(59, [null, 118, 90, 64])).toEqual({ start: 118, change: -59 });
  });

  it("is null without a live score or two recorded days", () => {
    expect(periodChange(null, [50, 60])).toBeNull();
    expect(periodChange(60, [null, 50])).toBeNull();
  });
});

describe("historyWindow", () => {
  it("keeps the recorded days of the last 30 days, boundary included, oldest first", () => {
    const history = [day("2026-08-01"), day("2026-10-08"), day("2026-10-09"), day("2026-11-07")];
    expect(historyWindow(history, 30, today).map((p) => p.date)).toEqual(["2026-10-08", "2026-10-09", "2026-11-07"]);
  });

  it("keeps every day of a 90 day history at 90", () => {
    const history = [day("2026-08-09"), day("2026-09-30"), day("2026-11-06")];
    expect(historyWindow(history, 90, today)).toHaveLength(3);
  });

  it("leaves out a day whose date cannot be read and a day after today", () => {
    const history = [day("not a date"), day("2026-11-01"), day("2026-11-08")];
    expect(historyWindow(history, 30, today).map((p) => p.date)).toEqual(["2026-11-01"]);
  });

  it("returns nothing for an empty history", () => {
    expect(historyWindow([], 30, today)).toEqual([]);
  });

  it("offers 30 and 90 days, the 90 the endpoint stores", () => {
    expect(TREND_PERIODS).toEqual([30, 90]);
  });
});

describe("formatTrendDay", () => {
  const fr = createIntl({ locale: "fr-fr", messages: {} });
  const en = createIntl({ locale: "en-us", messages: {} });

  it("writes the recorded day in the app language, never English inside French", () => {
    expect(formatTrendDay(fr, "2026-10-08")).toBe("8 oct.");
    expect(formatTrendDay(en, "2026-10-08")).toBe("Oct 8");
  });

  it("keeps the recorded day of a full timestamp, read in UTC", () => {
    expect(formatTrendDay(en, "2026-08-12T00:00:00+00:00")).toBe("Aug 12");
  });

  it("shows an unreadable date as it is", () => {
    expect(formatTrendDay(fr, "not a date")).toBe("not a date");
  });
});

describe("seriesChange", () => {
  it("is the last recorded value minus the first, from the first day with a value", () => {
    expect(seriesChange([null, 40, null, 52.6])).toEqual({ change: 13, fromIndex: 1 });
  });

  it("can be negative or zero", () => {
    expect(seriesChange([70, 61]).change).toBe(-9);
    expect(seriesChange([55, 55]).change).toBe(0);
  });

  it("is null with one recorded value: one day is no trend", () => {
    expect(seriesChange([null, 48, null])).toEqual({ change: null, fromIndex: 1 });
  });

  it("is null with no value, never 0", () => {
    expect(seriesChange([null, null])).toEqual({ change: null, fromIndex: null });
    expect(seriesChange([])).toEqual({ change: null, fromIndex: null });
  });
});
