// #838: the CTEM loop is never hidden. A signal shows in Live activity with its
// path, lens, state and why, and opens its loop trace.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { renderWithProviders } from "@/testing/providers";
import { eventStatus, mergeActivity, signalActivity, type ActivityEvent } from "../activity-feed";
import { ActivityFeed } from "../ActivityFeed";
import { currentStage, LOOP_STAGES, parseLoopParam, signalLoop } from "./loop-stages";
import { DecisionReason, decisionSentence } from "./DecisionReason";

const ID = "11111111-2222-3333-4444-555555555555";
const t = (id: string) => id;

function signal(over: Partial<CtemSignal> = {}): CtemSignal {
  return {
    id: ID,
    trigger_path: "new_vulnerability",
    trigger_path_label: "New vulnerability on a running version",
    lens: "world",
    source: { provider: "opencti", external_id: "vuln--1" },
    subject: { kind: "vulnerability", ref: "CVE-2026-123633", label: "CVE-2026-123633" },
    observed_at: "2026-11-21T08:00:00Z",
    first_seen_at: "2026-11-21T08:00:00Z",
    last_seen_at: "2026-11-21T08:05:00Z",
    seen_count: 1,
    evidence: {},
    state: "new",
    decision: null,
    case_id: null,
    recorded_via: "api",
    ...over,
  };
}

describe("signalLoop", () => {
  it("draws ten stages", () => {
    expect(LOOP_STAGES).toHaveLength(10);
    expect(signalLoop(signal())).toHaveLength(10);
  });

  it("a new signal passed intake and waits for reconcile", () => {
    const stages = signalLoop(signal());
    expect(stages[0]).toMatchObject({ key: "signal", status: "passed", at: "2026-11-21T08:00:00Z" });
    expect(currentStage(stages)).toMatchObject({ key: "reconcile", status: "current" });
    expect(stages.slice(2).every((s) => s.status === "not_reached")).toBe(true);
  });

  it("a watchlisted signal stops at the scope gate", () => {
    const stages = signalLoop(signal({ state: "watchlist", decision: { outcome: "watchlist", reason_code: "x", decided_at: "2026-11-21T08:06:00Z" } }));
    expect(stages[1].status).toBe("passed");
    expect(currentStage(stages)).toMatchObject({ key: "scope", status: "stopped", at: "2026-11-21T08:06:00Z" });
  });

  it("a discarded signal stops at reconcile; a case-opened one waits at prioritize", () => {
    expect(currentStage(signalLoop(signal({ state: "discarded" })))).toMatchObject({ key: "reconcile", status: "stopped" });
    expect(currentStage(signalLoop(signal({ state: "case_opened" })))).toMatchObject({ key: "prioritize", status: "current" });
  });
});

describe("parseLoopParam", () => {
  it("reads a signal or a case id and refuses anything else", () => {
    expect(parseLoopParam(`signal:${ID}`)).toEqual({ kind: "signal", id: ID });
    expect(parseLoopParam(`case:${ID}`)).toEqual({ kind: "case", id: ID });
    expect(parseLoopParam("signal:../x")).toBeNull();
    expect(parseLoopParam(null)).toBeNull();
  });
});

describe("decisionSentence", () => {
  it("never leaves the why empty", () => {
    expect(decisionSentence(null, t)).toBe("Waiting for reconcile");
    expect(decisionSentence({ reason_code: "unknown_code" }, t)).toBe("Reason not available in this version");
    // A decided record of an older shape (no code) is decided, never "waiting".
    expect(decisionSentence({ outcome: "discarded", reason: "contradicted by this signal" }, t)).toBe("Reason not available in this version");
  });

  // #851 UX round 2 B4: a decided signal says why, from the CTEM-02 record.
  it("reads the reason code of each decided outcome", () => {
    expect(decisionSentence({ outcome: "discarded", reason_code: "contradicted_by_evidence", reason_params: {} }, t)).toBe(
      "Contradicted by the verdict of its own source",
    );
    expect(decisionSentence({ outcome: "to_validate", reason_code: "subject_unresolved", reason_params: {} }, t)).toBe(
      "Not resolved to an asset, a crown jewel or the tech stack",
    );
    const watch = { outcome: "watchlist", reason_code: "below_scope_threshold", reason_params: { criticality: "low", threshold: "high" } };
    const seen: Record<string, string | number>[] = [];
    const capture = (id: string, values?: Record<string, string | number>) => {
      if (values) seen.push(values);
      return id;
    };
    expect(decisionSentence(watch, capture)).toBe("Criticality {criticality} below the scope threshold {threshold}");
    expect(seen[seen.length - 1]).toEqual({ criticality: "Low", threshold: "High" });
  });
});

