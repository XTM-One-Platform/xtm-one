// `timeAgo` in the reader's language (`lib/utils.ts` imports through `@/`, so vitest runs it).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { timeAgo } from "@/lib/utils";

const NOW = new Date("2026-10-26T12:00:00Z");
const before = (minutes: number) => new Date(NOW.getTime() - minutes * 60_000).toISOString();

describe("timeAgo", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it("keeps its English short form without a locale, or in English", () => {
    expect([timeAgo(before(0)), timeAgo(before(6)), timeAgo(before(180)), timeAgo(before(3 * 1440), "en")]).toEqual(["just now", "6m ago", "3h ago", "3d ago"]);
  });

  it("writes the same buckets in the reader's language", () => {
    // ICU parts its words with non-breaking spaces.
    const plain = (text: string) => text.replace(/\s/g, " ");
    expect([timeAgo(before(6), "fr"), timeAgo(before(180), "fr")].map(plain)).toEqual(["il y a 6 min", "il y a 3 h"]);
    expect(plain(timeAgo(before(6), "ja"))).toBe("6 分前");
  });
});
