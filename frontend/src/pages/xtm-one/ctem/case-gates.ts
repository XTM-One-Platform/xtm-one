// SPDX-License-Identifier: Apache-2.0

/**
 * The loop gates of a case in words (CTEM-17-UI): the `gate` events the case
 * history holds (CTEM-34, `ctem_case_loop.record_gate`: `from` the catalog key,
 * `to` the outcome, `reason` what let it run or who answered, `evidence` the
 * option chosen), and the remediation decision read from them. Pure: the case
 * page and Act now draw these, nothing here reads the network.
 */
import type { CtemCaseEvent } from "@/hooks/api/useCtemCasesApi";

/** The remediation decision gate (gate B, `ctem_remediation_decision.GATE_KEY`). */
export const REMEDIATION_GATE = "ctem.remediation.decision";

/** The work item a waiting remediation decision opens (`ctem_cases._park`). */
export function remediationDecisionKey(caseId: string): string {
  return `ctem:remediation:${caseId}`;
}

/** The prefix of every waiting remediation decision, for one read across cases. */
export const REMEDIATION_DECISION_PREFIX = "ctem:remediation:";

/** The case id a remediation decision key names, or null. */
export function caseIdOfDecisionKey(key: string | null | undefined): string | null {
  if (!key || !key.startsWith(REMEDIATION_DECISION_PREFIX)) return null;
  return key.slice(REMEDIATION_DECISION_PREFIX.length) || null;
}

/** The CTEM gates in plain words, the catalog's own actions (`builtin_fleet_docs._ctem_gates`). @i18n-keys */
const GATE_LABELS = {
  "ctem.validation.non_readonly": "Validate: run an OpenAEV test that is not read-only",
  "ctem.analyst_review": "Review: an analyst checks the threat package",
  "ctem.remediation.decision": "Decide: choose the remediation option",
  "ctem.mobilize": "Mobilize: create the tickets, notify the owners, hand over the prepared change",
  "ctem.production_change": "Apply a change on production through an integration",
  "ctem.accept_risk": "Accept a residual risk beyond the policy cap",
  "ctem.close_without_retest": "Close a case without a passing re-test",
} as const;

export const GATE_LABEL: Readonly<Record<string, string>> = GATE_LABELS;

export type GateOutcome = "passed_automatically" | "waited" | "answered";

/** What happened at a gate, in words. @i18n-keys */
export const GATE_OUTCOME_LABEL = {
  passed_automatically: "Passed automatically",
  waited: "Waited for a person",
  answered: "Answered by a person",
} as const satisfies Record<GateOutcome, string>;

export function isGateOutcome(value: string | null | undefined): value is GateOutcome {
  return value === "passed_automatically" || value === "waited" || value === "answered";
}

/** The newest `gate` event of one gate key, or null (history order is not assumed). */
export function latestGate(history: readonly CtemCaseEvent[], key: string): CtemCaseEvent | null {
  let found: CtemCaseEvent | null = null;
  let foundAt = Number.NEGATIVE_INFINITY;
  history.forEach((e) => {
    if (e.kind !== "gate" || e.from !== key || !isGateOutcome(e.to)) return;
    const at = e.at ? Date.parse(e.at) : Number.NaN;
    // Undated events rank by their place, after every dated one.
    const rank = Number.isNaN(at) ? Number.NEGATIVE_INFINITY : at;
    // At an equal time the later row wins: the history lists in write order.
    if (found === null || rank >= foundAt) {
      found = e;
      foundAt = rank;
    }
  });
  return found;
}

/**
 * The option lines of the newest remediation note (`outcome_note`: a head line
 * then one line per option, never a figure), so every viewer reads the same
 * options whatever their OpenCRQ access.
 */
export function remediationOptionLines(history: readonly CtemCaseEvent[]): string[] {
  let best: CtemCaseEvent | null = null;
  let bestAt = Number.NEGATIVE_INFINITY;
  for (const e of history) {
    if (e.kind !== "note" || !e.reason || !/^Remediation (option|decision waits)/.test(e.reason)) continue;
    const at = e.at ? Date.parse(e.at) : Number.NaN;
    const rank = Number.isNaN(at) ? Number.NEGATIVE_INFINITY : at;
    if (best === null || rank >= bestAt) {
      best = e;
      bestAt = rank;
    }
  }
  if (!best?.reason) return [];
  return best.reason
    .split("\n")
    .slice(1)
    .map((line) => line.trim())
    .filter(Boolean);
}
