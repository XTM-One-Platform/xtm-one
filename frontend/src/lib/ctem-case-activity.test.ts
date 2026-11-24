// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";
import type { CtemCaseEvent, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { CASE_RUN_LINE, caseActivity, caseRunLine } from "./ctem-case-activity";

const event = (at: string | null, kind = "state"): CtemCaseEvent => ({ at, actor: "system", kind, issue: null }) as CtemCaseEvent;
const run = (id: string, at: string): CtemCaseRun => ({
  id,
  agent_id: "a",
  agent_name: "Triage",
  status: "completed",
  created_at: at,
  completed_at: null,
  outcome: "ok",
});

describe("caseActivity (#915)", () => {
  it("merges the case events and the fleet runs newest first", () => {
    const items = caseActivity(
      [event("2026-11-21T10:00:00Z", "created"), event("2026-11-21T12:00:00Z")],
      [run("r1", "2026-11-21T11:00:00Z")],
    );
    expect(items.map((i) => i.key)).toEqual(["event-1", "run-r1", "event-0"]);
  });

  it("lists the run above the event that started it at the same time, undated events last", () => {
    const items = caseActivity([event(null, "note"), event("2026-11-21T10:00:00Z", "created")], [run("r1", "2026-11-21T10:00:00Z")]);
    expect(items.map((i) => i.key)).toEqual(["run-r1", "event-1", "event-0"]);
  });

  it("shows the history alone when no run is read", () => {
    expect(caseActivity([event("2026-11-21T10:00:00Z")], undefined)).toHaveLength(1);
  });
});

describe("caseRunLine (UX round 1 on #920, B1)", () => {
  it("words a queued run as queued", () => {
    expect(CASE_RUN_LINE[caseRunLine("pending")]).toBe("Fleet step queued on this case");
  });

  it("words a running run in the present", () => {
    expect(CASE_RUN_LINE[caseRunLine("running")]).toBe("Running a fleet step on this case");
  });

  it("words an ended run in the past, whatever its outcome", () => {
    for (const outcome of ["succeeded", "failed", "interrupted", "cancelled", null] as const) {
      expect(CASE_RUN_LINE[caseRunLine(outcome)]).toBe("Ran a fleet step on this case");
    }
  });
});
