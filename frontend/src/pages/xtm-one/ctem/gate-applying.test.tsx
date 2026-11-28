// A gate answered on the case page shows "being applied" until the loop
// resolves it (#2746): never the stale "waits for a person" text, a calm line
// past the polling window, the failed run when the run applying it failed, and
// a collapse (instant under prefers-reduced-motion) once the gate is applied.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemCaseGateRecord, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { renderWithProviders } from "@/testing/providers";

const state = vi.hoisted(() => ({
  byId: {} as Record<string, unknown>,
  runs: [] as unknown[],
  reduced: false,
}));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: true, user: { ctem_access: null } }) }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAgenticAIEnabled: () => true,
}));
vi.mock("@/hooks/usePrefersReducedMotion", () => ({ usePrefersReducedMotion: () => state.reduced }));
vi.mock("@/hooks/api/useCtemCasesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemCasesApi")>()),
  useCtemCaseRuns: () => ({ data: { case_id: "c1", runs: state.runs } }),
}));
vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItem: (id: string | null | undefined) => ({ data: id ? state.byId[id] : undefined, isLoading: false, refetch: vi.fn() }),
  useWorkItems: () => ({ data: undefined, isLoading: false, refetch: vi.fn() }),
}));
vi.mock("@/components/work-items/WorkItemDecisionCard", () => ({
  WorkItemDecisionCard: ({ item, onAnswerSent }: { item: { id: string }; onAnswerSent?: (s: { option: string | null; label: string | null }) => void }) => (
    <button type="button" data-testid="unified-decision-card" data-item={item.id} onClick={() => onAnswerSent?.({ option: "close", label: "Close without retest" })} />
  ),
}));

const { CaseWaitingGates } = await import("./CaseWaitingGates");
const { APPLY_WINDOW_MS, applyPollDelay, clearGateAnswer, doneItemAnswer, gateApplying, recordGateAnswer } = await import("./gate-applying");

const T0 = Date.parse("2026-11-28T08:00:00Z");

function rec(over: Partial<CtemCaseGateRecord> = {}): CtemCaseGateRecord {
  return {
    key: "ctem.close_without_retest",
    phase: null,
    status: "waiting",
    asked_at: "2026-11-28T07:00:00Z",
    asked_of: { type: "admins", name: null },
    asked_by_agent: { id: "a1", name: "Loop Orchestrator" },
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
    work_item_id: "wi-close",
    decision_filed: true,
    recorded_on_case: true,
    next: null,
    ...over,
  };
}

function run(over: Partial<CtemCaseRun>): CtemCaseRun {
  return { id: "r1", agent_id: "a1", agent_name: "Loop", status: "completed", created_at: "2026-11-28T08:00:05Z", completed_at: null, outcome: "active", ...over };
}

const OPEN = { id: "wi-close", status: "blocked", needs_human: true, ask: { question: "Close?", options: [{ key: "close", label: "Close without retest" }] } };

function page(gates: CtemCaseGateRecord[]) {
  return (
    <MemoryRouter>
      <CaseWaitingGates state="waiting_decision" gates={gates} history={[]} />
    </MemoryRouter>
  );
}

beforeEach(() => {
  state.byId = { "wi-close": OPEN };
  state.runs = [];
  state.reduced = false;
  clearGateAnswer("wi-close");
});

afterEach(() => {
  vi.useRealTimers();
});

describe("gateApplying (#2746)", () => {
  const answer = { label: "Close", at: T0 };
  it("is null when the gate does not wait or nothing was answered", () => {
    expect(gateApplying({ waiting: false, answer, now: T0 })).toBeNull();
    expect(gateApplying({ waiting: true, answer: null, now: T0 })).toBeNull();
  });
  it("is applying within the window, timeout past it", () => {
    expect(gateApplying({ waiting: true, answer, now: T0 + 1_000 })?.phase).toBe("applying");
    expect(gateApplying({ waiting: true, answer, now: T0 + APPLY_WINDOW_MS })?.phase).toBe("timeout");
  });
  it("is failed when the newest run after the answer failed and none is in flight", () => {
    const failed = [run({ id: "r9", outcome: "failed" })];
    expect(gateApplying({ waiting: true, answer, runs: failed, now: T0 + 1_000 })).toMatchObject({ phase: "failed", runId: "r9" });
    // A failed run from before the answer does not count.
    const older = [run({ outcome: "failed", created_at: "2026-11-28T07:59:00Z" })];
    expect(gateApplying({ waiting: true, answer, runs: older, now: T0 + 1_000 })?.phase).toBe("applying");
    // One still in flight keeps it applying.
    expect(gateApplying({ waiting: true, answer, runs: [...failed, run({ id: "r10", created_at: "2026-11-28T08:00:01Z" })], now: T0 + 1_000 })?.phase).toBe("applying");
  });
  it("backs off then stops at the window", () => {
    expect(applyPollDelay(0)).toBe(2_000);
    expect(applyPollDelay(15_000)).toBe(3_000);
    expect(applyPollDelay(60_000)).toBe(5_000);
    expect(applyPollDelay(APPLY_WINDOW_MS)).toBeNull();
  });
  it("reads the answer of a done item, none of an open one", () => {
    expect(doneItemAnswer({ status: "done", payload: { answer: { label: "Close", answered_at: "2026-11-28T08:00:00Z" } } } as never)).toEqual({ label: "Close", at: T0 });
    expect(doneItemAnswer(OPEN as never)).toBeNull();
  });
});

