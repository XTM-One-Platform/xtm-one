// SPDX-License-Identifier: Apache-2.0

/**
 * The why of a loop decision, the same component everywhere it shows
 * (dev-docs/ctem-loop-visibility.md section 3): the state in words as a hot
 * tone ToneBadge (#1267), one sentence built from the decision record's `reason_code` and
 * `reason_params` (the English `reason` line is for agents, never drawn), and
 * the inputs the rule read, labelled. No decision yet reads "Waiting for
 * reconcile"; a decided record whose code this version does not know keeps
 * its chip and says so, never an empty cell.
 */
import { type HotTone, ToneBadge } from "@/components/ui/tone-badge";
import { TruncatedText } from "@/components/ui/truncated-text";
import type { CtemDecisionRecord, CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { formatDayIntl } from "@/lib/relative-time";
import { primaryAssetsRow, thresholdSource, thresholdWithSource } from "./decision-primary-assets";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** English labels of the signal states, translated where drawn. */
export const SIGNAL_STATE_LABEL: Record<CtemSignalState, string> = {
  new: "New",
  confirmed: "Confirmed",
  // The stored state keeps its name; it reads as what it is, never as a task (Sam, 23 Nov 2026, #1505).
  to_validate: "Unmatched",
  discarded: "Discarded",
  watchlist: "Watchlist",
  case_opened: "Case opened",
};

/** A signal state in the hot tones of the case states (#1267). */
const SIGNAL_STATE_TONE: Record<CtemSignalState, HotTone> = {
  new: "blue",
  confirmed: "blue",
  to_validate: "neutral",
  discarded: "neutral",
  watchlist: "yellow",
  case_opened: "orange",
};

/**
 * The sentence of each `reason_code` the reconcile and scope gate writes
 * (backend/app/services/ctem_scope_gate.py, one per outcome branch), with its
 * ICU params. A backend item that adds a code adds its sentence here and in
 * the four catalogs. @i18n-keys
 */
const REASON_SENTENCE = {
  subject_unresolved: "Not in your inventory",
  dismissed_by_user: "Dismissed by {by}",
  contradicted_by_evidence: "Contradicted by the verdict of its own source",
  contradicted_by_signals: "Contradicted by {count, plural, one {# other signal} other {# other signals}}",
  excluded_by_policy: "Excluded by the policy ({kind}: {value})",
  accepted_risk: "Accepted risk owned by {owner} until {until}",
  score_below_policy: "{score} {value} below the policy minimum {minimum}",
  internet_exposure_required: "No internet-facing asset, and the policy requires internet exposure",
  primary_asset_below_policy: "No primary asset at or above {minimum}",
  no_risk_scenario: "No risk scenario holds the subject",
  risk_scenario_unreadable: "A risk scenario is required and OpenCRQ could not be read",
  criticality_unknown: "Criticality unknown, below the scope threshold {threshold}",
  below_scope_threshold: "Criticality {criticality} below the scope threshold {threshold}",
  in_scope: "In scope: criticality {criticality} (threshold {threshold}), confidence {confidence}",
  no_dependency: "No primary asset depends on this organization",
  account_disabled: "Not exposed: the account is disabled",
} as const;
const REASON_SENTENCES: Readonly<Record<string, string>> = REASON_SENTENCE;

/**
 * What would make a stopped signal a case, per `reason_code` (#874, Sam: "even
 * if the signal is ignored, we should understand why"). Built from the same
 * params as the reason; an in-scope or unknown code has no next step. @i18n-keys
 */
const NEXT_STEP = {
  subject_unresolved: "It stays here until your inventory holds it",
  dismissed_by_user: "Restore it to send it through the loop again",
  contradicted_by_evidence: "Becomes a case if a new sighting is not contradicted by its source",
  contradicted_by_signals: "Becomes a case if a new sighting is not contradicted by the other signals",
  excluded_by_policy: "Becomes a case if the policy exclusion is removed",
  accepted_risk: "Becomes a case when the accepted risk ends on {until}",
  score_below_policy: "Becomes a case if {score} reaches {minimum}",
  internet_exposure_required: "Becomes a case if an internet-facing asset holds it",
  primary_asset_below_policy: "Becomes a case if a primary asset at or above {minimum} holds it",
  no_risk_scenario: "Becomes a case once a risk scenario holds it",
  risk_scenario_unreadable: "Becomes a case once OpenCRQ can be read and a risk scenario holds it",
  criticality_unknown: "Becomes a case once an asset at or above {threshold} holds it",
  below_scope_threshold: "Becomes a case if criticality rises to {threshold}",
  no_dependency: "Becomes a case once a primary asset depends on this organization",
  account_disabled: "Becomes a case if the account is enabled again",
} as const;
const NEXT_STEPS: Readonly<Record<string, string>> = NEXT_STEP;

/** The level words a record carries (criticality, threshold, confidence), translated before they fill a sentence. */
const LEVEL_LABEL: Record<string, string> = { low: "Low", medium: "Medium", high: "High", critical: "Critical" };
const LEVEL_PARAMS = new Set(["criticality", "threshold", "minimum", "confidence"]);

/** The exclusion kinds the gate reads (`reason_params.kind` of excluded_by_policy), in words (#851 UX round 4 B7). @i18n-keys */
const EXCLUSION_KIND_LABEL = { product: "Product", asset_group: "Asset group", tag: "Tag" } as const;
const EXCLUSION_KIND_LABELS: Readonly<Record<string, string>> = EXCLUSION_KIND_LABEL;

/** The lens words, as the trace header draws them. @i18n-keys */
const LENS_LABEL = { inside: "Inside", outside: "Outside", world: "World" } as const;
const LENS_LABELS: Readonly<Record<string, string>> = LENS_LABEL;

/** The policy rules the gate checks (`policy_checks[].rule`), in words. @i18n-keys */
const POLICY_RULE_LABEL = {
  exclusion: "Exclusion",
  accepted_risk: "Accepted risk",
  kev_always_in: "Known exploited always in",
  cvss_min: "CVSS minimum",
  epss_min: "EPSS minimum",
  internet_exposure_required: "Internet exposure required",
  min_primary_asset_criticality: "Primary asset minimum criticality",
  require_risk_scenario: "Risk scenario required",
} as const;
const POLICY_RULE_LABELS: Readonly<Record<string, string>> = POLICY_RULE_LABEL;

/** A level word is translated; a number (a CVSS or EPSS minimum) passes through as it is. */
export function levelLabel(value: unknown, t_i18n: Translate): string | number {
  if (typeof value === "number") return value;
  if (typeof value !== "string" || !value) return "";
  return LEVEL_LABEL[value] ? t_i18n(LEVEL_LABEL[value]) : value;
}

function lensLabel(value: unknown, t_i18n: Translate): string {
  const lens = String(value);
  return LENS_LABELS[lens] ? t_i18n(LENS_LABELS[lens]) : lens;
}

function policyCheckLabel(rule: string, passed: boolean, t_i18n: Translate): string {
  const label = POLICY_RULE_LABELS[rule] ? t_i18n(POLICY_RULE_LABELS[rule]) : rule;
  return passed ? t_i18n("{rule}: passed", { rule: label }) : t_i18n("{rule}: failed", { rule: label });
}

function SignalStateChip({ state }: { state: CtemSignalState }) {
  const { t_i18n } = useFormatter();
  return <ToneBadge tone={SIGNAL_STATE_TONE[state] ?? "neutral"}>{t_i18n(SIGNAL_STATE_LABEL[state] ?? state)}</ToneBadge>;
}

/**
 * The sentence of a decision record, or the waiting and unknown-code wordings.
 * `formatDay` draws a date param (the `until` of an accepted risk) with the
 * product date formatter (#851 UX round 4 B8); without it the ISO day is kept.
 */
export function decisionSentence(
  decision: CtemDecisionRecord | null | undefined,
  t_i18n: Translate,
  formatDay: (iso: string) => string = (iso) => iso,
): string {
  if (!decision || (!decision.reason_code && !decision.outcome)) return t_i18n("Waiting for reconcile");
  const sentence = decision.reason_code ? REASON_SENTENCES[decision.reason_code] : undefined;
  if (!sentence) return t_i18n("Reason not available in this version");
  return t_i18n(sentence, sentenceParams(decision, t_i18n, formatDay));
}

/** The record's params, words translated and dates formatted, for a sentence. */
function sentenceParams(
  decision: CtemDecisionRecord,
  t_i18n: Translate,
  formatDay: (iso: string) => string,
): Record<string, string | number> {
  const params: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(decision.reason_params ?? {})) {
    if (LEVEL_PARAMS.has(key)) params[key] = levelLabel(value, t_i18n);
    else if (key === "kind" && EXCLUSION_KIND_LABELS[String(value)]) params[key] = t_i18n(EXCLUSION_KIND_LABELS[String(value)]);
    else if (key === "until" && typeof value === "string" && value) params[key] = formatDay(value);
    else params[key] = value;
  }
  return params;
}

