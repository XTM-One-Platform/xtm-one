import { describe, expect, it } from "vitest";

import { HEALTH_MODULE_LABELS, moduleCardSubtitle } from "@/lib/health-modules";
import type { HealthModuleCard } from "@/types";

const t = (key: string, values?: Record<string, string | number>) =>
  key.replace(/\{(\w+)\}/g, (_, name: string) => String(values?.[name] ?? `{${name}}`));

const card = (over: Partial<HealthModuleCard> = {}): HealthModuleCard => ({
  module: "opencti",
  score: 75,
  critical: 1,
  warning: 0,
  info: 2,
  previous_score: null,
  ...over,
});

describe("Platform Health overview cards (#101 S8)", () => {
  it("says the open counts by severity and nothing about a week with no score", () => {
    expect(moduleCardSubtitle(card(), t)).toBe("1 critical, 0 warning, 2 info");
  });

  it("adds the signed change over 7 days when the score of a week ago is known", () => {
    expect(moduleCardSubtitle(card({ previous_score: 90 }), t)).toBe("1 critical, 0 warning, 2 info · -15 in 7 days");
    expect(moduleCardSubtitle(card({ previous_score: 60 }), t)).toBe("1 critical, 0 warning, 2 info · +15 in 7 days");
    expect(moduleCardSubtitle(card({ previous_score: 75 }), t)).toBe("1 critical, 0 warning, 2 info · unchanged in 7 days");
  });

  it("names every module of the engine with its product name", () => {
    expect(Object.keys(HEALTH_MODULE_LABELS).sort()).toEqual(["agents", "fleets", "openaev", "opencrq", "opencti", "xtm_one"]);
    expect(HEALTH_MODULE_LABELS.xtm_one).toBe("XTM One");
  });
});
