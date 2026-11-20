import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  HEALTH_CATEGORY_LABELS,
  HEALTH_CHECK_TITLES,
  HEALTH_MODULE_LABELS,
  checkTitle,
  evidenceLabel,
  moduleCardSubtitle,
} from "@/lib/health-modules";
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

/** The check ids and categories the backend checks record (backend/app/services/health/*_checks.py). */
function backendChecks(): { ids: Set<string>; categories: Set<string> } {
  // vitest runs from frontend/: the backend checks sit beside it.
  const dir = join(process.cwd(), "..", "backend", "app", "services", "health");
  const ids = new Set<string>();
  const categories = new Set<string>();
  for (const name of readdirSync(dir).filter((file) => file.endsWith("_checks.py"))) {
    const source = readFileSync(join(dir, name), "utf8");
    for (const match of source.matchAll(/"((?:agents|fleets|xtm_one|opencti|openaev|opencrq)\.[a-z_]+)"/g)) ids.add(match[1]);
    for (const match of source.matchAll(/"(?:critical|warning|info)",\s*"([a-z_]+)"/g)) categories.add(match[1]);
  }
  return { ids, categories };
}

describe("Platform Health labels (UX round 1 B2 on #634)", () => {
  const { ids, categories } = backendChecks();

  it("gives every check the engine registers a title", () => {
    expect(ids.size).toBeGreaterThan(30);
    expect([...ids].filter((id) => !Object.prototype.hasOwnProperty.call(HEALTH_CHECK_TITLES, id))).toEqual([]);
  });

  it("gives every category a label", () => {
    expect(categories.size).toBeGreaterThan(3);
    expect([...categories].filter((category) => !Object.prototype.hasOwnProperty.call(HEALTH_CATEGORY_LABELS, category))).toEqual([]);
  });

  it("never shows a raw key, even for a check newer than this build", () => {
    expect(checkTitle("agents.assignment_failing", (key) => key)).toBe("Assignment failing");
    expect(checkTitle("opencti.brand_new_check", (key) => key)).toBe("Brand new check");
    expect(evidenceLabel("window_hours", (key) => key)).toBe("Window (hours)");
    expect(evidenceLabel("some_value", (key) => key)).toBe("Some value");
  });
});
