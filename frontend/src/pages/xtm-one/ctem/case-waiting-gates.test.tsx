// A waiting decision is shown on the case page whatever the gate (#2464): a
// waiting ctem.mobilize gate renders the unified decision card in Mobilization,
// read by the record's work_item_id; a waiting accept_risk gate gets its card
// in the page's "Waits for a decision" card; every waiting record has its line.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemCaseGateRecord } from "@/hooks/api/useCtemCasesApi";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { renderWithProviders } from "@/testing/providers";

const state = vi.hoisted(() => ({
  byId: {} as Record<string, unknown>,
  ids: [] as Array<string | null | undefined>,
  lists: [] as Array<{ filters: unknown; enabled: boolean }>,
  admin: false,
  access: null as string | null,
}));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: state.admin, user: { ctem_access: state.access } }) }));
vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItem: (id: string | null | undefined) => {
    state.ids.push(id);
    return { data: id ? state.byId[id] : undefined, isLoading: false, refetch: vi.fn() };
  },
  useWorkItems: (filters: unknown, enabled: boolean) => {
    state.lists.push({ filters, enabled });
    return { data: undefined, isLoading: false, refetch: vi.fn() };
  },
}));
vi.mock("@/components/work-items/WorkItemDecisionCard", () => ({
  WorkItemDecisionCard: ({ item, canAnswer }: { item: { id: string }; canAnswer: boolean }) => (
    <div data-testid="unified-decision-card" data-item={item.id} data-can-answer={String(canAnswer)} />
  ),
}));

const { CaseMobilization } = await import("./CaseMobilization");
const { CaseWaitingGates, waitingGateRecords } = await import("./CaseWaitingGates");

function rec(over: Partial<CtemCaseGateRecord>): CtemCaseGateRecord {
  return {
    key: "ctem.mobilize",
    phase: null,
    status: "waiting",
    asked_at: "2026-11-26T06:00:00Z",
    asked_of: { type: "group", id: "g1", name: "SecOps" },
    asked_by_agent: null,
    deadline: null,
    answered_at: null,
    answered_by: null,
    surface: null,
    option: null,
    comment: null,
    reason: null,
    passed_at: null,
    wait_seconds: 600,
    decision_id: null,
    work_item_id: "wi-mob",
    decision_filed: true,
    recorded_on_case: true,
    next: null,
    ...over,
  };
}

const OPEN = (id: string, is_company_managed = false) => ({ id, status: "blocked", needs_human: true, ask: { question: "Go?", options: [] }, is_company_managed });

beforeEach(() => {
  state.byId = {};
  state.ids = [];
  state.lists = [];
  state.admin = false;
  state.access = null;
});

describe("Mobilization gates (#2464)", () => {
  it("renders a waiting ctem.mobilize decision on the unified card, read by the record's work item", () => {
    state.byId["wi-mob"] = OPEN("wi-mob");
    renderWithProviders(
      <MemoryRouter>
        <CaseMobilization history={[]} tickets={[]} gates={[rec({})]} />
      </MemoryRouter>,
    );
    const gate = screen.getByTestId("ctem-case-mobilization-gate-mobilize");
    expect(within(gate).getByTestId("unified-decision-card").getAttribute("data-item")).toBe("wi-mob");
    expect(within(gate).getByTestId("ctem-case-mobilization-gate-mobilize-record-status")).toHaveTextContent("Waiting");
    expect(state.ids).toContain("wi-mob");
    // No correlation-key read for a record that names its item.
    expect(state.lists.every((l) => !l.enabled)).toBe(true);
    // A gate met is not an empty section.
    expect(screen.queryByTestId("ctem-case-mobilization-empty")).toBeNull();
  });

  it("keeps a company-managed decision read-only for a viewer without View and decide", () => {
    state.byId["wi-mob"] = OPEN("wi-mob", true);
    renderWithProviders(
      <MemoryRouter>
        <CaseMobilization history={[]} tickets={[]} gates={[rec({})]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("unified-decision-card").getAttribute("data-can-answer")).toBe("false");
  });

  it("says no decision was filed when the record says so", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseMobilization history={[]} tickets={[]} gates={[rec({ work_item_id: null, decision_filed: false })]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-case-mobilization-gate-mobilize-not-filed")).toBeInTheDocument();
    expect(screen.queryByTestId("unified-decision-card")).toBeNull();
  });

  it("says the decision is elsewhere when the viewer cannot read the item", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseMobilization history={[]} tickets={[]} gates={[rec({ key: "ctem.production_change", work_item_id: "wi-hidden" })]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-case-mobilization-gate-production-change-elsewhere")).toBeInTheDocument();
  });

  it("renders a waiting ctem.production_change decision on the unified card when its record names its item", () => {
    state.byId["wi-prod"] = OPEN("wi-prod");
    renderWithProviders(
      <MemoryRouter>
        <CaseMobilization history={[]} tickets={[]} gates={[rec({ key: "ctem.production_change", work_item_id: "wi-prod" })]} />
      </MemoryRouter>,
    );
    const gate = screen.getByTestId("ctem-case-mobilization-gate-production-change");
    expect(within(gate).getByTestId("unified-decision-card").getAttribute("data-item")).toBe("wi-prod");
    expect(screen.queryByTestId("ctem-case-mobilization-gate-production-change-elsewhere")).toBeNull();
  });

  it("shows the answered record once decided, with no card", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseMobilization
          history={[]}
          tickets={[]}
          gates={[rec({ status: "answered", answered_at: "2026-11-26T07:00:00Z", answered_by: { id: "u1", name: "Ada" }, option: { key: "approve", label: null } })]}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-case-mobilization-gate-mobilize-record-status")).toHaveTextContent("Approved");
    expect(screen.queryByTestId("unified-decision-card")).toBeNull();
    expect(state.ids.every((id) => !id)).toBe(true);
  });
});

