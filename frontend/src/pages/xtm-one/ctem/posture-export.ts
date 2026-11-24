// SPDX-License-Identifier: Apache-2.0

// D3-5 of the CTEM open-source program (#9477, `dev-docs/ctem-open-source.md`
// section 7): the posture as a CSV file, for reporting without AI. Built from
// the posture payload the command center already holds: no model, no endpoint.
// Long form, one value per row, so a spreadsheet pivots it whatever the
// vertical and however many facts a stage carries.
//
// The Section, Stage and Metric columns carry the labels the page shows, in
// the UI language (UX round 1 on #9478, B3): a first-time administrator reads
// "Posture score" and "Discovery", never `posture` or `covered_stages`. The
// last column, Key, keeps a stable id per metric so a spreadsheet built on one
// export still matches the next one, whatever the language.

import { csvFilename, toCsv, type CsvColumn, type CsvValue } from "@/lib/csv";
import { CTEM_BAND_CONFIG, CTEM_PRODUCT_INFO, CTEM_STAGE_CONFIG, type CtemStageKey } from "@/lib/constants";
import type { CtemHeadline, CtemPosture, CtemStage, CtemVertical } from "@/types";

/** Translates a catalog key (the `t_i18n` of the caller). */
export type Translate = (key: string) => string;

export interface PostureCsvRow {
  section: string;
  /** The stage label, empty for the posture and headline rows. */
  stage: string;
  metric: string;
  value: CsvValue;
  unit: string;
  /** Stable id of the metric, the same in every language. */
  key: string;
}

/** The column headers, translated by the caller (the file follows the UI language). */
export interface PostureCsvHeaders {
  section: string;
  stage: string;
  metric: string;
  value: string;
  unit: string;
  key: string;
}

type ExportablePosture = Pick<
  CtemPosture,
  "posture_score" | "posture_band" | "covered_stages" | "total_stages" | "stages" | "headline" | "generated_at"
>;

/** The label of each headline KPI: the label its KPI card shows (`VERTICAL_KPIS`
 *  and `LOCKED_KPIS`), so the file and the strip use one word for one metric. */
export const HEADLINE_LABELS: Record<keyof CtemHeadline, string> = {
  kev_count: "Known-exploited",
  kev_tailored_count: "Known-exploited in your software",
  kev_tailored_products: "Software affected",
  critical_count: "Critical CVEs",
  vulnerabilities_total: "Vulnerabilities",
  detection_coverage_pct: "Detection coverage",
  prevention_success_pct: "Prevention success",
  detection_success_pct: "Detection success",
  exercised_coverage_pct: "ATT&CK exercised",
  validation_success_pct: "Validation success",
  narratives_total: "Narratives tracked",
  channels_total: "Channels monitored",
  attribution_pct: "Attributed channels",
  corroboration_pct: "Corroborated",
  incidents: "Open incidents",
  phishing_assets: "Phishing assets",
  credentials_total: "Leaked credentials",
  confirmed_pct: "Confirmed malicious",
  containment_pct: "Containment",
  case_closure_pct: "Takedown closure",
};

const CONFIDENCE_LABELS: Record<CtemStage["confidence"], string> = {
  high: "High",
  medium: "Moderate",
  low: "Low",
  none: "None",
};

const bandLabel = (band: string, t: Translate) => t(CTEM_BAND_CONFIG[band]?.label ?? CTEM_BAND_CONFIG.unknown.label);
const stageLabel = (stage: CtemStage, t: Translate) => t(CTEM_STAGE_CONFIG[stage.key as CtemStageKey]?.label ?? stage.label);
/** A score's unit only when there is a score: an empty value keeps an empty unit. */
const scoreUnit = (value: number | null) => (value === null ? "" : "/100");

/** Every value of a posture, in lifecycle order: the posture, then per stage
 *  its summary and facts, then the headline KPIs that have a value. A KPI the
 *  posture could not compute (null) is left out rather than written as 0. */
export function postureCsvRows(posture: ExportablePosture, t: Translate): PostureCsvRow[] {
  const section = { posture: t("Exposure posture"), stage: t("Stage"), fact: t("Fact"), headline: t("Key indicators") };
  const rows: PostureCsvRow[] = [
    { section: section.posture, stage: "", metric: t("Posture score"), value: posture.posture_score, unit: scoreUnit(posture.posture_score), key: "posture.score" },
    { section: section.posture, stage: "", metric: t("Band"), value: bandLabel(posture.posture_band, t), unit: "", key: "posture.band" },
    { section: section.posture, stage: "", metric: t("Stages covered"), value: posture.covered_stages, unit: "", key: "posture.covered_stages" },
    { section: section.posture, stage: "", metric: t("Total stages"), value: posture.total_stages, unit: "", key: "posture.total_stages" },
  ];
  if (posture.generated_at) {
    rows.push({ section: section.posture, stage: "", metric: t("Built at"), value: posture.generated_at, unit: "", key: "posture.generated_at" });
  }

  for (const stage of posture.stages) {
    const label = stageLabel(stage, t);
    const id = `stage.${stage.key}`;
    const products = stage.products_present.map((p) => CTEM_PRODUCT_INFO[p]?.name ?? p).join(", ");
    rows.push(
      { section: section.stage, stage: label, metric: t("Stage score"), value: stage.score, unit: scoreUnit(stage.score), key: `${id}.score` },
      { section: section.stage, stage: label, metric: t("Band"), value: bandLabel(stage.band, t), unit: "", key: `${id}.band` },
      { section: section.stage, stage: label, metric: t("Covered"), value: t(stage.covered ? "Yes" : "No"), unit: "", key: `${id}.covered` },
      { section: section.stage, stage: label, metric: t("Confidence"), value: t(CONFIDENCE_LABELS[stage.confidence] ?? "None"), unit: "", key: `${id}.confidence` },
      { section: section.stage, stage: label, metric: t("Products present"), value: products, unit: "", key: `${id}.products_present` },
    );
    // A fact carries the label its stage card shows (written by the collector).
    for (const fact of stage.facts) {
      rows.push({ section: section.fact, stage: label, metric: fact.label, value: fact.value, unit: fact.unit, key: `fact.${stage.key}.${fact.label}` });
    }
  }

  for (const [key, value] of Object.entries(posture.headline ?? {})) {
    if (value === null || value === undefined) continue;
    const cell = Array.isArray(value) ? value.join(", ") : value;
    if (cell === "") continue;
    const label = HEADLINE_LABELS[key as keyof CtemHeadline];
    rows.push({
      section: section.headline,
      stage: "",
      metric: label ? t(label) : key,
      value: cell,
      unit: key.endsWith("_pct") ? "%" : "",
      key: `headline.${key}`,
    });
  }
  return rows;
}

/** The posture as CSV text (RFC 4180, formula-injection guard of `lib/csv`). */
export function postureCsv(posture: ExportablePosture, headers: PostureCsvHeaders, t: Translate): string {
  const columns: CsvColumn<PostureCsvRow>[] = [
    { header: headers.section, value: (r) => r.section },
    { header: headers.stage, value: (r) => r.stage },
    { header: headers.metric, value: (r) => r.metric },
    { header: headers.value, value: (r) => r.value },
    { header: headers.unit, value: (r) => r.unit },
    { header: headers.key, value: (r) => r.key },
  ];
  return toCsv(postureCsvRows(posture, t), columns);
}

/** `ctem-posture-<vertical>-<YYYY-MM-DD>.csv`. */
export function postureCsvFilename(vertical: CtemVertical, at: Date = new Date()): string {
  return csvFilename(`ctem-posture-${vertical}`, at);
}