describe("Being applied on the case page (#2746)", () => {
  it("replaces the card with the spinner and the option chosen, with no waiting text", () => {
    renderWithProviders(page([rec()]));
    act(() => screen.getByTestId("unified-decision-card").click());
    const notice = screen.getByTestId("ctem-case-waiting-gate-decision-applying");
    expect(notice.getAttribute("data-phase")).toBe("applying");
    expect(notice).toHaveTextContent("Decision recorded: Close without retest. The loop is applying it.");
    expect(screen.queryByTestId("unified-decision-card")).toBeNull();
    expect(screen.queryByTestId("ctem-case-waiting-gate-decision-elsewhere")).toBeNull();
    expect(screen.queryByTestId("ctem-case-waiting-gate-open")).toBeNull();
    expect(screen.queryByTestId("ctem-case-waiting-gate-decision-record-wait")).toBeNull();
    expect(screen.queryByTestId("ctem-case-waiting-gate-decision-record-explain")).toBeNull();
    const chip = screen.getByTestId("ctem-case-waiting-gate-decision-record-status");
    expect(chip).toHaveTextContent("Being applied");
    expect(chip.getAttribute("data-phase")).toBe("applying");
    expect(screen.getByTestId("ctem-case-waiting-gate")).toHaveTextContent("Decision recorded");
    // Every gate of the card answered: the card no longer reads that it waits (UX round 1 on #2757, B2).
    const card = screen.getByTestId("ctem-case-waiting-gates");
    expect(card).toHaveTextContent("Decision being applied");
    expect(card).not.toHaveTextContent("Waits for a decision");
  });

  it("keeps the card titled waiting while one of its gates is unanswered", () => {
    state.byId["wi-other"] = { ...OPEN, id: "wi-other" };
    recordGateAnswer("wi-close", "Close without retest");
    renderWithProviders(page([rec(), rec({ key: "ctem.other_gate", work_item_id: "wi-other" })]));
    const card = screen.getByTestId("ctem-case-waiting-gates");
    expect(card).toHaveTextContent("Waits for a decision");
    expect(card).not.toHaveTextContent("Decision being applied");
    clearGateAnswer("wi-other");
  });

  it("reads a done item as being applied while the gate still waits", () => {
    state.byId["wi-close"] = { id: "wi-close", status: "done", needs_human: false, payload: { answer: { label: "Close without retest" } } };
    renderWithProviders(page([rec()]));
    expect(screen.getByTestId("ctem-case-waiting-gate-decision-applying")).toHaveTextContent("Close without retest");
    expect(screen.queryByTestId("ctem-case-waiting-gate-decision-elsewhere")).toBeNull();
  });

  it("says to refresh later past the polling window", () => {
    recordGateAnswer("wi-close", "Close without retest", Date.now() - APPLY_WINDOW_MS - 1);
    renderWithProviders(page([rec()]));
    const notice = screen.getByTestId("ctem-case-waiting-gate-decision-applying");
    expect(notice.getAttribute("data-phase")).toBe("timeout");
    expect(notice).toHaveTextContent("Recorded, still being applied. Refresh later.");
    expect(screen.getByTestId("ctem-case-waiting-gate-decision-record-status")).toHaveTextContent("Being applied");
    act(() => screen.getByTestId("ctem-case-waiting-gate-decision-applying-refresh").click());
    expect(screen.getByTestId("ctem-case-waiting-gate-decision-applying").getAttribute("data-phase")).toBe("applying");
  });

  it("names the failed run with a button to it", () => {
    const at = Date.now() - 5_000;
    recordGateAnswer("wi-close", "Close without retest", at);
    state.runs = [run({ id: "run-x", outcome: "failed", created_at: new Date(at + 1_000).toISOString() })];
    renderWithProviders(page([rec()]));
    const notice = screen.getByTestId("ctem-case-waiting-gate-decision-applying");
    expect(notice.getAttribute("data-phase")).toBe("failed");
    expect(screen.getByTestId("ctem-case-waiting-gate-decision-applying-run")).toHaveTextContent("Open the run");
    // The chip says the same as the notice (UX round 1 on #2757, B1): never "Being applied" over a failed run.
    const chip = screen.getByTestId("ctem-case-waiting-gate-decision-record-status");
    expect(chip).toHaveTextContent("Not applied");
    expect(chip).not.toHaveTextContent("Being applied");
    expect(chip.getAttribute("data-phase")).toBe("failed");
  });

  it("collapses the waiting card once the gate is applied", () => {
    vi.useFakeTimers();
    const view = renderWithProviders(page([rec()]));
    act(() => screen.getByTestId("unified-decision-card").click());
    view.rerender(page([rec({ status: "answered", answered_at: "2026-11-28T08:01:00Z", option: { key: "close", label: "Close without retest" } })]));
    expect(screen.getByTestId("ctem-case-waiting-gates-frame").getAttribute("data-closing")).toBe("true");
    act(() => vi.advanceTimersByTime(400));
    expect(screen.queryByTestId("ctem-case-waiting-gates")).toBeNull();
  });

  it("goes at once under prefers-reduced-motion", () => {
    state.reduced = true;
    const view = renderWithProviders(page([rec()]));
    view.rerender(page([rec({ status: "answered" })]));
    expect(screen.queryByTestId("ctem-case-waiting-gates")).toBeNull();
  });
});