/**
 * What would make a stopped signal a case, or null when the signal is not
 * stopped (in scope, waiting, or a code this version does not know).
 */
export function nextStepSentence(
  decision: CtemDecisionRecord | null | undefined,
  t_i18n: Translate,
  formatDay: (iso: string) => string = (iso) => iso,
): string | null {
  if (!decision?.reason_code || decision.outcome === "case") return null;
  const sentence = NEXT_STEPS[decision.reason_code];
  return sentence ? t_i18n(sentence, sentenceParams(decision, t_i18n, formatDay)) : null;
}

/** One input of the trace: a value on one line, or several lines drawn whole (the primary asset chains). */
type DecisionInput = { label: string; value: string; lines?: string[] };

/** The inputs the gate read, labelled, in the order the rule reads them; empty values are left out. */
function decisionInputs(decision: CtemDecisionRecord | null | undefined, t_i18n: Translate): DecisionInput[] {
  if (!decision) return [];
  const level = (value: unknown) => String(levelLabel(value, t_i18n));
  const list = (value: unknown) => (Array.isArray(value) ? value.filter(Boolean).map(String).join(", ") : "");
  const reconcile = decision.reconcile ?? {};
  const scope = decision.scope ?? null;
  const checks = (decision.policy_checks ?? []).map((c) => policyCheckLabel(c.rule, c.passed, t_i18n)).join(", ");
  const lensList = Array.isArray(reconcile.lenses) ? reconcile.lenses.filter(Boolean) : [];
  const lenses = lensList.length
    ? t_i18n("{lenses} ({count, plural, one {# lens} other {# lenses agree}})", {
        lenses: lensList.map((lens) => lensLabel(lens, t_i18n)).join(", "),
        count: lensList.length,
      })
    : "";
  // Asset matched or not (#874): a decided record says "None matched", never an empty row.
  // CTEM-29-UI: a leaked identity resolves through the owned domain it is on, not an asset.
  const domains = list((scope?.sources ?? []).filter((s) => s.from === "owned_domain").map((s) => s.name));
  const matched = list(reconcile.assets) || (decision.reconcile && !domains ? t_i18n("None matched") : "");
  const reached = primaryAssetsRow(decision, t_i18n);
  const rows: DecisionInput[] = [
    { label: t_i18n("Lenses"), value: lenses },
    { label: t_i18n("Confidence"), value: level(reconcile.confidence) },
    { label: t_i18n("Matched assets"), value: matched },
    // UX r1 B3 on #1796: the names the record matched, next to the primary assets reached below.
    { label: t_i18n("Primary assets named"), value: list(reconcile.primary_assets_named) },
    { label: t_i18n("Software"), value: list(reconcile.products) },
    { label: t_i18n("Owned domain"), value: domains },
    { label: t_i18n("Criticality"), value: criticalityAgainstThreshold(scope, t_i18n) },
    // #1591 S3: the primary assets reached and their chains, then the one threshold and where it comes from.
    { label: t_i18n("Primary assets reached"), value: reached.join("\n"), lines: reached },
    {
      label: t_i18n("Scope threshold"),
      value: thresholdWithSource(scope?.threshold ? level(scope.threshold) : "", thresholdSource(decision), t_i18n),
    },
    { label: t_i18n("Risk scenarios"), value: list((scope?.risk_scenarios ?? []).map((s) => s.name)) },
    { label: t_i18n("Policy checks"), value: checks },
  ];
  return rows.filter((row) => row.value);
}

