// SPDX-License-Identifier: Apache-2.0

/**
 * The remediation decision of a case (CTEM-17-UI, gate B `ctem.remediation.decision`):
 * decided by policy, waiting for a person, or answered, read from the case's own
 * `gate` events (CTEM-34) and the figure-free option lines of its note. When it
 * waits, the open decision work item (`ctem:remediation:<case>`) is answered on
 * the unified decision card of work items, the one card of every human gate
 * (dev-docs/human-gates.md), never a CTEM card. The figures (cost, expected loss
 * before and after) live only in that item's options, written for the deciding
 * user's OpenCRQ access; a viewer who cannot see the item sees that it waits,
 * and no figure. Never blank: a case not decided yet says so.
 */
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CASE_SECTION_ICON, CaseCardTitleText } from "./case-icons";
import { WorkItemDecisionCard } from "@/components/work-items/WorkItemDecisionCard";
import { mayAnswerCaseGate } from "@/lib/ctem-access";
import type { CtemCaseEvent, CtemCaseGateRecord } from "@/hooks/api/useCtemCasesApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import {
  GATE_CARD_STATUS_LABEL,
  GATE_REASON_LABEL,
  OPTION_FACT_LABEL,
  REMEDIATION_GATE,
  gateReason,
  latestGate,
  parseRemediationOption,
  remediationDecisionKey,
  remediationOptionLines,
} from "./case-gates";
import { CaseSectionReason } from "./CaseSectionReason";
import { useGateDecisionItem } from "./CaseGateDecision";
import { GateDecisionRecord, gateAnswererName, gateRecordOf } from "./GateDecisionRecord";

export function CaseRemediationDecision({
  caseId,
  history,
  gates,
  personName,
}: {
  caseId: string;
  history: readonly CtemCaseEvent[];
  /** The resolved decision records of the case (#1518); absent on an older API. */
  gates?: readonly CtemCaseGateRecord[];
  /** The directory name of a person id, as the case page resolves it. */
  personName?: (id: string) => string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const { user, isAdmin } = useAuth();
  const gate = latestGate(history, REMEDIATION_GATE);
  // The decision record wins over the case event: an answered decision never reads as waiting (#1518).
  const record = gateRecordOf(gates, REMEDIATION_GATE);
  const waiting = record ? record.status === "waiting" : gate?.to === "waited";
  // The decision item, by the record's own work item (#2464), the key only on an API that names none.
  const items = useGateDecisionItem(record, waiting, remediationDecisionKey(caseId));
  const item = items.item;
  const options = remediationOptionLines(history).map(parseRemediationOption);
  // Who answered: their directory name, else "a person", never the raw id.
  const who = gate?.to === "answered" ? ((gate.reason && personName?.(gate.reason)) || t_i18n("a person")) : null;
  const why = gate ? gateReason(gate.reason, gate.to) : null;

  return (
    <Card data-testid="ctem-case-remediation">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.remediation_decision}>{t_i18n("Remediation decision")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {record ? (
          <GateDecisionRecord
            record={record}
            byLabel={gateAnswererName(record, history, personName)}
            testId="ctem-case-remediation-record"
          />
        ) : !gate ? (
          <CaseSectionReason
            own={t_i18n("The remediation options with their cost and the one approved, once the case is validated.")}
            icon={CASE_SECTION_ICON.remediation_decision}
            testId="ctem-case-remediation-empty"
          />
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-2" data-testid="ctem-case-remediation-outcome">
            <span className="font-medium">
              {gate.to === "passed_automatically"
                ? t_i18n("Decided by policy")
                : t_i18n(GATE_CARD_STATUS_LABEL[gate.to as keyof typeof GATE_CARD_STATUS_LABEL])}
            </span>
            {gate.evidence && <span>{t_i18n("Option {option}", { option: gate.evidence })}</span>}
            {who && <span className="text-default-secondary">{t_i18n("by {name}", { name: who })}</span>}
            {why && (
              <span className="text-default-secondary" data-testid="ctem-case-remediation-why">
                {t_i18n(GATE_REASON_LABEL[why.kind], { runbook: why.runbook ?? "" })}
              </span>
            )}
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
              onAnswered={items.refetch}
            />
        )}
        {waiting && !item && !items.isLoading && record?.decision_filed === false && (
          <p className="text-default-secondary" data-testid="ctem-case-remediation-not-filed">
            {t_i18n("No remediation decision was filed, so nobody can answer it yet. The fleet files it the next time it runs this case.")}
          </p>
        )}
        {waiting && !item && !items.isLoading && record?.decision_filed !== false && (
          <p className="text-default-secondary" data-testid="ctem-case-remediation-elsewhere">
            {t_i18n("A person decides it: the decision is in their Waiting for you list.")}
          </p>
        )}
        {options.length > 0 && !item && (
          <ul className="space-y-1 text-default-secondary" aria-label={t_i18n("Remediation options")}>
            {options.map((option, index) => (
              <li key={`${option.key}-${index}`} className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-default-primary">{option.key ? `${option.key}. ${option.label}` : option.label}</span>
                {option.facts.map((fact) => (
                  <span key={fact.kind}>
                    {t_i18n("{label}: {value}", { label: t_i18n(OPTION_FACT_LABEL[fact.kind]), value: fact.value })}
                  </span>
                ))}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
