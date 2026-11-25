// SPDX-License-Identifier: Apache-2.0

/**
 * One row of the case History opened in a dialog (#1679, slice 3): the row's
 * action as the title, its date under it, a strip of chips (who acted, the
 * phase, the autonomy class), the facts (what happened, the change, the issue,
 * the evidence) and the full body: a note's whole text, a gate's decision
 * record, a fleet run's link. The Table and the Timeline of the History open
 * the same dialog, so a row reads the same from both. The words come from the
 * page (`renderEvent`, `actionText`, `changeText`), never a raw key.
 */
import type * as React from "react";
import { Link as RouterLink } from "react-router-dom";
import type { IconName } from "@filigran/design-system";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { CtemCaseEvent, CtemCaseGateRecord, CtemCaseRun } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { AUDIT_PHASE_LABEL, type AuditRow } from "@/lib/ctem-case-audit";
import { CASE_FACT_ICON, CaseAutonomyChip, CaseFactChip, CaseIcon, actorIcon, caseEventIcon } from "./case-icons";
import { GateDecisionRecord, gateAnswererName, gateRecordOf } from "./GateDecisionRecord";
import { eventIssueLabel } from "./case-issue-label";

interface CaseEventDialogProps {
  /** The row shown; null closes the dialog. */
  row: AuditRow | null;
  onClose: () => void;
  renderEvent: (event: CtemCaseEvent) => React.ReactNode;
  renderRun: (run: CtemCaseRun) => React.ReactNode;
  actorName: (row: AuditRow) => string;
  actionText: (row: AuditRow) => string;
  changeText: (event: CtemCaseEvent) => { from: string; to: string } | null;
  personName: (id: string | null | undefined) => string | null;
  /** The case's resolved gate records: a gate row shows the record of its key. */
  gates?: readonly CtemCaseGateRecord[];
  history?: readonly CtemCaseEvent[];
  /** Where the run page goes back to. */
  backTo: string;
}

/** One fact of the dialog: its icon and label, then its value. */
function Fact({ icon, label, testId, children }: { icon: IconName; label: string; testId: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1" data-testid={testId}>
      <dt className="flex items-center gap-1.5 content-compact text-default-secondary">
        <CaseIcon name={icon} />
        {label}
      </dt>
      <dd className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1 break-words">{children}</dd>
    </div>
  );
}

export function CaseEventDialog({
  row,
  onClose,
  renderEvent,
  renderRun,
  actorName,
  actionText,
  changeText,
  personName,
  gates,
  history,
  backTo,
}: CaseEventDialogProps) {
  const { t_i18n, intl } = useFormatter();
  const when = (at: string | null) => (at ? intl.formatDate(at, { dateStyle: "medium", timeStyle: "short" }) : null);
  const event = row?.kind === "event" ? row.event : null;
  const run = row?.kind === "run" ? row.run : null;
  const change = event && event.kind !== "gate" ? changeText(event) : null;
  const record = event?.kind === "gate" && event.from ? gateRecordOf(gates, event.from) : null;
  const note = event?.kind === "note" ? (event.reason ?? "").trim() || (event.headline ?? "").trim() : "";
  // The member in words, never its id (#1936): a signal by its name, an issue by its CVE.
  const issue = event ? eventIssueLabel(event, t_i18n) : null;
  // An applied change's evidence is its `{ids, rollback}` JSON, drawn in words on the
  // Mobilization card: never printed raw here (UX round 1 on #2076, B2).
  const evidence =
    event && event.kind !== "gate" && event.kind !== "ticket" && event.kind !== "retest_requested" && event.kind !== "applied_change"
      ? event.evidence
      : null;
  // Each thing said once (UX round 1 on #1708, B1): a gate row's record says what happened
  // and a note row's text sits under Note alone, so neither repeats it as a fact.
  const isNote = event?.kind === "note";
  const showWhat = !record && !isNote;
  const facts = [showWhat, !!change, !!issue, !!evidence].filter(Boolean).length;

  // Mounted only while a row is open: the content always holds its title (an accessible name).
  if (!row) return null;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent size="md" data-testid="ctem-case-event-dialog">
        {
          <>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <CaseIcon name={caseEventIcon(row.kind === "run" ? "run" : row.event.kind)} className="text-default-secondary" />
                <span className="min-w-0">{isNote ? t_i18n("Added a note") : actionText(row)}</span>
              </DialogTitle>
              <DialogDescription>{when(row.at) ?? t_i18n("Not dated")}</DialogDescription>
            </DialogHeader>
            <DialogBody className="space-y-4 content-base">
              <div className="flex flex-wrap items-center gap-1.5" data-testid="ctem-case-event-chips">
                <CaseFactChip icon={actorIcon(row.actorType)} tone={row.actorType === "agent" ? "blue" : "neutral"} testId="ctem-case-event-actor">
                  {actorName(row)}
                </CaseFactChip>
                <CaseFactChip icon="waypoints" testId="ctem-case-event-phase">
                  {t_i18n(AUDIT_PHASE_LABEL[row.phase])}
                </CaseFactChip>
                <CaseAutonomyChip autonomy={row.autonomy} testId="ctem-case-event-autonomy" />
              </div>

              {/* Two columns only for two facts or more: one fact alone spans the body (B2). */}
              {facts > 0 && (
                <dl className={facts > 1 ? "grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2" : "grid grid-cols-1 gap-y-3"}>
                  {showWhat && (
                    <Fact icon={CASE_FACT_ICON.meaning} label={t_i18n("What happened")} testId="ctem-case-event-what">
                      {run ? renderRun(run) : event ? renderEvent(event) : null}
                    </Fact>
                  )}
                  {change && (
                    <Fact icon={CASE_FACT_ICON.state} label={t_i18n("Change (from → to)")} testId="ctem-case-event-change">
                      <span>{`${change.from} → ${change.to}`}</span>
                    </Fact>
                  )}
                  {issue && (
                    <Fact icon="list-checks" label={t_i18n("Case issue")} testId="ctem-case-event-issue">
                      <span>{issue}</span>
                    </Fact>
                  )}
                  {evidence && (
                    <Fact icon="paperclip" label={t_i18n("Evidence")} testId="ctem-case-event-evidence">
                      <span className="break-all">{evidence}</span>
                    </Fact>
                  )}
                </dl>
              )}

              {note && (
                <section className="flex flex-col gap-1" aria-label={t_i18n("Note")}>
                  <h4 className="flex items-center gap-1.5 content-compact text-default-secondary">
                    <CaseIcon name={caseEventIcon("note")} />
                    {t_i18n("Note")}
                  </h4>
                  <p className="whitespace-pre-wrap" data-testid="ctem-case-event-note">
                    {note}
                  </p>
                </section>
              )}

              {record && (
                <GateDecisionRecord
                  record={record}
                  byLabel={record.answered_by?.name ? null : gateAnswererName(record, history, (id) => personName(id))}
                  testId="ctem-case-event-gate-record"
                />
              )}

              {run && (
                <RouterLink
                  to={`/operations/executions/${run.id}`}
                  state={{ backTo, backLabel: "Back to Case" }}
                  className="inline-flex items-center gap-1.5 font-medium text-accent hover:underline"
                  data-testid="ctem-case-event-run-link"
                >
                  <CaseIcon name="external-link" />
                  {t_i18n("Open the run")}
                </RouterLink>
              )}
            </DialogBody>
          </>
        }
      </DialogContent>
    </Dialog>
  );
}
