// SPDX-License-Identifier: Apache-2.0

/**
 * "Treatment plan" on a case page (CTEM-18-UI, #1068): the OpenCRQ plan the
 * remediation decision created for the case, its name and a link to OpenCRQ.
 * A viewer who does not read OpenCRQ sees that a plan exists, not which.
 * Never blank: a case with no plan says when one appears.
 */
import { Icon } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TruncatedText } from "@/components/ui/truncated-text";
import { CASE_SECTION_ICON, CaseCardTitleText } from "./case-icons";
import type { CtemCaseTreatmentPlan } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { safeExternalUrl } from "@/lib/utils";
import { CaseSectionReason } from "./CaseSectionReason";

export function CaseTreatmentPlan({ plan }: { plan: CtemCaseTreatmentPlan | null | undefined }) {
  const { t_i18n, intl } = useFormatter();
  const href = plan && !plan.restricted ? safeExternalUrl(plan.url ?? undefined) : undefined;
  const name = plan && !plan.restricted ? plan.name || t_i18n("Unnamed plan") : null;

  return (
    <Card data-testid="ctem-case-treatment-plan">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.treatment_plan}>{t_i18n("Treatment plan")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="content-base">
        {!plan ? (
          <CaseSectionReason
            own={t_i18n("The treatment plan the approved remediation writes in OpenCRQ, with its cost and promised loss reduction.")}
            source="opencrq"
            icon={CASE_SECTION_ICON.treatment_plan}
            testId="ctem-case-treatment-plan-empty"
          />
        ) : (
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
            {plan.restricted ? (
              <span className="text-default-secondary">{t_i18n("A plan exists in OpenCRQ: restricted to OpenCRQ readers")}</span>
            ) : href ? (
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex min-w-0 items-center gap-1 font-medium hover:underline"
                data-testid="ctem-case-treatment-plan-link"
              >
                <TruncatedText text={name ?? ""} />
                <Icon name="external-link" size={16} className="shrink-0" />
                <span className="sr-only">{t_i18n("Open in OpenCRQ")}</span>
              </a>
            ) : (
              <TruncatedText text={name ?? ""} className="font-medium" />
            )}
            {plan.at && (
              <span className="text-default-secondary">
                {t_i18n("Attached on {date}", { date: intl.formatDate(plan.at, { dateStyle: "medium" }) })}
              </span>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
