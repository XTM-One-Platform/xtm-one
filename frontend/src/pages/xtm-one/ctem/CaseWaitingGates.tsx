// SPDX-License-Identifier: Apache-2.0

/**
 * The decisions a case waits on, at the top of its page (#2464): one line per
 * waiting gate record, "Waits for <recipient>: <gate>". The decision is
 * answered on the page: no link away while its card renders in place (here or
 * in its section); "Open the decision", a DS button, only when the viewer
 * cannot read the item on the page. Read from the case's resolved records
 * (`gates`) alone, never from who works the case, so a running, failed or
 * unknown worker never hides a waiting decision. A gate no section of the page
 * owns (`ctem.accept_risk`, `ctem.close_without_retest`, any gate added later)
 * gets its decision card here; a gate a section owns is answered in it.
 */
import { Link as RouterLink } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CtemCaseEvent, CtemCaseGateRecord, CtemCaseState } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { workItemPath } from "@/lib/task-routes";
import { GATE_LABEL, REMEDIATION_GATE } from "./case-gates";
import { CASE_FACT_ICON, CaseCardTitleText } from "./case-icons";
import { CaseGateDecision, useGateDecisionItem } from "./CaseGateDecision";
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

/** One waiting line; its link away only when the decision card cannot render on the page. */
function WaitingGateLine({ record, recipient }: { record: CtemCaseGateRecord; recipient: string }) {
  const { t_i18n } = useFormatter();
  // The same read as the card's own (CaseGateDecision, the sections): found, the card is on the page.
  const { item, isLoading } = useGateDecisionItem(record, true);
  const linkAway = !!record.work_item_id && !item && !isLoading;
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="ctem-case-waiting-gate" data-gate={record.key}>
      <span>{t_i18n("Waits for {name}: {gate}", { name: recipient, gate: t_i18n(GATE_LABEL[record.key] ?? "Gate") })}</span>
      {linkAway && (
        <Button variant="outline" size="sm" className="gap-1.5" asChild>
          <RouterLink to={workItemPath(record.work_item_id!)} data-testid="ctem-case-waiting-gate-open">
            <Icon name="external-link" size={16} />
            {t_i18n("Open the decision")}
          </RouterLink>
        </Button>
      )}
    </li>
  );
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
  // Set apart from the lines above, so a decision card never reads as one more line (#2477 UX round 1).
  const inline = waiting.filter((record) => !SECTION_GATES.has(record.key));
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
            <WaitingGateLine key={record.key} record={record} recipient={recipient(record)} />
          ))}
        </ul>
        {inline.length > 0 && (
          <div className="flex flex-col gap-4 border-t border-elevation-subtle-soft pt-4" data-testid="ctem-case-waiting-gate-decisions">
            {inline.map((record) => (
              <CaseGateDecision
                key={record.key}
                record={record}
                history={history}
                personName={personName}
                titled
                testId="ctem-case-waiting-gate-decision"
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
