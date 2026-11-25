// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import type { CtemCaseEvent, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { auditChange, auditRows, filterRows, foldAnsweredRequests, gateAnswerKind, groupByPhase, personReason, phaseBand, reasonLabel, runPhase, type ChangeWords } from "./ctem-case-audit";
import en from "@/i18n/lang/en.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

const event = (at: string, kind: string, extra: Partial<CtemCaseEvent> = {}): CtemCaseEvent =>
  ({ at, actor: "system", kind, issue: null, from: null, to: null, reason: null, evidence: null, ...extra }) as CtemCaseEvent;
const run = (id: string, at: string): CtemCaseRun => ({
  id,
  agent_id: "a",
  agent_name: "Triage",
  status: "completed",
  created_at: at,
  completed_at: null,
  outcome: "ok",
});

const history: CtemCaseEvent[] = [
  event("2026-11-23T11:58:00Z", "gate", { phase: "remediation", autonomy: "waiting", actor_type: "agent", actor: "ag-1" }),
  event("2026-11-23T11:08:00Z", "note", { phase: "prioritization", autonomy: "autonomous", actor_type: "agent", actor: "ag-1" }),
  event("2026-11-22T12:49:00Z", "created", { phase: "discovery", autonomy: "system", actor_type: "system" }),
];

describe("auditRows (#1490)", () => {
  it("keeps the served phase and autonomy class, newest first, runs placed in the phase they started in", () => {
    const rows = auditRows(history, [run("r1", "2026-11-23T11:30:00Z")]);
    expect(rows.map((r) => [r.key, r.phase, r.autonomy])).toEqual([
      ["event-0", "remediation", "waiting"],
      ["run-r1", "prioritization", "autonomous"],
      ["event-1", "prioritization", "autonomous"],
      ["event-2", "discovery", "system"],
    ]);
  });

  it("gives an event served without a phase (an older API) Discovery and its actor's class", () => {
    const [agent, system] = auditRows([event("2026-11-21T10:00:00Z", "note", { actor: "ag-1" }), event("2026-11-20T10:00:00Z", "state")], undefined);
    expect([agent.phase, agent.autonomy, agent.actorType]).toEqual(["discovery", "autonomous", "agent"]);
    expect([system.phase, system.autonomy, system.actorType]).toEqual(["discovery", "system", "system"]);
  });
});

describe("answered waiting gate (UX round 2 on #1520, B2)", () => {
  const request = event("2026-11-19T10:00:00Z", "gate", {
    phase: "prioritization",
    autonomy: "waiting",
    actor_type: "agent",
    actor: "ag-1",
    from: "ctem.analyst_review",
    to: "waited",
    answer: { at: "2026-11-19T11:00:00Z", outcome: "answered", by: "u-1", option: "A" },
  });
  const answer = event("2026-11-19T11:00:00Z", "gate", {
    phase: "prioritization",
    autonomy: "validated_by_person",
    actor_type: "person",
    actor: "u-1",
    from: "ctem.analyst_review",
    to: "answered",
    waited_since: "2026-11-19T10:00:00Z",
  });
  const open = event("2026-11-19T08:00:00Z", "gate", { phase: "remediation", autonomy: "waiting", to: "waited", answer: null });

  it("counts a gate and its answer as one validation; the answered request reads Waited, uncounted", () => {
    const rows = auditRows([answer, request, open], undefined);
    expect(rows.map((r) => r.autonomy)).toEqual(["validated_by_person", "waited", "waiting"]);
    const prioritization = groupByPhase(rows).find((g) => g.phase === "prioritization");
    expect(prioritization?.counts.validated_by_person).toBe(1);
    expect(prioritization?.counts.waiting).toBe(0);
    expect(prioritization?.counts.waited).toBe(0);
    expect(groupByPhase(rows).find((g) => g.phase === "remediation")?.counts.waiting).toBe(1);
  });

  it("gives the Autonomy filter one validated row for the gate", () => {
    const rows = auditRows([answer, request], undefined);
    expect(rows.filter((r) => r.autonomy === "validated_by_person")).toHaveLength(1);
  });

  it("folds an answered request into its answer for the Timeline, keeps an open one (#1679, point 2)", () => {
    const rows = auditRows([answer, request, open], undefined);
    expect(foldAnsweredRequests(rows).map((r) => r.autonomy)).toEqual(["validated_by_person", "waiting"]);
  });

  it("keeps an answered request whose answer row is not in the rows shown", () => {
    const rows = auditRows([request], undefined);
    expect(foldAnsweredRequests(rows).map((r) => r.autonomy)).toEqual(["waited"]);
  });
});

