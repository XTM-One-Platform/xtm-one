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
 * One row of Operations > Executions on the Operations list contract
 * (components/ui/operations-list.tsx): an assignment run or a flow run, one
 * height on every row (OPS_ROW_HEIGHT), the same columns as every other tab.
 *
 * The row carries the essentials (status, title, the one thing a waiting run
 * needs, the agent, the run id, the time); every fact is in the hover card.
 * A click opens the run (the run page, or the flow builder on the run's
 * trace). A run waiting for a person unfolds its prompt under the row with
 * the Answer action, so the row above keeps its height; the delegation tree
 * and the steps live on the run page.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Bot, GitBranch, Hash, Reply, Trash2, Workflow, Zap } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { HeldBadge } from "@/components/ui/held-badge";
import { SelectionBox } from "@/components/ui/list-control-bar";
import {
  OPS_ROW_CARD, OPS_ROW_HEIGHT, OpsChipStrip, OpsHoverCard, OpsMoreToken, OpsRowAction, OpsRowGrid, OpsToken, OpsUpdated,
} from "@/components/ui/operations-list";
import type { OpsFact } from "@/components/ui/operations-list";
import { RunPauseDecision, runPauseState } from "@/components/decisions/RunPauseDecision";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { STATUS_CONFIG, runStatusConfig } from "@/lib/constants";
import { runHeldReason } from "@/lib/held";
import { runErrorIsNeutral, runErrorText } from "@/lib/run-error-text";
import { formatInZone, viewerTimeZone } from "@/lib/scheduled-work-items";
import { cn, formatCompactNumber, formatDurationShort, pluralize } from "@/lib/utils";
import { summaryPreview } from "@/pages/chat/answerMarkers";
import type { ApprovalDecision, AssignmentRunDetail, FlowRunStatus, FlowRunSummary } from "@/types";

/** The actions column of Executions: Answer and Delete, one width on every row (TaskHistory's skeleton draws the same). */
const ACTIONS_COLUMN = "w-16";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** Seconds between two timestamps, or null while the run has not ended. */
function runSeconds(startedAt: string | null, endedAt: string | null): number | null {
  if (!startedAt || !endedAt) return null;
  return (new Date(endedAt).getTime() - new Date(startedAt).getTime()) / 1000;
}

/** What an assignment run is called in the list: its assignment, else how it started. */
export function assignmentRunTitle(run: AssignmentRunDetail, t_i18n: Translate): string {
  if (run.assignment_name) return run.assignment_name;
  const scheduled = run.trigger_context?.type === "scheduled_work_item" ? String(run.trigger_context?.title ?? "") : "";
  if (scheduled) return scheduled;
  if (run.parent_run_id) return t_i18n("Delegation");
  return run.agent_name;
}

/** The line under the title: what the run waits for, else its error, else its result. */
export function assignmentRunLine(
  run: AssignmentRunDetail,
  t_i18n: Translate,
): { text: string; tone: "warning" | "error" | "neutral" } | null {
  const { isApprovalPending, isFreeTextPending } = runPauseState(run);
  if (isApprovalPending) return { text: t_i18n("Approval needed"), tone: "warning" };
  if (isFreeTextPending && run.human_input_question) {
    return { text: summaryPreview(run.human_input_question, 160), tone: "warning" };
  }
  if (run.error) return { text: runErrorText(run, t_i18n), tone: runErrorIsNeutral(run) ? "neutral" : "error" };
  const summary = run.result_summary ? summaryPreview(run.result_summary, 160) : "";
  return summary ? { text: summary, tone: "neutral" } : null;
}

/**
 * Every fact of an assignment run, in the contract's order: what it waits
 * for, the agent, where it comes from, the state facts, the counts, the run
 * id, then the timestamps. Pure: the hover card and the tests read it.
 */
