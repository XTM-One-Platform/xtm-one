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
 * D1-D6): a section of the Approval tab listing every run of the fleet's home
 * members that waits for a person, the gated tool calls or the question, with
 * its age. A row unfolds the decision path under it (the shared
 * RunHumanInput: the approval prompt or the reply form), so a person decides
 * here without leaving the tab.
 *
 * Read only until a person decides: the list is the runs feed narrowed to the
 * fleet (`?fleet=`) and to `awaiting_human`; opening the tab starts no run and
 * uses no quota. No ninth tab, no drawer.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { RunHumanInput } from "@/components/ui/run-human-input";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useAllAssignmentRuns,
  useDismissRun,
  useRespondToHumanInput,
  useSubmitApprovalDecisions,
} from "@/hooks/api/useAssignmentApi";
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

export function FleetPendingApprovals({ fleetId }: { fleetId: string }) {
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
      <div className="space-y-1">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h3 id="fleet-pending-approvals-title" className="content-base font-semibold text-default-primary">
            {t_i18n("Waiting for approval now")}
          </h3>
          {rows.length > 0 && oldest && (
            <span className="content-compact text-default-secondary" data-testid="fleet-pending-summary">
              {t_i18n("{count, plural, one {# run waits for a person} other {# runs wait for a person}}", { count: rows.length })}
              {" · "}
              {t_i18n("oldest started {age}", { age: formatTimeAgo(intl, oldest) })}
            </span>
          )}
        </div>
        <p className="content-compact text-default-secondary">
          {t_i18n("Runs of this fleet's members paused until a person decides. Opening this tab starts no run and uses no quota.")}
        </p>
      </div>
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
                <RunHumanInput
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
