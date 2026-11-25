// SPDX-License-Identifier: Apache-2.0

/**
 * Who works a CTEM case (#1266), read from live state, never assumed: every
 * automation listening to the case events (`ctem_case.*` platform events),
 * whatever fleet or agent owns it, and the runs those events started on this
 * case. The built-in CTEM loop fleet is one of the ways, not the default: a
 * customer may run his own fleet, his own agent, or work the case by hand.
 * Pure: the page reads the assignments, the agents and the case runs.
 *
 * An automation counts for THIS case only when it can start on it (UX round 1
 * on #1275, B1): it hears `ctem_case.opened` or `ctem_case.reopened`, and its
 * `trigger_config.filter` takes the case event payload
 * (`ctem_case_loop.case_data`: case_id, state, trigger_path, object_type,
 * object_id, vertical). The filter grammar is the backend's
 * (`integration_event_filters.payload_filter_reason`); a key this page cannot
 * read (vertical) or an operator it does not evaluate (matches) never drops an
 * automation: the page refutes only what it can prove.
 *
 * An assignment whose trigger declares `role: "observer"` (#2054) watches the
 * case lifecycle without working it (a supervisor): it never counts as a
 * worker, so a case only an observer hears reads "off" or "none", never
 * "Worked by".
 */
import type { CtemCaseRun } from "@/hooks/api/useCtemCasesApi";

/** The fields of an assignment the reader needs. */
interface CaseAssignment {
  id: string;
  agent_id: string;
  name: string;
  enabled: boolean;
  trigger_type: string;
  trigger_config: { event?: unknown; filter?: unknown; role?: unknown } | null;
}

