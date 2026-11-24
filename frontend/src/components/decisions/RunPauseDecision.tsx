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
 * A paused run in a list (#842, S8; dev-docs/human-gates.md section 3): the
 * pause on the one `RunDecisionCard` (a tool approval or the run's question)
 * and the latest resolved round. Replaces `components/ui/run-human-input.tsx`.
 *
 * Shared by the RunCard (dashboard, assignment pages), the Executions rows of
 * Operations (it unfolds under the row so the row keeps its height), the runs
 * activity list and the fleet's pending approvals.
 */
import { Icon } from "@filigran/design-system";

import { RunDecisionCard } from "@/components/decisions/RunDecisionCard";
import { Badge } from "@/components/ui/badge";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { HITL_CONTINUATION_MESSAGE } from "@/lib/constants";
import { cn, formatDateTime } from "@/lib/utils";
import type { ActionProposal, ApprovalDecision, HumanInputHistoryEntry } from "@/types";

/** The fields of a run its pause reads. */
export interface RunPauseRun {
  id: string;
  status: string;
  is_company_managed?: boolean;
  human_input_question?: string | null;
  human_input_response?: string | null;
  human_input_history?: HumanInputHistoryEntry[] | null;
  pending_approvals?: ActionProposal[] | null;
  approval_answered?: boolean | null;
}

/** Where a run stands with the person it waits for. Pure: tests read it. */
export function runPauseState(run: RunPauseRun) {
  // Pending only while nobody has responded yet: the respond endpoint stores
  // the response immediately, so the block flips to the answered state even
  // before the engine picks the run back up.
  const isHitlPending = run.status === "awaiting_human" && !run.human_input_response;
  // A gated tool call pauses with structured proposals; request_human_input
  // pauses with a free-text question. Both land in awaiting_human, so the
  // presence of proposals is what decides which prompt to render.
  const pendingApprovals = run.pending_approvals ?? [];
  // ``approval_answered`` and not the fields, because the fields outlive the
  // decision: ``resume_assignment_run`` emits an outbox event and commits
  // without changing the status, so the run stays ``awaiting_human`` with its
  // proposals until a worker drains the queue. Reading only those offered the
  // same run again after a reload, and a second decision would overwrite the
  // one already recorded and enqueue a second resume (T225).
  const isApprovalPending = isHitlPending && pendingApprovals.length > 0 && !run.approval_answered;
  /* The free-text form is for a run that asked a QUESTION, never for one
     awaiting structured decisions: `/respond` refuses free text while
     proposals are outstanding. Keyed on the proposals rather than on
     `isApprovalPending`: that goes false the moment `approval_answered` is
     recorded, while the proposals survive until the worker resumes, so the
     window between the two offered a form the API would reject. */
  const isFreeTextPending = isHitlPending && pendingApprovals.length === 0;
  const hitlHistory = run.human_input_history ?? [];
  // Latest resolved round: from the durable history, with a legacy fallback
  // for runs resolved before the history column existed (their live fields
  // survived because they never went through the resume-clear path).
  const latestHitl: HumanInputHistoryEntry | null =
    hitlHistory.length > 0
      ? hitlHistory[hitlHistory.length - 1]
      : run.human_input_question && run.human_input_response && !isHitlPending
        ? {
            question: run.human_input_question,
            response: run.human_input_response === "[Dismissed by user]" ? null : run.human_input_response,
            resolution: run.human_input_response === "[Dismissed by user]" ? "dismissed" : "answered",
            responded_at: "",
            responded_by: null,
            responded_by_id: null,
          }
        : null;
  return { isHitlPending, pendingApprovals, isApprovalPending, isFreeTextPending, latestHitl };
}

export function RunPauseDecision({
  run,
  onRespond,
  onDismiss,
  onApprove,
  approveFailed,
  showResolved = true,
}: {
  run: RunPauseRun;
  onRespond?: (runId: string, response: string) => void;
  onDismiss?: (runId: string) => void;
  onApprove?: (runId: string, decisions: ApprovalDecision[]) => void;
  approveFailed?: boolean;
  /** Also show the latest resolved round (the card does; a list row shows
   *  only what still waits for someone, the run page holds the rest). */
  showResolved?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { isHitlPending, pendingApprovals, isApprovalPending, isFreeTextPending, latestHitl } = runPauseState(run);
  // Company-managed runs: all users see the question, only admins can respond/dismiss.
  const canAnswer = !run.is_company_managed || isAdmin;

  return (
    <>
      {/* The pause on the one decision card: a tool approval when proposals
          are pending (they take precedence: a gated pause has actions to
          decide, not a question), else the run's question. */}
      {(isApprovalPending || (isFreeTextPending && run.human_input_question)) && (
        <div className="mt-2 space-y-1" onClick={(e) => e.stopPropagation()}>
          {run.is_company_managed && <CompanyManagedBadge size="sm" />}
          <RunDecisionCard
            // A failed approval remounts the card so its controls answer again.
            key={`${run.id}-${approveFailed ? "failed" : "ok"}`}
            runId={run.id}
            question={run.human_input_question}
            proposals={isApprovalPending ? pendingApprovals : []}
            canAnswer={canAnswer}
            onApprove={onApprove}
            onRespond={onRespond}
            onDismiss={onDismiss}
          />
        </div>
      )}

      {/* Resolved round: answered/dismissed state, the reply, and the
          continuation notice, all driven by persisted data so the state
          survives panel re-opens. Also shown while a LATER round is pending
          so multi-round context is kept. */}
      {showResolved && latestHitl && (
        <div
          className={cn(
            "mt-2 rounded-sm p-3 space-y-2",
            latestHitl.resolution === "answered"
              ? "bg-feedback-success-primary/[0.04] border border-feedback-success-primary/10"
              : "bg-elevation-hover border border-elevation-subtle-soft",
          )}
          onClick={(e) => e.stopPropagation()}
          data-testid="run-pause-resolved"
        >
          <div className="flex items-center gap-2">
            {latestHitl.resolution === "answered" ? (
              <Badge variant="success" className="content-caption gap-1">
                <Icon name="circle-check" size={16} aria-hidden />
                {t_i18n("Answered")}
              </Badge>
            ) : (
              <Badge variant="outline" className="content-caption gap-1 text-default-secondary">
                <Icon name="x" size={16} aria-hidden />
                {t_i18n("Dismissed")}
              </Badge>
            )}
            {latestHitl.question && (
              <span className="content-caption text-default-secondary truncate max-w-[320px]">
                {latestHitl.question.length > 90 ? `${latestHitl.question.slice(0, 90)}…` : latestHitl.question}
              </span>
            )}
          </div>
          {latestHitl.resolution === "answered" && (
            <div className="flex items-start gap-2">
              <Icon name="message-square" size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden />
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="content-caption font-medium text-default-secondary">{latestHitl.responded_by ?? t_i18n("You")}</span>
                  {latestHitl.responded_at && (
                    <span className="content-caption text-default-secondary">{formatDateTime(latestHitl.responded_at)}</span>
                  )}
                </div>
                <p className="content-caption text-default-secondary whitespace-pre-wrap">{latestHitl.response}</p>
              </div>
            </div>
          )}
          {/* Continuation notice only for the CURRENT answered transition,
              never while a later round is waiting for input again. */}
          {latestHitl.resolution === "answered" && !isHitlPending && (
            <div className="flex items-start gap-2 rounded-sm bg-elevation-hover p-2">
              <Icon name="info" size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden />
              <p className="content-caption text-default-secondary">{t_i18n(HITL_CONTINUATION_MESSAGE)}</p>
            </div>
          )}
        </div>
      )}
    </>
  );
}
