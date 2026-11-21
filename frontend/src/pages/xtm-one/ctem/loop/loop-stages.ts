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

export type LoopStageKey = (typeof LOOP_STAGES)[number]["key"];
/** passed: done; current: the object waits here; stopped: the loop ends here, with its reason. */
export type LoopStageStatus = "passed" | "current" | "stopped" | "not_reached";

export interface LoopStageView {
  key: LoopStageKey;
  label: string;
  status: LoopStageStatus;
  /** When the stage was reached, when the signal tells it. */
  at: string | null;
}

/** The stage index each state has passed, and the one it waits at or stopped at. */
const STATE_PLACE: Record<CtemSignalState, { passed: number; at: number; stopped: boolean }> = {
  new: { passed: 0, at: 1, stopped: false },
  confirmed: { passed: 1, at: 2, stopped: false },
  to_validate: { passed: 0, at: 1, stopped: true },
  discarded: { passed: 0, at: 1, stopped: true },
  watchlist: { passed: 1, at: 2, stopped: true },
  case_opened: { passed: 3, at: 4, stopped: false },
};

/** The ten stages as one signal reached them. */
export function signalLoop(signal: Pick<CtemSignal, "state" | "first_seen_at" | "decision">): LoopStageView[] {
  const place = STATE_PLACE[signal.state] ?? STATE_PLACE.new;
  const decidedAt = signal.decision?.at ?? null;
  return LOOP_STAGES.map((stage, index) => {
    let status: LoopStageStatus = "not_reached";
    if (index <= place.passed) status = "passed";
    else if (index === place.at) status = place.stopped ? "stopped" : "current";
    let at: string | null = null;
    if (index === 0) at = signal.first_seen_at;
    else if (status !== "not_reached" && status !== "current" && index <= place.at) at = decidedAt;
    return { key: stage.key, label: stage.label, status, at };
  });
}

/** The stage a signal waits at or stopped at, for its one-line summary. */
export function currentStage(stages: LoopStageView[]): LoopStageView | undefined {
  return stages.find((s) => s.status === "current" || s.status === "stopped");
}

/** The `?loop=` value of a signal (section 4: the trace's deep link). */
export function loopParam(kind: "signal" | "case", id: string): string {
  return `${kind}:${id}`;
}

/** The object a `?loop=` value names, or null when it names none. */
export function parseLoopParam(value: string | null): { kind: "signal" | "case"; id: string } | null {
  if (!value) return null;
  const match = /^(signal|case):([0-9a-fA-F-]{36})$/.exec(value);
  return match ? { kind: match[1] as "signal" | "case", id: match[2] } : null;
}
