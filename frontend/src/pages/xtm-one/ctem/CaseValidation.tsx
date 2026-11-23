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
import type { CtemCaseEvent, CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";
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

/** The result word of a validation reference, translated where drawn. @i18n-keys */
const RESULT_LABEL = {
  passed: "Passed",
  not_exposed: "Not exposed",
  failed: "Failed",
  pending: "Pending",
} as const;

/** Product names of the validation providers a case can carry. */
const PROVIDER_NAMES: Readonly<Record<string, string>> = { openaev: "OpenAEV", opencti: "OpenCTI" };

export function CaseValidation({
  caseId,
  validations,
  history,
  personName,
}: {
  caseId: string;
  validations: CtemCaseSummary["validations"];
  history: readonly CtemCaseEvent[];
  /** The directory name of a person id, as the case page resolves it. */
  personName?: (id: string) => string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const { isAdmin } = useAuth();
  const rows = latestValidations(validations);
  const gate = latestGate(history, VALIDATION_GATE);
  const waiting = gate?.to === "waited";
  const answered = gate?.to === "answered";
  // The decision item, read with the viewer's own work-item visibility: the
  // open one while it waits, the closed one once answered (its option labels).
  const items = useWorkItems(
    { correlation_key: validationDecisionKey(caseId), open_only: waiting, limit: 1 },
    waiting || (answered && !!gate?.evidence),
  );
  const item = waiting ? ((items.data ?? []).find(waitsForAnswer) ?? null) : null;
  const chosen = answered ? chosenOptionLabel(items.data?.[0], gate?.evidence) : null;
  const auto = autoPassReason(gate?.to, rows[0]);
  const who = gate?.to === "answered" ? ((gate.reason && personName?.(gate.reason)) || t_i18n("a person")) : null;

  return (
    <Card data-testid="ctem-case-validation">
      <CardHeader>
        <CardTitle>{t_i18n("Validation")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {rows.length === 0 && !gate && (
          <CaseSectionReason
            own={t_i18n("Read-only checks in OpenAEV that prove whether the exposure is real, once the case is prioritized.")}
            source="openaev"
            testId="ctem-case-validation-empty"
          />
        )}
        {gate && (
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
                  <span className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-medium">{PROVIDER_NAMES[v.provider] ?? v.provider}</span>
                    <span>{t_i18n(RESULT_LABEL[v.result] ?? v.result)}</span>
                    {mode && (
                      <span className="text-default-secondary" data-testid="ctem-case-validation-mode">
                        {t_i18n(SAFE_MODE_LABEL[mode])}
                      </span>
                    )}
                    {v.at && (
                      <span className="ml-auto whitespace-nowrap text-default-secondary">
                        {intl.formatDate(v.at, { dateStyle: "medium", timeStyle: "short" })}
                      </span>
                    )}
                  </span>
                  {v.run_order && v.run_order.length > 0 && (
                    <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                      <span className="text-default-secondary">{t_i18n("Run order")}</span>
                      <ol className="flex min-w-0 flex-wrap items-baseline gap-x-2" aria-label={t_i18n("Run order")} data-testid="ctem-case-validation-order">
                        {v.run_order.map((group, step) => (
                          <li key={`${group}-${step}`} className="min-w-0 truncate" title={group}>
                            {t_i18n("{step}. {group}", { step: step + 1, group })}
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
