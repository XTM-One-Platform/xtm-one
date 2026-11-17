// D3-3 of the CTEM open-source program (#9459): the rule-based next actions of
// the Recommended actions card, read from the posture with no model.

import { describe, expect, it } from "vitest";

import type { CtemPosture, CtemStage } from "@/types";
import en from "@/i18n/lang/en.json";
import { CTEM_STAGE_CONFIG } from "@/lib/constants";
import {
  EXPECTATION_REASON_FALLBACK, EXPECTATION_REASONS, NEXT_ACTION_PRIORITY, deterministicNextActions, rankNextActions,
} from "./next-actions";

function stage(key: string, over: Partial<CtemStage> = {}): CtemStage {
  return {
    key, label: key[0].toUpperCase() + key.slice(1), score: null, prev_score: null, band: "unknown",
    covered: true, confidence: "high", primary_products: ["opencti"], secondary_products: [],
    missing_products: [], products_present: ["opencti"], methodology: "", facts: [], viz: {}, ...over,
  };
}

function posture(stages: CtemStage[], openaev = true): Pick<CtemPosture, "stages" | "products"> {
  return { stages, products: { opencti: { present: true }, openaev: { present: openaev } } };
}

const buckets = {
  PREVENTION: { SUCCESS: 10, FAILED: 2, PARTIAL: 1 },
  DETECTION: { SUCCESS: 3, FAILED: 0, PARTIAL: 0 },
  VULNERABILITY: { FAILED: 7 },
  HUMAN_RESPONSE: { PARTIAL: 3 },
  SOME_NEW_TYPE: { FAILED: 1 },
};

describe("deterministicNextActions", () => {
  it("answers nothing without a posture", () => {
    expect(deterministicNextActions(null)).toEqual([]);
  });

  it("lists every failed expectation type, the most failures first, Vulnerability and Human response included", () => {
    const actions = deterministicNextActions(posture([stage("validation", { viz: { expectation_buckets: buckets } })]));
    expect(actions).toEqual([
      { kind: "expectation", type: "VULNERABILITY", failed: 7 },
      { kind: "expectation", type: "HUMAN_RESPONSE", failed: 3 },
      { kind: "expectation", type: "PREVENTION", failed: 3 },
      { kind: "expectation", type: "SOME_NEW_TYPE", failed: 1 },
    ]);
  });

  it("never reads expectations while OpenAEV is not connected", () => {
    const actions = deterministicNextActions(posture([stage("validation", { viz: { expectation_buckets: buckets } })], false));
    expect(actions).toEqual([]);
  });

  it("names a stage whose source is connected but which gets no data, after the expectations", () => {
    const actions = deterministicNextActions(posture([
      stage("scoping", { covered: false, products_present: ["opencti"] }),
      stage("validation", { viz: { expectation_buckets: { DETECTION: { FAILED: 4 } } } }),
      stage("remediation", { covered: false, products_present: [], missing_products: ["openaev"] }),
    ]));
    expect(actions).toEqual([
      { kind: "expectation", type: "DETECTION", failed: 4 },
      { kind: "stage_no_data", stage: "scoping", label: "Scoping", products: ["opencti"] },
    ]);
  });
});

describe("rankNextActions", () => {
  // The card's other rule rows: KEV past triage 95, detection coverage gap 80.
  const KEV = 95;
  const COVERAGE_GAP = 80;

  it("ranks every row under KEV and above the coverage gap, expectations before stages with no data", () => {
    const ranked = rankNextActions(deterministicNextActions(posture([
      stage("scoping", { covered: false }),
      stage("validation", { viz: { expectation_buckets: buckets } }),
      stage("remediation", { covered: false }),
    ])));
    expect(ranked.map((a) => a.kind)).toEqual([
      "expectation", "expectation", "expectation", "expectation", "stage_no_data", "stage_no_data",
    ]);
    for (const a of ranked) {
      expect(a.priority).toBeLessThan(KEV);
      expect(a.priority).toBeGreaterThan(COVERAGE_GAP);
    }
    const priorities = ranked.map((a) => a.priority);
    expect(priorities).toEqual([...priorities].sort((x, y) => y - x));
  });

  it("keeps a stage with no data inside the six-row cap beside four failed types and KEV", () => {
    const rows = [
      { id: "kev", priority: KEV },
      { id: "coverage", priority: COVERAGE_GAP },
      ...rankNextActions(deterministicNextActions(posture([
        stage("validation", { viz: { expectation_buckets: buckets } }),
        stage("remediation", { covered: false }),
      ]))).map((a) => ({ id: a.kind === "stage_no_data" ? `no-data-${a.stage}` : a.type, priority: a.priority })),
    ];
    const shown = rows.sort((a, b) => b.priority - a.priority).slice(0, 6).map((r) => r.id);
    expect(shown).toContain("no-data-remediation");
    expect(shown.indexOf("no-data-remediation")).toBe(5);
  });

  it("never lets many rows of one kind reach the next band", () => {
    const many = Array.from({ length: 500 }, (_, i) => ({ kind: "stage_no_data" as const, stage: `s${i}`, label: "", products: [] }));
    expect(Math.min(...rankNextActions(many).map((a) => a.priority))).toBeGreaterThan(COVERAGE_GAP);
    expect(NEXT_ACTION_PRIORITY.expectation).toBeGreaterThan(NEXT_ACTION_PRIORITY.stage_no_data);
  });
});

describe("expectation reasons", () => {
  it("translates every reason and the fallback", () => {
    const catalog = en as Record<string, string>;
    for (const reason of [...Object.values(EXPECTATION_REASONS), EXPECTATION_REASON_FALLBACK]) {
      expect(catalog[reason], reason).toBeDefined();
    }
    expect(catalog["Human response"]).toBeDefined();
    expect(catalog["No data for the {stage} stage"]).toBeDefined();
  });

  it("translates every stage name the rows pass as {stage}", () => {
    const catalog = en as Record<string, string>;
    for (const cfg of Object.values(CTEM_STAGE_CONFIG)) expect(catalog[cfg.label], cfg.label).toBeDefined();
  });
});
