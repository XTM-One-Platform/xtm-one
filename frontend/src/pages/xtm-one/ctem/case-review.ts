// SPDX-License-Identifier: Apache-2.0

/**
 * The analyst review of a case in words (CTEM-16-UI, #1115): gate
 * `ctem.analyst_review` (CTEM-34), read from the case's own `gate` events, and
 * the threat package the review step drafted (`threat_package`: the OpenCTI
 * Grouping and the security coverage requested on it). Pure: the case page
 * draws these, nothing here reads the network.
 */
import { isUserId } from "./case-gates";

/** The review step: an analyst checks the threat package before its coverage is requested. */
export const REVIEW_GATE = "ctem.analyst_review";

/** The work item a waiting review opens (the key both CTEM orchestrator personas give it). */
export function reviewDecisionKey(caseId: string): string {
  return `ctem:review:${caseId}`;
}

/** Who answered a review: a person (by id), the analyst's mark in OpenCTI, or someone unnamed. */
export type ReviewAnswerer = { kind: "person"; id: string } | { kind: "mark" } | { kind: "unknown" };

/**
 * Who answered the review, from the gate event's `by` (`record_gate`): a user
 * id names a person; the mark answers in OpenCTI (`ctem_analyst_mark`,
 * "OpenCTI analyst mark") is named as such, never as "a person".
 */
export function reviewAnswerer(by: string | null | undefined): ReviewAnswerer {
  const text = (by ?? "").trim();
  if (isUserId(text)) return { kind: "person", id: text };
  if (/analyst'?s? mark/i.test(text)) return { kind: "mark" };
  return { kind: "unknown" };
}

/**
 * Where the review step stands on a case, for the card's headline (#1480):
 * `drafted` the package sits in an OpenCTI draft with no coverage yet;
 * `awaiting_validation` the coverage is written in that draft too, so nothing
 * reached OpenAEV until the draft is validated; `validated` the package is in
 * OpenCTI's live graph without a coverage; `coverage_requested` the validated
 * package has its coverage, sent to OpenAEV.
 */
export type ReviewStage = "not_started" | "drafted" | "awaiting_validation" | "validated" | "coverage_requested";

export function reviewStage(
  pkg: { state?: "drafted" | "validated"; coverage_requested?: boolean; coverage_drafted?: boolean } | null | undefined,
): ReviewStage {
  if (!pkg) return "not_started";
  if (pkg.coverage_requested) return "coverage_requested";
  if (pkg.coverage_drafted) return "awaiting_validation";
  return pkg.state === "validated" ? "validated" : "drafted";
}

/** The headline of each stage, translated where drawn. @i18n-keys */
export const REVIEW_STAGE_LABEL = {
  not_started: "The threat package an analyst reviews in OpenCTI before any validation, once the case is triaged.",
  drafted: "Threat package drafted, coverage not requested yet",
  awaiting_validation: "Threat package drafted, not validated: coverage not sent to OpenAEV",
  validated: "Threat package validated, coverage not requested yet",
  coverage_requested: "Coverage requested in OpenCTI",
} as const satisfies Record<ReviewStage, string>;
