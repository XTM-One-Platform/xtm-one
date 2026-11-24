// D3-5 of the CTEM open-source program (#9477): the posture as a CSV file,
// built from the posture payload alone, written with the labels the page shows.

import { describe, expect, it } from "vitest";

import type { CtemPosture, CtemStage } from "@/types";
import en from "@/i18n/lang/en.json";
import fr from "@/i18n/lang/fr.json";
import es from "@/i18n/lang/es.json";
import ja from "@/i18n/lang/ja.json";
import { HEADLINE_LABELS, postureCsv, postureCsvFilename, postureCsvRows, type Translate } from "./posture-export";

const HEADERS = { section: "Section", stage: "Stage", metric: "Metric", value: "Value", unit: "Unit", key: "Key" };
/** English: the catalog key is its own text. */
const t: Translate = (key) => key;
const tFr: Translate = (key) => (fr as Record<string, string>)[key] ?? key;

function stage(key: string, over: Partial<CtemStage> = {}): CtemStage {
  return {
    key,
    label: key,
    score: 60,
    prev_score: null,
    band: "fair",
    covered: true,
    confidence: "high",
    primary_products: ["opencti"],
    secondary_products: [],
    missing_products: [],
    products_present: ["opencti"],
    methodology: "",
    facts: [],
    viz: {},
    ...over,
  };
}

function posture(over: Partial<CtemPosture> = {}): CtemPosture {
  return {
    posture_score: 58,
    posture_band: "fair",
    covered_stages: 2,
    total_stages: 5,
    weights: {},
    stages: [
      stage("discovery", { facts: [{ label: "CVEs tracked", value: 120, unit: "", kind: "fact" }] }),
      stage("validation", { score: null, band: "unknown", covered: false, confidence: "none", products_present: ["openaev", "opencti"] }),
    ],
    products: {},
    trend: [],
    blind_spots: [],
    headline: { kev_count: 4, critical_count: 12, vulnerabilities_total: null, detection_coverage_pct: 37, prevention_success_pct: null, detection_success_pct: null, exercised_coverage_pct: null },
    last_sync: {},
    generated_at: "2026-11-07T04:00:00Z",
    ...over,
  };
}