describe("Waits for a decision (#2464)", () => {
  it("lists every waiting gate with its recipient and link, and answers a gate no section owns", () => {
    state.byId["wi-risk"] = OPEN("wi-risk");
    const gates = [rec({}), rec({ key: "ctem.accept_risk", work_item_id: "wi-risk", asked_of: { type: "admins", name: null }, asked_at: "2026-11-26T06:30:00Z" })];
    renderWithProviders(
      <MemoryRouter>
        <CaseWaitingGates state="waiting_decision" gates={gates} history={[]} />
      </MemoryRouter>,
    );
    const lines = screen.getAllByTestId("ctem-case-waiting-gate");
    expect(lines.map((l) => l.getAttribute("data-gate"))).toEqual(["ctem.mobilize", "ctem.accept_risk"]);
    expect(lines[0]).toHaveTextContent("Waits for SecOps: Mobilize: create the tickets");
    expect(lines[1]).toHaveTextContent("Waits for the administrators: Accept a residual risk beyond the policy cap");
    expect(within(lines[0]).getByTestId("ctem-case-waiting-gate-open").getAttribute("href")).toContain("wi-mob");
    // The mobilize card is answered in Mobilization; only accept_risk gets its card here.
    const cards = screen.getAllByTestId("unified-decision-card");
    expect(cards.map((c) => c.getAttribute("data-item"))).toEqual(["wi-risk"]);
    // The inline cards are set apart from the lines, never read as one more line.
    expect(within(screen.getByTestId("ctem-case-waiting-gate-decisions")).getByTestId("unified-decision-card")).toBeInTheDocument();
  });

  it("says a gate with no decision of its own filed cannot be answered yet", () => {
    renderWithProviders(
      <MemoryRouter>
        <CaseWaitingGates state="waiting_decision" gates={[rec({ key: "ctem.accept_risk", work_item_id: null, decision_filed: false })]} history={[]} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("ctem-case-waiting-gate-decision-not-filed")).toBeInTheDocument();
    expect(screen.queryByTestId("ctem-case-waiting-gate-decision-elsewhere")).toBeNull();
    expect(screen.queryByTestId("ctem-case-waiting-gate-open")).toBeNull();
  });

  it("draws nothing when no gate waits or the case is closed", () => {
    expect(waitingGateRecords("waiting_decision", [rec({ status: "answered" })])).toEqual([]);
    expect(waitingGateRecords("resolved", [rec({})])).toEqual([]);
    expect(waitingGateRecords("remediating", undefined)).toEqual([]);
    const { container } = renderWithProviders(
      <MemoryRouter>
        <CaseWaitingGates state="remediating" gates={[]} history={[]} />
      </MemoryRouter>,
    );
    expect(container.querySelector("[data-testid='ctem-case-waiting-gates']")).toBeNull();
  });

  it("has every new word in the four catalogs", () => {
    const keys = [
      "a person",
      "a group",
      "everyone in the company",
      "the administrators",
      "Waits for a decision",
      "Waits for {name}: {gate}",
      "No decision was filed for this gate, so nobody can answer it yet. The fleet files it the next time it runs this case.",
      "This gate waits for a person: the decision is in their Waiting for you list.",
    ];
    for (const catalog of [en, fr, es, ja] as Record<string, string>[])
      for (const key of keys) expect(catalog[key], `${key} is in every catalog`).toBeTruthy();
  });
});