export function assignmentRunFacts(
  run: AssignmentRunDetail,
  t_i18n: Translate,
  formatTime: (iso: string) => string,
): OpsFact[] {
  const facts: OpsFact[] = [];
  const line = assignmentRunLine(run, t_i18n);
  const cfg = runStatusConfig(run);
  if (line?.tone === "warning") facts.push({ label: t_i18n("Status"), tone: "warning", value: line.text });
  else facts.push({ label: t_i18n("Status"), value: t_i18n(cfg.label) });
  facts.push({ label: t_i18n("Agent"), value: run.agent_name });
  if (run.assignment_name) facts.push({ label: t_i18n("Assignment"), value: run.assignment_name });
  if (run.parent_agent_name) facts.push({ label: t_i18n("Delegated from"), value: run.parent_agent_name });
  if (run.error && line?.tone !== "warning") {
    facts.push({ label: t_i18n("Error"), tone: runErrorIsNeutral(run) ? undefined : "warning", value: runErrorText(run, t_i18n) });
  } else if (run.result_summary && line?.tone !== "warning") {
    facts.push({ label: t_i18n("Result"), value: summaryPreview(run.result_summary, 400) });
  }
  if (run.child_run_count > 0) {
    facts.push({
      label: t_i18n("Delegation"),
      value: t_i18n("{count} delegation {noun}", { count: run.child_run_count, noun: t_i18n(pluralize(run.child_run_count, "step")) }),
    });
  }
  const tokens = (run.total_input_tokens ?? 0) + (run.total_output_tokens ?? 0);
  if (tokens > 0) facts.push({ label: t_i18n("Tokens"), value: formatCompactNumber(tokens) });
  facts.push({ label: t_i18n("Run"), value: <span className="font-mono">{run.id}</span> });
  facts.push({ label: t_i18n("Created"), value: formatTime(run.created_at) });
  if (run.started_at) facts.push({ label: t_i18n("Started"), value: formatTime(run.started_at) });
  if (run.completed_at) facts.push({ label: t_i18n("Ended"), value: formatTime(run.completed_at) });
  const seconds = runSeconds(run.started_at, run.completed_at);
  if (seconds !== null) facts.push({ label: t_i18n("Duration"), value: formatDurationShort(seconds) });
  return facts;
}

/** Same vocabulary as the builder's RunsPanel: `awaiting` reads as the pending clock. Catalog keys. @i18n-keys */
const FLOW_RUN_STATUS_LABELS = {
  pending: "Pending",
  running: "Running",
  awaiting: "Waiting",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  skipped: "Skipped",
} as const satisfies Record<FlowRunStatus, string>;

function flowStatusConfig(status: FlowRunStatus) {
  const key = status === "awaiting" ? "pending" : status;
  return STATUS_CONFIG[key] || STATUS_CONFIG.pending;
}

/** The status badge of a run in the Executions table (list view), the same badge as its card. */
export function ExecutionStatusBadge({ run, kind }: { run: AssignmentRunDetail | FlowRunSummary; kind: "assignment" | "flow" }) {
  const { t_i18n } = useFormatter();
  const flow = kind === "flow" ? (run as FlowRunSummary) : null;
  const cfg = flow ? flowStatusConfig(flow.status) : runStatusConfig(run as AssignmentRunDetail);
  return (
    <Badge variant={cfg.badge} className="content-compact" data-testid="execution-status">
      {t_i18n(flow ? FLOW_RUN_STATUS_LABELS[flow.status] : cfg.label)}
    </Badge>
  );
}

/** Every fact of a flow run, in the contract's order. Pure. */
export function flowRunFacts(run: FlowRunSummary, t_i18n: Translate, formatTime: (iso: string) => string): OpsFact[] {
  const facts: OpsFact[] = [{ label: t_i18n("Status"), value: t_i18n(FLOW_RUN_STATUS_LABELS[run.status]) }];
  facts.push({ label: t_i18n("Trigger"), value: t_i18n(run.trigger_type.replace("_", " ")) });
  facts.push({ label: t_i18n("Version"), value: String(run.flow_version_number) });
  if (run.error) facts.push({ label: t_i18n("Error"), tone: runErrorIsNeutral(run) ? undefined : "warning", value: run.error });
  else if (run.result_summary) facts.push({ label: t_i18n("Result"), value: summaryPreview(run.result_summary, 400) });
  facts.push({ label: t_i18n("Run"), value: <span className="font-mono">{run.id}</span> });
  facts.push({ label: t_i18n("Created"), value: formatTime(run.created_at) });
  if (run.started_at) facts.push({ label: t_i18n("Started"), value: formatTime(run.started_at) });
  if (run.completed_at) facts.push({ label: t_i18n("Ended"), value: formatTime(run.completed_at) });
  const seconds = runSeconds(run.started_at, run.completed_at);
  if (seconds !== null) facts.push({ label: t_i18n("Duration"), value: formatDurationShort(seconds) });
  return facts;
}

/** The viewer's absolute time formatter, for the hover card facts. */
function useFormatTime() {
  const { locale } = useFormatter();
  const { user } = useAuth();
  const zone = viewerTimeZone(user?.timezone);
  return (iso: string) => formatInZone(iso, zone, locale);
}

/** The title button: the hover card's trigger, a click opens the run. */
function RowTitle({ title, facts, onOpen }: { title: string; facts: OpsFact[]; onOpen: () => void }) {
  return (
    <OpsHoverCard title={title} facts={facts}>
      <button
        type="button"
        className="min-w-[4rem] shrink truncate text-left font-medium hover:text-accent focus-visible:outline-none focus-visible:underline"
        data-testid="execution-title"
        onClick={(e) => {
          e.stopPropagation();
          onOpen();
        }}
      >
        {title}
      </button>
    </OpsHoverCard>
  );
}

