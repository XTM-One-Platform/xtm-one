// SPDX-License-Identifier: Apache-2.0

/**
 * "Mobilization" on a case page (#2061): how the remediation is carried out,
 * a ticket, a change applied through a bound integration under the gate
 * `ctem.production_change`, or both, and for every applied change the
 * integration, its kind, what was pushed, the ids the integration returned and
 * how to revert it. The ticket itself keeps its chip in the page header.
 * The two gates of this step, `ctem.mobilize` and `ctem.production_change`,
 * show their decision here (#2464): the record, and while one waits its open
 * decision on the unified card. Never blank: a case with nothing mobilized and
 * no gate met says what will appear.
 */
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CtemCaseEvent, CtemCaseGateRecord, CtemCaseTicket } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { CASE_SECTION_ICON, CaseCardTitleText, CaseFactChip } from "./case-icons";
import { APPLIED_CHANGE_KIND_LABEL, MOBILIZATION_MODE_LABEL, appliedChanges, mobilizationMode, type AppliedChange } from "./case-mobilization";
import { CaseGateDecision } from "./CaseGateDecision";
import { CaseSectionReason } from "./CaseSectionReason";
import { gateRecordOf } from "./GateDecisionRecord";

/** The gates of the Mobilize step, in the order they are met. */
export const MOBILIZATION_GATES = ["ctem.mobilize", "ctem.production_change"] as const;

/** The ids shown before "+N more" (long lists: 8, then the count). */
const IDS_SHOWN = 8;

function AppliedChangeRow({ change }: { change: AppliedChange }) {
  const { t_i18n, intl } = useFormatter();
  const shown = change.ids.slice(0, IDS_SHOWN);
  const more = change.ids.length - shown.length;
  return (
    <li className="space-y-2 rounded-sm border border-elevation-subtle-soft p-3" data-testid="ctem-case-applied-change">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <CaseFactChip icon={CASE_SECTION_ICON.mobilization} tone="blue" testId="ctem-case-applied-change-kind">
          {change.kind ? t_i18n(APPLIED_CHANGE_KIND_LABEL[change.kind]) : t_i18n("Applied change")}
        </CaseFactChip>
        {change.integration && (
          <CaseFactChip icon="network" testId="ctem-case-applied-change-integration">
            {change.integration}
          </CaseFactChip>
        )}
        {change.at && (
          <span className="ml-auto whitespace-nowrap text-default-secondary">
            {intl.formatDate(change.at, { dateStyle: "medium", timeStyle: "short" })}
          </span>
        )}
      </div>
      <dl className="grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)] gap-x-3 gap-y-1">
        <dt className="text-default-secondary">{t_i18n("Pushed")}</dt>
        <dd className="min-w-0 whitespace-pre-wrap break-words" data-testid="ctem-case-applied-change-pushed">
          {change.pushed || t_i18n("None")}
        </dd>
        <dt className="text-default-secondary">{t_i18n("Returned ids")}</dt>
        <dd className="min-w-0" data-testid="ctem-case-applied-change-ids">
          {shown.length === 0 ? (
            t_i18n("None")
          ) : (
            <span className="flex flex-wrap gap-1">
              {shown.map((id) => (
                <code key={id} className="rounded-sm border border-elevation-subtle bg-elevation-default px-2 font-mono-plex content-caption break-all">
                  {id}
                </code>
              ))}
              {more > 0 && (
                <span className="text-default-secondary" data-testid="ctem-case-applied-change-more">
                  {t_i18n("+{count} more", { count: more })}
                </span>
              )}
            </span>
          )}
        </dd>
        <dt className="text-default-secondary">{t_i18n("How to revert")}</dt>
        <dd className="min-w-0 whitespace-pre-wrap break-words" data-testid="ctem-case-applied-change-rollback">
          {change.rollback || t_i18n("None")}
        </dd>
      </dl>
    </li>
  );
}

export function CaseMobilization({
  history,
  tickets,
  gates,
  personName,
}: {
  history: readonly CtemCaseEvent[];
  tickets: readonly CtemCaseTicket[] | undefined;
  /** The resolved decision records of the case (#1518); absent on an older API. */
  gates?: readonly CtemCaseGateRecord[];
  /** The directory name of a person id, as the case page resolves it. */
  personName?: (id: string) => string | null;
}) {
  const { t_i18n } = useFormatter();
  const changes = appliedChanges(history);
  const mode = mobilizationMode(changes, tickets);
  const records = MOBILIZATION_GATES.map((key) => gateRecordOf(gates, key)).filter((r): r is CtemCaseGateRecord => r !== null);

  return (
    <Card data-testid="ctem-case-mobilization">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.mobilization}>{t_i18n("Mobilization")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        {records.map((record) => (
          <CaseGateDecision
            key={record.key}
            record={record}
            history={history}
            personName={personName}
            titled
            testId={`ctem-case-mobilization-gate-${record.key === "ctem.mobilize" ? "mobilize" : "production-change"}`}
          />
        ))}
        {!mode && records.length === 0 ? (
          <CaseSectionReason
            own={t_i18n(
              "How the fix is carried out once decided: a ticket for the owner, a change applied through a bound integration after the production change gate, or both.",
            )}
            icon={CASE_SECTION_ICON.mobilization}
            testId="ctem-case-mobilization-empty"
          />
        ) : !mode ? null : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-default-secondary">{t_i18n("Mode")}</span>
              <CaseFactChip icon={mode === "ticket" ? "tag" : CASE_SECTION_ICON.mobilization} testId="ctem-case-mobilization-mode">
                {t_i18n(MOBILIZATION_MODE_LABEL[mode])}
              </CaseFactChip>
            </div>
            {changes.length > 0 && (
              <ul className="space-y-2" aria-label={t_i18n("Applied changes")}>
                {changes.map((change, index) => (
                  <AppliedChangeRow key={`${change.at ?? ""}-${index}`} change={change} />
                ))}
              </ul>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
