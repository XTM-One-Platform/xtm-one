// SPDX-License-Identifier: Apache-2.0

/**
 * Who works a CTEM case (#1266), read from live state, never assumed: every
 * automation listening to the case events (`ctem_case.*` platform events),
 * whatever fleet or agent owns it, and the runs those events started on this
 * case. The built-in CTEM loop fleet is one of the ways, not the default: a
 * customer may run his own fleet, his own agent, or work the case by hand.
 * Pure: the page reads the assignments, the agents and the case runs.
 */
import type { CtemCaseRun } from "@/hooks/api/useCtemCasesApi";

/** The fields of an assignment the reader needs. */
interface CaseAssignment {
  id: string;
  agent_id: string;
  name: string;
  enabled: boolean;
  trigger_type: string;
  trigger_config: { event?: unknown } | null;
}

/** The fields of an agent the reader needs: its name and home fleet. */
interface CaseAgent {
  id: string;
  name: string;
  fleet?: { fleet_id: string; fleet_name: string } | null;
}

/** One automation that works cases. */
export interface CaseAutomation {
  id: string;
  name: string;
  enabled: boolean;
  agentId: string;
  agentName: string | null;
  fleetId: string | null;
  fleetName: string | null;
}

function triggerEvents(config: { event?: unknown } | null): string[] {
  const raw = config?.event;
  if (Array.isArray(raw)) return raw.filter((e): e is string => typeof e === "string");
  return typeof raw === "string" ? [raw] : [];
}

/** Whether an assignment listens to a case event (any `ctem_case.*`). */
export function listensToCases(a: Pick<CaseAssignment, "trigger_type" | "trigger_config">): boolean {
  return a.trigger_type === "platform_event" && triggerEvents(a.trigger_config).some((e) => e.startsWith("ctem_case."));
}

/** Every automation on case events, enabled first, then by agent and name. */
export function caseAutomations(assignments: readonly CaseAssignment[] | undefined, agents: readonly CaseAgent[] | undefined): CaseAutomation[] {
  const byId = new Map((agents ?? []).map((a) => [a.id, a]));
  return (assignments ?? [])
    .filter(listensToCases)
    .map((a) => {
      const agent = byId.get(a.agent_id);
      return {
        id: a.id,
        name: a.name,
        enabled: a.enabled,
        agentId: a.agent_id,
        agentName: agent?.name ?? null,
        fleetId: agent?.fleet?.fleet_id ?? null,
        fleetName: agent?.fleet?.fleet_name ?? null,
      };
    })
    .sort(
      (x, y) =>
        Number(y.enabled) - Number(x.enabled) ||
        (x.agentName ?? "").localeCompare(y.agentName ?? "") ||
        x.name.localeCompare(y.name),
    );
}

/** What works the case now. */
export type CaseWork =
  /** Not read yet, unreadable or not licensed: the page claims nothing. */
  | { kind: "unknown" }
  /** No automation listens to the case events. */
  | { kind: "none" }
  /** Automations exist but every one is switched off. */
  | { kind: "off"; automations: CaseAutomation[] }
  /** A run works the case now (queued or running). */
  | { kind: "running"; run: CtemCaseRun; automations: CaseAutomation[] }
  /** The newest run on the case failed and none runs now. */
  | { kind: "failed"; run: CtemCaseRun; automations: CaseAutomation[] }
  /** At least one automation is on; no run works the case now. */
  | { kind: "on"; automations: CaseAutomation[] };

export function caseWork(input: {
  licensed: boolean;
  automations: CaseAutomation[] | undefined;
  runs: readonly CtemCaseRun[] | undefined;
}): CaseWork {
  if (!input.licensed) return { kind: "unknown" };
  const runs = input.runs ?? [];
  const live = runs.filter((r) => r.outcome === "active");
  const automations = input.automations;
  if (live.length > 0) {
    const run = live.reduce((a, b) => (a.created_at <= b.created_at ? a : b));
    return { kind: "running", run, automations: automations ?? [] };
  }
  if (automations === undefined) return { kind: "unknown" };
  const enabled = automations.filter((a) => a.enabled);
  const newest = runs.length > 0 ? runs.reduce((a, b) => (a.created_at >= b.created_at ? a : b)) : null;
  if (newest && newest.outcome === "failed") return { kind: "failed", run: newest, automations: enabled.length > 0 ? enabled : automations };
  if (automations.length === 0) return newest ? { kind: "on", automations: [] } : { kind: "none" };
  if (enabled.length === 0) return { kind: "off", automations };
  return { kind: "on", automations: enabled };
}

/** The product a case section is written from. */
export type CaseSource = "opencrq" | "opencti" | "openaev";

/** Which products are registered: `null` while the registrations are not read. */
export type CaseSources = Record<CaseSource, boolean | null>;

export function caseSources(registrations: readonly { platform_identifier: string }[] | undefined): CaseSources {
  if (registrations === undefined) return { opencrq: null, opencti: null, openaev: null };
  const ids = new Set(registrations.map((r) => (r.platform_identifier === "openbas" ? "openaev" : r.platform_identifier)));
  return { opencrq: ids.has("opencrq"), opencti: ids.has("opencti"), openaev: ids.has("openaev") };
}
