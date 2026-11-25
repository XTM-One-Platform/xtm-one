// The History event dialog says each thing once (UX round 1 on #1708, B1): a gate row
// leaves "What happened" and the record's own title to the dialog title, a note row is
// titled "Added a note" and shows its text once, under Note; a single fact spans the
// body instead of half a two-column grid (B2).

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemCaseEvent, CtemCaseGateRecord } from "@/hooks/api/useCtemCasesApi";
import type { AuditRow } from "@/lib/ctem-case-audit";
import { CaseEventDialog } from "./CaseEventDialog";

function ev(over: Partial<CtemCaseEvent>): CtemCaseEvent {
  return { at: "2026-11-23T12:41:00Z", actor: "system", kind: "note", issue: null, from: null, to: null, reason: null, evidence: null, ...over };
}

function row(event: CtemCaseEvent): AuditRow {
  return { kind: "event", key: "k1", at: event.at, phase: "validation", autonomy: "system", actorType: "system", event };
}

const GATE: CtemCaseGateRecord = {
  key: "ctem.validation.non_readonly",
  phase: "validation",
  status: "answered",
  asked_at: "2026-11-23T11:56:00Z",
  asked_of: null,
  asked_by_agent: null,
  deadline: null,
  answered_at: "2026-11-23T12:41:00Z",
  answered_by: { id: "u-1", name: "Samuel Hassine" },
  surface: null,
  option: { key: "A", label: "Launch the atomic test" },
  comment: null,
  reason: null,
  passed_at: null,
  wait_seconds: null,
  decision_id: null,
  work_item_id: null,
  recorded_on_case: true,
  next: null,
};

function open(r: AuditRow, action: string, gates?: CtemCaseGateRecord[]) {
  renderWithProviders(
    <MemoryRouter>
      <CaseEventDialog
        row={r}
        onClose={vi.fn()}
        renderEvent={(e) => <span>{`rendered ${e.kind}`}</span>}
        renderRun={() => null}
        actorName={() => "System"}
        actionText={() => action}
        changeText={() => null}
        personName={() => null}
        gates={gates}
        backTo="/cases/c1"
      />
    </MemoryRouter>,
  );
  return screen.getByTestId("ctem-case-event-dialog");
}

describe("CaseEventDialog", () => {
  it("leaves a gate row's story to its decision record, titled once by the dialog", () => {
    const dialog = open(row(ev({ kind: "gate", from: GATE.key, to: "approved" })), "Validation of a test that changes the target", [GATE]);
    expect(within(dialog).queryByTestId("ctem-case-event-what")).toBeNull();
    expect(within(dialog).getAllByText("Validation of a test that changes the target")).toHaveLength(1);
    expect(within(dialog).getByTestId("ctem-case-event-gate-record")).toBeInTheDocument();
  });

  it("titles a note row 'Added a note' and shows its text once, under Note", () => {
    const text = "Patch window agreed with the network team for Thursday.";
    const dialog = open(row(ev({ kind: "note", reason: text, headline: text })), text);
    expect(within(dialog).getByRole("heading", { name: "Added a note" })).toBeInTheDocument();
    expect(within(dialog).getAllByText(text)).toHaveLength(1);
    expect(within(dialog).getByTestId("ctem-case-event-note").textContent).toBe(text);
    expect(within(dialog).queryByTestId("ctem-case-event-what")).toBeNull();
  });

  it("never prints an applied change's evidence JSON (UX round 1 on #2076, B2)", () => {
    const evidence = JSON.stringify({ ids: ["ioc-1"], rollback: "Delete the indicator" });
    const dialog = open(row(ev({ kind: "applied_change", from: "Microsoft Defender", to: "ioc_prevention", evidence })), "Applied a change");
    expect(within(dialog).queryByTestId("ctem-case-event-evidence")).toBeNull();
    expect(dialog.textContent).not.toContain("ioc-1");
  });

  it("keeps What happened for any other row", () => {
    const dialog = open(row(ev({ kind: "retargeted" })), "Changed the target version");
    expect(within(dialog).getByTestId("ctem-case-event-what").textContent).toContain("rendered retargeted");
  });
});
