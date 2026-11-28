// #838: the CTEM loop is never hidden. A signal shows in Live activity with its
// path, lens, state and why, and opens its loop trace.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { renderWithProviders } from "@/testing/providers";
import { eventStatus, mergeActivity, signalActivity, type ActivityEvent } from "../activity-feed";
import { ActivityFeed } from "../ActivityFeed";
import { currentStage, LOOP_STAGES, signalLoop } from "./loop-stages";
import { DecisionReason, decisionSentence, nextStepSentence } from "./DecisionReason";
import { SignalCounter } from "./SignalCounter";

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

describe("SignalCounter", () => {
  it("shows every state with its count, zeros included, and opens the state clicked", () => {
    const onOpen = vi.fn();
    renderWithProviders(<SignalCounter counts={{ new: 2, to_validate: 1, watchlist: 0, discarded: 3, case_opened: 0, confirmed: 1 }} onOpen={onOpen} />);
    const counter = screen.getByTestId("ctem-signal-counter");
    expect(counter.querySelectorAll("[data-state]")).toHaveLength(5);
    expect(counter.querySelector('[data-state="watchlist"]')?.textContent).toContain("0");
    fireEvent.click(counter.querySelector('[data-state="to_validate"]') as Element);
    expect(onOpen).toHaveBeenCalledWith("to_validate");
    fireEvent.click(screen.getByRole("button", { name: "Open all signals" }));
    expect(onOpen).toHaveBeenLastCalledWith(null);
    // All counts every state, confirmed included.
    expect(screen.getByRole("button", { name: "Open all signals" }).textContent).toContain("7");
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
    expect(decisionSentence({ outcome: "to_validate", reason_code: "subject_unresolved", reason_params: {} }, t)).toBe("Not in your inventory");
    // #2628: an unproven CVE match says the running version was not verified.
    expect(decisionSentence({ outcome: "to_validate", reason_code: "version_not_verified", reason_params: { assets: 3 } }, t)).toMatch(
      /^Version not verified/,
    );
    expect(nextStepSentence({ outcome: "to_validate", reason_code: "version_not_verified" }, t)).toBe(
      "Becomes a case once an asset reports the affected product with its version",
    );
    expect(decisionSentence({ outcome: "discarded", reason_code: "dismissed_by_user", reason_params: { by: "Ana" } }, t)).toBe("Dismissed by {by}");
    const watch = { outcome: "watchlist", reason_code: "below_scope_threshold", reason_params: { criticality: "low", threshold: "high" } };
    const seen: Record<string, string | number>[] = [];
    const capture = (id: string, values?: Record<string, string | number>) => {
      if (values) seen.push(values);
      return id;
    };
    expect(decisionSentence(watch, capture)).toBe("Criticality {criticality} below the scope threshold {threshold}");
    expect(seen[seen.length - 1]).toEqual({ criticality: "Low", threshold: "High" });
  });

  // #851 UX round 3 B5: a numeric minimum is kept, never blanked by the level lookup.
  it("keeps the numeric minimum of a score below the policy", () => {
    const below = { outcome: "watchlist", reason_code: "score_below_policy", reason_params: { score: "CVSS", value: 5.3, minimum: 7 } };
    const seen: Record<string, string | number>[] = [];
    const capture = (id: string, values?: Record<string, string | number>) => {
      if (values) seen.push(values);
      return id;
    };
    decisionSentence(below, capture);
    expect(seen[seen.length - 1]).toEqual({ score: "CVSS", value: 5.3, minimum: 7 });
  });

  // #851 UX round 4 B7: the exclusion kind is a word, never the raw identifier.
  it("translates the kind of a policy exclusion", () => {
    const excluded = { outcome: "discarded", reason_code: "excluded_by_policy", reason_params: { kind: "asset_group", value: "DMZ" } };
    const seen: Record<string, string | number>[] = [];
    const capture = (id: string, values?: Record<string, string | number>) => {
      if (values) seen.push(values);
      return id;
    };
    expect(decisionSentence(excluded, capture)).toBe("Excluded by the policy ({kind}: {value})");
    expect(seen[seen.length - 1]).toEqual({ kind: "Asset group", value: "DMZ" });
    decisionSentence({ ...excluded, reason_params: { kind: "product", value: "Nginx" } }, capture);
    expect(seen[seen.length - 1]).toEqual({ kind: "Product", value: "Nginx" });
  });

  // #851 UX round 4 B8: the expiry of an accepted risk goes through the date formatter.
  it("formats the expiry of an accepted risk", () => {
    const accepted = { outcome: "watchlist", reason_code: "accepted_risk", reason_params: { owner: "Lab owner", until: "2027-06-30" } };
    const seen: Record<string, string | number>[] = [];
    const capture = (id: string, values?: Record<string, string | number>) => {
      if (values) seen.push(values);
      return id;
    };
    decisionSentence(accepted, capture, (iso) => `day(${iso})`);
    expect(seen[seen.length - 1]).toEqual({ owner: "Lab owner", until: "day(2027-06-30)" });
  });

  it("draws the accepted-risk expiry as a formatted day in the component", () => {
    renderWithProviders(
      <DecisionReason
        state="watchlist"
        decision={{ outcome: "watchlist", reason_code: "accepted_risk", reason_params: { owner: "Lab owner", until: "2027-06-30" } }}
      />,
    );
    expect(screen.queryByText(/2027-06-30/)).toBeNull();
    expect(screen.getByText(/Accepted risk owned by Lab owner until Jun 30, 2027/)).toBeInTheDocument();
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
    // #1505: an unmatched signal is parked at reconcile, no fault, never "Stopped".
    expect(currentStage(signalLoop(decided("to_validate", "subject_unresolved")))).toMatchObject({ key: "reconcile", status: "parked" });
  });

  it("labels an unmatched signal as such, never as a task to validate (#1505)", () => {
    renderWithProviders(<DecisionReason state="to_validate" decision={decided("to_validate", "subject_unresolved").decision} />);
    expect(screen.getByText("Unmatched")).toBeInTheDocument();
    expect(screen.queryByText(/To validate/)).toBeNull();
  });

  it("lists the decision inputs with labels", () => {
    renderWithProviders(<DecisionReason state="to_validate" decision={decided("to_validate", "subject_unresolved").decision} showInputs />);
    const inputs = screen.getByTestId("ctem-decision-inputs");
    expect(inputs).toHaveTextContent("Lenses");
    expect(inputs).toHaveTextContent("Outside");
    expect(inputs).toHaveTextContent("Matched assets");
    expect(inputs).toHaveTextContent("vpn-gw-01");
  });

  // #851 UX round 3 B6: lens and policy rules in words, passed or failed as text.
  it("labels the lens and the policy checks, never raw identifiers", () => {
    const decision = {
      outcome: "watchlist",
      reason_code: "score_below_policy",
      reason_params: { score: "CVSS", value: 5.3, minimum: 7 },
      reconcile: { lenses: ["world"] },
      policy_checks: [
        { rule: "cvss_min", value: 5.3, threshold: 7, passed: false },
        { rule: "internet_exposure_required", value: true, passed: true },
      ],
    };
    renderWithProviders(<DecisionReason state="watchlist" decision={decision} showInputs />);
    const inputs = screen.getByTestId("ctem-decision-inputs");
    expect(inputs).toHaveTextContent("World");
    expect(inputs).toHaveTextContent("CVSS minimum: failed");
    expect(inputs).toHaveTextContent("Internet exposure required: passed");
    expect(inputs).not.toHaveTextContent("cvss_min");
    expect(screen.getByText("CVSS 5.3 below the policy minimum 7")).toBeInTheDocument();
  });
});

