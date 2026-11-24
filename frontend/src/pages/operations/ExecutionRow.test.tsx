// Operations > Executions on the Operations list contract
// (components/ui/operations-list.tsx): one row anatomy for assignment and flow
// runs, the essentials on the row, every fact in the hover card, a run waiting
// for a person answered from under its row. Tests read text, roles and
// data-* attributes only (no-class-name-assertions.test.ts).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { AssignmentRunDetail, FlowRunSummary } from "@/types";
import {
  AssignmentExecutionRow,
  FlowExecutionRow,
  assignmentRunFacts,
  assignmentRunLine,
  assignmentRunTitle,
  flowRunFacts,
} from "./ExecutionRow";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ isAdmin: true, user: { id: "u-1", timezone: "UTC" } }),
}));

const t = (id: string, values?: Record<string, string | number>) =>
  values ? Object.entries(values).reduce((s, [k, v]) => s.replace(`{${k}}`, String(v)), id) : id;
const at = (iso: string) => `@${iso}`;

function run(over: Partial<AssignmentRunDetail> = {}): AssignmentRunDetail {
  return {
    id: "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
    assignment_id: "a-1",
    assignment_name: "Daily threat digest with a name long enough to be cut in the row",
    agent_id: "agent-1",
    agent_name: "Threat Intel Analyst",
    agent_icon: "bot",
    status: "completed",
    outcome: "ok",
    is_company_managed: false,
    notify_on_complete: false,
    show_in_digest: false,
    parent_run_id: null,
    child_run_count: 0,
    parent_agent_name: null,
    parent_agent_icon: null,
    trigger_payload: null,
    trigger_context: null,
    actions_taken: null,
    result_summary: "Digest sent to the channel.",
    rendered_prompt: null,
    agentic_steps: null,
    attachments: null,
    error: null,
    failure_cause: null,
    failure_determination: null,
    stop_reason: null,
    human_input_question: null,
    human_input_response: null,
    human_input_history: null,
    pending_approvals: null,
    langfuse_trace_id: null,
    llm_trace_id: null,
    total_input_tokens: 1200,
    total_output_tokens: 300,
    started_at: "2026-11-04T10:00:00Z",
    completed_at: "2026-11-04T10:02:05Z",
    created_at: "2026-11-04T09:59:58Z",
    ...over,
  };
}

const flow: FlowRunSummary = {
  id: "9a8b7c6d-5e4f-3a2b-1c0d-ef0123456789",
  flow_id: "flow-1",
  flow_version_number: 3,
  status: "failed",
  trigger_type: "schedule",
  depth: 0,
  parent_run_id: null,
  result_summary: null,
  error: "Node 4 timed out",
  created_at: "2026-11-04T09:00:00Z",
  started_at: "2026-11-04T09:00:01Z",
  completed_at: "2026-11-04T09:00:31Z",
  flow_name: "Enrich new indicators",
};

function renderRow(node: React.ReactNode) {
  renderWithProviders(<MemoryRouter>{node}</MemoryRouter>);
}

const handlers = {
  onSelect: vi.fn(),
  onDelete: vi.fn(),
  onRespond: vi.fn(),
  onDismiss: vi.fn(),
  onApprove: vi.fn(),
};

describe("assignment run facts", () => {
  it("names the run by its assignment, else the scheduled item, else a delegation, else the agent", () => {
    expect(assignmentRunTitle(run(), t)).toBe("Daily threat digest with a name long enough to be cut in the row");
    expect(
      assignmentRunTitle(run({ assignment_name: null, trigger_context: { type: "scheduled_work_item", title: "Post the reminder" } }), t),
    ).toBe("Post the reminder");
    expect(assignmentRunTitle(run({ assignment_name: null, parent_run_id: "p-1" }), t)).toBe("Delegation");
    expect(assignmentRunTitle(run({ assignment_name: null }), t)).toBe("Threat Intel Analyst");
  });

  it("puts what a waiting run needs on its line, before its error or result", () => {
    const waiting = run({ status: "awaiting_human", human_input_question: "Which feed?", result_summary: "partial" });
    expect(assignmentRunLine(waiting, t)).toEqual({ text: "Which feed?", tone: "warning" });
    expect(assignmentRunLine(run({ status: "failed", error: "Boom" }), t)).toEqual({ text: "Boom", tone: "error" });
    expect(assignmentRunLine(run(), t)).toEqual({ text: "Digest sent to the channel.", tone: "neutral" });
    expect(assignmentRunLine(run({ result_summary: null }), t)).toBeNull();
  });

  it("reads an interrupted run's error neutral", () => {
    expect(assignmentRunLine(run({ status: "failed", outcome: "interrupted", error: "Worker stopped" }), t)?.tone).toBe("neutral");
  });

  it("lists every fact in the contract's order: status, agent, origin, result, counts, run id, timestamps", () => {
    const facts = assignmentRunFacts(run({ child_run_count: 2, parent_agent_name: "Orchestrator" }), t, at);
    expect(facts.map((f) => f.label)).toEqual([
      "Status", "Agent", "Assignment", "Delegated from", "Result", "Delegation", "Tokens", "Run", "Created", "Started", "Ended", "Duration",
    ]);
    expect(facts.find((f) => f.label === "Duration")?.value).toBe("2m 5s");
  });
});

