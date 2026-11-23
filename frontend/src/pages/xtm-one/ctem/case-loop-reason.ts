// SPDX-License-Identifier: Apache-2.0

/**
 * The CTEM loop fleet the reader sees and the reader sentences of a case
 * opened by no signal. Who works a case is read in `case-workers.ts` (#1266):
 * the loop fleet is one way among others, never assumed.
 */
import { CTEM_LOOP_FLEET_SLUG as CTEM_LOOP_SLUG } from "@/components/fleets/CtemLoopSetupEntry";

/** The fields of a fleet the reason reads. */
export interface LoopFleet {
  id: string;
  slug?: string;
  is_builtin?: boolean;
  is_archived: boolean;
  forked_from?: { slug: string; is_builtin: boolean } | null;
}

/**
 * The CTEM loop fleet the reader sees: the seeded one first, else a fork of
 * it; `null` when the reader sees neither (not seeded, archived, or shared
 * with someone else).
 */
export function loopFleetOf<F extends LoopFleet>(fleets: readonly F[] | undefined): F | null {
  const active = (fleets ?? []).filter((f) => !f.is_archived);
  return (
    active.find((f) => f.is_builtin && f.slug === CTEM_LOOP_SLUG) ??
    active.find((f) => !!f.forked_from?.is_builtin && f.forked_from.slug === CTEM_LOOP_SLUG) ??
    null
  );
}

/**
 * The fleet page of a case's owner (#1390) the reader sees: the seeded fleet
 * with that slug first, then any fleet with it, then a fork of the seeded one;
 * `null` when the reader sees none.
 */
export function ownerFleetId(slug: string, fleets: readonly LoopFleet[] | undefined): string | null {
  const live = (fleets ?? []).filter((f) => !f.is_archived);
  const found =
    live.find((f) => f.is_builtin && f.slug === slug) ??
    live.find((f) => f.slug === slug) ??
    live.find((f) => !!f.forked_from?.is_builtin && f.forked_from.slug === slug);
  return found?.id ?? null;
}

/**
 * The sentence of a case opened by no signal, from the sources of its issues:
 * the source readers matched them to the inventory. @i18n-keys
 */
export const READER_OPENED_LABEL = {
  opencti: "Opened from OpenCTI vulnerabilities matched to your inventory",
  openaev: "Opened from OpenAEV findings on your assets",
} as const;

/** The reader sentences that apply, in a stable order; empty for a case with no visible issue. */
export function readerOpenedLines(sources: readonly string[]): (keyof typeof READER_OPENED_LABEL)[] {
  const seen = new Set(sources.map((s) => (s === "openbas" ? "openaev" : s)));
  return (Object.keys(READER_OPENED_LABEL) as (keyof typeof READER_OPENED_LABEL)[]).filter((k) => seen.has(k));
}
