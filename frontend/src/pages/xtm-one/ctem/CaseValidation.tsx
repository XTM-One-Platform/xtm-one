// SPDX-License-Identifier: Apache-2.0

/**
 * The validation of a case (CTEM-12-UI, gate A `ctem.validation.non_readonly`):
 * the order the target asset groups were tested in, and gate A: passed
 * automatically at the fleet's level, or the waiting decision on the unified
 * work-item decision card (dev-docs/human-gates.md), never a CTEM card. The
 * platform never classifies an inject (#1810). Read from the case's own
 * validation references and `gate` events. Never blank: a case not validated
 * yet says so.
 */
import { Icon, type IconName } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { WorkItemDecisionCard } from "@/components/work-items/WorkItemDecisionCard";
import { mayAnswerCaseGate } from "@/lib/ctem-access";
import type { CtemCaseEvent, CtemCaseGateRecord, CtemCaseScenario, CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useWorkItems } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { GATE_CARD_STATUS_LABEL, latestGate } from "./case-gates";
import { PROVIDER_NAMES } from "./case-loop";
import { VALIDATION_GATE, autoPassReason, chosenOptionLabel, latestValidations, validationDecisionKey } from "./case-validation";
import { CaseSectionReason } from "./CaseSectionReason";
import { useGateDecisionItem } from "./CaseGateDecision";
import { CASE_FACT_ICON, CASE_SECTION_ICON, CaseCardTitleText, CaseIcon } from "./case-icons";
import { GateDecisionRecord, gateAnswererName, gateRecordOf } from "./GateDecisionRecord";
import { ToneBadge, type HotTone } from "@/components/ui/tone-badge";
import { TruncatedText } from "@/components/ui/truncated-text";
import { safeExternalUrl } from "@/lib/utils";

/** The result word of a validation reference, translated where drawn. @i18n-keys */
const RESULT_LABEL = {
  passed: "Passed",
  not_exposed: "Not exposed",
  failed: "Failed",
  pending: "Pending",
  inconclusive: "Inconclusive",
} as const;

const RESULT_TONE: Record<keyof typeof RESULT_LABEL, HotTone> = {
  passed: "green",
  not_exposed: "green",
  failed: "red",
  pending: "yellow",
  // A pass that proves nothing (#1692): no verdict colour.
  inconclusive: "neutral",
};

/**
 * The word of an inconclusive pass, with the result the check reported (#2466):
 * "clean but not proof" never reads like "failed" or "no data". @i18n-keys
 */
const REPORTED_INCONCLUSIVE_LABEL = {
  passed: "Passed, not conclusive",
  not_exposed: "Not exposed, not conclusive",
} as const;

/** The badge word of a validation reference: the reported result kept on an inconclusive pass (#2466). */
function resultLabel(v: { result: string; reported_result?: string }): string | null {
  if (v.result === "inconclusive" && v.reported_result && v.reported_result in REPORTED_INCONCLUSIVE_LABEL) {
    return REPORTED_INCONCLUSIVE_LABEL[v.reported_result as keyof typeof REPORTED_INCONCLUSIVE_LABEL];
  }
  return v.result in RESULT_LABEL ? RESULT_LABEL[v.result as keyof typeof RESULT_LABEL] : null;
}

/** The icon of each result, before its word (#1679, point 5). */
const RESULT_ICON: Record<keyof typeof RESULT_LABEL, IconName> = {
  passed: "circle-check",
  not_exposed: "shield-check",
  failed: "circle-x",
  pending: "clock",
  inconclusive: "circle-question-mark",
};

/** DS logos of the validation providers a case can carry; their names come from case-loop's shared map. */
const PROVIDER_ICON = { openaev: "custom/openaev", opencti: "custom/opencti" } as const;

