// SPDX-License-Identifier: Apache-2.0

/**
 * The figures of the CTEM Governance & Risk page (dev-docs/ctem-stage-viz.md
 * section 3.6, slice S2e of #11547): the band zones of the posture chart, the
 * gap to the posture target and the accepted risks by days until expiry.
 * Pure and deterministic: they read the posture history and the organization
 * profile, never a model, and never invent a value.
 */
import type { CtemBand } from "@/types";

/** The days the posture chart covers. */
export const GOVERNANCE_DAYS = 90;

/** The furthest expiry the accepted risk strip draws to scale; later ones sit at its end. */
export const RISK_HORIZON_DAYS = 90;

/** The five band zones of the posture score, low to high (the BANDS of ctem_scoring.py). */
export const BAND_ZONES: readonly { band: Exclude<CtemBand, "unknown">; from: number; to: number }[] = [
  { band: "critical", from: 0, to: 40 },
  { band: "at_risk", from: 40, to: 60 },
  { band: "fair", from: 60, to: 75 },
  { band: "good", from: 75, to: 90 },
  { band: "strong", from: 90, to: 100 },
];

/**
 * How far the posture is below its target, in whole points: positive below the
 * target, 0 or negative at or above it; null without a score or a target.
 */
export function postureGap(score: number | null | undefined, target: number | null | undefined): number | null {
  if (score == null || target == null || !Number.isFinite(score) || !Number.isFinite(target)) return null;
  return Math.round(target - score);
}

/** The gap of each recorded day, for the shaded area and the gap delta. */
export function gapSeries(scores: readonly (number | null)[], target: number | null | undefined): (number | null)[] {
  return scores.map((score) => postureGap(score, target));
}

export interface AcceptedRiskLike {
  subject: string;
  scope: string;
  owner: string;
  expires_on: string;
}

export interface RiskOnStrip extends AcceptedRiskLike {
  /** Whole days from today to the expiry; negative once expired. */
  days: number;
  expired: boolean;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/**
 * The accepted risks with a readable expiry, by days until it (soonest
 * first). A risk with no date or an unreadable one is left out, never placed
 * at a guessed day.
 */
export function risksByExpiry(risks: readonly AcceptedRiskLike[], today: Date): RiskOnStrip[] {
  const start = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  const placed: RiskOnStrip[] = [];
  for (const risk of risks) {
    if (!risk.subject.trim()) continue;
    const at = Date.parse(`${risk.expires_on.slice(0, 10)}T00:00:00Z`);
    if (!risk.expires_on || !Number.isFinite(at)) continue;
    const days = Math.round((at - start) / DAY_MS);
    placed.push({ ...risk, days, expired: days < 0 });
  }
  return placed.sort((a, b) => a.days - b.days || a.subject.localeCompare(b.subject));
}

/** Where a risk sits on the strip, 0 (expired or today) to 1 (the horizon or later). */
export function stripPosition(days: number, horizon = RISK_HORIZON_DAYS): number {
  return Math.min(1, Math.max(0, days / horizon));
}
