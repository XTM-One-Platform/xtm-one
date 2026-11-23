/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The fleet's pending approvals (#10202, slice D of #10000, mockups 9399aada
 * D1-D6): a section of the Approval tab listing what waits for a person in the
 * fleet, with its age. A row unfolds the decision under it, so a person decides
 * here without leaving the tab.
 *
 * Every open decision of the fleet (#1493, Sam 23 Nov 2026): while the decisions
 * read switch is on, the list is `GET /decisions/waiting?fleet=`, the paused runs
 * AND the needs_human work items of the fleet's home members (a fleet gate with
 * no tool asks through a work item, gate 5 of dev-docs/human-gates.md), the same
 * rows and count as the "Waiting for you" inbox; a row unfolds the shared
 * DecisionCard and answers through the one answer path. A decision addressed to
 * someone else (listed for a manager of the fleet) is read only, and says so.
 * With the switch off, the runs feed narrowed to the fleet (`?fleet=`) and to
 * `awaiting_human` stays, with the RunPauseDecision under each row.
 *
 * Read only until a person decides: opening the tab starts no run and uses no
 * quota. No ninth tab, no drawer.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { DecisionCard } from "@/components/decisions/DecisionCard";
import { RunPauseDecision } from "@/components/decisions/RunPauseDecision";
import { DecisionSourceCell, sourceText, waitingDecisionView } from "@/components/decisions/WaitingForYou";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useAllAssignmentRuns,
  useDismissRun,
  useRespondToHumanInput,
  useSubmitApprovalDecisions,
} from "@/hooks/api/useAssignmentApi";
import { useAnswerDecision, useWaitingDecisions, type WaitingDecision } from "@/hooks/api/useDecisionApi";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { toolDisplayName } from "@/lib/constants";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
import type { AssignmentRunDetail } from "@/types";

/** One read of the fleet's paused runs: enough for a fleet, the table pages them. */
const PENDING_LIMIT = 100;

type PendingKind = "actions" | "question";

/** What a paused run waits on: gated tool calls, or a question. Pure: tests read it. */
export function pendingKind(run: Pick<AssignmentRunDetail, "pending_approvals">): PendingKind {
  return (run.pending_approvals?.length ?? 0) > 0 ? "actions" : "question";
}

/** The tool names a run waits on, in order, each once. */
export function pendingToolNames(run: Pick<AssignmentRunDetail, "pending_approvals">): string[] {
  return [...new Set((run.pending_approvals ?? []).map((proposal) => proposal.tool_name))];
}

/** The actions a run waits on as a person reads them (never an internal tool id, UX r1 B3 of #10208). */
export function pendingActionLabels(
  run: Pick<AssignmentRunDetail, "pending_approvals">,
  t_i18n: (id: string) => string,
): string[] {
  return pendingToolNames(run).map((name) => toolDisplayName(name, t_i18n));
}

/** Fixed widths (UX r1 B1/B2 of #10208): "Waits on" takes what is left, so its truncate applies. */
const AGENT_WIDTH = 176;
const ASSIGNMENT_WIDTH = 208;
const AGE_WIDTH = 136;
const TOGGLE_WIDTH = 112;

/** The runs still waiting for a person: one already decided waits on the worker, not on a person. */
export function stillWaiting(runs: AssignmentRunDetail[] | undefined): AssignmentRunDetail[] {
  return (runs ?? []).filter((run) => run.status === "awaiting_human" && !run.human_input_response && !run.approval_answered);
}

/** The Kind filter of the decisions list, in the inbox's kinds. Catalog keys. @i18n-keys */
const DECISION_KIND_LABELS = {
  tool_approval: "Actions to approve",
  hitl: "Question",
  work_item: "Work item decision",
  flow_ask: "Flow",
} as const satisfies Record<WaitingDecision["kind"], string>;
const DECISION_KINDS = Object.keys(DECISION_KIND_LABELS) as WaitingDecision["kind"][];

