import { describe, expect, it } from "vitest";
import { createIntl } from "react-intl";

import { formatShortDateTimeIntl, formatTimeAgo, formatTimeAgoLine, relativeTimeParts } from "./relative-time";

const NOW = Date.parse("2026-11-03T10:00:00Z");
const fr = createIntl({ locale: "fr-fr", messages: {}, timeZone: "UTC" });
const en = createIntl({ locale: "en-us", messages: {}, timeZone: "UTC" });

describe("relative time in the app language", () => {
  it("reads now for a timestamp a few seconds ahead or behind", () => {
    expect(relativeTimeParts(NOW + 5_000, NOW)).toEqual({ value: 0, unit: "second" });
    expect(relativeTimeParts(Number.NaN, NOW)).toBeNull();
  });

  it("says how long ago in French and in English", () => {
    expect(formatTimeAgo(fr, "2026-11-03T09:54:00Z", NOW)).toBe("il y a 6 minutes");
    expect(formatTimeAgo(en, "2026-11-03T09:54:00Z", NOW)).toBe("6 minutes ago");
    expect(formatTimeAgo(en, "2026-11-01T10:00:00Z", NOW)).toBe("2 days ago");
  });

  it("keeps a value that is not a date as written", () => {
    expect(formatTimeAgo(en, "soon", NOW)).toBe("soon");
    expect(formatShortDateTimeIntl(en, "soon")).toBe("soon");
  });

  it("wraps the relative time in its sentence, with its own just-now sentence", () => {
    const opts = { line: (when: string) => `Checked ${when}`, justNow: () => "Checked just now", fallback: "later" };
    expect(formatTimeAgoLine(en, Date.parse("2026-11-03T09:54:00Z"), opts, NOW)).toBe("Checked 6 minutes ago");
    expect(formatTimeAgoLine(en, NOW - 10_000, opts, NOW)).toBe("Checked just now");
    expect(formatTimeAgoLine(en, Number.NaN, opts, NOW)).toBe("Checked later");
  });

  it("formats a short date and time in the app language", () => {
    expect(formatShortDateTimeIntl(fr, "2026-11-04T08:30:00Z")).toBe("4 nov., 08:30");
  });
});