describe("decided signals in the trace and the row", () => {
  const decided = (state: CtemSignal["state"], reason_code: string) =>
    signal({ state, decision: { outcome: state, reason_code, reason_params: {}, decided_at: "2026-11-21T08:06:00Z", reconcile: { lenses: ["outside"], confidence: "low", assets: ["vpn-gw-01"] } } });

  it("a discarded signal reads its reason in the row, never waiting", () => {
    renderWithProviders(<ActivityFeed events={signalActivity([decided("discarded", "contradicted_by_evidence")])} onOpenSignal={vi.fn()} />);
    expect(screen.queryByText(/Waiting for reconcile/)).toBeNull();
    expect(screen.getByText(/Contradicted by the verdict of its own source/)).toBeInTheDocument();
  });

  it("a watchlist signal stops at the scope gate and a case-bound one waits at the case step", () => {
    expect(currentStage(signalLoop(decided("watchlist", "below_scope_threshold")))).toMatchObject({ key: "scope", status: "stopped" });
    const toCase = signal({ state: "confirmed", decision: { outcome: "case", reason_code: "in_scope", decided_at: "2026-11-21T08:06:00Z" } });
    expect(currentStage(signalLoop(toCase))).toMatchObject({ key: "case", status: "current" });
    expect(currentStage(signalLoop(decided("to_validate", "subject_unresolved")))).toMatchObject({ key: "reconcile", status: "stopped" });
  });

  it("lists the decision inputs with labels", () => {
    renderWithProviders(<DecisionReason state="to_validate" decision={decided("to_validate", "subject_unresolved").decision} showInputs />);
    const inputs = screen.getByTestId("ctem-decision-inputs");
    expect(inputs).toHaveTextContent("Lenses");
    expect(inputs).toHaveTextContent("outside");
    expect(inputs).toHaveTextContent("Matched assets");
    expect(inputs).toHaveTextContent("vpn-gw-01");
  });
});

describe("signal rows in Live activity", () => {
  it("joins the deterministic events newest first, and a moving signal pulses", () => {
    const signals = signalActivity([signal()]);
    const older: ActivityEvent = { kind: "posture_synced", id: "posture", at: "2026-11-21T07:00:00Z", covered: 4, total: 5 };
    const feed = mergeActivity(signals, [older]);
    expect(feed.map((e) => e.kind)).toEqual(["signal_received", "posture_synced"]);
    expect(eventStatus(feed[0])).toBe("running");
    expect(eventStatus(signalActivity([signal({ state: "watchlist" })])[0])).toBe("partial");
  });

  it("shows path, lens, state and why, and opens the trace", () => {
    const open = vi.fn();
    renderWithProviders(<ActivityFeed events={signalActivity([signal()])} onOpenSignal={open} />);
    expect(screen.getByText("Signal received: CVE-2026-123633")).toBeInTheDocument();
    expect(screen.getByText("New vulnerability on a running version · World · New · Waiting for reconcile")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show CVE-2026-123633 in the loop" }));
    expect(open).toHaveBeenCalledWith(ID);
  });

  // #851 UX round 1 A3: the dot of a signal reads its state, never the run wording.
  it("reads the signal state for the status dot", () => {
    renderWithProviders(<ActivityFeed events={signalActivity([signal({ state: "watchlist" })])} onOpenSignal={vi.fn()} />);
    const status = screen.getByTestId("activity-row-status");
    expect(status).toHaveTextContent("Watchlist");
    expect(status).not.toHaveTextContent("Partial");
  });

  it("keeps the other rows read-only", () => {
    const older: ActivityEvent = { kind: "posture_synced", id: "posture", at: "2026-11-21T07:00:00Z", covered: 4, total: 5 };
    renderWithProviders(<ActivityFeed events={[older]} onOpenSignal={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