/** The fields of the case event payload the page knows (`ctem_case_loop.case_data`). */
export interface CaseEventFacts {
  id: string;
  trigger_path?: string | null;
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

/** The case events that start the work on a case; resolved, SLA or validation events only continue it. */
const STARTING_EVENTS = new Set(["ctem_case.opened", "ctem_case.reopened"]);

/** Whether an assignment starts on a case event (`ctem_case.opened` or `ctem_case.reopened`). */
export function listensToCases(a: Pick<CaseAssignment, "trigger_type" | "trigger_config">): boolean {
  return a.trigger_type === "platform_event" && triggerEvents(a.trigger_config).some((e) => STARTING_EVENTS.has(e));
}

const OPERATORS = new Set(["eq", "ne", "in", "not_in", "starts_with", "ends_with", "contains", "matches", "exists"]);
/** Filter keys the backend applies where the events are read, never on the payload. */
const READ_SIDE_KEYS = new Set(["scope", "channels"]);
const MISSING = Symbol("missing");
/** A verdict the page cannot reach: the automation is kept. */
const UNKNOWN = "unknown" as const;
type Verdict = boolean | typeof UNKNOWN;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isOperatorObject(v: unknown): v is Record<string, unknown> {
  return isRecord(v) && Object.keys(v).length > 0 && Object.keys(v).every((k) => OPERATORS.has(k));
}

function isCombinator(key: string, v: unknown): boolean {
  if (key === "all" || key === "any") return Array.isArray(v) && v.length > 0 && v.every(isRecord);
  return key === "not" && isRecord(v) && !isOperatorObject(v);
}

function asList(operand: unknown): unknown[] {
  return Array.isArray(operand) ? operand : [operand];
}

function operatorHolds(op: string, actual: unknown, operand: unknown): Verdict {
  const present = actual !== MISSING && actual !== null && actual !== undefined;
  if (op === "exists") return present === Boolean(operand);
  if (actual === MISSING) return op === "ne" || op === "not_in";
  switch (op) {
    case "eq":
      return actual === operand;
    case "ne":
      return actual !== operand;
    case "in":
      return asList(operand).some((o) => actual === o);
    case "not_in":
      return asList(operand).every((o) => actual !== o);
    case "starts_with":
      return typeof actual === "string" && asList(operand).some((o) => typeof o === "string" && actual.startsWith(o));
    case "ends_with":
      return typeof actual === "string" && asList(operand).some((o) => typeof o === "string" && actual.endsWith(o));
    case "contains":
      return typeof actual === "string" && asList(operand).some((o) => typeof o === "string" && actual.includes(o));
    default:
      return UNKNOWN;
  }
}

function and(verdicts: Verdict[]): Verdict {
  if (verdicts.includes(false)) return false;
  return verdicts.includes(UNKNOWN) ? UNKNOWN : true;
}

function valueMatches(actual: unknown, expected: unknown): Verdict {
  if (isOperatorObject(expected)) return and(Object.entries(expected).map(([op, operand]) => operatorHolds(op, actual, operand)));
  if (actual === MISSING) return false;
  if (Array.isArray(expected)) {
    const options = expected.map((o) => (isRecord(o) ? UNKNOWN : actual === o));
    return options.includes(true) ? true : options.includes(UNKNOWN) ? UNKNOWN : false;
  }
  if (isRecord(expected)) return UNKNOWN;
  return actual === expected;
}

function filterVerdict(payload: Record<string, unknown>, known: ReadonlySet<string>, filter: Record<string, unknown>, depth: number): Verdict {
  const verdicts: Verdict[] = [];
  for (const [key, expected] of Object.entries(filter)) {
    if (READ_SIDE_KEYS.has(key)) continue;
    if (isCombinator(key, expected) && depth < 8) {
      if (key === "all") verdicts.push(and((expected as Record<string, unknown>[]).map((b) => filterVerdict(payload, known, b, depth + 1))));
      else if (key === "any") {
        const branches = (expected as Record<string, unknown>[]).map((b) => filterVerdict(payload, known, b, depth + 1));
        verdicts.push(branches.includes(true) ? true : branches.includes(UNKNOWN) ? UNKNOWN : false);
      } else {
        const negated = filterVerdict(payload, known, expected as Record<string, unknown>, depth + 1);
        verdicts.push(negated === UNKNOWN ? UNKNOWN : !negated);
      }
      continue;
    }
    // A payload field the page does not know (vertical, a dotted path): never refuted.
    if (!known.has(key)) {
      verdicts.push(UNKNOWN);
      continue;
    }
    verdicts.push(valueMatches(key in payload ? payload[key] : MISSING, expected));
  }
  return and(verdicts);
}

/**
 * Whether an assignment's filter takes this case's event: false only when the
 * page proves it does not (a `trigger_path` the filter excludes, say).
 */
export function filterTakesCase(filter: unknown, kase: CaseEventFacts): boolean {
  if (!isRecord(filter) || Object.keys(filter).length === 0) return true;
  const payload: Record<string, unknown> = {
    case_id: kase.id,
    object_id: kase.id,
    object_type: "ctem_case",
  };
  if (kase.trigger_path) payload.trigger_path = kase.trigger_path;
  // trigger_path is known only when the API names it; an older API leaves it unknown.
  // The state is never judged: the event that starts the next run carries the state
  // of that moment (opened or reopened), not the state the page shows now.
  const known = new Set(Object.keys(payload));
  return filterVerdict(payload, known, filter, 0) !== false;
}

/** Whether an assignment declares itself an observer of its events (`trigger_config.role`, #2054). */
export function observesOnly(a: Pick<CaseAssignment, "trigger_config">): boolean {
  return a.trigger_config?.role === "observer";
}

/** Every automation that can start on this case and work it (observers left out), enabled first, then by agent and name. */
export function caseAutomations(
  assignments: readonly CaseAssignment[] | undefined,
  agents: readonly CaseAgent[] | undefined,
  kase: CaseEventFacts,
): CaseAutomation[] {
  const byId = new Map((agents ?? []).map((a) => [a.id, a]));
  return (assignments ?? [])
    .filter((a) => listensToCases(a) && !observesOnly(a) && filterTakesCase(a.trigger_config?.filter, kase))
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
  /** At least one automation is on; no run works the case now. `off`: the switched-off ones that could also work it. */
  | { kind: "on"; automations: CaseAutomation[]; off: CaseAutomation[] };

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
  if (automations.length === 0) return newest ? { kind: "on", automations: [], off: [] } : { kind: "none" };
  if (enabled.length === 0) return { kind: "off", automations };
  return { kind: "on", automations: enabled, off: automations.filter((a) => !a.enabled) };
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
