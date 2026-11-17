// humanizeCron in the viewer's language, with Japanese phrasing of its own
// for a daily or weekly time.

import { describe, expect, it } from "vitest";

import { humanizeCron } from "@/lib/cron";

describe("humanizeCron", () => {
  it("reads a daily time in Japanese as 毎日 HH:MM", () => {
    expect(humanizeCron("0 7 * * *", "ja-jp")).toBe("毎日 07:00");
  });

  it("reads a weekly time in Japanese as 毎週<weekday> HH:MM", () => {
    expect(humanizeCron("30 9 * * 1", "ja-jp")).toBe("毎週月曜日 09:30");
    // Sunday as 0 or 7, several days in week order, each named once.
    expect(humanizeCron("0 8 * * 5,1,7", "ja-jp")).toBe("毎週日曜日・月曜日・金曜日 08:00");
  });

  it("keeps cronstrue's Japanese wording for any other shape", () => {
    const phrase = humanizeCron("*/5 * * * *", "ja-jp");
    expect(phrase).not.toBe("*/5 * * * *");
    expect(phrase).not.toMatch(/^毎/);
    // A day of the month is not a daily or weekly time.
    expect(humanizeCron("0 7 1 * *", "ja-jp")).not.toMatch(/^毎日/);
  });

  it("leaves the other languages to cronstrue", () => {
    expect(humanizeCron("0 7 * * *", "fr-fr")).not.toBe(humanizeCron("0 7 * * *", "ja-jp"));
    expect(humanizeCron("0 7 * * *", "fr-fr")).toContain("07:00");
  });

  it("returns an empty string for no cron and the raw string for an invalid one", () => {
    expect(humanizeCron(null, "ja-jp")).toBe("");
    expect(humanizeCron("not a cron", "ja-jp")).toBe("not a cron");
  });
});
