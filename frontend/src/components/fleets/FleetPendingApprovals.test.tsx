// The fleet's pending approvals (#10202, slice D of #10000): the runs of the
// fleet that wait for a person, read with a GET narrowed to the fleet and to
// awaiting_human (no run started, no quota), a row unfolding the decision path,
// an empty state when nothing waits.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { AssignmentRunDetail } from "@/types";
import { renderWithProviders } from "@/testing/providers";

import { FleetPendingApprovals, pendingActionLabels, pendingKind, pendingToolNames, stillWaiting } from "./FleetPendingApprovals";

const api = vi.hoisted(() => ({
  runs: [] as unknown[],
  calls: [] as unknown[][],
  mutate: vi.fn(),
}));
vi.mock("@/hooks/api/useAssignmentApi", () => ({
  useAllAssignmentRuns: (...args: unknown[]) => {
    api.calls.push(args);
    return { data: api.runs, isLoading: false, isError: false, refetch: vi.fn() };
  },
  useRespondToHumanInput: () => ({ mutate: api.mutate, isError: false }),
  useSubmitApprovalDecisions: () => ({ mutate: api.mutate, isError: false }),
  useDismissRun: () => ({ mutate: api.mutate, isError: false }),
}));

// The decisions read (#1493): off by default here, so the runs-feed suite below reads the fallback.
const decisions = vi.hoisted(() => ({
  data: { enabled: false, reason: "off", items: [] as unknown[] } as { enabled: boolean; reason: string | null; items: unknown[] },
  calls: [] as unknown[][],
  answer: vi.fn(async () => undefined),
}));
vi.mock("@/hooks/api/useDecisionApi", () => ({
  useWaitingDecisions: (...args: unknown[]) => {
    decisions.calls.push(args);
    return { data: decisions.data, isLoading: false, isError: false, refetch: vi.fn() };
  },
  useAnswerDecision: () => ({ mutateAsync: decisions.answer, isPending: false }),
}));

const decision = (id: string, extra: Record<string, unknown>) => ({
  id,
  source_type: "work_item",
  source_id: id,
  kind: "work_item",
  question: `Question ${id}?`,
  ask: { question: `Question ${id}?`, options: [{ key: "A", label: "Go" }, { key: "B", label: "Stop" }] },
  origin: `Item ${id}`,
  agent: "Loop Orchestrator",
  asked_at: "2026-11-23T10:00:00Z",
  deadline: null,
  link: `/tasks/work-items/${id}`,
  can_answer: true,
  ...extra,
});

const run = (id: string, extra: Partial<AssignmentRunDetail>): AssignmentRunDetail =>
  ({
    id,
    assignment_id: "a1",
    assignment_name: "Daily triage",
    agent_id: "ag1",
    agent_name: "Analyst",
    status: "awaiting_human",
    is_company_managed: false,
    human_input_question: null,
    human_input_response: null,
    human_input_history: null,
    pending_approvals: null,
    approval_answered: false,
    created_at: "2026-11-08T20:00:00Z",
    ...extra,
  }) as AssignmentRunDetail;

const proposal = (name: string) => ({
  tool_call_id: `c-${name}`,
  tool_name: name,
  tool_description: "",
  arguments: {},
  input_schema: {},
});

beforeEach(() => {
  api.runs = [];
  api.calls = [];
  api.mutate.mockClear();
  decisions.data = { enabled: false, reason: "off", items: [] };
  decisions.calls = [];
  decisions.answer.mockClear();
});
afterEach(cleanup);

describe("pending approvals helpers", () => {
  it("names what a run waits on and keeps only what still waits for a person", () => {
    const actions = run("r1", { pending_approvals: [proposal("send_email"), proposal("send_email"), proposal("create_ticket")] as never });
    expect(pendingKind(actions)).toBe("actions");
    expect(pendingToolNames(actions)).toEqual(["send_email", "create_ticket"]);
    expect(pendingKind(run("r2", { human_input_question: "Which tenant?" }))).toBe("question");
    const decided = run("r3", { pending_approvals: [proposal("x")] as never, approval_answered: true });
    const answered = run("r4", { human_input_response: "yes" });
    const finished = run("r5", { status: "completed" });
    expect(stillWaiting([actions, decided, answered, finished]).map((r) => r.id)).toEqual(["r1"]);
  });

  it("names the actions as a person reads them, never the internal tool id (UX r1 B3)", () => {
    const actions = run("r1", { pending_approvals: [proposal("send_email"), proposal("jira_create_ticket")] as never });
    const labels = pendingActionLabels(actions, (id) => id);
    expect(labels).toEqual(["Sending email", "Jira create ticket"]);
    expect(labels.join(" ")).not.toMatch(/_/);
  });
});

