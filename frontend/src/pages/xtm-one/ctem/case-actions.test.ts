// The case actions are one set for the Cases bulk bar and the case page (#1751):
// a case action added for one surface appears on the other, and the case page
// lists every action of the bulk bar for one case, as a button or a More
// actions row, disabled with its reason when the state refuses it.

import { describe, expect, it } from "vitest";

import { CTEM_CASE_STATES, type CtemCaseState } from "@/hooks/api/useCtemCasesApi";
import {
  BULK_CASE_ACTIONS,
  CASE_ACTIONS,
  SINGLE_CASE_ACTIONS,
  caseActionAvailability,
  caseHeaderActions,
  primaryCaseAction,
} from "./case-actions";

const multiOnly = CASE_ACTIONS.filter((a) => a.scope === "bulk").map((a) => a.id);
const singleOnly = CASE_ACTIONS.filter((a) => a.scope === "single").map((a) => a.id);

describe("case actions parity", () => {
  it("offers on one case the bulk set minus multi-only actions, plus the single-only ones", () => {
    const expected = [...BULK_CASE_ACTIONS.filter((id) => !multiOnly.includes(id)), ...singleOnly].sort();
    expect([...SINGLE_CASE_ACTIONS].sort()).toEqual(expected);
  });

  it("has no duplicate action", () => {
    expect(new Set(CASE_ACTIONS.map((a) => a.id)).size).toBe(CASE_ACTIONS.length);
  });

  it.each(CTEM_CASE_STATES.map((s) => [s]))("lists every bulk action for one %s case, once", (state) => {
    const { buttons, menu } = caseHeaderActions(state as CtemCaseState);
    const shown = [...buttons, ...menu.map((m) => m.id)];
    expect(new Set(shown).size).toBe(shown.length);
    for (const id of BULK_CASE_ACTIONS.filter((a) => !multiOnly.includes(a))) expect(shown).toContain(id);
  });
});

describe("case actions by state", () => {
  it("keeps Accept risk and the next state as the buttons of an open case", () => {
    expect(caseHeaderActions("new").buttons).toEqual(["accept", "start"]);
    expect(caseHeaderActions("remediating").buttons).toEqual(["accept", "resolve"]);
  });

  it("puts Resolve in the menu before remediation, enabled", () => {
    const row = caseHeaderActions("new").menu.find((m) => m.id === "resolve");
    expect(row?.availability.enabled).toBe(true);
  });

  it("shows Reopen alone on an accepted or resolved case, the rest disabled with a reason", () => {
    for (const state of ["accepted_risk", "resolved"] as const) {
      const { buttons, menu } = caseHeaderActions(state);
      expect(buttons).toEqual(["reopen"]);
      for (const id of ["assign", "due"] as const) expect(menu.find((m) => m.id === id)?.availability.enabled).toBe(true);
      for (const row of menu.filter((m) => m.id !== "assign" && m.id !== "due")) {
        expect(row.availability.enabled).toBe(false);
        expect(row.availability.reason).toBeTruthy();
      }
    }
    expect(caseActionAvailability("start", "accepted_risk").reason).toBe("Accepted risk: reopen first");
  });

  it("never offers Reopen on an open case", () => {
    expect(caseHeaderActions("validating").menu.map((m) => m.id)).not.toContain("reopen");
    expect(primaryCaseAction("validating")).toBe("resolve");
  });

  it("gives no action to a viewer who may not decide", () => {
    expect(caseHeaderActions("new", false)).toEqual({ buttons: [], menu: [] });
  });
});