describe("flow run facts", () => {
  it("carries the trigger, the version, the error and the timestamps", () => {
    const facts = flowRunFacts(flow, t, at);
    expect(facts.map((f) => f.label)).toEqual(["Status", "Trigger", "Version", "Error", "Run", "Created", "Started", "Ended", "Duration"]);
    expect(facts.find((f) => f.label === "Version")?.value).toBe("3");
  });
});

describe("execution rows", () => {
  it("draws an assignment run on the shared grid: one line each, agent and run id in the meta column", () => {
    renderRow(<AssignmentExecutionRow run={run()} typeBadge selected={false} approveFailed={false} {...handlers} />);
    const row = screen.getByTestId("execution-row");
    expect(row.dataset.kind).toBe("assignment");
    expect(within(row).getByTestId("execution-title")).toHaveTextContent("Daily threat digest");
    expect(within(row).getByTestId("execution-status")).toHaveTextContent("Completed");
    expect(within(row).getByTestId("execution-summary")).toHaveTextContent("Digest sent to the channel.");
    expect(within(row).getByTestId("execution-agent")).toHaveTextContent("Threat Intel Analyst");
    expect(within(row).getByTestId("execution-run")).toHaveTextContent("0f1e2d3c");
    expect(within(row).getByRole("button", { name: "Delete run" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Answer" })).not.toBeInTheDocument();
  });

  it("keeps the selection box and the delete action wired", () => {
    renderRow(<AssignmentExecutionRow run={run()} typeBadge={false} selected={false} approveFailed={false} {...handlers} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "Select run" }));
    expect(handlers.onSelect).toHaveBeenCalledWith(run().id);
    fireEvent.click(screen.getByRole("button", { name: "Delete run" }));
    expect(handlers.onDelete).toHaveBeenCalledWith(run().id);
  });

  it("unfolds a waiting run's question under the row on Answer, the row line unchanged", () => {
    const waiting = run({ status: "awaiting_human", human_input_question: "Which feed should I use?" });
    renderRow(<AssignmentExecutionRow run={waiting} typeBadge={false} selected={false} approveFailed={false} {...handlers} />);
    expect(screen.getByTestId("execution-summary").dataset.tone).toBe("warning");
    expect(screen.queryByTestId("execution-answer")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Answer" }));
    const answer = screen.getByTestId("execution-answer");
    // The shared decision card (#842, S3): the question heads it, free text under it.
    expect(within(answer).getByTestId("run-decision-question").textContent).toBe("Which feed should I use?");
    expect(within(answer).getByLabelText("Your answer")).toBeInTheDocument();
    // The panel sits outside the fixed-height line.
    expect(screen.getByTestId("execution-line").contains(answer)).toBe(false);
  });

  it("tells assistive tech that Answer opens and closes the panel it controls", () => {
    const waiting = run({ status: "awaiting_human", human_input_question: "Which feed should I use?" });
    renderRow(<AssignmentExecutionRow run={waiting} typeBadge={false} selected={false} approveFailed={false} {...handlers} />);
    const answerButton = screen.getByRole("button", { name: "Answer" });
    expect(answerButton).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(answerButton);
    expect(answerButton).toHaveAttribute("aria-expanded", "true");
    const panel = screen.getByTestId("execution-answer");
    expect(answerButton).toHaveAttribute("aria-controls", panel.id);
    fireEvent.click(answerButton);
    expect(answerButton).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("execution-answer")).not.toBeInTheDocument();
  });

  it("draws a flow run on the same grid with its trigger, its error and no actions", () => {
    renderRow(<FlowExecutionRow run={flow} typeBadge />);
    const row = screen.getByTestId("execution-row");
    expect(row.dataset.kind).toBe("flow");
    expect(within(row).getByTestId("execution-title")).toHaveTextContent("Enrich new indicators");
    expect(within(row).getByTestId("execution-status")).toHaveTextContent("Failed");
    expect(within(row).getByTestId("execution-summary")).toHaveTextContent("Node 4 timed out");
    expect(within(row).getByTestId("execution-trigger")).toHaveTextContent("schedule");
    expect(within(row).getByTestId("execution-actions")).toBeEmptyDOMElement();
  });
});
