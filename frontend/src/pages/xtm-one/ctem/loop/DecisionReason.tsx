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
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import type { CtemDecisionRecord, CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import { formatDayIntl } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
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

/** #2542: the lookups that settle a decision (`ctem_scope_gate.LOOKUP_SETTLED`); any other one is a read that did not answer. */
const SETTLED_LOOKUPS = new Set(["read", "not needed", "not applicable"]);
const FAILED_LOOKUPS = new Set(["OpenCTI did not answer", "no OpenCTI access"]);

/**
 * #2542: whether an unmatched decision waits for an OpenCTI software read: "scheduled" (the
 * retry pass takes it again), "exhausted" (retries stopped, the failure stays shown), else null.
 * A record of a build before #2542 carries no `incomplete` flag: a failed lookup reads as scheduled.
 */
export function lookupRetry(decision: CtemDecisionRecord | null | undefined): "scheduled" | "exhausted" | null {
  if (!decision || decision.reason_code !== "subject_unresolved") return null;
  if (decision.incomplete === false) return null;
  if (decision.incomplete !== true) {
    const reconcile = decision.reconcile ?? {};
    const failed = [reconcile.threat_lookup, reconcile.cve_lookup].some((value) => typeof value === "string" && FAILED_LOOKUPS.has(value));
    if (!failed) return null;
  }
  return decision.lookup_retry === "exhausted" ? "exhausted" : "scheduled";
}

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

/** A policy rule in words; a rule this version does not know keeps its name. */
export function policyRuleLabel(rule: string, t_i18n: Translate): string {
  return POLICY_RULE_LABELS[rule] ? t_i18n(POLICY_RULE_LABELS[rule]) : rule;
}

function policyCheckLabel(rule: string, passed: boolean, t_i18n: Translate): string {
  const label = policyRuleLabel(rule, t_i18n);
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
  // #2542: an unmatched decision taken while OpenCTI did not answer is not final, and says so.
  const lookup = lookupRetry(decision);
  if (lookup === "scheduled") return t_i18n("OpenCTI software not read, will retry");
  if (lookup === "exhausted") return t_i18n("OpenCTI software not read, retries stopped");
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
  if (lookupRetry(decision) === "scheduled") return t_i18n("Decided again once OpenCTI answers");
  if (lookupRetry(decision) === "exhausted") return t_i18n("Recheck it once OpenCTI answers");
  const sentence = NEXT_STEPS[decision.reason_code];
  return sentence ? t_i18n(sentence, sentenceParams(decision, t_i18n, formatDay)) : null;
}

/** What one gate of the loop decided for a signal, drawn as a step of the trace (#874 S2). */
export interface GateStep {
  /** The outcome in words: Confirmed, In scope, or the state the signal stopped in. */
  decision: string;
  /** The why, from the record's reason code; empty when the gate gave none. */
  reason: string;
  /** What would make a stopped signal go on (S1's next step), null when it went on. */
  next: string | null;
}

/**
 * The decision of the reconcile gate (`stage` "reconcile") or of the scope gate
 * (`stage` "scope") for a signal, read from its state and decision record (#874 S2,
 * dev-docs/ctem-loop-visibility.md section 3: the trace lists the gates as labelled
 * steps). `status` is the stage's status in `signalLoop`: a gate not reached yet or
 * still waiting has no decision (null). A signal that passed reconcile reads
 * "Confirmed" with the confidence; one that passed the scope gate reads "In scope" with
 * the in-scope sentence; the gate it stopped at carries its state, reason and next step.
 */
export function gateStep(
  stage: string,
  status: string,
  state: CtemSignalState,
  decision: CtemDecisionRecord | null | undefined,
  t_i18n: Translate,
  formatDay: (iso: string) => string = (iso) => iso,
): GateStep | null {
  if (stage !== "reconcile" && stage !== "scope") return null;
  if (status === "stopped" || status === "parked") {
    return {
      decision: t_i18n(SIGNAL_STATE_LABEL[state] ?? state),
      reason: decisionSentence(decision, t_i18n, formatDay),
      next: nextStepSentence(decision, t_i18n, formatDay),
    };
  }
  if (status !== "passed") return null;
  if (stage === "reconcile") {
    const confidence = decision?.reconcile?.confidence;
    return {
      decision: t_i18n(SIGNAL_STATE_LABEL.confirmed),
      reason: confidence ? t_i18n("Confidence {confidence}", { confidence: levelLabel(confidence, t_i18n) }) : "",
      next: null,
    };
  }
  return {
    decision: t_i18n("In scope"),
    reason: decision?.reason_code === "in_scope" ? decisionSentence(decision, t_i18n, formatDay) : "",
    next: null,
  };
}

/** One input of the trace: a value on one line, or several lines drawn whole (the primary asset chains). */
export type DecisionInput = {
  key: string;
  label: string;
  value: string;
  lines?: string[];
  /** A long list (the matched assets): the count and the first names drawn, every name in the tooltip. */
  names?: string[];
  /** UX r2 B7 on #1995: the names are a floor (the record's list was cut), so the count reads "At least". */
  capped?: boolean;
  /** UX r1 B1 on #2533: what the names count ("assets" by default, "software" for the software tried). */
  noun?: "assets" | "software";
};

/**
 * UX r1 B2 on #2533: why the software of the signal was never read from OpenCTI, from the record's
 * `threat_lookup` then `cve_lookup` (#2523 / #2171); empty when the read went through or was not needed.
 */
function softwareNotRead(reconcile: NonNullable<CtemDecisionRecord["reconcile"]>, t_i18n: Translate): string {
  const lookup = [reconcile.threat_lookup, reconcile.cve_lookup].find((value) => typeof value === "string" && value && !SETTLED_LOOKUPS.has(value));
  if (lookup === "no OpenCTI access") return t_i18n("Not read: no OpenCTI access");
  if (lookup === "OpenCTI did not answer") return t_i18n("Not read: OpenCTI did not answer");
  if (lookup === "no reader") return t_i18n("Not read: no OpenCTI reader");
  return "";
}

/**
 * Whether the matched asset list of a record is shorter than what it matched (UX r2 B7 on #1995):
 * the record says so (`assets_capped`), or, on a record older than that flag, a primary asset
 * is reached by more inventory assets than the list names.
 */
function matchedAssetsCapped(reconcile: NonNullable<CtemDecisionRecord["reconcile"]>, count: number): boolean {
  if (reconcile.assets_capped) return true;
  const totals = reconcile.primary_asset_totals;
  if (!totals || typeof totals !== "object") return false;
  return Object.values(totals as Record<string, { assets?: unknown }>).some((t) => typeof t?.assets === "number" && t.assets > count);
}

/** The inputs the gate read, labelled, in the order the rule reads them; empty values are left out. */
export function decisionInputs(decision: CtemDecisionRecord | null | undefined, t_i18n: Translate): DecisionInput[] {
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
  // UX r2 B7 on #1995: each name once (an older record can name an asset twice).
  const assetNames = Array.isArray(reconcile.assets) ? [...new Set(reconcile.assets.filter(Boolean).map(String))] : [];
  const assetsCapped = assetNames.length > 0 && matchedAssetsCapped(reconcile, assetNames.length);
  const matched = list(assetNames) || (decision.reconcile && !domains ? t_i18n("None matched") : "");
  const reached = primaryAssetsRow(decision, t_i18n);
  // #2523: an unmatched signal names the software the gate tried, so a reader sees why nothing matched;
  // UX r1 B2 on #2533: a read that never happened says so instead of an empty row.
  const unresolved = decision.reason_code === "subject_unresolved";
  const triedNames = unresolved && Array.isArray(reconcile.software_refs) ? [...new Set(reconcile.software_refs.filter(Boolean).map(String))] : [];
  const notRead = unresolved ? softwareNotRead(reconcile, t_i18n) : "";
  const rows: DecisionInput[] = [
    { key: "lenses", label: t_i18n("Lenses"), value: lenses },
    { key: "confidence", label: t_i18n("Confidence"), value: level(reconcile.confidence) },
    {
      key: "matched_assets",
      label: t_i18n("Matched assets"),
      value: matched,
      names: assetNames.length > 0 ? assetNames : undefined,
      capped: assetsCapped || undefined,
    },
    // UX r1 B3 on #1796: the names the record matched, next to the primary assets reached below.
    { key: "primary_assets_named", label: t_i18n("Primary assets named"), value: list(reconcile.primary_assets_named) },
    { key: "software", label: t_i18n("Software"), value: list(reconcile.products) },
    {
      key: "software_tried",
      label: t_i18n("Software tried"),
      value: list(triedNames) || notRead,
      names: triedNames.length > 0 ? triedNames : undefined,
      noun: "software",
    },
    // UX r1 B2 on #2533: names were tried from the evidence, but the OpenCTI read of more never happened.
    { key: "software_not_read", label: t_i18n("OpenCTI software read"), value: triedNames.length > 0 ? notRead : "" },
    { key: "owned_domain", label: t_i18n("Owned domain"), value: domains },
    { key: "criticality", label: t_i18n("Criticality"), value: criticalityAgainstThreshold(scope, t_i18n) },
    // #1591 S3: the primary assets reached and their chains, then the one threshold and where it comes from.
    { key: "primary_assets_reached", label: t_i18n("Primary assets reached"), value: reached.join("\n"), lines: reached },
    {
      key: "scope_threshold",
      label: t_i18n("Scope threshold"),
      value: thresholdWithSource(scope?.threshold ? level(scope.threshold) : "", thresholdSource(decision), t_i18n),
    },
    { key: "risk_scenarios", label: t_i18n("Risk scenarios"), value: list((scope?.risk_scenarios ?? []).map((s) => s.name)) },
    { key: "policy_checks", label: t_i18n("Policy checks"), value: checks },
  ];
  return rows.filter((row) => row.value);
}

/**
 * The hero tile of the criticality (UX r1 B1 on #1995): the level alone as the value,
 * "≥ High" or "< High" (the threshold) as its caption, so the tile never repeats the sentence.
 */
export function criticalityFigure(
  scope: CtemDecisionRecord["scope"],
  t_i18n: Translate,
): { value: string; caption?: string } | null {
  if (!scope || (!scope.criticality && !scope.threshold)) return null;
  const threshold = scope.threshold ? String(levelLabel(scope.threshold, t_i18n)) : "";
  const value = scope.criticality ? String(levelLabel(scope.criticality, t_i18n)) : t_i18n("Unknown");
  if (!threshold) return { value };
  const rank = (level: string | null | undefined) => ["low", "medium", "high", "critical"].indexOf(String(level));
  const above = Boolean(scope.criticality) && rank(scope.criticality) >= rank(scope.threshold);
  return { value, caption: above ? t_i18n("≥ {threshold}", { threshold }) : t_i18n("< {threshold}", { threshold }) };
}

/** How many names a list input shows before "+N" (UX r1 B5 on #1995). */
const LIST_INPUT_NAMES = 3;

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

/**
 * The inputs the gate read as a labelled list (`ctem-decision-inputs`). The loop trace v2
 * (#1979) draws it in its scope section and leaves out the rows it draws elsewhere
 * (`omit`: the policy checks as the funnel, the primary assets reached as cards).
 */
/**
 * "2.5K assets: pay-api-01, vpn-gw-01, vpn-gw-lyon, +2.5K": the count compact, the first names,
 * the rest counted; a capped list reads "At least 5K assets: ..." (UX r2 B7 on #1995), never a cut length as the total.
 */
export function listSummary(
  names: readonly string[],
  locale: string,
  t_i18n: Translate,
  capped = false,
  noun: "assets" | "software" = "assets",
): string {
  const shown = names.slice(0, LIST_INPUT_NAMES).join(", ");
  const rest = names.length - LIST_INPUT_NAMES;
  const values = { total: formatCount(locale, names.length), count: names.length, names: shown };
  // UX r1 B1 on #2533: the software tried are counted as software, never as assets.
  const head =
    noun === "software"
      ? t_i18n("{total} {count, plural, one {software name} other {software names}}: {names}", values)
      : capped
        ? t_i18n("At least {total} {count, plural, one {asset} other {assets}}: {names}", values)
        : t_i18n("{total} {count, plural, one {asset} other {assets}}: {names}", values);
  return rest > 0 ? `${head}, ${t_i18n("+{count} more", { count: formatCount(locale, rest) })}` : head;
}

/**
 * How many names a list tooltip draws before "+N more": a matched asset list can hold thousands,
 * and a tooltip neither scrolls by keyboard nor leaves the viewport, so the list stays at one screen.
 */
const TIP_LIST_NAMES = 20;

/**
 * The names of a list input, one per line, up to TIP_LIST_NAMES (UX r2 B1 on #2533):
 * a joined string went through the tooltip's 280-character cut and lost the last names.
 */
function NameListTip({ names, testId }: { names: readonly string[]; testId: string }) {
  const { t_i18n, intl } = useFormatter();
  const shown = names.slice(0, TIP_LIST_NAMES);
  const rest = names.length - shown.length;
  return (
    <ul className="flex max-w-xs flex-col break-words" data-testid={testId}>
      {shown.map((name, index) => (
        <li key={`${index}-${name}`}>{name}</li>
      ))}
      {rest > 0 && <li className="text-default-secondary">{t_i18n("+{count} more", { count: formatCount(intl.locale, rest) })}</li>}
    </ul>
  );
}

export function DecisionInputs({ decision, omit = [] }: { decision: CtemDecisionRecord | null | undefined; omit?: readonly string[] }) {
  const { t_i18n, intl } = useFormatter();
  const inputs = decisionInputs(decision, t_i18n).filter((input) => !omit.includes(input.key));
  if (inputs.length === 0) return null;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 content-caption" data-testid="ctem-decision-inputs">
      {inputs.map((input) => (
        <div key={input.key} className="contents">
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
          ) : input.names && input.names.length > LIST_INPUT_NAMES ? (
            // UX r1 B5 on #1995: the count and the first names, every name in the design-system tooltip.
            <dd className="min-w-0" data-testid={`ctem-decision-input-${input.key}`}>
              <Tip content={<NameListTip names={input.names} testId={`ctem-decision-input-tip-${input.key}`} />}>
                <span className={cn("block truncate", TIP_WRAP_FOCUS_RING)} tabIndex={0}>
                  {listSummary(input.names, intl.locale, t_i18n, input.capped, input.noun)}
                </span>
              </Tip>
            </dd>
          ) : (
            <dd className="min-w-0">
              <TruncatedText text={input.value} />
            </dd>
          )}
        </div>
      ))}
    </dl>
  );
}

export function DecisionReason({
  state,
  decision,
  showInputs = false,
  showNextStep = showInputs,
}: {
  state: CtemSignalState;
  decision: CtemDecisionRecord | null | undefined;
  /** The loop trace lists the inputs; a row keeps the chip and the sentence. */
  showInputs?: boolean;
  /** The next step under the sentence (with the inputs by default; the trace v2 hero alone, #1979). */
  showNextStep?: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  const formatDay = (iso: string) => formatDayIntl(intl, iso);
  const next = showNextStep ? nextStepSentence(decision, t_i18n, formatDay) : null;
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
      {showInputs && <DecisionInputs decision={decision} />}
    </div>
  );
}
