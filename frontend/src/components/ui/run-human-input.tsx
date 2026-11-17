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
 * The human-in-the-loop part of a run in a list: the per-tool approval prompt,
 * the free-text question with its reply form, and the latest resolved round.
 *
 * Shared by the RunCard (dashboard, assignment pages) and the Executions rows
 * of Operations, where it unfolds under the row so the row keeps its height
 * (components/ui/operations-list.tsx, the Operations list contract).
 */
import { useState } from "react";
import { Check, CheckCircle2, Info, Mic, MicOff, Send, User, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { ActionProposalList } from "@/components/ui/action-proposal";
import { Input } from "@/components/ui/input";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { useSpeechToText } from "@/hooks/useSpeechToText";
import { useFormatter } from "@/i18n";
import { HITL_CONTINUATION_MESSAGE } from "@/lib/constants";
import { approvalCodesIn, approvalReply } from "@/lib/hitl-approval";
import { cn, formatDateTime } from "@/lib/utils";
import { WARNING_ON_HOVER_TEXT } from "@/lib/warning-text";
import type { ActionProposal, ApprovalDecision, HumanInputHistoryEntry } from "@/types";

/** The fields of a run the human-in-the-loop part reads. */
export interface RunHumanInputRun {
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
export function runHitlState(run: RunHumanInputRun) {
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
  // Latest resolved HITL round: from the durable history, with a legacy
  // fallback for runs resolved before the history column existed (their live
  // fields survived because they never went through the resume-clear path).
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

export function RunHumanInput({
  run,
  onRespond,
  onDismiss,
  onApprove,
  approveFailed,
  showResolved = true,
}: {
  run: RunHumanInputRun;
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
  const [hitlResponse, setHitlResponse] = useState("");
  const speech = useSpeechToText({
    onTranscript: (text) => setHitlResponse((prev) => (prev ? prev + " " : "") + text.trim()),
  });
  const { isHitlPending, pendingApprovals, isApprovalPending, isFreeTextPending, latestHitl } = runHitlState(run);
  const approvalCodes = isHitlPending ? approvalCodesIn(run.human_input_question) : [];
  // Company-managed runs: all users see the HITL question, only admins can respond/dismiss.
  const canInteractHitl = !run.is_company_managed || isAdmin;

  const handleSubmitResponse = (e: React.MouseEvent | React.FormEvent) => {
    e.stopPropagation();
    e.preventDefault();
    if (hitlResponse.trim() && onRespond) {
      onRespond(run.id, hitlResponse.trim());
      setHitlResponse("");
    }
  };

  return (
    <>
      {/* Per-tool approval: structured proposals. Takes precedence over
          the free-text block below: a gated pause has no question to answer,
          it has actions to decide. */}
      {isApprovalPending && (
        <div className="mt-2 rounded-sm bg-feedback-warning-primary/5 p-3" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center gap-2 mb-2">
            {run.is_company_managed && <CompanyManagedBadge size="sm" />}
          </div>
          {canInteractHitl && onApprove ? (
            <ActionProposalList
              proposals={pendingApprovals}
              submitFailed={approveFailed}
              onSubmit={(decisions) => onApprove(run.id, decisions)}
            />
          ) : (
            <p className={cn("content-compact", WARNING_ON_HOVER_TEXT)}>
              {t_i18n("Waiting for approval from someone with access to this run.")}
            </p>
          )}
        </div>
      )}

      {/* HITL: question + response input */}
      {isFreeTextPending && run.human_input_question && (
        <div className="mt-2 rounded-sm bg-feedback-warning-primary/5 p-3 space-y-2" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center gap-2">
            <p className={cn("content-compact font-medium", WARNING_ON_HOVER_TEXT)}>{t_i18n("Agent needs your input:")}</p>
            {run.is_company_managed && <CompanyManagedBadge size="sm" />}
          </div>
          <p className="content-compact text-default-secondary whitespace-pre-wrap">{run.human_input_question}</p>
          {canInteractHitl && (onRespond || onDismiss) && (
            <form onSubmit={handleSubmitResponse} className="flex gap-2 mt-2">
              <div className="flex-1">
                <Input aria-label={t_i18n("Type your response...")}
                  type="text"
                  className="w-full"
                  placeholder={t_i18n("Type your response...")}
                  value={hitlResponse}
                  onChange={(e) => setHitlResponse(e.target.value)}
                  endIcon={speech.isSupported ? {
                    type: "iconButton",
                    icon: speech.isListening ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />,
                    label: speech.isListening ? t_i18n("Stop dictation") : t_i18n("Dictate response"),
                    onClick: (e) => { e.stopPropagation(); speech.toggle(); },
                  } : undefined}
                />
              </div>
              {onRespond && approvalCodes.length > 0 && (
                <Tip content={t_i18n("Replies {reply} and resumes the run", { reply: approvalReply(approvalCodes) })}>
                  <button
                    type="button"
                    className="h-9 px-3 rounded-sm content-compact font-medium flex items-center gap-1.5 transition-colors bg-feedback-success-primary/15 text-feedback-success-primary hover:bg-feedback-success-primary/25"
                    onClick={(e) => {
                      e.stopPropagation();
                      e.preventDefault();
                      speech.stop();
                      onRespond(run.id, approvalReply(approvalCodes));
                      setHitlResponse("");
                    }}
                  >
                    <Check className="h-3 w-3" />
                    {approvalCodes.length === 1
                      ? t_i18n("Approve {code}", { code: approvalCodes[0] })
                      : t_i18n("Approve all ({count})", { count: approvalCodes.length })}
                  </button>
                </Tip>
              )}
              {onRespond && (
                <button
                  type="submit"
                  disabled={!hitlResponse.trim()}
                  className={cn(
                    "h-9 px-3 rounded-sm content-compact font-medium flex items-center gap-1.5 transition-colors",
                    hitlResponse.trim()
                      ? "bg-feedback-warning-primary/20 text-feedback-warning-tertiary hover:bg-feedback-warning-primary/30"
                      : "bg-elevation-hover text-default-secondary cursor-not-allowed",
                  )}
                  onClick={() => speech.stop()}
                >
                  <Send className="h-3 w-3" />
                  {t_i18n("Reply")}
                </button>
              )}
              {onDismiss && (
                <Tip content={t_i18n("Discard this run")}>
                  <button
                    type="button"
                    className="h-9 px-3 rounded-sm content-compact font-medium flex items-center gap-1.5 transition-colors bg-elevation-hover text-default-secondary hover:bg-feedback-error-primary/10 hover:text-feedback-error-primary"
                    onClick={(e) => { e.stopPropagation(); e.preventDefault(); speech.stop(); onDismiss(run.id); }}
                  >
                    <X className="h-3 w-3" />
                    {t_i18n("Discard")}
                  </button>
                </Tip>
              )}
            </form>
          )}
          {!canInteractHitl && (
            <p className="content-caption text-default-secondary mt-1">{t_i18n("Only admins can respond to company-managed agent requests.")}</p>
          )}
        </div>
      )}

      {/* Resolved HITL round: answered/dismissed state, the user's reply, and
          the continuation system notice, all driven by persisted data so the
          state survives panel re-opens. Also shown while a LATER round is
          pending so multi-round context is kept. */}
      {showResolved && latestHitl && (
        <div
          className={cn(
            "mt-2 rounded-sm p-3 space-y-2",
            latestHitl.resolution === "answered"
              ? "bg-feedback-success-primary/[0.04] border border-feedback-success-primary/10"
              : "bg-elevation-hover border border-elevation-subtle-soft",
          )}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center gap-2">
            {latestHitl.resolution === "answered" ? (
              <Badge variant="success" className="content-caption gap-1">
                <CheckCircle2 className="h-3 w-3" />
                {t_i18n("Answered")}
              </Badge>
            ) : (
              <Badge variant="outline" className="content-caption gap-1 text-default-secondary">
                <X className="h-3 w-3" />
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
              <div className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent/10 mt-0.5">
                <User className="h-3 w-3 text-accent" />
              </div>
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
              never while a later HITL round is waiting for input again. */}
          {latestHitl.resolution === "answered" && !isHitlPending && (
            <div className="flex items-start gap-2 rounded-sm bg-elevation-hover p-2">
              <Info className="h-3 w-3 text-accent shrink-0 mt-0.5" />
              <p className="content-caption text-default-secondary">{t_i18n(HITL_CONTINUATION_MESSAGE)}</p>
            </div>
          )}
        </div>
      )}
    </>
  );
}