describe("FleetPendingApprovals with the decisions read on (#1493)", () => {
  it("reads the fleet's decisions and lists runs and work items, one row each, counted in the summary", () => {
    decisions.data = {
      enabled: true,
      reason: null,
      items: [
        decision("run:1", { source_type: "run", kind: "tool_approval", question: "1 action: Manage coverage", origin: "Gate answered" }),
        decision("run:2", { source_type: "run", kind: "hitl", question: "Which feed?", origin: "Daily triage" }),
        decision("work_item:3", {}),
        decision("work_item:4", {}),
        decision("work_item:5", {}),
      ],
    };
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    expect(decisions.calls[0]).toEqual([true, "f1"]);
    expect(api.calls).toHaveLength(0);
    expect(screen.getAllByTestId("fleet-pending-row")).toHaveLength(5);
    expect(screen.getByTestId("fleet-pending-summary").textContent).toContain("5 decisions wait for a person");
    expect(screen.getByText("Work item: Item work_item:3")).toBeTruthy();
  });

  it("unfolds the shared decision card in place, with a link to the source", () => {
    decisions.data = { enabled: true, reason: null, items: [decision("work_item:3", {})] };
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    fireEvent.click(screen.getByTestId("fleet-pending-toggle"));
    const panel = screen.getByTestId("fleet-pending-decision");
    expect(panel).toBeTruthy();
    expect(screen.getByTestId("fleet-pending-open-source").getAttribute("href")).toBe("/tasks/work-items/work_item:3");
  });

  it("marks a decision addressed to someone else read only, never hidden", () => {
    decisions.data = { enabled: true, reason: null, items: [decision("work_item:9", { can_answer: false })] };
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    expect(screen.getAllByTestId("fleet-pending-row")).toHaveLength(1);
    expect(screen.getByTestId("fleet-pending-read-only").textContent).toBe("Read only");
    fireEvent.click(screen.getByTestId("fleet-pending-toggle"));
    expect(screen.getByText("Read only: this decision is addressed to someone else.")).toBeTruthy();
    // A card shown read only never says "your decision" (UX r1 B2 of #1497).
    expect(screen.queryByText("Waiting for your decision")).toBeNull();
    expect(screen.getByText("Waiting for a decision")).toBeTruthy();
  });

  it("names the source with the inbox cell, never a native title (UX r1 B1 of #1497)", () => {
    decisions.data = { enabled: true, reason: null, items: [decision("work_item:3", {})] };
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    const source = screen.getByText("Work item: Item work_item:3");
    expect(source.closest("[title]")).toBeNull();
  });
});

describe("FleetPendingApprovals", () => {
  it("reads the fleet's awaiting_human runs only, through the runs feed", () => {
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    const args = api.calls[0];
    expect(args[2]).toBe("awaiting_human");
    expect(args[13]).toBe("f1");
  });

  it("shows an empty state when nothing waits", () => {
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    expect(screen.getByText("Nothing waits for approval")).toBeTruthy();
    expect(screen.queryByTestId("fleet-pending-row")).toBeNull();
  });

  it("lists each waiting run and unfolds its decision path on click", () => {
    api.runs = [
      run("r1", { pending_approvals: [proposal("send_email")] as never }),
      run("r2", { agent_name: "Writer", human_input_question: "Which tenant?" }),
    ];
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    const rows = screen.getAllByTestId("fleet-pending-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("1 action: Sending email")).toBeTruthy();
    expect(screen.queryByText(/send_email/)).toBeNull();
    expect(screen.queryByTestId("fleet-pending-decision")).toBeNull();
    fireEvent.click(screen.getByText("1 action: Sending email"));
    expect(screen.getByTestId("fleet-pending-decision")).toBeTruthy();
  });

  it("heads the section with a pending summary and a trailing Review / Hide per row (UX r1 B5, B6)", () => {
    api.runs = [
      run("r1", { pending_approvals: [proposal("send_email")] as never }),
      run("r2", { agent_name: "Writer", human_input_question: "Which tenant?" }),
    ];
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    expect(screen.getByTestId("fleet-pending-summary").textContent).toContain("2 runs wait for a person");
    // A relative time reads after "started", never after "since" (UX r2 B2).
    expect(screen.getByTestId("fleet-pending-summary").textContent).toContain("oldest started ");
    expect(screen.getByTestId("fleet-pending-summary").textContent).not.toContain("oldest since");
    const toggles = screen.getAllByTestId("fleet-pending-toggle");
    expect(toggles).toHaveLength(2);
    expect(toggles[0].textContent).toBe("Review");
    expect(toggles[0].getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggles[0]);
    expect(screen.getAllByTestId("fleet-pending-toggle")[0].textContent).toBe("Hide");
    expect(screen.getAllByTestId("fleet-pending-toggle")[0].getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByTestId("fleet-pending-decision")).toHaveLength(1);
  });

  it("keeps every row one line high: agent and waits-on truncate on one line (UX r1 B1, B2)", () => {
    api.runs = [run("r1", { agent_name: "A very long agent name that would wrap", human_input_question: "A long question ".repeat(20) })];
    renderWithProviders(<MemoryRouter><FleetPendingApprovals fleetId="f1" /></MemoryRouter>);
    for (const id of ["fleet-pending-agent", "fleet-pending-waits-on"]) {
      expect(screen.getByTestId(id).getAttribute("data-truncate")).toBe("one-line");
    }
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
  });
});
