// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import {
  CASE_QUICK_FILTER_LABEL,
  applyCaseQuickFilters,
  caseQuickFilterCount,
  caseWaitFacet,
  isStalledCase,
  readCaseScope,
  readCaseToggles,
  writeCaseToggles,
} from "./cases-quick-filters";

const NOW = Date.parse("2026-11-23T12:00:00Z");
const DAY = 86_400_000;

type Kase = Parameters<typeof applyCaseQuickFilters>[0][number] & { id: string };

function kase(over: Partial<Kase> & { id: string }): Kase {
  return {
    state: "new",
    owner_user_id: null,
    due_at: null,
    accepted_until: null,
    priority: "medium",
    sla_breached_at: null,
    ...over,
  } as Kase;
}

const CASES: Kase[] = [
  kase({ id: "a", state: "waiting_decision", priority: "critical", owner_user_id: "me" }),
  kase({ id: "b", state: "remediating", priority: "high", due_at: new Date(NOW - DAY).toISOString() }),
  kase({ id: "c", state: "analyzing", priority: "low", owner_user_id: "other" }),
  kase({ id: "d", state: "resolved", priority: "critical" }),
  kase({ id: "e", state: "accepted_risk", priority: "medium" }),
  kase({ id: "f", state: "mitigated_watching", priority: "high" }),
];

const ids = (list: Kase[]) => list.map((c) => c.id);

describe("cases quick filters (#1503)", () => {
  it("lands on every open case, a case waiting on a person included", () => {
    expect(readCaseScope(null)).toBe("open");
    expect(ids(applyCaseQuickFilters(CASES, "open", [], NOW, "me"))).toEqual(["a", "b", "c"]);
  });

  it("reads a first-model state of a saved link as the open scope", () => {
    expect(readCaseScope("needs_attention")).toBe("open");
    expect(readCaseScope("in_progress")).toBe("open");
    expect(readCaseScope("bogus")).toBe("open");
    expect(readCaseScope("remediating")).toBe("remediating");
    expect(readCaseScope("closed")).toBe("closed");
  });

  it("keeps the closed cases, resolved and accepted risk, apart from the watched ones", () => {
    expect(ids(applyCaseQuickFilters(CASES, "closed", [], NOW, "me"))).toEqual(["d", "e"]);
    expect(ids(applyCaseQuickFilters(CASES, "all", [], NOW, "me"))).toHaveLength(6);
  });

  it("combines the toggles with the scope and with each other", () => {
    expect(ids(applyCaseQuickFilters(CASES, "open", ["critical_high"], NOW, "me"))).toEqual(["a", "b"]);
    expect(ids(applyCaseQuickFilters(CASES, "open", ["critical_high", "unassigned"], NOW, "me"))).toEqual(["b"]);
    expect(ids(applyCaseQuickFilters(CASES, "open", ["mine"], NOW, "me"))).toEqual(["a"]);
    expect(ids(applyCaseQuickFilters(CASES, "open", ["overdue"], NOW, "me"))).toEqual(["b"]);
    expect(ids(applyCaseQuickFilters(CASES, "open", ["mine"], NOW, null))).toEqual([]);
  });

  it("reads an open case the sweep found waiting on nobody as stalled, never a closed one (#1736)", () => {
    const at = new Date(NOW - DAY).toISOString();
    expect(isStalledCase({ state: "analyzing", stalled_since: at })).toBe(true);
    expect(isStalledCase({ state: "resolved", stalled_since: at })).toBe(false);
    expect(isStalledCase({ state: "analyzing", stalled_since: null })).toBe(false);
    expect(caseWaitFacet({ state: "analyzing", stalled_since: at, waiting_on: null })).toEqual(["nobody"]);
    expect(caseWaitFacet({ state: "waiting_decision", stalled_since: null, waiting_on: { gate: "g", by: null, since: at } })).toEqual(["person"]);
    expect(caseWaitFacet({ state: "remediating", stalled_since: null, waiting_on: null })).toEqual([]);
  });

  it("counts on each chip what one click on it shows", () => {
    expect(caseQuickFilterCount("waiting_decision", CASES, "open", [], NOW, "me")).toBe(1);
    expect(caseQuickFilterCount("closed", CASES, "open", ["critical_high"], NOW, "me")).toBe(1);
    expect(caseQuickFilterCount("unassigned", CASES, "open", ["critical_high"], NOW, "me")).toBe(1);
    expect(caseQuickFilterCount("critical_high", CASES, "open", ["critical_high"], NOW, "me")).toBe(2);
  });

  it("labels the open and closed scopes as the cases they hold, never the verb buttons use", () => {
    const catalogs = { en, es, fr, ja } as Record<string, Record<string, string>>;
    for (const scope of ["open", "closed"] as const) {
      const key = CASE_QUICK_FILTER_LABEL[scope];
      expect(key).not.toBe("Open");
      expect(key).not.toBe("Closed");
      for (const [locale, catalog] of Object.entries(catalogs)) {
        expect(catalog[key], `${locale} has no "${key}"`).toBeTruthy();
        if (locale !== "en") expect(catalog[key], `${locale} reads the ${scope} scope as the verb`).not.toBe(catalog.Open);
      }
    }
    expect(fr["Open (case scope)"]).toBe("Ouverts");
    expect(fr["Closed (case scope)"]).toBe("Fermés");
  });

  it("keeps the chip labels of every locale within one toolbar row at 1080 px", () => {
    // Width budget in Latin-character units, a CJK glyph counting two: FR at 87
    // units wrapped the toggles to a second row at 1080 px (UX round 3, #1515),
    // EN at 71 and ES at 74 fit.
    const BUDGET = 76;
    const width = (text: string) => [...text].reduce((sum, ch) => sum + (/[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 2 : 1), 0);
    const catalogs = { en, es, fr, ja } as Record<string, Record<string, string>>;
    for (const [locale, catalog] of Object.entries(catalogs)) {
      const total = Object.values(CASE_QUICK_FILTER_LABEL).reduce((sum, key) => sum + width(catalog[key] ?? key), 0);
      expect(total, `${locale} chip labels take ${total} units`).toBeLessThanOrEqual(BUDGET);
    }
    expect(fr[CASE_QUICK_FILTER_LABEL.waiting_decision]).toBe("En attente");
    expect(fr["Waiting for decision"]).toBe("En attente de décision");
  });

  it("writes the toggles in chip order and reads unknown ones as absent", () => {
    expect(writeCaseToggles(["overdue", "mine"])).toBe("mine,overdue");
    expect(readCaseToggles("overdue,nope,mine")).toEqual(["mine", "overdue"]);
    expect(readCaseToggles(null)).toEqual([]);
  });
});
