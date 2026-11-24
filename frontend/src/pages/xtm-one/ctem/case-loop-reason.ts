// SPDX-License-Identifier: Apache-2.0

/**
 * The CTEM loop fleet the reader sees and how a case with no signal came to
 * be. Who works a case is read in `case-workers.ts` (#1266): the loop fleet is
 * one way among others, never assumed.
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

/** The fields of a history event the origin reads. */
interface OriginEvent {
  kind: string;
  to: string | null;
  reason?: string | null;
}

/** A history event that says a signal opened or joined the case. */
function namesASignal(e: OriginEvent): boolean {
  return e.kind === "signal" || (e.kind === "created" && (e.reason ?? "").startsWith("signal "));
}

/**
 * How a case whose signals the reader does not see came to be (#1529): a case
 * is opened only by a signal or split into its own case by a person or an
 * agent. A history that names a signal the reader cannot read is
 * `restricted`; a case with neither a signal nor a split was built by a source
 * read before the readers recorded signals (`legacy`).
 */
export function noSignalOrigin(history: readonly OriginEvent[] | undefined): "split" | "restricted" | "legacy" {
  const events = history ?? [];
  if (events.some((e) => e.kind === "created" && e.to === "manual")) return "split";
  if (events.some(namesASignal)) return "restricted";
  return "legacy";
}

/** The sentence of each origin. @i18n-keys */
export const NO_SIGNAL_ORIGIN_LABEL = {
  split: "Split into its own case by a person or an agent",
  restricted: "A signal you cannot see",
  legacy: "A source read before signals (legacy)",
} as const;