describe("auditChange (UX round 2 on #1520, B1)", () => {
  const states: Record<string, string> = { needs_attention: "Needs attention", in_progress: "In progress" };
  const people: Record<string, string> = { "u-1": "Alice Martin", "u-2": "Bob Stone" };
  const words: ChangeWords = {
    word: (_kind, value) => states[value] ?? null,
    person: (id) => people[id] ?? "a person",
    date: (iso) => iso.slice(0, 10),
  };

  it("says the owner by name, never the user id", () => {
    expect(auditChange({ kind: "owner", from: "u-1", to: "u-2" }, words)).toEqual({ from: "Alice Martin", to: "Bob Stone" });
  });

  it("says the due date as a date, never the raw timestamp", () => {
    expect(auditChange({ kind: "due", from: "2026-11-27T17:00:00+00:00", to: "2026-11-28T17:00:00+00:00" }, words)).toEqual({
      from: "2026-11-27",
      to: "2026-11-28",
    });
    expect(auditChange({ kind: "due", from: "soon", to: "2026-11-28T17:00:00+00:00" }, words)).toBeNull();
  });

  it("says a state by its label, and gives no change for a value or kind it cannot say", () => {
    expect(auditChange({ kind: "state", from: "needs_attention", to: "in_progress" }, words)).toEqual({ from: "Needs attention", to: "In progress" });
    expect(auditChange({ kind: "state", from: "needs_attention", to: "weird_key" }, words)).toBeNull();
    expect(auditChange({ kind: "source_event", from: "a", to: "b" }, words)).toBeNull();
  });
});

describe("gate record step headings (advisory A1 of #1708 round 4)", () => {
  it("names the step in ja, never the date label of its field", () => {
    const catalog = ja as Record<string, string>;
    expect(catalog["Asked (gate step)"]).not.toBe(catalog["Asked at"]);
    expect(catalog["Asked (gate step)"]).not.toMatch(/日時/);
    expect(catalog["Decided (gate step)"]).not.toMatch(/日時/);
  });
});

describe("runPhase", () => {
  it("reads the phase of the latest event at or before the run, Discovery before any", () => {
    expect(runPhase("2026-11-23T11:59:00Z", history)).toBe("remediation");
    expect(runPhase("2026-11-20T00:00:00Z", history)).toBe("discovery");
  });
});

describe("groupByPhase and filterRows", () => {
  it("groups the rows by phase, the latest phase on top, with the count of each class", () => {
    const groups = groupByPhase(auditRows(history, [run("r1", "2026-11-23T11:30:00Z")]));
    expect(groups.map((g) => g.phase)).toEqual(["remediation", "prioritization", "discovery"]);
    expect(groups[1].counts.autonomous).toBe(2);
    expect(groups[0].counts.waiting).toBe(1);
  });

  it("shows the same rows in both views for a phase picked on the band", () => {
    const rows = auditRows(history, undefined);
    const picked = filterRows(rows, { phase: "prioritization" });
    expect(picked.map((r) => r.key)).toEqual(["event-1"]);
    expect(groupByPhase(picked).flatMap((g) => g.rows)).toEqual(picked);
    expect(filterRows(rows, { phase: null })).toHaveLength(3);
  });
});

describe("reasonLabel and personReason (UX round 3 on #1520, B2)", () => {
  it("says a stored reason with a catalog key, never the raw value", () => {
    expect(reasonLabel(event("t", "announced", { reason: "source read" }))).toBe("Announced to the loop");
    expect(reasonLabel(event("t", "announced", { reason: "catch-up" }))).toBe("Announced to the loop by the catch-up pass");
    expect(reasonLabel(event("t", "state", { reason: "accepted risk expired" }))).toBe("The accepted risk expired");
    for (const key of ["Announced to the loop", "Announced to the loop by the catch-up pass", "Announced to the loop by the backfill", "The accepted risk expired"]) {
      expect((en as Record<string, string>)[key]).toBe(key);
    }
  });

  it("shows nothing for a platform reason without a label", () => {
    expect(reasonLabel(event("t", "state", { reason: "source read" }))).toBeNull();
    expect(reasonLabel(event("t", "created", { reason: "manual split" }))).toBeNull();
    expect(personReason(event("t", "state", { reason: "source read", actor_type: "system" }))).toBeNull();
  });

  it("keeps the words a person typed for their own change", () => {
    expect(personReason(event("t", "state", { reason: "Fixed in 7.2.7", actor_type: "person" }))).toBe("Fixed in 7.2.7");
  });
});

describe("answer row copy in the past tense (UX round 3 on #1520, B1)", () => {
  // "Waited for a person" is shared with case-gates.ts on main: its fr/ja fix ships in its own PR on main.
  it("never reads Waited since as still waiting in fr and ja", () => {
    expect((fr as Record<string, string>)["Waited since {date}"]).not.toMatch(/^En attente/);
    expect((ja as Record<string, string>)["Waited since {date}"]).not.toMatch(/待機$/);
  });
});

describe("gateAnswerKind (review of #1520, P3)", () => {
  it("tells a gate passed by policy from one a person answered", () => {
    expect(gateAnswerKind({ outcome: "passed_automatically" })).toBe("policy");
    expect(gateAnswerKind({ outcome: "answered" })).toBe("person");
    expect(gateAnswerKind({ outcome: null })).toBe("person");
    expect(gateAnswerKind(null)).toBeNull();
  });

  it("words the policy answer in every locale", () => {
    for (const lang of [en, fr, ja] as Record<string, string>[]) {
      expect(lang["Validated by policy on {date}"]).toContain("{date}");
    }
  });
});

describe("phaseBand", () => {
  it("lists the five phases in CTEM order, a phase not served as not reached", () => {
    const band = phaseBand([{ phase: "discovery", entered_at: "2026-11-22T12:49:00Z", exited_at: null, seconds: 60, events: 1, runs: 0, waiting: 0, current: true }]);
    expect(band.map((p) => p.phase)).toEqual(["scoping", "discovery", "prioritization", "validation", "remediation"]);
    expect(band[0].entered_at).toBeNull();
    expect(band[1].current).toBe(true);
  });
});
