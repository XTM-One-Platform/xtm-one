// SPDX-License-Identifier: Apache-2.0

/**
 * The case History as an audit trail grouped by CTEM phase (#1490, slice 3).
 * `GET /ctem-cases/{id}` serves each event with its phase, autonomy class,
 * actor type and name, headline and refs, and the phases with their times
 * (backend `ctem_case_audit`, slice 1); this module merges the fleet runs
 * into it, places each run in the phase the case was in when it started,
 * groups by phase and filters. Pure: nothing here reads the network.
 */
import type { CtemCaseEvent, CtemCasePhase, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { caseActivity } from "@/lib/ctem-case-activity";

/** The CTEM phases in order (backend `ctem_case_audit.PHASES`). */
const AUDIT_PHASES = ["scoping", "discovery", "prioritization", "validation", "remediation"] as const;
export type AuditPhase = (typeof AUDIT_PHASES)[number];

/** A phase in words: `remediation` is the Mobilization phase of the case. @i18n-keys */
export const AUDIT_PHASE_LABEL = {
  scoping: "Scoping",
  discovery: "Discovery",
  prioritization: "Prioritization",
  validation: "Validation",
  remediation: "Mobilization",
} as const satisfies Record<AuditPhase, string>;

/** The autonomy classes in the order they are counted (backend `AUTONOMY_CLASSES`). */
export const AUTONOMY_CLASSES = [
  "autonomous",
  "validated_by_person",
  "validated_by_policy",
  "waiting",
  "changed",
  "by_person",
  "system",
] as const;
export type AutonomyClass = (typeof AUTONOMY_CLASSES)[number];

/** What an autonomy class says of a row, in words. @i18n-keys */
export const AUTONOMY_LABEL = {
  autonomous: "Autonomous",
  validated_by_person: "Validated by a person",
  validated_by_policy: "Validated by policy",
  waiting: "Waiting for a person",
  changed: "Changed",
  by_person: "Done by a person",
  system: "System",
} as const satisfies Record<AutonomyClass, string>;

/** The badge variant of each class: only a gate still waiting stands out. */
export const AUTONOMY_VARIANT: Record<AutonomyClass, "success" | "warning" | "secondary" | "outline"> = {
  autonomous: "secondary",
  validated_by_person: "success",
  validated_by_policy: "outline",
  waiting: "warning",
  changed: "outline",
  by_person: "secondary",
  system: "outline",
};

export type ActorType = "person" | "agent" | "system";

/** Who acted, in words. @i18n-keys */
export const ACTOR_TYPE_LABEL = {
  person: "Person",
  agent: "Agent",
  system: "System",
} as const satisfies Record<ActorType, string>;

export function isAuditPhase(value: unknown): value is AuditPhase {
  return typeof value === "string" && (AUDIT_PHASES as readonly string[]).includes(value);
}

function isAutonomy(value: unknown): value is AutonomyClass {
  return typeof value === "string" && (AUTONOMY_CLASSES as readonly string[]).includes(value);
}

/** One row of the audit trail: a case event or a fleet run, with its phase and class. */
export type AuditRow =
  | { kind: "event"; key: string; at: string | null; phase: AuditPhase; autonomy: AutonomyClass; actorType: ActorType; event: CtemCaseEvent }
  | { kind: "run"; key: string; at: string; phase: AuditPhase; autonomy: AutonomyClass; actorType: ActorType; run: CtemCaseRun };

function time(at: string | null | undefined): number {
  const t = at ? Date.parse(at) : Number.NaN;
  return Number.isNaN(t) ? Number.NEGATIVE_INFINITY : t;
}

/** The actor type of an event an API older than slice 1 serves without one. */
function fallbackActorType(e: CtemCaseEvent): ActorType {
  if (e.actor_type === "person" || e.actor_type === "agent" || e.actor_type === "system") return e.actor_type;
  return !e.actor || e.actor === "system" ? "system" : "agent";
}

/**
 * The phase the case was in when a run started: the phase of the latest event
 * at or before it (backend `ctem_case_audit.run_phase`), Discovery before any.
 */
export function runPhase(createdAt: string, history: readonly CtemCaseEvent[]): AuditPhase {
  const at = time(createdAt);
  let best: { t: number; phase: AuditPhase } | null = null;
  for (const e of history) {
    const t = time(e.at);
    if (t === Number.NEGATIVE_INFINITY || t > at || !isAuditPhase(e.phase)) continue;
    if (best === null || t >= best.t) best = { t, phase: e.phase };
  }
  return best?.phase ?? "discovery";
}

/**
 * The case events and the fleet runs, newest first (the order of `caseActivity`),
 * each with its phase and autonomy class. An event served without them (an API
 * older than slice 1) reads Discovery and its actor's class, never no phase.
 */
export function auditRows(history: readonly CtemCaseEvent[], runs: readonly CtemCaseRun[] | undefined): AuditRow[] {
  return caseActivity(history, runs).map((item): AuditRow => {
    if (item.kind === "run") {
      return { ...item, phase: runPhase(item.run.created_at, history), autonomy: "autonomous", actorType: "agent" };
    }
    const e = item.event;
    const actorType = fallbackActorType(e);
    const served: AutonomyClass = isAutonomy(e.autonomy)
      ? e.autonomy
      : actorType === "agent"
        ? "autonomous"
        : actorType === "person"
          ? "by_person"
          : "system";
    return { ...item, phase: isAuditPhase(e.phase) ? e.phase : "discovery", autonomy: answeredClass(e, served), actorType };
  });
}

/**
 * A waiting gate that has an answer reads as its answer (UX round 1 on #1520, B2):
 * validated by policy when the answer passed it automatically, else by a person.
 * Only an unanswered gate stays Waiting, in the row badge, the group counts and the
 * Autonomy filter alike.
 */
function answeredClass(e: CtemCaseEvent, served: AutonomyClass): AutonomyClass {
  if (served !== "waiting" || e.kind !== "gate" || !e.answer) return served;
  return e.answer.outcome === "passed_automatically" ? "validated_by_policy" : "validated_by_person";
}

export interface AuditFilter {
  phase?: AuditPhase | null;
}

/** The rows of one phase, or all of them. */
export function filterRows(rows: readonly AuditRow[], filter: AuditFilter): AuditRow[] {
  return rows.filter((r) => !filter.phase || r.phase === filter.phase);
}

export interface PhaseGroup {
  phase: AuditPhase;
  rows: AuditRow[];
  /** Rows of each autonomy class in the group. */
  counts: Record<AutonomyClass, number>;
}

/**
 * The rows grouped by phase, newest phase first (the phase the case is in on
 * top), each group's rows in the order given; a phase with no row is left out.
 */
export function groupByPhase(rows: readonly AuditRow[]): PhaseGroup[] {
  const groups = new Map<AuditPhase, PhaseGroup>();
  for (const row of rows) {
    let group = groups.get(row.phase);
    if (!group) {
      group = {
        phase: row.phase,
        rows: [],
        counts: Object.fromEntries(AUTONOMY_CLASSES.map((c) => [c, 0])) as Record<AutonomyClass, number>,
      };
      groups.set(row.phase, group);
    }
    group.rows.push(row);
    group.counts[row.autonomy] += 1;
  }
  return [...groups.values()].sort((a, b) => AUDIT_PHASES.indexOf(b.phase) - AUDIT_PHASES.indexOf(a.phase));
}

/** The phases the case went through, in CTEM order, with a phase the API did not serve as empty. */
export function phaseBand(served: readonly CtemCasePhase[] | undefined): CtemCasePhase[] {
  return AUDIT_PHASES.map(
    (phase) =>
      served?.find((p) => p.phase === phase) ?? {
        phase,
        entered_at: null,
        exited_at: null,
        seconds: 0,
        events: 0,
        runs: 0,
        waiting: 0,
        current: false,
      },
  );
}