/** The criticality found, against the threshold X of the scope gate (#874). */
function criticalityAgainstThreshold(scope: CtemDecisionRecord["scope"], t_i18n: Translate): string {
  if (!scope) return "";
  const threshold = scope.threshold ? String(levelLabel(scope.threshold, t_i18n)) : "";
  if (!scope.criticality) return threshold ? t_i18n("Unknown, threshold {threshold}", { threshold }) : "";
  const found = String(levelLabel(scope.criticality, t_i18n));
  if (!threshold) return found;
  const rank = (value: string | null | undefined) => ["low", "medium", "high", "critical"].indexOf(String(value));
  return rank(scope.criticality) >= rank(scope.threshold)
    ? t_i18n("{criticality}, at or above {threshold}", { criticality: found, threshold })
    : t_i18n("{criticality}, below {threshold}", { criticality: found, threshold });
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
  const { t_i18n, intl } = useFormatter();
  const formatDay = (iso: string) => formatDayIntl(intl, iso);
  const inputs = showInputs ? decisionInputs(decision, t_i18n) : [];
  const next = showInputs ? nextStepSentence(decision, t_i18n, formatDay) : null;
  return (
    <div className="flex min-w-0 flex-col gap-1" data-testid="ctem-decision-reason">
      <div className="flex min-w-0 items-center gap-2">
        <SignalStateChip state={state} />
        {/* Cut with an ellipsis and named whole by the tooltip in a narrow column (UX r1 B2 on #1272). */}
        <TruncatedText text={decisionSentence(decision, t_i18n, formatDay)} className="content-compact text-default-secondary" />
      </div>
      {next && (
        <p className="content-compact text-default-primary" data-testid="ctem-decision-next-step">
          {next}
        </p>
      )}
      {inputs.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 content-caption" data-testid="ctem-decision-inputs">
          {inputs.map((input) => (
            <div key={input.label} className="contents">
              <dt className="text-default-secondary">{input.label}</dt>
              {input.lines ? (
                // UX r1 B1 on #1796: every chain on its own line, wrapped, never cut.
                <dd className="min-w-0 break-words">
                  <ul className="flex flex-col" data-testid="ctem-decision-input-lines">
                    {input.lines.map((line, index) => (
                      <li key={`${index}-${line}`}>{line}</li>
                    ))}
                  </ul>
                </dd>
              ) : (
                <dd className="min-w-0 truncate" title={input.value}>
                  {input.value}
                </dd>
              )}
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
