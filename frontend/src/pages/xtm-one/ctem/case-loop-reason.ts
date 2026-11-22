// SPDX-License-Identifier: Apache-2.0

/**
 * Why a section of a case page is empty (#1206, slice D): read from live
 * state, never a promise the platform cannot keep. A section the fleet fills
 * (exploitation reading, threat package, validation, remediation decision,
 * treatment plan) says "No automation works this case" when the CTEM loop
 * fleet is not set up, "Waiting for <agent>" while a fleet run works the case,
 * and "OpenCRQ is not connected" for the two sections written from OpenCRQ.
 * Only when the loop runs and nothing blocks it does a section keep its own
 * "once the case is ..." sentence. Pure: the page reads the fleets, the case
 * runs and the platform registrations.
 */
import type { CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { CTEM_LOOP_FLEET_SLUG as CTEM_LOOP_SLUG } from "@/components/fleets/CtemLoopSetupEntry";
import { fleetOptIn, isFleetOn } from "@/lib/fleet-activity";

/** The fields of a fleet the reason reads. */
interface LoopFleet {
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

/** What works the case now, as far as the reader can tell. */
export type CaseLoopWorker =
  /** Not read yet, or unreadable (no licence, a refusal): the sections keep their own sentence. */
  | { kind: "unknown" }
  /** No CTEM loop fleet runs: nothing will fill the sections. */
  | { kind: "not_set_up"; fleetId: string | null }
  /** A fleet run works the case now. */
  | { kind: "running"; agent: string | null; since: string }
  /** The loop runs and no run is on the case now. */
  | { kind: "on" };

export function caseLoopWorker(input: {
  /** False without agentic AI licensed: no fleet read, nothing claimed. */
  licensed: boolean;
  fleets: readonly LoopFleet[] | undefined;
  fleetsError: boolean;
  automations: { total: number; enabled: number; paused: number } | null | undefined;
  runs: readonly CtemCaseRun[] | undefined;
}): CaseLoopWorker {
  if (!input.licensed) return { kind: "unknown" };
  const active = (input.runs ?? []).filter((r) => r.outcome === "active");
  if (active.length > 0) {
    const first = active.reduce((a, b) => (a.created_at <= b.created_at ? a : b));
    return { kind: "running", agent: first.agent_name, since: first.created_at };
  }
  if (input.fleetsError || input.fleets === undefined) return { kind: "unknown" };
  const fleet = loopFleetOf(input.fleets);
  if (!fleet) return { kind: "not_set_up", fleetId: null };
  const optIn = fleetOptIn(input.automations);
  if (optIn === null) return { kind: "unknown" };
  return isFleetOn(optIn) ? { kind: "on" } : { kind: "not_set_up", fleetId: fleet.id };
}

/** Whether OpenCRQ is registered: `null` while the registrations are not read. */
export function opencrqConnected(registrations: readonly { platform_identifier: string }[] | undefined): boolean | null {
  if (registrations === undefined) return null;
  return registrations.some((r) => r.platform_identifier === "opencrq");
}

/** Which reason a section shows: its own sentence (`own`) only when nothing blocks it. */
export type SectionReason = "opencrq" | "not_set_up" | "running" | "own";

export function sectionReason(worker: CaseLoopWorker, needsOpencrq: boolean, opencrq: boolean | null): SectionReason {
  if (needsOpencrq && opencrq === false) return "opencrq";
  if (worker.kind === "not_set_up") return "not_set_up";
  if (worker.kind === "running") return "running";
  return "own";
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
