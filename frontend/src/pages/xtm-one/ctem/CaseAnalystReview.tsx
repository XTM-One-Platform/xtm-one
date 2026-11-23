// SPDX-License-Identifier: Apache-2.0

/**
 * "Analyst review" on a case page (CTEM-16-UI, #1115, gate `ctem.analyst_review`):
 * the threat package the fleet drafted in OpenCTI (a Grouping, its link), whether
 * it is still in its draft (never "coverage requested" while the coverage sits
 * in the draft, and a signal once the draft outlives the case SLA, #1480) or
 * validated with its security coverage requested, and the gate: "Analyst review skipped by
 * policy" when it ran without a person (never hidden), the waiting review on the
 * unified work-item decision card (dev-docs/human-gates.md), never a CTEM card,
 * or who answered: a person or the analyst's mark in OpenCTI. A viewer who does
 * not read OpenCTI sees that a package exists, not which. Never blank.
 */
import { Icon } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { WorkItemDecisionCard } from "@/components/work-items/WorkItemDecisionCard";
import type { CtemCaseEvent, CtemCaseGateRecord, CtemCaseThreatPackage } from "@/hooks/api/useCtemCasesApi";
import { useWorkItems } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { safeExternalUrl } from "@/lib/utils";
import { waitsForAnswer } from "@/lib/work-item-context";
import { GATE_OUTCOME_LABEL, latestGate } from "./case-gates";
import { REVIEW_GATE, REVIEW_STAGE_LABEL, reviewAnswerer, reviewDecisionKey, reviewStage } from "./case-review";
import { CaseSectionReason } from "./CaseSectionReason";
import { GateDecisionRecord, gateRecordOf } from "./GateDecisionRecord";

export function CaseAnalystReview({
  caseId,
  pkg,
  history,
  gates,
  personName,
}: {
  caseId: string;
  pkg: CtemCaseThreatPackage | null | undefined;
  history: readonly CtemCaseEvent[];
  /** The resolved decision records of the case (#1518); absent on an older API. */
  gates?: readonly CtemCaseGateRecord[];
  /** The directory name of a person id, as the case page resolves it. */
  personName?: (id: string) => string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const { isAdmin } = useAuth();
  const gate = latestGate(history, REVIEW_GATE);
  // The decision record wins over the case event: an answered decision never reads as waiting (#1518).
  const record = gateRecordOf(gates, REVIEW_GATE);
  const waiting = record ? record.status === "waiting" : gate?.to === "waited";
  const items = useWorkItems({ correlation_key: reviewDecisionKey(caseId), open_only: true, limit: 1 }, waiting);
  const item = waiting ? ((items.data ?? []).find(waitsForAnswer) ?? null) : null;
  const stage = reviewStage(pkg);
  const href = pkg && !pkg.restricted ? safeExternalUrl(pkg.url ?? undefined) : undefined;
  const name = pkg && !pkg.restricted ? pkg.name || t_i18n("Unnamed Grouping") : null;
  const answerer = record?.answered_by
    ? reviewAnswerer(record.answered_by.id)
    : gate?.to === "answered"
      ? reviewAnswerer(gate.reason)
      : null;
  const who =
    answerer?.kind === "person"
      ? (personName?.(answerer.id) ?? t_i18n("a person"))
      : answerer?.kind === "mark"
        ? t_i18n("the analyst's mark in OpenCTI")
        : answerer
          ? t_i18n("a person")
          : null;

  return (
    <Card data-testid="ctem-case-analyst-review">
      <CardHeader>
        <CardTitle>{t_i18n("Analyst review")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {record && (
          <GateDecisionRecord
            record={record}
            byLabel={record.answered_by?.name ? null : who}
            testId="ctem-case-analyst-review-record"
          />
        )}
        {gate && !record && (
          <div className="flex flex-wrap items-baseline gap-x-2" data-testid="ctem-case-analyst-review-gate">
            <span className="font-medium">
              {gate.to === "passed_automatically"
                ? t_i18n("Analyst review skipped by policy")
                : t_i18n(GATE_OUTCOME_LABEL[gate.to as keyof typeof GATE_OUTCOME_LABEL] ?? gate.to ?? "")}
            </span>
            {gate.to === "passed_automatically" && (
              <span className="text-default-secondary">{t_i18n("The fleet requests the coverage without a person")}</span>
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
          <p className="text-default-secondary" data-testid="ctem-case-analyst-review-elsewhere">
            {t_i18n("The review waits for an analyst: the decision is in their Waiting for you list, or they set the review label on the Grouping in OpenCTI.")}
          </p>
        )}
        <div className="flex min-w-0 flex-col gap-1" data-testid="ctem-case-threat-package">
          {stage === "not_started" && !gate && !record ? (
            <CaseSectionReason own={t_i18n(REVIEW_STAGE_LABEL[stage])} source="opencti" testId="ctem-case-analyst-review-empty" />
          ) : (
            <span className={stage === "not_started" ? "text-default-secondary" : undefined} data-testid="ctem-case-threat-package-stage">
              {t_i18n(REVIEW_STAGE_LABEL[stage])}
            </span>
          )}
          {pkg?.draft_overdue && (
            <span className="flex items-start gap-1 content-compact text-feedback-warning-primary" data-testid="ctem-case-threat-package-overdue">
              <Icon name="triangle-alert" size={16} className="mt-0.5 shrink-0" aria-hidden />
              <span>{t_i18n("Still in a draft past the case SLA: validate it in OpenCTI or answer the review")}</span>
            </span>
          )}
          {pkg && (
            <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              {pkg.restricted ? (
                <span className="text-default-secondary">{t_i18n("A Grouping exists in OpenCTI: restricted to OpenCTI readers")}</span>
              ) : href ? (
                <a
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex min-w-0 items-center gap-1 font-medium hover:underline"
                  data-testid="ctem-case-threat-package-link"
                >
                  <span className="truncate" title={name ?? undefined}>{name}</span>
                  <Icon name="external-link" size={16} className="shrink-0" />
                  <span className="sr-only">{t_i18n("Open in OpenCTI")}</span>
                </a>
              ) : (
                <span className="min-w-0 truncate font-medium" title={name ?? undefined}>{name}</span>
              )}
              {pkg.at && (
                <span className="ml-auto whitespace-nowrap text-default-secondary">
                  {intl.formatDate(pkg.at, { dateStyle: "medium", timeStyle: "short" })}
                </span>
              )}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