export function CaseValidation({
  caseId,
  validations,
  history,
  gates,
  personName,
  scenario,
}: {
  caseId: string;
  validations: CtemCaseSummary["validations"];
  /** The OpenAEV scenario of the validation (#2468); absent or null when none is recorded. */
  scenario?: CtemCaseScenario | null;
  history: readonly CtemCaseEvent[];
  /** The resolved decision records of the case (#1518); absent on an older API. */
  gates?: readonly CtemCaseGateRecord[];
  /** The directory name of a person id, as the case page resolves it. */
  personName?: (id: string) => string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const { user, isAdmin } = useAuth();
  const rows = latestValidations(validations);
  const gate = latestGate(history, VALIDATION_GATE);
  // The decision record wins over the case event: an answered decision never reads as waiting (#1518).
  const record = gateRecordOf(gates, VALIDATION_GATE);
  const waiting = record ? record.status === "waiting" : gate?.to === "waited";
  const answered = !record && gate?.to === "answered";
  // The open decision while it waits, by the record's own work item (#2464), the
  // key only on an API that names none; the closed one once answered (its option labels).
  const open = useGateDecisionItem(record, waiting, validationDecisionKey(caseId));
  const items = useWorkItems({ correlation_key: validationDecisionKey(caseId), open_only: false, limit: 1 }, answered && !!gate?.evidence);
  const item = open.item;
  const chosen = answered ? chosenOptionLabel(items.data?.[0], gate?.evidence) : null;
  const auto = autoPassReason(gate?.to);
  const who = answered && gate ? ((gate.reason && personName?.(gate.reason)) || t_i18n("a person")) : null;
  // The scenario link, as the threat package's (#2468): only for an OpenAEV reader, only a safe URL.
  const scenarioHref = scenario && !scenario.restricted ? safeExternalUrl(scenario.url ?? undefined) : undefined;
  const missing = scenario?.playable === false && typeof scenario.techniques_missing === "number" ? scenario.techniques_missing : null;

  return (
    <Card data-testid="ctem-case-validation">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.validation}>{t_i18n("Validation")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {scenario && (scenarioHref || missing !== null) && (
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="ctem-case-validation-scenario">
            {scenarioHref && (
              <Button variant="outline" size="sm" className="gap-1.5" asChild>
                <a href={scenarioHref} target="_blank" rel="noopener noreferrer" data-testid="ctem-case-scenario-link">
                  <Icon name="external-link" size={16} className="shrink-0" aria-hidden />
                  {t_i18n("Open scenario")}
                </a>
              </Button>
            )}
            {missing !== null && (
              <ToneBadge tone="yellow" icon={<CaseIcon name="triangle-alert" />} data-testid="ctem-case-scenario-not-playable">
                {t_i18n("{total, plural, one {Not playable: {missing} of # technique has no payload} other {Not playable: {missing} of # techniques have no payload}}", {
                  missing,
                  total: scenario.techniques_total ?? 0,
                })}
              </ToneBadge>
            )}
          </div>
        )}
        {rows.length === 0 && !gate && !record && (
          <CaseSectionReason
            own={t_i18n("Checks in OpenAEV that prove whether the exposure is real, once the case is prioritized.")}
            source="openaev"
            icon={CASE_SECTION_ICON.validation}
            testId="ctem-case-validation-empty"
          />
        )}
        {record && (
          <GateDecisionRecord
            record={record}
            byLabel={gateAnswererName(record, history, personName)}
            testId="ctem-case-validation-record"
          />
        )}
        {gate && !record && (
          <div className="flex flex-wrap items-baseline gap-x-2" data-testid="ctem-case-validation-gate">
            <span className="font-medium">
              {t_i18n(GATE_CARD_STATUS_LABEL[gate.to as keyof typeof GATE_CARD_STATUS_LABEL] ?? gate.to ?? "")}
            </span>
            {auto === "fleet_runs" && <span className="text-default-secondary">{t_i18n("The fleet runs this gate without a person")}</span>}
            {gate.evidence && (
              <span data-testid="ctem-case-validation-option">
                {chosen
                  ? t_i18n("Option {option}: {label}", { option: gate.evidence, label: chosen })
                  : t_i18n("Option {option}", { option: gate.evidence })}
              </span>
            )}
            {who && <span className="text-default-secondary">{t_i18n("by {name}", { name: who })}</span>}
            {gate.at && (
              <span className="ml-auto whitespace-nowrap text-default-secondary">
                {intl.formatDate(gate.at, { dateStyle: "medium", timeStyle: "short" })}
              </span>
            )}
          </div>
        )}
        {waiting && item && (
          <WorkItemDecisionCard item={item} inCase canAnswer={mayAnswerCaseGate(item, user, Boolean(isAdmin))}
              readOnlyNote={t_i18n("Only an administrator or a member of a group with View and decide can answer this decision.")}
              onAnswered={open.refetch}
            />
        )}
        {waiting && !item && !open.isLoading && record?.decision_filed === false && (
          <p className="text-default-secondary" data-testid="ctem-case-validation-not-filed">
            {t_i18n("No validation decision was filed, so nobody can answer it yet. The fleet files it the next time it runs this case.")}
          </p>
        )}
        {waiting && !item && !open.isLoading && record?.decision_filed !== false && (
          <p className="text-default-secondary" data-testid="ctem-case-validation-elsewhere">
            {t_i18n("The validation waits for a person: the decision is in their Waiting for you list.")}
          </p>
        )}
        {rows.length > 0 && (
          <ul className="space-y-2" aria-label={t_i18n("Validation runs")}>
            {rows.map((v, index) => {
              const label = resultLabel(v);
              // A detection or behaviour test that reported a pass on a patch or upgrade case (#1500, #2466).
              const needsVersionRead = v.result === "inconclusive" && v.evidence_type === "validation";
              return (
                <li key={`${v.ref}-${index}`} className="flex min-w-0 flex-col gap-1" data-testid="ctem-case-validation-run">
                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="inline-flex items-center gap-1.5 font-medium" data-testid="ctem-case-validation-provider">
                      {v.provider in PROVIDER_ICON && <CaseIcon name={PROVIDER_ICON[v.provider as keyof typeof PROVIDER_ICON]} />}
                      {PROVIDER_NAMES[v.provider] ?? v.provider}
                    </span>
                    {label ? (
                      <ToneBadge
                        tone={RESULT_TONE[v.result as keyof typeof RESULT_LABEL]}
                        icon={<CaseIcon name={RESULT_ICON[v.result as keyof typeof RESULT_LABEL]} />}
                        data-testid="ctem-case-validation-result"
                      >
                        {t_i18n(label)}
                      </ToneBadge>
                    ) : (
                      <ToneBadge tone="neutral" icon={<CaseIcon name={RESULT_ICON.inconclusive} />} data-testid="ctem-case-validation-result">
                        {v.result}
                      </ToneBadge>
                    )}
                    {v.at && (
                      <span className="ml-auto whitespace-nowrap text-default-secondary">
                        {intl.formatDate(v.at, { dateStyle: "medium", timeStyle: "short" })}
                      </span>
                    )}
                  </span>
                  {v.note && (
                    <span className="min-w-0 text-default-secondary" data-testid="ctem-case-validation-note">
                      <TruncatedText text={v.note} />
                    </span>
                  )}
                  {needsVersionRead && (
                    <span className="text-default-secondary" data-testid="ctem-case-validation-hint">
                      {t_i18n("To make it conclusive, run a check that reads the running version on the asset: an authenticated scan or an agent.")}
                    </span>
                  )}
                  {v.run_order && v.run_order.length > 0 && (
                    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="inline-flex items-center gap-1.5 text-default-secondary">
                        <CaseIcon name={CASE_FACT_ICON.order} />
                        {t_i18n("Run order")}
                      </span>
                      <ol className="flex min-w-0 flex-wrap items-center gap-1.5" aria-label={t_i18n("Run order")} data-testid="ctem-case-validation-order">
                        {v.run_order.map((group, step) => (
                          <li key={`${group}-${step}`} className="min-w-0">
                            {/* A cut group name is named whole by the shared tooltip, keyboard included (UX round 1 on #1708, B4). */}
                            <ToneBadge tone="neutral" className="max-w-full">
                              <TruncatedText text={t_i18n("{step}. {group}", { step: step + 1, group })} />
                            </ToneBadge>
                          </li>
                        ))}
                      </ol>
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
