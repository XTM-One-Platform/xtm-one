// SPDX-License-Identifier: Apache-2.0

/**
 * The reference exchange rates and the PIR details of the organization profile (#1773):
 * the form rows, their checks and the profile values a save sends. The server's
 * contract is `FxRate` and `PirDetail` in backend/app/services/organization_context.py;
 * these checks mirror it so the form marks a bad row before the save, never after.
 */

/** One unit of `from_currency` is `rate` units of `to_currency`, as published on `as_of` by `source`. */
export interface FxRate {
  from_currency: string;
  to_currency: string;
  rate: number;
  as_of: string;
  source: string;
}

/** What the organization says about one PIR it follows. */
export interface PirDetail {
  question: string;
  owner: string;
  review_cadence: ReviewCadence;
  crown_jewels: string[];
}

export const REVIEW_CADENCES = ["", "weekly", "monthly", "quarterly", "half_yearly", "yearly"] as const;
export type ReviewCadence = (typeof REVIEW_CADENCES)[number];

/** The English label of each cadence (translated where shown). */
export const REVIEW_CADENCE_LABELS: Record<ReviewCadence, string> = {
  "": "Not set",
  weekly: "Weekly",
  monthly: "Monthly",
  quarterly: "Quarterly",
  half_yearly: "Every six months",
  yearly: "Yearly",
};

/** The server's bounds (FxRate, PirDetail, OrganizationProfile). */
export const REFERENCE_BOUNDS = { rates: 50, source: 200, question: 1000, owner: 200, jewels: 50 } as const;

/** A rate row as the form edits it: every value as typed. */
export interface FxRateRow {
  from_currency: string;
  to_currency: string;
  rate: string;
  as_of: string;
  source: string;
}

/** Why a rate row cannot be saved, as an English message (translated where shown); `null` when it can. */
export type FxRateProblem =
  | "currency"
  | "same_currency"
  | "rate"
  | "date"
  | "source"
  | null;

export const FX_RATE_PROBLEM_LABELS: Record<Exclude<FxRateProblem, null>, string> = {
  currency: "Both currencies are ISO 4217 codes, such as EUR or USD.",
  same_currency: "A reference rate converts between two different currencies.",
  rate: "The rate is a number greater than 0.",
  date: "The date is a day, such as 2026-11-24.",
  source: "Name where the rate was published.",
};

const ISO_CURRENCY = /^[A-Z]{3}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

export function rateRowOf(rate: FxRate): FxRateRow {
  return { ...rate, rate: String(rate.rate) };
}

export function emptyRateRow(toCurrency: string): FxRateRow {
  return { from_currency: "", to_currency: toCurrency, rate: "", as_of: "", source: "" };
}

/** The first thing wrong with *row*, or `null`. */
export function rateProblem(row: FxRateRow): FxRateProblem {
  const from = row.from_currency.trim().toUpperCase();
  const to = row.to_currency.trim().toUpperCase();
  if (!ISO_CURRENCY.test(from) || !ISO_CURRENCY.test(to)) return "currency";
  if (from === to) return "same_currency";
  const rate = Number(row.rate.trim().replace(",", "."));
  if (!row.rate.trim() || !Number.isFinite(rate) || rate <= 0) return "rate";
  if (!DAY.test(row.as_of.trim()) || Number.isNaN(Date.parse(row.as_of.trim()))) return "date";
  if (!row.source.trim() || row.source.trim().length > REFERENCE_BOUNDS.source) return "source";
  return null;
}

/** The rates a save sends, or `null` when a row is not valid (the form marks it). */
export function ratesOf(rows: readonly FxRateRow[]): FxRate[] | null {
  if (rows.some((row) => rateProblem(row) !== null)) return null;
  return rows.map((row) => ({
    from_currency: row.from_currency.trim().toUpperCase(),
    to_currency: row.to_currency.trim().toUpperCase(),
    rate: Number(row.rate.trim().replace(",", ".")),
    as_of: row.as_of.trim(),
    source: row.source.trim(),
  }));
}

/**
 * The currencies an amount priced elsewhere cannot be converted from: the profile has a
 * currency and no rate converts into it. Empty when the profile has no currency or a rate
 * into it exists (the server's `rate_notice`).
 */
export function missingRateInto(currency: string, rates: readonly FxRate[]): boolean {
  return Boolean(currency) && !rates.some((rate) => rate.to_currency === currency);
}

export function emptyPirDetail(): PirDetail {
  return { question: "", owner: "", review_cadence: "", crown_jewels: [] };
}

/** Whether *detail* says nothing (the save leaves it out). */
function pirDetailEmpty(detail: PirDetail): boolean {
  return !detail.question.trim() && !detail.owner.trim() && !detail.review_cadence && detail.crown_jewels.length === 0;
}

/**
 * The PIR details a save sends: one per PIR still followed that says something, trimmed,
 * each crown jewel once. A PIR no longer followed loses its details, as it loses its name.
 */
export function pirDetailsOf(pirIds: readonly string[], details: Record<string, PirDetail>): Record<string, PirDetail> {
  const kept: Record<string, PirDetail> = {};
  for (const id of pirIds) {
    const detail = details[id];
    if (!detail) continue;
    const clean: PirDetail = {
      question: detail.question.trim(),
      owner: detail.owner.trim(),
      review_cadence: detail.review_cadence,
      crown_jewels: [...new Set(detail.crown_jewels.map((name) => name.trim()).filter(Boolean))],
    };
    if (!pirDetailEmpty(clean)) kept[id] = clean;
  }
  return kept;
}

/** Why a PIR detail cannot be saved (an English message), or `null`. */
export function pirDetailProblem(detail: PirDetail): string | null {
  if (detail.question.trim().length > REFERENCE_BOUNDS.question) return "The question is 1,000 characters at most.";
  if (detail.owner.trim().length > REFERENCE_BOUNDS.owner) return "The owner is 200 characters at most.";
  if (detail.crown_jewels.length > REFERENCE_BOUNDS.jewels) return "50 crown jewels at most.";
  return null;
}

/** The provenance a version keeps for a field: who set it, on what basis, and when it was confirmed. */
export interface FieldProvenance {
  source: string;
  basis: string;
  confirmed_at?: string;
}

/** The provenance of *field* in a version's `sources`, or `null` when the version holds none for it. */
export function provenanceOf(sources: Record<string, unknown> | null | undefined, field: string): FieldProvenance | null {
  const entry = sources?.[field];
  if (!entry || typeof entry !== "object") return null;
  const value = entry as Record<string, unknown>;
  const source = typeof value.source === "string" ? value.source : "";
  const basis = typeof value.basis === "string" ? value.basis : "";
  if (!source && !basis && !value.confirmed_at) return null;
  return {
    source,
    basis,
    ...(typeof value.confirmed_at === "string" && value.confirmed_at ? { confirmed_at: value.confirmed_at } : {}),
  };
}
