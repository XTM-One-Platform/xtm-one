// The Act now queue of the CTEM command center (C2 slice V2, refocused by
// #10721): one ranked list of what to do first, never repeating the briefing,
// the stage assessments or the chat, one row per stage for the module facts.

import { describe, expect, it } from "vitest";
import { Zap } from "lucide-react";

import en from "@/i18n/lang/en.json";
import {
  ACT_NOW_COLLAPSED, ACT_NOW_MAX, ASK_AGENT_ROW_ID, actNowAskAiPrompt, actNowFleetWork, actNowGroup, actNowGroupCounts, actNowIsGeneric, actNowOffersAskAi,
  actNowPriorityLabel, actNowQueue, actNowShowsPriority, type ActNowRow,
} from "./act-now";

const t = (text: string, values: Record<string, string> = {}) =>
  text.replace(/\{(\w+)\}/g, (_, k: string) => values[k] ?? `{${k}}`);

function row(id: string, priority: number, over: Partial<ActNowRow> = {}): ActNowRow {
  return {
    id, priority, icon: Zap, tone: "", title: `Row ${id}`, reason: "", cta: "Open", onAction: () => undefined,
    source: "module", ...over,
  };
}

describe("actNowQueue", () => {
  it("ranks by priority, then puts what a person owes before the rest", () => {
    const q = actNowQueue([
      row("setup", 50, { source: "setup" }),
      row("kev", 95),
      row("approval", 95, { source: "approval" }),
      row("work", 95, { source: "work_item" }),
    ]);
    expect(q.map((r) => r.id)).toEqual(["work", "approval", "kev", "setup"]);
  });

  it("never holds a briefing step, a stage assessment or the chat row: they live elsewhere on the page", () => {
    const q = actNowQueue([
      row("briefing", 92, { source: "briefing" }),
      row("stage", 70, { source: "stage" }),
      row(ASK_AGENT_ROW_ID, 10, { source: "agent" }),
      row("kev", 95),
    ]);
    expect(q.map((r) => r.id)).toEqual(["kev"]);
  });

  it("drops a row that says what a briefing next step already says", () => {
    const q = actNowQueue([row("patch", 80, { title: "Patch the edge VPN." }), row("kev", 95)], undefined, ["patch the  edge VPN"]);
    expect(q.map((r) => r.id)).toEqual(["kev"]);
  });

  it("merges the module facts of one stage into one row, the highest leading", () => {
    const q = actNowQueue([
      row("detection", 90, { stage: "validation", title: "Detection: 232 failed expectations" }),
      row("prevention", 88, { stage: "validation", title: "Prevention: 232 failed expectations" }),
      row("kev", 95, { stage: "prioritization", title: "Triage 1.3K known-exploited exposures" }),
    ]);
    expect(q).toHaveLength(2);
    expect(q[0].id).toBe("kev");
    expect(q[1].id).toBe("detection+1");
    expect(q[1].title).toBe("Detection: 232 failed expectations · Prevention: 232 failed expectations");
    expect(q.filter((r) => r.title.startsWith("Prevention"))).toHaveLength(0);
  });

  it("keeps a row once when two say the same thing, the higher one", () => {
    const q = actNowQueue([row("a", 76, { title: "Wire in your stack." }), row("b", 70, { title: "wire in your  stack" })]);
    expect(q.map((r) => r.id)).toEqual(["a"]);
  });

  it("holds at most twenty rows, and the card shows three before Show all (#11547 S2d)", () => {
    const rows = Array.from({ length: 30 }, (_, i) => row(`r${i}`, 100 - i, { source: "setup" }));
    const q = actNowQueue(rows);
    expect(q).toHaveLength(ACT_NOW_MAX);
    expect(q[0].id).toBe("r0");
    expect(ACT_NOW_COLLAPSED).toBe(3);
  });

  it("groups the rows by source for the bar: posture, blind spots, setup, waiting for you", () => {
    expect(actNowGroup(row("kev", 95))).toBe("posture");
    expect(actNowGroup(row("connect-validation", 100, { source: "setup" }))).toBe("blind_spots");
    expect(actNowGroup(row("connect-stack", 50, { source: "setup" }))).toBe("setup");
    expect(actNowGroup(row("wire", 50, { source: "setup" }))).toBe("setup");
    expect(actNowGroup(row("w", 98, { source: "work_item" }))).toBe("waiting");
    expect(actNowGroup(row("a", 97, { source: "approval" }))).toBe("waiting");
    const counts = actNowGroupCounts([
      row("w", 98, { source: "work_item" }),
      row("kev", 95),
      row("det", 90),
      row("wire", 50, { source: "setup" }),
    ]);
    expect(counts).toEqual([
      { group: "posture", count: 2 },
      { group: "setup", count: 1 },
      { group: "waiting", count: 1 },
    ]);
  });

  it("drops a repeated id", () => {
    expect(actNowQueue([row("a", 5), row("a", 9, { title: "Other" })]).map((r) => r.id)).toEqual(["a"]);
  });

  it("labels each row high, medium or low", () => {
    expect(actNowPriorityLabel(row("a", 95))).toBe("high");
    expect(actNowPriorityLabel(row("a", 80))).toBe("medium");
    expect(actNowPriorityLabel(row("a", 50))).toBe("low");
    expect(actNowPriorityLabel(row("a", 50, { priorityLabel: "high" }))).toBe("high");
  });

  it("shows no priority label on a fleet working row, a label on every other row (UX round 1 on #920, B2)", () => {
    expect(actNowShowsPriority(row("f", 60, { source: "fleet" }))).toBe(false);
    for (const source of ["work_item", "approval", "module", "setup"] as const) {
      expect(actNowShowsPriority(row("x", 60, { source }))).toBe(true);
    }
  });

  it("merges the facts of one stage that quote the same figure into one phrase (#11213)", () => {
    const merge = (subjects: string[]) => `${subjects.join(" and ")}: 232 failed expectations`;
    const q = actNowQueue([
      row("detection", 90, { stage: "validation", title: "Detection: 232 failed expectations", subject: "Detection", figure: "232", mergeTitle: merge }),
      row("prevention", 88, { stage: "validation", title: "Prevention: 232 failed expectations", subject: "Prevention", figure: "232", mergeTitle: merge }),
    ]);
    expect(q.map((r) => r.title)).toEqual(["Detection and Prevention: 232 failed expectations"]);
  });

  it("keeps both phrases when the figures differ", () => {
    const merge = (subjects: string[]) => subjects.join(" and ");
    const q = actNowQueue([
      row("detection", 90, { stage: "validation", title: "Detection: 232 failed expectations", subject: "Detection", figure: "232", mergeTitle: merge }),
      row("prevention", 88, { stage: "validation", title: "Prevention: 12 failed expectations", subject: "Prevention", figure: "12", mergeTitle: merge }),
    ]);
    expect(q[0].title).toBe("Detection: 232 failed expectations · Prevention: 12 failed expectations");
  });

  it("calls the queue generic when it shows module facts and none is tailored", () => {
    expect(actNowIsGeneric([row("kev", 95)])).toBe(true);
    expect(actNowIsGeneric([row("kev", 95, { tailored: true }), row("cov", 80)])).toBe(false);
    expect(actNowIsGeneric([row("setup", 100, { source: "setup" }), row("w", 110, { source: "work_item" })])).toBe(false);
    expect(actNowIsGeneric([])).toBe(false);
  });

  it("offers Ask AI on the facts and the setup, not on what waits where it is answered", () => {
    expect(actNowOffersAskAi({ source: "module" })).toBe(true);
    expect(actNowOffersAskAi({ source: "setup" })).toBe(true);
    expect(actNowOffersAskAi({ source: "work_item" })).toBe(false);
    expect(actNowOffersAskAi({ source: "approval" })).toBe(false);
  });

  it("words the Ask AI message with the row, its why, its scope and the guided path asked", () => {
    const generic = actNowAskAiPrompt(t, { title: "Triage 4 known-exploited exposures", reason: "Actively exploited", stage: "prioritization" });
    expect(generic).toContain("\"Triage 4 known-exploited exposures\" (stage: prioritization)");
    expect(generic).toContain("Why it is there: Actively exploited.");
    expect(generic).toContain("This action is generic");
    expect(generic).toContain("guide me step by step");
    const tailored = actNowAskAiPrompt(t, { title: "Patch", reason: "Primary asset", tailored: true });
    expect(tailored).toContain("ranked by our organization context");
    expect(tailored).not.toContain("(stage:");
  });

  it("has every Ask AI sentence in the English catalog", () => {
    const catalog = en as Record<string, string>;
    const seen: string[] = [];
    const record = (text: string) => { seen.push(text); return text; };
    actNowAskAiPrompt(record, { title: "a", reason: "b", stage: "c" });
    actNowAskAiPrompt(record, { title: "a", reason: "b", tailored: true });
    for (const key of seen) expect(catalog[key]).toBe(key);
  });
});