// #874: every stopped signal says what would make it a case; the inputs say
// whether an asset matched and how the criticality compares with the threshold.
describe("what would make it a case", () => {
  const below = {
    outcome: "watchlist",
    reason_code: "below_scope_threshold",
    reason_params: { criticality: "medium", threshold: "high" },
    reconcile: { assets: ["vpn-gw-01"], lenses: ["world", "outside"] },
    scope: { criticality: "medium", threshold: "high", risk_scenarios: [] },
  };

  it("names the next step of each stopped outcome, none for a case or an unknown code", () => {
    expect(nextStepSentence(below, t)).toBe("Becomes a case if criticality rises to {threshold}");
    expect(nextStepSentence({ outcome: "to_validate", reason_code: "subject_unresolved" }, t)).toBe(
      "It stays here until your inventory holds it",
    );
    expect(nextStepSentence({ outcome: "case", reason_code: "in_scope" }, t)).toBeNull();
    expect(nextStepSentence({ outcome: "watchlist", reason_code: "later_code" }, t)).toBeNull();
    expect(nextStepSentence(null, t)).toBeNull();
  });

  it("names the unmatched retention window as the next step, the inventory sentence when it is 0 (#2726)", () => {
    const unmatched = { outcome: "to_validate" as const, reason_code: "subject_unresolved" };
    const withCount = (id: string, values?: Record<string, unknown>) => `${id}|${String(values?.count ?? "")}`;
    expect(nextStepSentence(unmatched, withCount, undefined, 30)).toBe(
      "Deleted if not seen for {count, plural, one {# day} other {# days}}, decided again at its next sighting|30",
    );
    expect(nextStepSentence(unmatched, t, undefined, 0)).toBe("It stays here until your inventory holds it");
    expect(nextStepSentence(below, t, undefined, 30)).toBe("Becomes a case if criticality rises to {threshold}");
  });

  it("draws the next step and the criticality against the threshold in the trace", () => {
    renderWithProviders(<DecisionReason state="watchlist" decision={below} showInputs />);
    expect(screen.getByTestId("ctem-decision-next-step")).toHaveTextContent("Becomes a case if criticality rises to High");
    const inputs = screen.getByTestId("ctem-decision-inputs");
    expect(inputs).toHaveTextContent("Medium, below High");
    expect(inputs).toHaveTextContent("2 lenses agree");
  });

  it("says no asset matched on an unresolved subject, and draws no next step on a row", () => {
    const unresolved = { outcome: "to_validate", reason_code: "subject_unresolved", reconcile: { assets: [], lenses: ["world"] } };
    const { unmount } = renderWithProviders(<DecisionReason state="to_validate" decision={unresolved} showInputs />);
    expect(screen.getByTestId("ctem-decision-inputs")).toHaveTextContent("None matched");
    unmount();
    renderWithProviders(<DecisionReason state="to_validate" decision={unresolved} />);
    expect(screen.queryByTestId("ctem-decision-next-step")).toBeNull();
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
