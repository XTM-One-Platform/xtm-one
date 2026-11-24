// SPDX-License-Identifier: Apache-2.0

/**
 * The ten stages of the CTEM loop (dev-docs/ctem-loop-visibility.md section 2)
 * and where one signal stands in them: a pure function of the signal as the API
 * answers it, so the loop trace, a stage counter and a test all read the same
 * answer. A stage the signal's state cannot tell yet is "not reached", never
 * guessed.
 */
import type { CtemSignal, CtemSignalState } from "@/hooks/api/useCtemSignalsApi";

export const LOOP_STAGES = [
  { key: "signal", label: "Signal received" },
  { key: "reconcile", label: "Reconcile" },
  { key: "scope", label: "Scope gate" },
  { key: "case", label: "Case opened" },
  { key: "prioritize", label: "Prioritize" },
  { key: "validate", label: "Validate" },
  { key: "decide", label: "Decide" },
  { key: "mobilize", label: "Mobilize" },
  { key: "retest", label: "Re-test" },
  { key: "close", label: "Close and feed back" },
] as const;

type LoopStageKey = (typeof LOOP_STAGES)[number]["key"];
/**
 * passed: done; current: the object waits here; stopped: the loop ends here, with its reason;
 * parked: an unmatched signal rests here until the inventory holds its subject, no fault (#1505).
 */
type LoopStageStatus = "passed" | "current" | "stopped" | "parked" | "not_reached";

export interface LoopStageView {
  key: LoopStageKey;
  label: string;
  status: LoopStageStatus;
  /** When the stage was reached, when the signal tells it. */
  at: string | null;
}

/** The stage index each state has passed, and the one it waits at or stopped at. */
const STATE_PLACE: Record<CtemSignalState, { passed: number; at: number; stopped: boolean; parked?: boolean }> = {
  new: { passed: 0, at: 1, stopped: false },
  confirmed: { passed: 1, at: 2, stopped: false },
  to_validate: { passed: 0, at: 1, stopped: true, parked: true },
  discarded: { passed: 0, at: 1, stopped: true },
  watchlist: { passed: 1, at: 2, stopped: true },
  case_opened: { passed: 3, at: 4, stopped: false },
};

/** The ten stages as one signal reached them. */
export function signalLoop(signal: Pick<CtemSignal, "state" | "first_seen_at" | "decision">): LoopStageView[] {
  // A confirmed signal the gate sent to a case passed the scope gate and waits
  // for the case step (CTEM-03); a confirmed one with no decision waits at the gate.
  const place =
    signal.state === "confirmed" && signal.decision?.outcome === "case"
      ? { passed: 2, at: 3, stopped: false }
      : (STATE_PLACE[signal.state] ?? STATE_PLACE.new);
  const stopStatus: LoopStageStatus = "parked" in place && place.parked ? "parked" : "stopped";
  const decidedAt = signal.decision?.decided_at ?? null;
  return LOOP_STAGES.map((stage, index) => {
    let status: LoopStageStatus = "not_reached";
    if (index <= place.passed) status = "passed";
    else if (index === place.at) status = place.stopped ? stopStatus : "current";
    let at: string | null = null;
    if (index === 0) at = signal.first_seen_at;
    else if (status !== "not_reached" && status !== "current" && index <= place.at) at = decidedAt;
    return { key: stage.key, label: stage.label, status, at };
  });
}

/** The stage a signal waits at or stopped at, for its one-line summary. */
export function currentStage(stages: LoopStageView[]): LoopStageView | undefined {
  return stages.find((s) => s.status === "current" || s.status === "stopped" || s.status === "parked");
}

