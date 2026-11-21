// SPDX-License-Identifier: Apache-2.0

/**
 * The why of a loop decision, the same component everywhere it shows
 * (dev-docs/ctem-loop-visibility.md section 3): the state in words as a DS
 * Chip, one sentence built from the decision record's `reason_code` and
 * `reason_params` (the English `reason` line is for agents, never drawn), and
 * the inputs the rule read, labelled. No decision yet reads "Waiting for
 * reconcile"; a decided record whose code this version does not know keeps
 * its chip and says so, never an empty cell.
 */
import { Chip } from "@filigran/design-system";
import type { CtemDecisionRecord, CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";

type ChipSeverity = "neutral" | "info" | "low" | "medium" | "high" | "critical";
type Translate = (id: string, values?: Record<string, string | number>) => string;

/** English labels of the signal states, translated where drawn. */
export const SIGNAL_STATE_LABEL: Record<CtemSignalState, string> = {
  new: "New",
  confirmed: "Confirmed",
  to_validate: "To validate",
  discarded: "Discarded",
  watchlist: "Watchlist",
  case_opened: "Case opened",
};

const SIGNAL_STATE_SEVERITY: Record<CtemSignalState, ChipSeverity> = {
  new: "info",
  confirmed: "info",
  to_validate: "medium",
  discarded: "neutral",
  watchlist: "medium",
  case_opened: "high",
};

/**
 * The sentence of each `reason_code` the reconcile and scope gate writes
 * (backend/app/services/ctem_scope_gate.py, one per outcome branch), with its
 * ICU params. A backend item that adds a code adds its sentence here and in
 * the four catalogs. @i18n-keys
 */
const REASON_SENTENCE: Record<string, string> = {
  subject_unresolved: "Not resolved to an asset, a crown jewel or the tech stack",
  contradicted_by_evidence: "Contradicted by the verdict of its own source",
  contradicted_by_signals: "Contradicted by {count, plural, one {# other signal} other {# other signals}}",
  excluded_by_policy: "Excluded by the policy ({kind}: {value})",
  accepted_risk: "Accepted risk owned by {owner} until {until}",
  score_below_policy: "{score} {value} below the policy minimum {minimum}",
  internet_exposure_required: "No internet-facing asset, and the policy requires internet exposure",
  crown_jewel_below_policy: "No crown jewel at or above {minimum}",
  no_risk_scenario: "No risk scenario holds the subject",
  risk_scenario_unreadable: "A risk scenario is required and OpenCRQ could not be read",
  criticality_unknown: "Criticality unknown, below the scope threshold {threshold}",
  below_scope_threshold: "Criticality {criticality} below the scope threshold {threshold}",
  in_scope: "In scope: criticality {criticality} at or above {threshold}, confidence {confidence}",
};

/** The level words a record carries (criticality, threshold, confidence), translated before they fill a sentence. */
const LEVEL_LABEL: Record<string, string> = { low: "Low", medium: "Medium", high: "High", critical: "Critical" };
const LEVEL_PARAMS = new Set(["criticality", "threshold", "minimum", "confidence"]);

function levelLabel(value: unknown, t_i18n: Translate): string {
  if (typeof value !== "string" || !value) return "";
  return LEVEL_LABEL[value] ? t_i18n(LEVEL_LABEL[value]) : value;
}

function SignalStateChip({ state }: { state: CtemSignalState }) {
  const { t_i18n } = useFormatter();
  return <Chip label={t_i18n(SIGNAL_STATE_LABEL[state] ?? state)} severity={SIGNAL_STATE_SEVERITY[state] ?? "neutral"} className="shrink-0" />;
}

/** The sentence of a decision record, or the waiting and unknown-code wordings. */
export function decisionSentence(decision: CtemDecisionRecord | null | undefined, t_i18n: Translate): string {
  if (!decision || (!decision.reason_code && !decision.outcome)) return t_i18n("Waiting for reconcile");
  const sentence = decision.reason_code ? REASON_SENTENCE[decision.reason_code] : undefined;
  if (!sentence) return t_i18n("Reason not available in this version");
  const params: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(decision.reason_params ?? {})) {
    params[key] = LEVEL_PARAMS.has(key) ? levelLabel(value, t_i18n) : value;
  }
  return t_i18n(sentence, params);
}

/** The inputs the gate read, labelled, in the order the rule reads them; empty values are left out. */
function decisionInputs(decision: CtemDecisionRecord | null | undefined, t_i18n: Translate): { label: string; value: string }[] {
  if (!decision) return [];
  const level = (value: unknown) => levelLabel(value, t_i18n);
  const list = (value: unknown) => (Array.isArray(value) ? value.filter(Boolean).map(String).join(", ") : "");
  const reconcile = decision.reconcile ?? {};
  const scope = decision.scope ?? null;
  const checks = (decision.policy_checks ?? []).map((c) => `${c.rule} ${c.passed ? "✓" : "✗"}`).join(", ");
  const rows: { label: string; value: string }[] = [
    { label: t_i18n("Lenses"), value: list(reconcile.lenses) },
    { label: t_i18n("Confidence"), value: level(reconcile.confidence) },
    { label: t_i18n("Matched assets"), value: list(reconcile.assets) },
    { label: t_i18n("Crown jewels"), value: list(reconcile.crown_jewels_named) },
    { label: t_i18n("Tech stack"), value: list(reconcile.tech_stack) },
    { label: t_i18n("Criticality"), value: level(scope?.criticality) },
    { label: t_i18n("Scope threshold"), value: level(scope?.threshold) },
    { label: t_i18n("Risk scenarios"), value: list((scope?.risk_scenarios ?? []).map((s) => s.name)) },
    { label: t_i18n("Policy checks"), value: checks },
  ];
  return rows.filter((row) => row.value);
}

export function DecisionReason({
  state,
  decision,
  showInputs = false,
}: {
  state: CtemSignalState;
  decision: CtemDecisionRecord | null | undefined;
  /** The loop trace lists the inputs; a row keeps the chip and the sentence. */
  showInputs?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const inputs = showInputs ? decisionInputs(decision, t_i18n) : [];
  return (
    <div className="flex min-w-0 flex-col gap-1" data-testid="ctem-decision-reason">
      <div className="flex min-w-0 items-center gap-2">
        <SignalStateChip state={state} />
        <span className="content-compact text-default-secondary truncate">{decisionSentence(decision, t_i18n)}</span>
      </div>
      {inputs.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 content-caption" data-testid="ctem-decision-inputs">
          {inputs.map((input) => (
            <div key={input.label} className="contents">
              <dt className="text-default-secondary">{input.label}</dt>
              <dd className="min-w-0 truncate" title={input.value}>
                {input.value}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