describe("actNowFleetWork (#915)", () => {
  const run = (runId: string, at: string, over: Partial<Parameters<typeof actNowFleetWork>[0][number]> = {}) => ({
    runId, at, fleetName: "Vulnerability prioritization", agentName: "Triage", outcome: "running", caseId: "c-1", ...over,
  });

  it("keeps one entry per case, its newest in-flight run, and counts the others", () => {
    const work = actNowFleetWork(
      [
        run("old", "2026-11-21T10:00:00Z", { agentName: "Orchestrator" }),
        run("new", "2026-11-21T11:00:00Z"),
        run("done", "2026-11-21T12:00:00Z", { outcome: "succeeded" }),
        run("q", "2026-11-21T09:00:00Z", { caseId: "c-2", outcome: "pending", agentName: null }),
      ],
      { "c-1": "Upgrade nginx", "c-2": "Disable HTTP/2" },
    );
    expect(work).toEqual([
      { caseId: "c-1", caseTitle: "Upgrade nginx", fleetName: "Vulnerability prioritization", agentName: "Triage", pending: false, runId: "new", others: 1 },
      { caseId: "c-2", caseTitle: "Disable HTTP/2", fleetName: "Vulnerability prioritization", agentName: null, pending: true, runId: "q", others: 0 },
    ]);
  });

  it("leaves out a run with no case, a finished run and a case the reader cannot see", () => {
    expect(actNowFleetWork(
      [
        run("a", "2026-11-21T10:00:00Z", { caseId: null }),
        run("b", "2026-11-21T10:00:00Z", { outcome: "failed" }),
        run("c", "2026-11-21T10:00:00Z", { caseId: "hidden" }),
      ],
      { "c-1": "Upgrade nginx" },
    )).toEqual([]);
  });

  it("puts a fleet row in the working group, after what waits for the reader", () => {
    expect(actNowGroup(row("f", 60, { source: "fleet" }))).toBe("working");
    const q = actNowQueue([row("f", 98, { source: "fleet" }), row("w", 98, { source: "work_item" })]);
    expect(q.map((r) => r.id)).toEqual(["w", "f"]);
    expect(actNowOffersAskAi({ source: "fleet" })).toBe(false);
    expect(actNowIsGeneric([row("f", 60, { source: "fleet" })])).toBe(false);
  });
});