/** The run id token and the "+N" token, the meta column's second line. */
function RunIdLine({ id, hidden, title, facts, onOpen }: { id: string; hidden: number; title: string; facts: OpsFact[]; onOpen: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="flex min-w-0 items-center gap-2">
      <OpsToken
        icon={Hash}
        tone="muted"
        full={id}
        copy={id}
        copyLabel={t_i18n("Copy run ID {id}", { id: id.slice(0, 8) })}
        testId="execution-run"
        className="font-mono"
      >
        {id.slice(0, 8)}
      </OpsToken>
      <OpsMoreToken count={hidden} title={title} facts={facts} onOpen={onOpen} testId="execution-more" />
    </span>
  );
}

const LINE_TONE = {
  warning: "text-feedback-warning-tertiary",
  error: "text-feedback-error-primary",
  neutral: "text-default-secondary",
} as const;

export function AssignmentExecutionRow({
  run,
  typeBadge,
  selected,
  onSelect,
  onDelete,
  onRespond,
  onDismiss,
  onApprove,
  approveFailed,
}: {
  run: AssignmentRunDetail;
  typeBadge: boolean;
  selected: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRespond: (runId: string, response: string) => void;
  onDismiss: (runId: string) => void;
  onApprove: (runId: string, decisions: ApprovalDecision[]) => void;
  approveFailed: boolean;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const formatTime = useFormatTime();
  const { isHitlPending } = runPauseState(run);
  const [answering, setAnswering] = useState(false);
  const cfg = runStatusConfig(run);
  const StatusIcon = cfg.icon;
  const heldReason = runHeldReason(run);
  const title = assignmentRunTitle(run, t_i18n);
  const line = assignmentRunLine(run, t_i18n);
  const facts = assignmentRunFacts(run, t_i18n, formatTime);
  // The row shows the status, the line, the agent and the run id; "+N" counts the rest.
  const hiddenFacts = Math.max(0, facts.length - 3 - (line && line.tone !== "warning" ? 1 : 0));
  const canDelete = !run.is_company_managed || isAdmin;
  const open = () => navigate(`/operations/executions/${run.id}`);
  const answerPanelId = `execution-answer-${run.id}`;

  return (
    <Card
      data-testid="execution-row"
      data-kind="assignment"
      className={cn(
        OPS_ROW_CARD,
        "group cursor-pointer overflow-hidden transition-all hover:bg-elevation-hover",
        run.status === "failed" && run.outcome !== "interrupted" && "border-feedback-error-primary/30",
        isHitlPending && "border-feedback-warning-primary/30",
        selected && "border-elevation-default bg-elevation-highlight/[0.04]",
      )}
      onClick={open}
    >
      <CardContent className="p-0">
        <div className={cn(OPS_ROW_HEIGHT, "px-4 py-2.5")} data-testid="execution-line">
          <OpsRowGrid
            actionsClassName={ACTIONS_COLUMN}
            actionsTestId="execution-actions"
            lead={
              <>
                <SelectionBox
                  checked={selected}
                  onToggle={() => onSelect(run.id)}
                  label={selected ? t_i18n("Deselect run") : t_i18n("Select run")}
                />
                <StatusIcon
                  className={cn("h-4 w-4 shrink-0", cfg.color, run.status === "running" && "animate-spin", isHitlPending && "animate-pulse")}
                  aria-hidden="true"
                />
              </>
            }
            main={
              <>
                <div className="flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap">
                  <RowTitle title={title} facts={facts} onOpen={open} />
                  <span className="flex shrink-0 items-center">
                    <Badge variant={cfg.badge} className="content-compact" data-testid="execution-status">
                      {t_i18n(cfg.label)}
                    </Badge>
                  </span>
                  <OpsChipStrip testId="execution-chips">
                    {heldReason && <HeldBadge reason={heldReason} className="content-compact" />}
                    {typeBadge && (
                      <Badge variant="outline" className="content-compact gap-1 border-accent/40 text-accent bg-accent/5">
                        <Bot className="h-3 w-3" aria-hidden="true" />
                        {t_i18n("Assignment")}
                      </Badge>
                    )}
                    {run.is_company_managed && <CompanyManagedBadge size="sm" />}
                    {run.child_run_count > 0 && (
                      <Badge variant="outline" className="content-compact gap-1 text-accent">
                        <GitBranch className="h-3 w-3" aria-hidden="true" />
                        {run.child_run_count} {t_i18n(pluralize(run.child_run_count, "step"))}
                      </Badge>
                    )}
                  </OpsChipStrip>
                </div>
                <div
                  className={cn("min-w-0 truncate content-compact", line ? LINE_TONE[line.tone] : "text-default-secondary")}
                  data-testid="execution-summary"
                  data-tone={line?.tone}
                >
                  {line?.text ?? ""}
                </div>
              </>
            }
            meta={
              <>
                <OpsToken icon={Bot} full={run.agent_name} testId="execution-agent">{run.agent_name}</OpsToken>
                <RunIdLine id={run.id} hidden={hiddenFacts} title={title} facts={facts} onOpen={open} />
              </>
            }
            updated={<OpsUpdated at={run.completed_at ?? run.started_at ?? run.created_at} testId="execution-updated" />}
            actions={
              <>
                {isHitlPending && (
                  <OpsRowAction
                    icon={Reply}
                    label={t_i18n("Answer")}
                    expanded={answering}
                    controls={answerPanelId}
                    onClick={() => setAnswering((v) => !v)}
                  />
                )}
                {canDelete && <OpsRowAction icon={Trash2} tone="danger" label={t_i18n("Delete run")} onClick={() => onDelete(run.id)} />}
              </>
            }
          />
        </div>
        {isHitlPending && answering && (
          <div id={answerPanelId} className="px-4 pb-3" onClick={(e) => e.stopPropagation()} data-testid="execution-answer">
            <RunPauseDecision
              run={run}
              showResolved={false}
              onRespond={onRespond}
              onDismiss={onDismiss}
              onApprove={onApprove}
              approveFailed={approveFailed}
            />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function FlowExecutionRow({ run, typeBadge }: { run: FlowRunSummary; typeBadge: boolean }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const formatTime = useFormatTime();
  const cfg = flowStatusConfig(run.status);
  const StatusIcon = cfg.icon;
  const title = run.flow_name || t_i18n("Flow run");
  const facts = flowRunFacts(run, t_i18n, formatTime);
  const summary = run.result_summary ? summaryPreview(run.result_summary, 160) : "";
  const errorTone = runErrorIsNeutral(run) ? "neutral" : "error";
  // The row shows the status, the trigger, the line and the run id; "+N" counts the rest.
  const hiddenFacts = Math.max(0, facts.length - 3 - (run.error || summary ? 1 : 0));
  const open = () => navigate(`/flows/library/${run.flow_id}?run=${run.id}`);
  const trigger = t_i18n(run.trigger_type.replace("_", " "));

  return (
    <Card
      data-testid="execution-row"
      data-kind="flow"
      className={cn(
        OPS_ROW_CARD,
        "group cursor-pointer overflow-hidden transition-all hover:bg-elevation-hover",
        run.status === "failed" && "border-feedback-error-primary/30",
      )}
      onClick={open}
    >
      <CardContent className="p-0">
        <div className={cn(OPS_ROW_HEIGHT, "px-4 py-2.5")} data-testid="execution-line">
          <OpsRowGrid
            actionsClassName={ACTIONS_COLUMN}
            actionsTestId="execution-actions"
            lead={
              <>
                {/* A flow run is not selectable (no bulk action takes it): the
                    box's room is kept so the status icons share one x. */}
                <span className="h-4 w-4 shrink-0" aria-hidden="true" />
                <StatusIcon className={cn("h-4 w-4 shrink-0", cfg.color, run.status === "running" && "animate-spin")} aria-hidden="true" />
              </>
            }
            main={
              <>
                <div className="flex min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap">
                  <RowTitle title={title} facts={facts} onOpen={open} />
                  <span className="flex shrink-0 items-center">
                    <Badge variant={cfg.badge} className="content-compact" data-testid="execution-status">
                      {t_i18n(FLOW_RUN_STATUS_LABELS[run.status])}
                    </Badge>
                  </span>
                  <OpsChipStrip testId="execution-chips">
                    {typeBadge && (
                      <Badge variant="outline" className="content-compact gap-1 border-filigran-ia-primary/40 text-filigran-ia-primary bg-filigran-ia-primary/5">
                        <Workflow className="h-3 w-3" aria-hidden="true" />
                        {t_i18n("Flow")}
                      </Badge>
                    )}
                  </OpsChipStrip>
                </div>
                <div
                  className={cn("min-w-0 truncate content-compact", run.error ? LINE_TONE[errorTone] : "text-default-secondary")}
                  data-testid="execution-summary"
                  data-tone={run.error ? errorTone : "neutral"}
                >
                  {run.error || summary}
                </div>
              </>
            }
            meta={
              <>
                <OpsToken icon={Zap} full={trigger} testId="execution-trigger">{trigger}</OpsToken>
                <RunIdLine id={run.id} hidden={hiddenFacts} title={title} facts={facts} onOpen={open} />
              </>
            }
            updated={<OpsUpdated at={run.completed_at ?? run.started_at ?? run.created_at} testId="execution-updated" />}
            actions={null}
          />
        </div>
      </CardContent>
    </Card>
  );
}
