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
  "ctem.validation.non_readonly": "Validate: create or launch an OpenAEV scenario or atomic test",
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

/**
 * The status a gate card shows: the newest gate of a card is live, so a gate that waits reads
 * in the present ("Waiting for a person"), while History rows keep GATE_OUTCOME_LABEL, a past
 * event (#1539, UX round 1 on #1541 B1). @i18n-keys
 */
export const GATE_CARD_STATUS_LABEL = {
  passed_automatically: "Passed automatically",
  waited: "Waiting for a person",
  answered: "Answered by a person",
} as const satisfies Record<GateOutcome, string>;

export function isGateOutcome(value: string | null | undefined): value is GateOutcome {
  return value === "passed_automatically" || value === "waited" || value === "answered";
}

/** A user id as the case history stores it (a gate answered by a person names them by id). */
export function isUserId(value: string | null | undefined): value is string {
  return !!value && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

/** Why a gate ran or waited, in words: the kinds of reason the backend writes. @i18n-keys */
export const GATE_REASON_LABEL = {
  runbook: "Pre-approved runbook {runbook}",
  within_threshold: "Within the approval threshold",
  above_threshold: "Above the approval threshold",
  loss_unknown: "Expected loss unknown, an approval threshold is set",
  fleet_runs: "The fleet runs this gate without a person",
  fleet_waits: "The fleet makes this gate wait for a person",
} as const;

export type GateReason = { kind: keyof typeof GATE_REASON_LABEL; runbook?: string };

/**
 * The kind of reason a `gate` event carries (`ctem_remediation_decision.decide`,
 * `record_gate` by: the level or pin, or a person), so the screen renders it
 * with translated words and never prints the backend text. A person (answered)
 * is not a reason: the caller names them. Unknown text gives null: no line.
 */
export function gateReason(reason: string | null | undefined, outcome: string | null | undefined): GateReason | null {
  const text = (reason ?? "").trim();
  if (!text || outcome === "answered") return null;
  const runbook = /pre-approved runbook (.+?)(?: \([^()]*\))?$/i.exec(text);
  if (runbook) return { kind: "runbook", runbook: runbook[1].trim() };
  if (/within the approval threshold/i.test(text)) return { kind: "within_threshold" };
  if (/above the approval threshold/i.test(text)) return { kind: "above_threshold" };
  if (/expected loss is unknown/i.test(text)) return { kind: "loss_unknown" };
  if (outcome === "passed_automatically") return { kind: "fleet_runs" };
  if (outcome === "waited") return { kind: "fleet_waits" };
  return null;
}

/** The facts of an option line, in words. @i18n-keys */
export const OPTION_FACT_LABEL = {
  residual_risk: "Residual risk",
  time_to_apply: "Time to apply",
  runbook: "Pre-approved runbook",
} as const;

type OptionFact = { kind: keyof typeof OPTION_FACT_LABEL; value: string };
export type RemediationOptionLine = { key: string; label: string; facts: OptionFact[] };

const FACT_PREFIX: ReadonlyArray<[OptionFact["kind"], string]> = [
  ["residual_risk", "residual risk "],
  ["time_to_apply", "time to apply "],
  ["runbook", "pre-approved runbook "],
];

function parseFacts(text: string): OptionFact[] | null {
  if (text === "no figures given") return [];
  const facts: OptionFact[] = [];
  for (const part of text.split("; ")) {
    const found = FACT_PREFIX.find(([, prefix]) => part.startsWith(prefix));
    if (!found) return null;
    facts.push({ kind: found[0], value: part.slice(found[1].length).trim() });
  }
  return facts;
}

/**
 * One option line of the note (`<key>. <label>: <facts>`, the facts written by
 * `option_effect` without figures) as its key, the label the agent typed and
 * the facts by kind, so the screen labels them in the reader's language. A
 * line that does not read so keeps its text as the label, with no fact.
 */
export function parseRemediationOption(line: string): RemediationOptionLine {
  const head = /^([^.\s]{1,40})\. (.*)$/.exec(line);
  if (!head) return { key: "", label: line, facts: [] };
  const [, key, rest] = head;
  let at = rest.lastIndexOf(": ");
  while (at > 0) {
    const facts = parseFacts(rest.slice(at + 2));
    if (facts) return { key, label: rest.slice(0, at), facts };
    at = rest.lastIndexOf(": ", at - 1);
  }
  return { key, label: rest, facts: [] };
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
