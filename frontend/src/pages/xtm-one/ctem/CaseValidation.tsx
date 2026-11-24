// SPDX-License-Identifier: Apache-2.0

/**
 * The validation of a case (CTEM-12-UI, gate A `ctem.validation.non_readonly`):
 * whether the tests ran in safe mode (read-only checks only), the order the
 * target asset groups were tested in, and gate A: "Passed automatically: safe
 * mode" when nothing could change the target, or the waiting decision on the
 * unified work-item decision card (dev-docs/human-gates.md), never a CTEM card.
 * Read from the case's own validation references and `gate` events. Never
 * blank: a case not validated yet says so.
 */
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { WorkItemDecisionCard } from "@/components/work-items/WorkItemDecisionCard";
import type { CtemCaseEvent, CtemCaseGateRecord, CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
import { useWorkItems } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { waitsForAnswer } from "@/lib/work-item-context";
import { GATE_CARD_STATUS_LABEL, latestGate } from "./case-gates";
import {
  SAFE_MODE_LABEL,
  VALIDATION_GATE,
  autoPassReason,
  chosenOptionLabel,
  latestValidations,
  safeModeOf,
  validationDecisionKey,
} from "./case-validation";
import { CaseSectionReason } from "./CaseSectionReason";
import { CASE_FACT_ICON, CASE_SECTION_ICON, CaseCardTitleText, CaseFactChip, CaseIcon } from "./case-icons";
import { GateDecisionRecord, gateAnswererName, gateRecordOf } from "./GateDecisionRecord";
import { ToneBadge, type HotTone } from "@/components/ui/tone-badge";
import { TruncatedText } from "@/components/ui/truncated-text";

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

/** Product names and DS logos of the validation providers a case can carry. */
const PROVIDER_NAMES: Readonly<Record<string, string>> = { openaev: "OpenAEV", opencti: "OpenCTI" };
const PROVIDER_ICON = { openaev: "custom/openaev", opencti: "custom/opencti" } as const;

/** The short words of a safe-mode chip; the full sentence is its tooltip. @i18n-keys */
const SAFE_MODE_CHIP = { safe: "Read-only", unsafe: "Changes the target" } as const;

export function CaseValidation({
  caseId,
  validations,
  history,
  gates,
  personName,
}: {
  caseId: string;
  validations: CtemCaseSummary["validations"];
  history: readonly CtemCaseEvent[];
  /** The resolved decision records of the case (#1518); absent on an older API. */
  gates?: readonly CtemCaseGateRecord[];
  /** The directory name of a person id, as the case page resolves it. */
  personName?: (id: string) => string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const { isAdmin } = useAuth();
  const rows = latestValidations(validations);
  const gate = latestGate(history, VALIDATION_GATE);
  // The decision record wins over the case event: an answered decision never reads as waiting (#1518).
  const record = gateRecordOf(gates, VALIDATION_GATE);
  const waiting = record ? record.status === "waiting" : gate?.to === "waited";
  const answered = !record && gate?.to === "answered";
  // The decision item, read with the viewer's own work-item visibility: the
  // open one while it waits, the closed one once answered (its option labels).
  const items = useWorkItems(
    { correlation_key: validationDecisionKey(caseId), open_only: waiting, limit: 1 },
    waiting || (answered && !!gate?.evidence),
  );
  const item = waiting ? ((items.data ?? []).find(waitsForAnswer) ?? null) : null;
  const chosen = answered ? chosenOptionLabel(items.data?.[0], gate?.evidence) : null;
  const auto = autoPassReason(gate?.to, rows[0]);
  const who = answered && gate ? ((gate.reason && personName?.(gate.reason)) || t_i18n("a person")) : null;

  return (
    <Card data-testid="ctem-case-validation">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.validation}>{t_i18n("Validation")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {rows.length === 0 && !gate && !record && (
          <CaseSectionReason
            own={t_i18n("Read-only checks in OpenAEV that prove whether the exposure is real, once the case is prioritized.")}
            source="openaev"
            testId="ctem-case-validation-empty"
          />
        )}
        {record && (
          <GateDecisionRecord
            record={auto === "safe_mode" && record.status === "passed_automatically" ? { ...record, reason: { kind: "safe_mode" } } : record}
            byLabel={gateAnswererName(record, history, personName)}
            testId="ctem-case-validation-record"
          />
        )}
        {gate && !record && (
          <div className="flex flex-wrap items-baseline gap-x-2" data-testid="ctem-case-validation-gate">
            <span className="font-medium">
              {auto === "safe_mode"
                ? t_i18n("Passed automatically: safe mode")
                : t_i18n(GATE_CARD_STATUS_LABEL[gate.to as keyof typeof GATE_CARD_STATUS_LABEL] ?? gate.to ?? "")}
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
          <WorkItemDecisionCard item={item} canAnswer={!item.is_company_managed || isAdmin} onAnswered={() => items.refetch()} />
        )}
        {waiting && !item && !items.isLoading && (
          <p className="text-default-secondary" data-testid="ctem-case-validation-elsewhere">
            {t_i18n("A test that is not read-only waits for a person: the decision is in their Waiting for you list.")}
          </p>
        )}
        {rows.length > 0 && (
          <ul className="space-y-2" aria-label={t_i18n("Validation runs")}>
            {rows.map((v, index) => {
              const mode = safeModeOf(v);
              return (
                <li key={`${v.ref}-${index}`} className="flex min-w-0 flex-col gap-1" data-testid="ctem-case-validation-run">
                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="inline-flex items-center gap-1.5 font-medium" data-testid="ctem-case-validation-provider">
                      {v.provider in PROVIDER_ICON && <CaseIcon name={PROVIDER_ICON[v.provider as keyof typeof PROVIDER_ICON]} />}
                      {PROVIDER_NAMES[v.provider] ?? v.provider}
                    </span>
                    {v.result in RESULT_LABEL ? (
                      <ToneBadge tone={RESULT_TONE[v.result as keyof typeof RESULT_LABEL]} data-testid="ctem-case-validation-result">
                        {t_i18n(RESULT_LABEL[v.result as keyof typeof RESULT_LABEL])}
                      </ToneBadge>
                    ) : (
                      <ToneBadge tone="neutral" data-testid="ctem-case-validation-result">
                        {v.result}
                      </ToneBadge>
                    )}
                    {mode && (
                      <CaseFactChip
                        icon={mode === "safe" ? CASE_FACT_ICON.safe : CASE_FACT_ICON.unsafe}
                        tone={mode === "safe" ? "green" : "orange"}
                        tip={t_i18n(SAFE_MODE_LABEL[mode])}
                        testId="ctem-case-validation-mode"
                      >
                        {t_i18n(SAFE_MODE_CHIP[mode])}
                      </CaseFactChip>
                    )}
                    {v.at && (
                      <span className="ml-auto whitespace-nowrap text-default-secondary">
                        {intl.formatDate(v.at, { dateStyle: "medium", timeStyle: "short" })}
                      </span>
                    )}
                  </span>
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
