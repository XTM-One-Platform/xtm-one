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
import { WorkItemDecisionCard } from "@/components/work-items/WorkItemDecisionCard";
import type { CtemCaseEvent } from "@/hooks/api/useCtemCasesApi";
import { useWorkItems } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { waitsForAnswer } from "@/lib/work-item-context";
import { GATE_OUTCOME_LABEL, REMEDIATION_GATE, latestGate, remediationDecisionKey, remediationOptionLines } from "./case-gates";

export function CaseRemediationDecision({
  caseId,
  history,
  personName,
}: {
  caseId: string;
  history: readonly CtemCaseEvent[];
  /** The directory name of a person id, as the case page resolves it. */
  personName?: (id: string) => string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const { isAdmin } = useAuth();
  const gate = latestGate(history, REMEDIATION_GATE);
  const waiting = gate?.to === "waited";
  // The decision item, read with the viewer's own work-item visibility.
  const items = useWorkItems({ correlation_key: remediationDecisionKey(caseId), open_only: true, limit: 1 }, waiting);
  const item = waiting ? (items.data ?? []).find(waitsForAnswer) ?? null : null;
  const options = remediationOptionLines(history);
  const who = gate?.reason ? (personName?.(gate.reason) ?? gate.reason) : null;

  return (
    <Card data-testid="ctem-case-remediation">
      <CardHeader>
        <CardTitle>{t_i18n("Remediation decision")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {!gate ? (
          <p className="text-default-secondary">
            {t_i18n("Not decided yet: the fleet decides the remediation once the case is validated.")}
          </p>
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-2" data-testid="ctem-case-remediation-outcome">
            <span className="font-medium">
              {gate.to === "passed_automatically"
                ? t_i18n("Decided by policy")
                : t_i18n(GATE_OUTCOME_LABEL[gate.to as keyof typeof GATE_OUTCOME_LABEL])}
            </span>
            {gate.evidence && <span>{t_i18n("Option {option}", { option: gate.evidence })}</span>}
            {gate.to === "answered" && who && <span className="text-default-secondary">{t_i18n("by {name}", { name: who })}</span>}
            {gate.to !== "answered" && gate.reason && <span className="text-default-secondary">{gate.reason}</span>}
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
          <p className="text-default-secondary" data-testid="ctem-case-remediation-elsewhere">
            {t_i18n("A person decides it: the decision is in their Waiting for you list.")}
          </p>
        )}
        {options.length > 0 && !item && (
          <ul className="space-y-1 text-default-secondary" aria-label={t_i18n("Remediation options")}>
            {options.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
