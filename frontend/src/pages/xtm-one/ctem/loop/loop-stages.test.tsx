// #838: the CTEM loop is never hidden. A signal shows in Live activity with its
// path, lens, state and why, and opens its loop trace.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { renderWithProviders } from "@/testing/providers";
import { eventStatus, mergeActivity, signalActivity, type ActivityEvent } from "../activity-feed";
import { ActivityFeed } from "../ActivityFeed";
import { currentStage, LOOP_STAGES, parseLoopParam, signalLoop } from "./loop-stages";
import { decisionSentence } from "./DecisionReason";

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
    const stages = signalLoop(signal({ state: "watchlist", decision: { reason_code: "x", at: "2026-11-21T08:06:00Z" } }));
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

  it("keeps the other rows read-only", () => {
    const older: ActivityEvent = { kind: "posture_synced", id: "posture", at: "2026-11-21T07:00:00Z", covered: 4, total: 5 };
    renderWithProviders(<ActivityFeed events={[older]} onOpenSignal={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
