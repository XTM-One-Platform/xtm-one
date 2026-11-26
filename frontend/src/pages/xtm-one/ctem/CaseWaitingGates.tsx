// SPDX-License-Identifier: Apache-2.0

/**
 * The decisions a case waits on, at the top of its page (#2464): one line per
 * waiting gate record, "Waits for <recipient>: <gate>", with "Open the
 * decision" when an item was filed. Read from the case's resolved records
 * (`gates`) alone, never from who works the case, so a running, failed or
 * unknown worker never hides a waiting decision. A gate no section of the page
 * owns (`ctem.accept_risk`, `ctem.close_without_retest`, any gate added later)
 * gets its decision card here; a gate a section owns is answered in it.
 */
import { Link as RouterLink } from "react-router-dom";
import { Link } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CtemCaseEvent, CtemCaseGateRecord, CtemCaseState } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { workItemPath } from "@/lib/task-routes";
import { GATE_LABEL, REMEDIATION_GATE } from "./case-gates";
import { CASE_FACT_ICON, CaseCardTitleText } from "./case-icons";
import { CaseGateDecision } from "./CaseGateDecision";
import { MOBILIZATION_GATES } from "./CaseMobilization";
import { REVIEW_GATE } from "./case-review";
import { VALIDATION_GATE } from "./case-validation";

/** The gates a section of the case page answers in place. */
const SECTION_GATES: ReadonlySet<string> = new Set([REVIEW_GATE, VALIDATION_GATE, REMEDIATION_GATE, ...MOBILIZATION_GATES]);

/** Who a decision was asked of when the viewer may not see the name. @i18n-keys */
const RECIPIENT_FALLBACK = {
  user: "a person",
  group: "a group",
  company: "everyone in the company",
  admins: "the administrators",
} as const;

/** The waiting records of a case, oldest asked first; none unless the case waits for a decision or a record waits. */
export function waitingGateRecords(state: CtemCaseState | null | undefined, gates: readonly CtemCaseGateRecord[] | undefined): CtemCaseGateRecord[] {
  if (state === "resolved" || state === "accepted_risk") return [];
  return (gates ?? [])
    .filter((g) => g.status === "waiting")
    .slice()
    .sort((a, b) => (a.asked_at ?? "").localeCompare(b.asked_at ?? ""));
}

export function CaseWaitingGates({
  state,
  gates,
  history,
  personName,
}: {
  state: CtemCaseState | null | undefined;
  gates?: readonly CtemCaseGateRecord[];
  history: readonly CtemCaseEvent[];
  personName?: (id: string) => string | null;
}) {
  const { t_i18n } = useFormatter();
  const waiting = waitingGateRecords(state, gates);
  if (waiting.length === 0) return null;
  const recipient = (record: CtemCaseGateRecord) =>
    record.asked_of?.name ||
    t_i18n(RECIPIENT_FALLBACK[(record.asked_of?.type ?? "user") as keyof typeof RECIPIENT_FALLBACK] ?? RECIPIENT_FALLBACK.user);

  return (
    <Card data-testid="ctem-case-waiting-gates">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_FACT_ICON.wait}>{t_i18n("Waits for a decision")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 content-base">
        <ul className="space-y-1">
          {waiting.map((record) => (
            <li key={record.key} className="flex flex-wrap items-baseline gap-x-3 gap-y-1" data-testid="ctem-case-waiting-gate" data-gate={record.key}>
              <span>
                {t_i18n("Waits for {name}: {gate}", { name: recipient(record), gate: t_i18n(GATE_LABEL[record.key] ?? "Gate") })}
              </span>
              {record.work_item_id && (
                <Link asChild>
                  <RouterLink to={workItemPath(record.work_item_id)} data-testid="ctem-case-waiting-gate-open">
                    {t_i18n("Open the decision")}
                  </RouterLink>
                </Link>
              )}
            </li>
          ))}
        </ul>
        {waiting
          .filter((record) => !SECTION_GATES.has(record.key))
          .map((record) => (
            <CaseGateDecision
              key={record.key}
              record={record}
              history={history}
              personName={personName}
              titled
              testId="ctem-case-waiting-gate-decision"
            />
          ))}
      </CardContent>
    </Card>
  );
}
