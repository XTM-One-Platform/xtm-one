// The Mobilization card of a case page (#2061): the mode the case's Mobilize step
// took (ticket, applied change, both) and every change applied through a bound
// integration under ctem.production_change, with what was pushed, the ids the
// integration returned and how to revert it, in the four catalogs.
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import type { CtemCaseEvent, CtemCaseTicket } from "@/hooks/api/useCtemCasesApi";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { renderWithProviders } from "@/testing/providers";

import { CaseMobilization } from "./CaseMobilization";
import { CASE_EVENT_LABEL } from "./case-events";
import { APPLIED_CHANGE_KIND_LABEL, MOBILIZATION_MODE_LABEL, appliedChanges, mobilizationMode } from "./case-mobilization";

function change(overrides: Partial<CtemCaseEvent> = {}): CtemCaseEvent {
  return {
    at: "2026-11-25T09:00:00Z",
    actor: "agent-1",
    kind: "applied_change",
    issue: null,
    from: "Microsoft Defender",
    to: "ioc_prevention",
    reason: "3 IPv4 indicators, action Block, all devices, expire in 30 days",
    evidence: JSON.stringify({ ids: ["ind-1", "ind-2", "ind-3"], rollback: "Delete the three indicators" }),
    ...overrides,
  };
}

const TICKET: CtemCaseTicket = { integration: "i1", key: "SEC-12", url: null, status: "open", at: "2026-11-25T08:00:00Z" };

describe("case Mobilization (#2061)", () => {
  it("reads every applied change from the history, newest first, with its ids and rollback", () => {
    const older = change({ at: "2026-11-24T09:00:00Z", to: "detection_rule", reason: "Sigma rule as a SIEM query", evidence: JSON.stringify({ ids: ["r-9"], rollback: "Disable the rule" }) });
    const read = appliedChanges([older, change(), { ...change(), kind: "note" }]);
    expect(read.map((c) => c.kind)).toEqual(["ioc_prevention", "detection_rule"]);
    expect(read[0]).toMatchObject({ integration: "Microsoft Defender", ids: ["ind-1", "ind-2", "ind-3"], rollback: "Delete the three indicators" });
  });

  it("reads evidence that is not the JSON object as no ids and no rollback, an unknown kind as none", () => {
    const [read] = appliedChanges([change({ evidence: "not json", to: "teleport" })]);
    expect(read).toMatchObject({ kind: null, ids: [], rollback: "" });
  });

  it("names the mode from the tickets and the applied changes", () => {
    const changes = appliedChanges([change()]);
    expect(mobilizationMode([], undefined)).toBeNull();
    expect(mobilizationMode([], [TICKET])).toBe("ticket");
    expect(mobilizationMode(changes, [])).toBe("applied_change");
    expect(mobilizationMode(changes, [TICKET])).toBe("both");
  });

  it("draws the mode, the kind, the integration, what was pushed, the ids and how to revert", () => {
    renderWithProviders(<CaseMobilization history={[change()]} tickets={[TICKET]} />);
    expect(screen.getByTestId("ctem-case-mobilization-mode")).toHaveTextContent("Ticket and applied change");
    expect(screen.getByTestId("ctem-case-applied-change-kind")).toHaveTextContent("Indicators blocked");
    expect(screen.getByTestId("ctem-case-applied-change-integration")).toHaveTextContent("Microsoft Defender");
    expect(screen.getByTestId("ctem-case-applied-change-pushed")).toHaveTextContent("action Block");
    expect(screen.getByTestId("ctem-case-applied-change-ids")).toHaveTextContent("ind-2");
    expect(screen.getByTestId("ctem-case-applied-change-rollback")).toHaveTextContent("Delete the three indicators");
  });

  it("shows 8 ids then +N more", () => {
    const ids = Array.from({ length: 11 }, (_, i) => `id-${i}`);
    renderWithProviders(<CaseMobilization history={[change({ evidence: JSON.stringify({ ids, rollback: "r" }) })]} tickets={[]} />);
    expect(screen.getByTestId("ctem-case-applied-change-ids").querySelectorAll("code")).toHaveLength(8);
    expect(screen.getByTestId("ctem-case-applied-change-more")).toHaveTextContent("+3 more");
  });

  it("says what will appear when nothing was mobilized yet", () => {
    renderWithProviders(<CaseMobilization history={[]} tickets={[]} />);
    expect(screen.getByTestId("ctem-case-mobilization-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("ctem-case-mobilization-mode")).toBeNull();
  });

  it("draws the card in French", () => {
    renderWithProviders(<CaseMobilization history={[change()]} tickets={[]} />, { locale: "fr" });
    expect(screen.getByTestId("ctem-case-mobilization-mode")).toHaveTextContent("Changement appliqué");
    expect(screen.getByTestId("ctem-case-applied-change-kind")).toHaveTextContent("Indicateurs bloqués");
  });

  it("has every word of the card in the four catalogs", () => {
    const keys = [
      ...Object.values(MOBILIZATION_MODE_LABEL),
      ...Object.values(APPLIED_CHANGE_KIND_LABEL),
      CASE_EVENT_LABEL.applied_change,
      "Mobilization",
      "Mode",
      "Pushed",
      "Returned ids",
      "How to revert",
      "Applied changes",
      "None",
      "+{count} more",
    ];
    for (const catalog of [en, fr, es, ja] as Record<string, string>[])
      for (const key of keys) expect(catalog[key], `${key} is in every catalog`).toBeTruthy();
  });
});
