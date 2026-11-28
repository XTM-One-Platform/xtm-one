import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { createIntl } from "react-intl";
import { describe, expect, it } from "vitest";

import {
  HEALTH_CATEGORY_LABELS,
  HEALTH_CHECK_RECOMMENDATIONS,
  HEALTH_CHECK_TITLES,
  HEALTH_MODULE_LABELS,
  checkRecommendation,
  checkTitle,
  evidenceLabel,
  evidenceTime,
  moduleCardSubtitle,
  periodLabel,
  productHealth,
  productPillTone,
} from "@/lib/health-modules";
import type { HealthModuleCard, HealthOverview } from "@/types";

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

/**
 * The evidence keys a check writes through a dict built elsewhere (the quota usage rows, the
 * OpenCTI broker snapshot, an OpenAEV healthcheck's status counts, a certificate refused, the
 * engine's out-of-scope close-out): the literal scan below cannot see them.
 */
const INDIRECT_EVIDENCE_KEYS = ["period", "used", "limit", "consumers", "queue_messages", "ERROR", "WARNING", "error", "expires_at", "in_scope"];

/** The check ids, categories and evidence keys the backend checks record (backend/app/services/health/*_checks.py). */
function backendChecks(): { ids: Set<string>; categories: Set<string>; evidenceKeys: Set<string> } {
  // vitest runs from frontend/: the backend checks sit beside it.
  const dir = join(process.cwd(), "..", "backend", "app", "services", "health");
  const ids = new Set<string>();
  const categories = new Set<string>();
  const evidenceKeys = new Set<string>(INDIRECT_EVIDENCE_KEYS);
  for (const name of readdirSync(dir).filter((file) => file.endsWith("_checks.py"))) {
    const source = readFileSync(join(dir, name), "utf8");
    for (const match of source.matchAll(/"((?:agents|fleets|xtm_one|opencti|openaev|opencrq)\.[a-z_]+)"/g)) ids.add(match[1]);
    for (const match of source.matchAll(/"(?:critical|warning|info)",\s*"([a-z_]+)"/g)) categories.add(match[1]);
    // Every `"values": {...}` literal of an evidence, its keys only.
    for (const block of source.matchAll(/"values":\s*\{([^{}]*)\}/g)) {
      for (const key of block[1].matchAll(/"([a-z_]+)":/g)) evidenceKeys.add(key[1]);
    }
  }
  return { ids, categories, evidenceKeys };
}

describe("Platform Health evidence times (UX round 2 B1 on #2724)", () => {
  const fr = createIntl({ locale: "fr-fr", messages: {}, timeZone: "UTC" });
  const en = createIntl({ locale: "en-us", messages: {}, timeZone: "UTC" });

  it("draws a time in the app language, as the Last seen column does", () => {
    expect(evidenceTime("2026-11-20T09:00:00Z", fr, t)).toBe("20 nov., 09:00");
    expect(evidenceTime("2026-11-20T09:00:00Z", en, t)).toContain("Nov 20");
  });

  it("reads Never for a missing time", () => {
    expect(evidenceTime(null, fr, (key) => `t:${key}`)).toBe("t:Never");
    expect(evidenceTime("", fr, (key) => `t:${key}`)).toBe("t:Never");
  });
});

describe("Platform Health labels (UX round 1 B2 on #634)", () => {
  const { ids, categories } = backendChecks();

  it("gives every check the engine registers a title", () => {
    expect(ids.size).toBeGreaterThan(30);
    expect([...ids].filter((id) => !Object.prototype.hasOwnProperty.call(HEALTH_CHECK_TITLES, id))).toEqual([]);
  });

  it("gives every check a recommendation to translate (UX round 2 B2 on #634)", () => {
    expect([...ids].filter((id) => !Object.prototype.hasOwnProperty.call(HEALTH_CHECK_RECOMMENDATIONS, id))).toEqual([]);
    expect(checkRecommendation("xtm_one.smtp", "backend text", (key) => `t:${key}`)).toBe(
      "t:Set SMTP_HOSTNAME and its credentials so the platform can send email.",
    );
    // A check newer than this build keeps the text the backend wrote.
    expect(checkRecommendation("opencti.brand_new_check", "backend text", (key) => `t:${key}`)).toBe("backend text");
  });

  it("gives every evidence value a check writes a label (UX round 2 B2 on #634)", () => {
    const { evidenceKeys } = backendChecks();
    expect(evidenceKeys.size).toBeGreaterThan(40);
    const labelled = (key: string) => evidenceLabel(key, (text) => `t:${text}`).startsWith("t:");
    expect([...evidenceKeys].filter((key) => !labelled(key))).toEqual([]);
  });

  it("names a quota period in words, never its raw value", () => {
    expect(periodLabel("month", (key) => key)).toBe("Monthly");
    expect(periodLabel("daily", (key) => key)).toBe("Daily");
    expect(periodLabel("fortnight", (key) => key)).toBe("fortnight");
  });

  it("gives every category a label", () => {
    expect(categories.size).toBeGreaterThan(3);
    expect([...categories].filter((category) => !Object.prototype.hasOwnProperty.call(HEALTH_CATEGORY_LABELS, category))).toEqual([]);
  });

  it("feeds a product pill from its module card, and nothing without one (#101 S8b)", () => {
    const overview: HealthOverview = { modules: [card({ module: "opencti", critical: 2, warning: 1 })], overall: 42, scope: "all" };
    expect(productHealth("opencti", overview)).toEqual({ critical: 2, warning: 1 });
    expect(productHealth("openaev", overview)).toBeUndefined();
    expect(productHealth("xtm-hub", overview)).toBeUndefined();
    expect(productHealth("opencti", undefined)).toBeUndefined();
  });

  it("tones a pill red when unreachable or critical, amber on a warning, green otherwise", () => {
    expect(productPillTone(false, undefined)).toBe("error");
    expect(productPillTone(true, { critical: 1, warning: 0 })).toBe("error");
    expect(productPillTone(true, { critical: 0, warning: 3 })).toBe("warning");
    expect(productPillTone(true, { critical: 0, warning: 0 })).toBe("success");
    expect(productPillTone(true, undefined)).toBe("success");
  });

  it("never shows a raw key, even for a check newer than this build", () => {
    expect(checkTitle("agents.assignment_failing", (key) => key)).toBe("Assignment failing");
    expect(checkTitle("opencti.brand_new_check", (key) => key)).toBe("Brand new check");
    expect(evidenceLabel("window_hours", (key) => key)).toBe("Window (hours)");
    expect(evidenceLabel("some_value", (key) => key)).toBe("Some value");
  });

  it("titles a Module Analyst finding by its slug, never 'Agentic.' (UX round 1 B2 on #953)", () => {
    expect(checkTitle("opencti.agentic.stalled_connector", (key) => key)).toBe("Stalled connector");
    expect(checkTitle("openaev.agentic.injector_down", (key) => key)).toBe("Injector down");
  });
});