/** The heading block both lists share: title, pending summary, what the list is. */
function PendingHeading({ summary, description }: { summary: string | null; description: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 id="fleet-pending-approvals-title" className="content-base font-semibold text-default-primary">
          {t_i18n("Waiting for approval now")}
        </h3>
        {summary && (
          <span className="content-compact text-default-secondary" data-testid="fleet-pending-summary">
            {summary}
          </span>
        )}
      </div>
      <p className="content-compact text-default-secondary">{description}</p>
    </div>
  );
}

/** One decision unfolded under its row: the shared card, answered in place (#1493). */
function FleetDecisionPanel({ row, onAnswered }: { row: WaitingDecision; onAnswered: () => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const answer = useAnswerDecision();
  // The answer resumes an agent: with AI off the card is read only and says why (FR-018).
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();
  const mine = row.can_answer !== false;
  const send = async ({ option, text }: { option?: string; text?: string }) => {
    if (aiOff || !mine || (!option && !text)) return;
    try {
      await answer.mutateAsync({ id: row.id, option, text });
      toast({ title: t_i18n("Answer sent"), description: t_i18n("The agent has your answer and goes on."), variant: "success" });
      onAnswered();
    } catch (e) {
      console.error("decision answer failed", e);
      toast({
        title: t_i18n("Failed to send the answer"),
        description: t_i18n("Try again, or reload the item if it changed."),
        variant: "error",
      });
    }
  };
  return (
    <div data-testid="fleet-pending-decision" className="space-y-3">
      <Button variant="outline" size="sm" className="h-8 content-compact" asChild>
        <Link to={row.link} data-testid="fleet-pending-open-source">
          {t_i18n("Open the source")}
        </Link>
      </Button>
      <DecisionCard
        view={waitingDecisionView(row)}
        canAnswer={mine && !aiOff}
        readOnlyNote={!mine ? t_i18n("Read only: this decision is addressed to someone else.") : (aiReason ?? undefined)}
        pending={answer.isPending}
        onAnswer={(a) => void send(a)}
        testId="fleet-pending-card"
      />
    </div>
  );
}

/** Every open decision of the fleet, from the decisions table (#1493). */
function FleetPendingDecisions({ rows, isLoading, isError, onRetry }: {
  rows: WaitingDecision[];
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const [open, setOpen] = useState<string | null>(null);
  const toggle = (id: string) => setOpen((current) => (current === id ? null : id));
  const oldest = rows.reduce<string | null>(
    (min, row) => (row.asked_at && (min === null || row.asked_at < min) ? row.asked_at : min),
    null,
  );
  const summary =
    rows.length > 0
      ? [
          t_i18n("{count, plural, one {# decision waits for a person} other {# decisions wait for a person}}", { count: rows.length }),
          ...(oldest ? [t_i18n("oldest asked {age}", { age: formatTimeAgo(intl, oldest) })] : []),
        ].join(" · ")
      : null;

  const columns: DataTableColumn<WaitingDecision>[] = [
    {
      key: "agent",
      header: "Agent",
      width: AGENT_WIDTH,
      cell: (row) => (
        <Tip content={row.agent ?? ""}>
          <span className="block min-w-0 truncate whitespace-nowrap font-medium text-default-primary" data-testid="fleet-pending-agent" data-truncate="one-line">
            {row.agent ?? ""}
          </span>
        </Tip>
      ),
      sortValue: (row) => row.agent ?? "",
      csv: (row) => row.agent ?? "",
    },
    {
      key: "decide",
      header: "What to decide",
      cell: (row) => (
        <span className="flex min-w-0 items-center gap-2">
          <Tip content={row.question}>
            <span className="block min-w-0 truncate whitespace-nowrap" data-testid="fleet-pending-waits-on" data-truncate="one-line">
              {row.question}
            </span>
          </Tip>
          {row.can_answer === false && (
            <span className="shrink-0 content-compact text-default-secondary" data-testid="fleet-pending-read-only">
              {t_i18n("Read only")}
            </span>
          )}
        </span>
      ),
      csv: (row) => row.question,
    },
    {
      key: "source",
      header: "Source",
      // Steps aside below 4xl (896 px) so "What to decide" keeps the room at 1024 (UX r2 B3 of #10208).
      hideBelow: "4xl",
      hideable: true,
      width: ASSIGNMENT_WIDTH,
      // The inbox From cell: kind icon and DS tooltip on cut text (UX r1 B1 of #1497).
      cell: (row) => <DecisionSourceCell row={row} />,
      sortValue: (row) => sourceText(row, t_i18n),
      csv: (row) => sourceText(row, t_i18n),
    },
    {
      key: "age",
      header: "Waiting since",
      align: "right",
      width: AGE_WIDTH,
      firstSort: "asc",
      cell: (row) =>
        row.asked_at ? (
          <Tip content={formatShortDateTimeIntl(intl, row.asked_at)}>
            <span className="whitespace-nowrap text-default-secondary">{formatTimeAgo(intl, row.asked_at)}</span>
          </Tip>
        ) : null,
      sortValue: (row) => row.asked_at ?? "",
      csv: (row) => row.asked_at ?? "",
    },
  ];

  return (
    <section className="space-y-3 pt-2" aria-labelledby="fleet-pending-approvals-title" data-testid="fleet-pending-approvals">
      <PendingHeading
        summary={summary}
        description={t_i18n(
          "Decisions of this fleet's members waiting for a person: paused runs and work items. Opening this tab starts no run and uses no quota.",
        )}
      />
      {!isLoading && !isError && rows.length === 0 ? (
        <EmptyState
          variant="inset"
          icon={CheckCircle2}
          title={t_i18n("Nothing waits for approval")}
          description={t_i18n("When a member's action or question waits for a person, it appears here.")}
        />
      ) : (
        <DataTable
          rows={rows}
          columns={columns}
          rowKey={(row) => row.id}
          defaultSort={{ key: "age", direction: "asc" }}
          searchFields={(row) => [row.agent, row.question, sourceText(row, t_i18n)]}
          filters={[
            { key: "agent", label: "Agent", values: (row) => (row.agent ? [row.agent] : []) },
            {
              key: "kind",
              label: "Kind",
              values: (row) => [row.kind],
              options: DECISION_KINDS.map((kind) => ({ value: kind, label: t_i18n(DECISION_KIND_LABELS[kind]) })),
            },
          ]}
          isLoading={isLoading}
          isError={isError}
          onRetry={onRetry}
          emptyLabel={t_i18n("Nothing waits for approval")}
          noResultsLabel={t_i18n("No waiting decision matches the search and filters.")}
          defaultPageSize={10}
          layout="fixed"
          rowActionsWidth={TOGGLE_WIDTH}
          toggleInCell
          rowActions={(row) => (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 gap-1 content-compact"
              aria-expanded={open === row.id}
              onClick={(e) => {
                e.stopPropagation();
                toggle(row.id);
              }}
              data-testid="fleet-pending-toggle"
            >
              <ChevronRight className={cn("h-3 w-3 transition-transform", open === row.id && "rotate-90")} aria-hidden />
              {open === row.id ? t_i18n("Hide") : t_i18n("Review")}
            </Button>
          )}
          exportName="fleet-pending-approvals"
          onRowClick={(row) => toggle(row.id)}
          expandedRow={(row) =>
            open === row.id ? (
              <FleetDecisionPanel row={row} onAnswered={() => setOpen((current) => (current === row.id ? null : current))} />
            ) : null
          }
          rowTestId="fleet-pending-row"
        />
      )}
    </section>
  );
}

export function FleetPendingApprovals({ fleetId }: { fleetId: string }) {
  const decisions = useWaitingDecisions(true, fleetId);
  // The read switch off (or not read yet): the per-engine runs list stays the fleet's (#1493).
  if (decisions.data?.enabled) {
    return (
      <FleetPendingDecisions
        rows={decisions.data.items}
        isLoading={false}
        isError={decisions.isError}
        onRetry={() => void decisions.refetch()}
      />
    );
  }
  if (decisions.isLoading) {
    return <FleetPendingDecisions rows={[]} isLoading isError={false} onRetry={() => void decisions.refetch()} />;
  }
  return <FleetPendingRuns fleetId={fleetId} />;
}

/** The fleet's paused runs from the runs feed: the list while the decisions read switch is off. */
function FleetPendingRuns({ fleetId }: { fleetId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const [open, setOpen] = useState<string | null>(null);
  const { data, isLoading, isError, refetch } = useAllAssignmentRuns(
    PENDING_LIMIT, 0, "awaiting_human", undefined, undefined, undefined, undefined, undefined, undefined,
    true, undefined, undefined, undefined, fleetId,
  );
  const respond = useRespondToHumanInput();
  const submitApprovals = useSubmitApprovalDecisions();
  const dismiss = useDismissRun();
  const rows = stillWaiting(data);
  const oldest = rows.reduce<string | null>((min, run) => (min === null || run.created_at < min ? run.created_at : min), null);
  const toggle = (id: string) => setOpen((current) => (current === id ? null : id));

  const kindLabel = (kind: PendingKind) => (kind === "actions" ? t_i18n("Actions to approve") : t_i18n("Question"));

  const columns: DataTableColumn<AssignmentRunDetail>[] = [
    {
      key: "agent",
      header: "Agent",
      width: AGENT_WIDTH,
      cell: (run) => (
        <Tip content={run.agent_name}>
          <span
            className="block min-w-0 truncate whitespace-nowrap font-medium text-default-primary"
            data-testid="fleet-pending-agent"
            data-truncate="one-line"
          >
            {run.agent_name}
          </span>
        </Tip>
      ),
      sortValue: (run) => run.agent_name,
      csv: (run) => run.agent_name,
    },
    {
      key: "waits_on",
      header: "Waits on",
      cell: (run) => {
        const actions = pendingActionLabels(run, t_i18n);
        const label =
          pendingKind(run) === "actions"
            ? t_i18n("{count, plural, one {# action: {names}} other {# actions: {names}}}", {
                count: run.pending_approvals?.length ?? 0,
                names: actions.join(", "),
              })
            : run.human_input_question ?? t_i18n("Question");
        return (
          <Tip content={label}>
            <span className="block min-w-0 truncate whitespace-nowrap" data-testid="fleet-pending-waits-on" data-truncate="one-line">
              {label}
            </span>
          </Tip>
        );
      },
      csv: (run) => (pendingKind(run) === "actions" ? pendingActionLabels(run, t_i18n).join(", ") : run.human_input_question ?? ""),
    },
    {
      key: "assignment",
      header: "Assignment",
      // Steps aside below 4xl (896 px) so "Waits on", what to decide, keeps the room at 1024 (UX r2 B3).
      hideBelow: "4xl",
      hideable: true,
      width: ASSIGNMENT_WIDTH,
      // Plain text: the run page opens from the row's decision panel, no cell draws a link (#12070).
      cell: (run) => (
        <span className="block min-w-0 truncate whitespace-nowrap" title={run.assignment_name ?? undefined}>
          {run.assignment_name ?? t_i18n("Run")}
        </span>
      ),
      sortValue: (run) => run.assignment_name ?? "",
      csv: (run) => run.assignment_name ?? "",
    },
    {
      key: "age",
      header: "Waiting since",
      align: "right",
      width: AGE_WIDTH,
      firstSort: "asc",
      cell: (run) => (
        <Tip content={formatShortDateTimeIntl(intl, run.created_at)}>
          <span className="whitespace-nowrap text-default-secondary">{formatTimeAgo(intl, run.created_at)}</span>
        </Tip>
      ),
      sortValue: (run) => run.created_at,
      csv: (run) => run.created_at,
    },
  ];

  return (
    <section className="space-y-3 pt-2" aria-labelledby="fleet-pending-approvals-title" data-testid="fleet-pending-approvals">
      {/* The strip's heading style, a pending summary and what the list is (UX r1 B5 of #10208, mockup D1). */}
      <PendingHeading
        summary={
          rows.length > 0 && oldest
            ? `${t_i18n("{count, plural, one {# run waits for a person} other {# runs wait for a person}}", { count: rows.length })} · ${t_i18n("oldest started {age}", { age: formatTimeAgo(intl, oldest) })}`
            : null
        }
        description={t_i18n("Runs of this fleet's members paused until a person decides. Opening this tab starts no run and uses no quota.")}
      />
      {!isLoading && !isError && rows.length === 0 ? (
        <EmptyState
          variant="inset"
          icon={CheckCircle2}
          title={t_i18n("Nothing waits for approval")}
          description={t_i18n("When a member's action or question waits for a person, it appears here.")}
        />
      ) : (
        <DataTable
          rows={rows}
          columns={columns}
          rowKey={(run) => run.id}
          defaultSort={{ key: "age", direction: "asc" }}
          searchFields={(run) => [run.agent_name, run.assignment_name, run.human_input_question, ...pendingToolNames(run)]}
          filters={[
            { key: "agent", label: "Agent", values: (run) => [run.agent_name] },
            {
              key: "kind",
              label: "Waits on",
              values: (run) => [pendingKind(run)],
              options: [
                { value: "actions", label: kindLabel("actions") },
                { value: "question", label: kindLabel("question") },
              ],
            },
          ]}
          isLoading={isLoading}
          isError={isError}
          onRetry={() => void refetch()}
          emptyLabel={t_i18n("Nothing waits for approval")}
          noResultsLabel={t_i18n("No waiting run matches the search and filters.")}
          defaultPageSize={10}
          layout="fixed"
          rowActionsWidth={TOGGLE_WIDTH}
          toggleInCell
          rowActions={(run) => (
            /* The row's own disclosure, trailing (UX r1 B6 of #10208, ToolingSection chevron pattern). */
            <Button
              variant="ghost"
              size="sm"
              className="h-8 gap-1 content-compact"
              aria-expanded={open === run.id}
              onClick={(e) => {
                e.stopPropagation();
                toggle(run.id);
              }}
              data-testid="fleet-pending-toggle"
            >
              <ChevronRight className={cn("h-3 w-3 transition-transform", open === run.id && "rotate-90")} aria-hidden />
              {open === run.id ? t_i18n("Hide") : t_i18n("Review")}
            </Button>
          )}
          exportName="fleet-pending-approvals"
          onRowClick={(run) => toggle(run.id)}
          expandedRow={(run) =>
            open === run.id ? (
              <div data-testid="fleet-pending-decision" className="space-y-3">
                <Button variant="outline" size="sm" className="h-8 content-compact" asChild>
                  <Link to={`/operations/executions/${run.id}`} data-testid="fleet-pending-open-run">
                    {t_i18n("Open run")}
                  </Link>
                </Button>
                <RunPauseDecision
                  run={run}
                  showResolved={false}
                  approveFailed={submitApprovals.isError}
                  onApprove={(runId, decisions) =>
                    submitApprovals.mutate(
                      { runId, decisions },
                      {
                        onSuccess: () => toast({ title: t_i18n("Decision recorded: assignment resuming"), variant: "success" }),
                        onError: (err) => toast({ title: t_i18n("Could not submit decisions"), description: err.message, variant: "error" }),
                      },
                    )
                  }
                  onRespond={(runId, response) =>
                    respond.mutate(
                      { runId, response },
                      { onSuccess: () => toast({ title: t_i18n("Response sent: assignment resuming"), variant: "success" }) },
                    )
                  }
                  onDismiss={(runId) =>
                    dismiss.mutate(runId, { onSuccess: () => toast({ title: t_i18n("Run dismissed"), variant: "success" }) })
                  }
                />
              </div>
            ) : null
          }
          rowTestId="fleet-pending-row"
        />
      )}
    </section>
  );
}