describe("postureCsvRows", () => {
  it("writes the posture, each stage with its facts, then the headline KPIs", () => {
    const rows = postureCsvRows(posture(), t);
    expect(rows.map((r) => r.section)).toEqual([
      "Exposure posture", "Exposure posture", "Exposure posture", "Exposure posture", "Exposure posture",
      "Stage", "Stage", "Stage", "Stage", "Stage", "Fact",
      "Stage", "Stage", "Stage", "Stage", "Stage",
      "Key indicators", "Key indicators", "Key indicators",
    ]);
    expect(rows[0]).toEqual({ section: "Exposure posture", stage: "", metric: "Posture score", value: 58, unit: "/100", key: "posture.score" });
    expect(rows.find((r) => r.section === "Fact")).toEqual({ section: "Fact", stage: "Discovery", metric: "CVEs tracked", value: 120, unit: "", key: "fact.discovery.CVEs tracked" });
    expect(rows.find((r) => r.key === "stage.validation.products_present")?.value).toBe("OpenAEV, OpenCTI");
  });

  it("writes display labels, never an internal key, in the Section, Stage, Metric and Value columns", () => {
    const rows = postureCsvRows(posture(), t);
    const internal = /^(posture|stage|fact|headline|scoping|discovery|validation|covered_stages|kev_count|true|false|fair|unknown|none)$|_/;
    for (const row of rows.filter((r) => r.section !== "Fact")) {
      for (const cell of [row.section, row.stage, row.metric, typeof row.value === "string" && !row.key.endsWith("generated_at") ? row.value : ""]) {
        expect(cell, `${row.key}: ${cell}`).not.toMatch(internal);
      }
    }
    const covered = rows.filter((r) => r.key.endsWith(".covered")).map((r) => r.value);
    expect(covered).toEqual(["Yes", "No"]);
    expect(rows.find((r) => r.key === "stage.validation.band")?.value).toBe("No data");
    expect(rows.find((r) => r.key === "headline.kev_count")?.metric).toBe("Known-exploited");
  });

  it("follows the UI language", () => {
    const rows = postureCsvRows(posture(), tFr);
    expect(rows[0].section).toBe("Posture d'exposition");
    expect(rows[0].metric).toBe("Score de posture");
    expect(rows.find((r) => r.key === "stage.discovery.covered")?.value).toBe("Oui");
    expect(rows.find((r) => r.key === "stage.discovery.score")?.stage).toBe(fr["Discovery" as keyof typeof fr]);
  });

  it("leaves out a headline KPI the posture could not compute", () => {
    const keys = postureCsvRows(posture(), t).filter((r) => r.key.startsWith("headline.")).map((r) => r.key);
    expect(keys).toEqual(["headline.kev_count", "headline.critical_count", "headline.detection_coverage_pct"]);
  });

  it("writes the tailored KEV products as one cell and leaves out an empty list", () => {
    const base = posture();
    const tailored = postureCsvRows(posture({ headline: { ...base.headline, kev_tailored_count: 3, kev_tailored_products: ["Fortinet FortiOS", "Microsoft Exchange"] } }), t);
    expect(tailored.find((r) => r.key === "headline.kev_tailored_products")?.value).toBe("Fortinet FortiOS, Microsoft Exchange");
    expect(tailored.find((r) => r.key === "headline.kev_tailored_count")?.metric).toBe("Known-exploited in your software");
    const empty = postureCsvRows(posture({ headline: { ...base.headline, kev_tailored_count: 0, kev_tailored_products: [] } }), t);
    expect(empty.some((r) => r.key === "headline.kev_tailored_products")).toBe(false);
  });

  it("marks percentage KPIs with a % unit", () => {
    const row = postureCsvRows(posture(), t).find((r) => r.key === "headline.detection_coverage_pct");
    expect(row?.unit).toBe("%");
  });

  it("keeps a stage with no score as an empty cell with no unit, never 0", () => {
    const csv = postureCsv(posture(), HEADERS, t);
    expect(csv).toContain("Stage,Validation,Stage score,,,stage.validation.score\r\n");
  });

  it("omits the build time row when the posture carries none", () => {
    const rows = postureCsvRows(posture({ generated_at: undefined }), t);
    expect(rows.some((r) => r.key === "posture.generated_at")).toBe(false);
  });
});

describe("postureCsv", () => {
  it("starts with the translated header line and ends every line with CRLF", () => {
    const csv = postureCsv(posture(), HEADERS, t);
    expect(csv.startsWith("Section,Stage,Metric,Value,Unit,Key\r\n")).toBe(true);
    expect(csv.endsWith("\r\n")).toBe(true);
  });

  it("guards a fact label a spreadsheet would read as a formula", () => {
    const p = posture({ stages: [stage("scoping", { facts: [{ label: "=HYPERLINK(1)", value: 1, unit: "", kind: "fact" }] })] });
    expect(postureCsv(p, HEADERS, t)).toContain("Fact,Scoping,'=HYPERLINK(1),1,,fact.scoping.=HYPERLINK(1)\r\n");
  });
});

describe("postureCsvFilename", () => {
  it("names the file after the vertical and the day", () => {
    expect(postureCsvFilename("cyber", new Date("2026-11-07T10:00:00Z"))).toBe("ctem-posture-cyber-2026-11-07.csv");
  });
});

describe("catalog", () => {
  it("translates the button, its tooltips, the headers and every label the file writes, in every language", () => {
    const keys = [
      "Export CSV",
      "Download the exposure posture, its stages and KPIs as a CSV file",
      "Available once the posture is computed from a connected platform",
      "Section", "Stage", "Metric", "Value", "Unit", "Key",
      "Exposure posture", "Fact", "Key indicators", "Posture score", "Band", "Stages covered", "Total stages",
      "Built at", "Stage score", "Covered", "Confidence", "Products present", "Yes", "No",
      "High", "Moderate", "Low", "None", "Critical", "At risk", "Fair", "Good", "Strong", "No data",
      ...Object.values(HEADLINE_LABELS),
    ];
    for (const catalog of [en, fr, es, ja] as Record<string, string>[]) {
      for (const key of keys) expect(catalog[key], key).toBeDefined();
    }
  });
});

