// SPDX-License-Identifier: Apache-2.0

/**
 * The policy funnel of the loop trace v2 (#1979, Sam 24 Nov 2026: "the real funnel
 * on policy check ... right now it's too little"): one step per check of the
 * decision record's `policy_checks`, in the order the scope gate ran them
 * (backend/app/services/ctem_scope_gate.py), each with the rule in words, the
 * signal's value against the policy threshold and passed or failed, the steps
 * narrowing down to the gate's outcome and the one threshold with its source.
 *
 * A record with no check (no policy, or a signal stopped before the checks)
 * draws the outcome step alone.
 */
import { Icon } from "@filigran/design-system";
import type { CtemDecisionRecord, CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { formatDayIntl } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
import { decisionSentence, levelLabel, policyRuleLabel, SIGNAL_STATE_LABEL } from "./DecisionReason";
import { thresholdSource, thresholdWithSource } from "./decision-primary-assets";

type Translate = (id: string, values?: Record<string, string | number>) => string;
type PolicyCheck = NonNullable<CtemDecisionRecord["policy_checks"]>[number] & Record<string, unknown>;

/** #2631: a score the gate read nowhere; "not scored" is the note of a record written before it. */
const NO_SCORE_NOTES = new Set(["no score read", "not scored"]);

/** A check's value in words: a level translated, a yes/no, a number as it is, an unscored score said so. */
function checkValue(check: PolicyCheck, t_i18n: Translate): string {
  if (NO_SCORE_NOTES.has(String(check.note ?? ""))) return t_i18n("No score read");
  const value = check.value;
  if (typeof value === "boolean") return value ? t_i18n("Yes") : t_i18n("No");
  if (typeof value === "number") return String(value);
  if (typeof value === "string" && value) return String(levelLabel(value, t_i18n));
  // An exclusion or an accepted risk carries its own fields, not a value.
  if (typeof check.kind === "string" && check.value === undefined) return String(check.kind);
  if (typeof check.owner === "string") return String(check.owner);
  return "";
}

/** "7.8 ≥ 7" or "5.3 < 7": the value against the threshold, when the check has both. */
function checkComparison(check: PolicyCheck, t_i18n: Translate): string {
  const value = checkValue(check, t_i18n);
  const threshold = check.threshold;
  if (threshold === undefined || threshold === null || threshold === "") return value;
  const minimum = String(levelLabel(threshold, t_i18n));
  if (!value || NO_SCORE_NOTES.has(String(check.note ?? ""))) return value ? t_i18n("{value}, minimum {threshold}", { value, threshold: minimum }) : minimum;
  return check.passed ? t_i18n("{value} ≥ {threshold}", { value, threshold: minimum }) : t_i18n("{value} < {threshold}", { value, threshold: minimum });
}

export function PolicyFunnel({ state, decision }: { state: CtemSignalState; decision: CtemDecisionRecord | null | undefined }) {
  const { t_i18n, intl } = useFormatter();
  const checks = (decision?.policy_checks ?? []) as PolicyCheck[];
  const scope = decision?.scope ?? null;
  const threshold = thresholdWithSource(scope?.threshold ? String(levelLabel(scope.threshold, t_i18n)) : "", thresholdSource(decision), t_i18n);
  const stopped = decision?.outcome && decision.outcome !== "case";
  return (
    <ol className="flex min-w-0 flex-col gap-1" aria-label={t_i18n("Policy funnel")} data-testid="ctem-policy-funnel">
      {checks.map((check, index) => {
        // Each step narrows: the bar loses a little width at every check passed (a funnel, never a list).
        const inset = Math.min(index, 6) * 4;
        return (
          <li
            key={`${index}-${check.rule}`}
            data-rule={check.rule}
            data-passed={check.passed ? "true" : "false"}
            className={cn(
              "flex min-w-0 items-center gap-3 rounded-sm border px-3 py-2",
              check.passed ? "border-elevation-subtle layer-1 bg-elevation-default" : "border-feedback-error-primary layer-1 bg-elevation-hover",
            )}
            style={{ marginInline: `${inset}%` }}
          >
            <Icon
              name={check.passed ? "circle-check" : "circle-x"}
              size={16}
              className={cn("shrink-0", check.passed ? "text-feedback-success-primary" : "text-feedback-error-primary")}
              aria-hidden
            />
            <span className="min-w-0 flex-1 truncate content-compact font-medium">{policyRuleLabel(check.rule, t_i18n)}</span>
            <span className="shrink-0 content-compact tabular-nums" data-testid="ctem-policy-funnel-value">
              {checkComparison(check, t_i18n)}
            </span>
            <span className={cn("shrink-0 content-caption", check.passed ? "text-feedback-success-primary" : "text-feedback-error-primary")}>
              {check.passed ? t_i18n("Passed") : t_i18n("Failed")}
            </span>
          </li>
        );
      })}
      <li
        data-rule="outcome"
        className="flex min-w-0 items-center gap-3 rounded-sm border border-elevation-subtle layer-1 bg-elevation-hover px-3 py-2"
        style={{ marginInline: `${Math.min(checks.length, 6) * 4}%` }}
        data-testid="ctem-policy-funnel-outcome"
      >
        <Icon name={stopped ? "ban" : "shield-check"} size={16} className={cn("shrink-0", stopped ? "text-feedback-warning-primary" : "text-accent")} aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block content-compact font-medium">
            {t_i18n("Scope gate")}: {t_i18n(SIGNAL_STATE_LABEL[state] ?? state)}
          </span>
          <span className="block content-caption text-default-secondary">
            {decisionSentence(decision, t_i18n, (iso) => formatDayIntl(intl, iso))}
            {threshold ? ` · ${t_i18n("Scope threshold")}: ${threshold}` : ""}
          </span>
        </span>
      </li>
    </ol>
  );
}
