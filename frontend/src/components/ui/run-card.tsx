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
 * Shared RunCard component – used everywhere a assignment run is displayed.
 *
 * Accepts both `AssignmentRun` (per-assignment, no names) and `AssignmentRunDetail`
 * (global, has names/icons). Optional agent icon/name are only shown when
 * provided.
 *
 * When the run status is `awaiting_human`, shows the AI's question and a
 * response input so the user can answer inline without navigating away.
 *
 * Supports delegation tree display: when `child_run_count > 0`, descendants
 * are fetched via `useDescendantRuns` and rendered as a tree (`orderRunTree`):
 * each run under the run that delegated it, one guide column per level.
 * Delegation runs show a cyan "Delegation" badge instead of the assignment name.
 * The tree is **expanded by default** in full mode, but **collapsed by default**
 * in compact mode (dashboard Live Activity) so the list stays compact.
 *
 * The expand chevron is only rendered when a run has children, keeping rows
 * without sub-runs flush left with no wasted space.
 */
import { workItemPath } from "@/lib/task-routes";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Bot, CalendarClock, Trash2, ChevronRight, ChevronDown, GitBranch, Wrench } from "lucide-react";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { DelegationIndent } from "@/components/ui/delegation-indent";
import { HeldBadge } from "@/components/ui/held-badge";
import { runHeldReason } from "@/lib/held";
import { orderRunTree } from "@/lib/run-tree";
import { Tip } from "@/components/ui/tooltip";
import { cn, formatDateTime, pluralize } from "@/lib/utils";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { WARNING_ON_HOVER_TEXT } from "@/lib/warning-text";
import { runErrorIsNeutral, runErrorText } from "@/lib/run-error-text";
import { ICON_MAP, runStatusConfig, toolDisplayName, providerLogoSrc } from "@/lib/constants";
import { summaryPreview } from "@/pages/chat/answerMarkers";
import { useAuth } from "@/hooks/useAuth";
import { useDescendantRuns } from "@/hooks/api/useAssignmentApi";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { RunPauseDecision, runPauseState } from "@/components/decisions/RunPauseDecision";
import type { A2AExchange, ActionProposal, ApprovalDecision, HumanInputHistoryEntry } from "@/types";

// ── Props ───────────────────────────────────────────────────────────────────
interface RunCardRun {
  id: string;
  status: string;
  /** `interrupted` when the platform ended the run: labelled so, not "Failed". */
  outcome?: string | null;
  result_summary: string | null;
  error: string | null;
  actions_taken: { tool: string; source?: string }[] | null;
  created_at: string;
  /** Present on AssignmentRunDetail */
  assignment_name?: string | null;
  agent_name?: string;
  agent_icon?: string;
  /** Whether this run belongs to a company-managed agent */
  is_company_managed?: boolean;
  /** HITL fields */
  human_input_question?: string | null;
  human_input_response?: string | null;
  human_input_history?: HumanInputHistoryEntry[] | null;
  /** Structured proposals when the run paused on the approval gate. */
  pending_approvals?: ActionProposal[] | null;
  /** A decision is recorded and the run is waiting on the worker, not on a
   *  person — set by the server so no surface has to infer it. */
  approval_answered?: boolean | null;
  /** Carries the calling A2A registration on externally submitted runs. */
  trigger_context?: Record<string, unknown> | null;
  /** The A2A provider that submitted this run, resolved server-side. */
  a2a_provider?: string | null;
  /** Its A2A task's exchange with the caller: the status reads "Waiting on caller" while it asks. */
  a2a_exchange?: A2AExchange | null;
  /** Delegation chain fields */
  parent_run_id?: string | null;
  child_run_count?: number;
  parent_agent_name?: string | null;
  parent_agent_icon?: string | null;
}

interface RunCardProps {
  run: RunCardRun;
  /** Show delete button on hover. Provide the callback to enable. */
  onDelete?: (id: string) => void;
  /** If true, wrap in a Card with padding (history-style). If false, use a lighter row. Default true. */
  card?: boolean;
  /** If true, hide HITL inline details (question, response form). Shows a brief preview instead. Default false. */
  compact?: boolean;
  /** Callback when the user responds to a HITL question. */
  onRespond?: (runId: string, response: string) => void;
  /** Callback when the user dismisses (discards) a HITL run. */
  onDismiss?: (runId: string) => void;
  /** Callback when the user decides a set of gated tool calls. */
  onApprove?: (runId: string, decisions: ApprovalDecision[]) => void;
  /** True when the last approval submit failed, so the prompt un-latches and
   *  can be retried. Shared across a list of cards, which is harmless: only the
   *  card that submitted is latched, and un-latching the others is a no-op. */
  approveFailed?: boolean;
  /** Whether the card shows a selection checkbox. */
  selectable?: boolean;
  /** Whether this card is currently selected. */
  selected?: boolean;
  /** Toggle selection for this run. */
  onSelect?: (id: string) => void;
  /** Show an "Assignment" type badge — for feeds mixing run kinds
   * (Operations → Executions renders flow runs alongside these). */
  typeBadge?: boolean;
  /** Optional extra class names forwarded to the outer element. */
  className?: string;
}

// ── Descendant Row (one run of the delegation tree) ─────────────────────────
/** Lightweight row for a single descendant step (no nested expansion). */
function DescendantRow({ run, depth, onDelete }: { run: RunCardRun; depth: number; onDelete?: (id: string) => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const cfg = runStatusConfig(run);
  const StatusIcon = cfg.icon;
  const AgentIcon = run.agent_icon ? (ICON_MAP[run.agent_icon] ?? Bot) : null;
  const isRunning = run.status === "running";
  const canDelete = !run.is_company_managed || isAdmin;
  const ParentAgentIcon = run.parent_agent_icon ? (ICON_MAP[run.parent_agent_icon] ?? Bot) : null;
  const heldReason = runHeldReason(run);

  return (
    <div
      className="group flex items-center gap-3 py-2 pr-0 rounded-sm cursor-pointer hover:bg-elevation-hover transition-colors"
      onClick={(e) => { e.stopPropagation(); navigate(`/operations/executions/${run.id}`); }}
    >
      {/* Visual indent spacer with vertical connection line */}
      <div className="w-6 self-stretch flex justify-center shrink-0">
        <div className="w-px bg-accent/15" />
      </div>
      <DelegationIndent depth={depth} />

      {/* Left content */}
      <div className="flex items-center gap-2.5 min-w-0 flex-1">
        <StatusIcon className={cn("h-3.5 w-3.5 shrink-0", cfg.color, isRunning && "animate-spin")} />

        {AgentIcon && (
          <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">
            <AgentIcon className="h-3 w-3 text-accent" />
          </div>
        )}

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <Badge variant="outline" className="text-[7px] border-accent/30 text-accent/80 bg-accent/5 px-1 py-0">
              <GitBranch className="h-2 w-2 mr-0.5" />
              {t_i18n("Delegation")}
            </Badge>
            {run.parent_agent_name && (
              <span className="text-[9px] text-default-secondary flex items-center gap-0.5">
                {t_i18n("from")}
                {ParentAgentIcon && <ParentAgentIcon className="h-2.5 w-2.5 text-accent/50 inline" />}
                <span className="font-medium text-default-disabled">{run.parent_agent_name}</span>
              </span>
            )}
            {run.agent_name && <span className="text-[9px] text-default-secondary">→ {run.agent_name}</span>}
          </div>
          <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
            <Badge variant={cfg.badge} className="text-[7px]">{t_i18n(cfg.label)}</Badge>
            {heldReason && <HeldBadge reason={heldReason} className="text-[7px]" />}
            {run.result_summary && summaryPreview(run.result_summary, 70) && (
              <span className="text-[8px] text-default-secondary truncate max-w-[250px]">
                {summaryPreview(run.result_summary, 70)}
              </span>
            )}
            {run.error && (
              <span data-testid="run-error" data-tone={runErrorIsNeutral(run) ? "neutral" : "error"} className={cn("text-[8px] truncate max-w-[150px]", runErrorIsNeutral(run) ? "text-default-secondary" : "text-feedback-error-primary")}>
                {(() => { const text = runErrorText(run, t_i18n); return text.length > 50 ? `${text.slice(0, 50)}…` : text; })()}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Right: timestamp + delete — flush-right aligned with parent row */}
      <div className="flex items-center gap-2 shrink-0">
        <span className="text-[9px] text-default-secondary whitespace-nowrap">{formatDateTime(run.created_at)}</span>
        {onDelete && canDelete && (
          <button aria-label={t_i18n("Delete run")}
            className="hidden group-hover:flex h-5 w-5 items-center justify-center rounded-sm hover:bg-feedback-error-primary/10 text-feedback-error-primary"
            onClick={(e) => { e.stopPropagation(); onDelete(run.id); }}
          >
            <Trash2 className="h-2.5 w-2.5" />
          </button>
        )}
      </div>
    </div>
  );
}

// ── Inline Action Row (tool call step) ──────────────────────────────────────
function ActionRow({ action, index }: { action: { tool: string; source?: string }; index: number }) {
  const { t_i18n } = useFormatter();
  const sourceLabel = action.source
    ? action.source.split(":").slice(1).join(":") || action.source
    : null;

  return (
    <div className="flex items-center gap-2 py-1 pl-6">
      <span className="text-[8px] text-default-secondary tabular-nums w-4 text-right shrink-0">{index + 1}.</span>
      <Wrench className="h-2.5 w-2.5 text-accent/50 shrink-0" />
      <span className="text-[8px] text-default-secondary font-mono-plex truncate">{toolDisplayName(action.tool, t_i18n)}</span>
      {sourceLabel && (
        <span className="text-[7px] text-default-secondary truncate">({sourceLabel})</span>
      )}
    </div>
  );
}

// ── Descendant List (delegation tree) ───────────────────────────────────────
function DescendantList({ rootRunId, onDelete }: { rootRunId: string; onDelete?: (id: string) => void }) {
  const { t_i18n } = useFormatter();
  const { data: descendants, isLoading } = useDescendantRuns(rootRunId);

  if (isLoading) {
    return (
      <div className="mt-1.5">
        <div className="flex items-center gap-2 content-caption text-default-secondary animate-pulse p-2 pl-6">
          <div className="h-3 w-3 rounded-sm bg-elevation-hover" />
          <span>{t_i18n("Loading delegation steps…")}</span>
        </div>
      </div>
    );
  }

  if (!descendants || descendants.length === 0) return null;

  return (
    <div className="mt-1.5 space-y-0.5">
      {orderRunTree(rootRunId, descendants).map(({ run: desc, depth }) => (
        <DescendantRow key={desc.id} run={desc} depth={depth} onDelete={onDelete} />
      ))}
    </div>
  );
}

// ── Component ───────────────────────────────────────────────────────────────
export function RunCard({ run, onDelete, card = true, compact = false, onRespond, onDismiss, onApprove, approveFailed, selectable = false, selected = false, onSelect, typeBadge = false, className }: RunCardProps) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const hasChildren = (run.child_run_count ?? 0) > 0;
  const hasActions = (run.actions_taken?.length ?? 0) > 0;
  // Expanded by default when there are children (but collapsed in compact mode)
  const [expanded, setExpanded] = useState(hasChildren && !compact);
  const [actionsExpanded, setActionsExpanded] = useState(false);
  const cfg = runStatusConfig(run);
  const StatusIcon = cfg.icon;
  const AgentIcon = run.agent_icon ? (ICON_MAP[run.agent_icon] ?? Bot) : null;
  const isRunning = run.status === "running";
  const isAwaitingHuman = run.status === "awaiting_human";
  const { isHitlPending, isApprovalPending, isFreeTextPending } = runPauseState(run);
  const isDelegation = !!run.parent_run_id && !run.assignment_name;
  // Which A2A partner submitted this run, if any. An external run has no
  // assignment name and no parent, so without this its origin line is blank and
  // it is indistinguishable from a manually triggered one — on an agent whose
  // whole point is that partners task it.
  const isExternal = run.trigger_context?.source === "a2a";
  // A scheduled work item's run: no assignment, so its origin is the
  // item it executed, linked.
  const scheduledItemId =
    run.trigger_context?.type === "scheduled_work_item"
      ? String(run.trigger_context?.work_item_id ?? "")
      : "";
  const scheduledTitle = scheduledItemId ? String(run.trigger_context?.title ?? "") : "";
  // Resolved by the server from what the registration is OWNED by, not from
  // what its id looks like — so every surface labels a run identically. Null on
  // an external run means genuinely unattributable, not "not yet looked up".
  const a2aProvider = isExternal ? (run.a2a_provider ?? null) : null;
  // Waiting while agentic AI is unavailable here: held, not failed.
  const heldReason = runHeldReason(run);
  // Company-managed runs: only admins can delete.
  const canDelete = !run.is_company_managed || isAdmin;

  // Resolve parent agent icon for delegation badge
  const ParentAgentIcon = run.parent_agent_icon ? (ICON_MAP[run.parent_agent_icon] ?? Bot) : null;

  const inner = (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          {/* Expand chevron for delegation tree (hidden in compact/dashboard mode) */}
          {hasChildren && !compact && (
            <button aria-label={expanded ? t_i18n("Collapse") : t_i18n("Expand")}
              type="button"
              onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
              className="h-5 w-5 flex items-center justify-center shrink-0 rounded-sm hover:bg-elevation-hover transition-colors"
            >
              <ChevronRight className={cn("h-3 w-3 transition-transform", expanded && "rotate-90")} />
            </button>
          )}

          {/* Selection checkbox */}
          {selectable && (
            <SelectionBox
              checked={!!selected}
              onToggle={() => onSelect?.(run.id)}
              label={selected ? t_i18n("Deselect run") : t_i18n("Select run")}
            />
          )}

          {/* Status icon */}
          <StatusIcon className={cn("h-4 w-4 shrink-0", cfg.color, isRunning && "animate-spin", isHitlPending && "animate-pulse")} />

          {/* Agent icon (only when names are available) */}
          {AgentIcon && (
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">
              <AgentIcon className="h-4 w-4 text-accent" />
            </div>
          )}

          {/* Text content */}
          <div className="min-w-0">
            {compact ? (
              /* Compact (dashboard Live Activity): the origin is a muted
                 icon with a tooltip, never a chip, so every trigger gives the
                 same two text lines: the title line and the status line. */
              <div className="flex min-w-0 items-center gap-1.5 whitespace-nowrap" data-testid="run-card-title-line">
                {scheduledItemId ? (
                  <Tip content={t_i18n("Run of a scheduled work item: a one-off action, with no assignment.")}>
                    <Link
                      to={workItemPath(scheduledItemId)}
                      onClick={(e) => e.stopPropagation()}
                      aria-label={t_i18n("Scheduled")}
                      className="inline-flex shrink-0 items-center rounded-sm text-default-secondary hover:text-accent focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                      data-testid="scheduled-run-badge"
                    >
                      <CalendarClock className="h-3.5 w-3.5" />
                    </Link>
                  </Tip>
                ) : isDelegation ? (
                  <Tip content={t_i18n("Delegation")}>
                    <span role="img" aria-label={t_i18n("Delegation")} className="inline-flex shrink-0 items-center text-default-secondary" data-testid="run-trigger-marker">
                      <GitBranch className="h-3.5 w-3.5" />
                    </span>
                  </Tip>
                ) : isExternal ? (
                  <Tip content={a2aProvider
                    ? t_i18n("Submitted by an external agent platform over A2A. It ran inside this exposure's run-as identity.")
                    : t_i18n("Submitted over A2A. Which partner can no longer be determined: its credential was shared between both A2A providers, or has since been deleted.")}>
                    <span role="img" aria-label={t_i18n("A2A")} className="inline-flex shrink-0 items-center text-default-secondary" data-testid="run-trigger-marker">
                      {a2aProvider ? <img src={providerLogoSrc(a2aProvider)} alt="" className="h-3.5 w-3.5" /> : <GitBranch className="h-3.5 w-3.5" />}
                    </span>
                  </Tip>
                ) : null}
                {scheduledItemId ? (
                  scheduledTitle && (
                    <Tip content={scheduledTitle}>
                      <span className="content-compact font-semibold truncate">{scheduledTitle}</span>
                    </Tip>
                  )
                ) : isDelegation ? (
                  run.parent_agent_name && (
                    <Tip content={`${t_i18n("from")} ${run.parent_agent_name}`}>
                      <span className="content-compact font-semibold truncate">
                        {t_i18n("from")} {run.parent_agent_name}
                      </span>
                    </Tip>
                  )
                ) : isExternal ? (
                  <span className="content-compact font-semibold truncate">
                    {a2aProvider ? (INTEGRATION_PROVIDER_NAMES[a2aProvider] ?? a2aProvider) : t_i18n("A2A")}
                  </span>
                ) : (
                  run.assignment_name && (
                    <Tip content={run.assignment_name}>
                      <span className="content-compact font-semibold truncate">{run.assignment_name}</span>
                    </Tip>
                  )
                )}
                {run.agent_name && <span className="content-caption text-default-secondary truncate">· {run.agent_name}</span>}
              </div>
            ) : (
            <div className="flex items-center gap-2 flex-wrap">
              {/* Run origin indicator: assignment name OR delegation badge */}
              {scheduledItemId ? (
                <div className="flex items-center gap-1.5">
                  <Tip content={t_i18n("Run of a scheduled work item: a one-off action, with no assignment.")}>
                    <Link
                      to={workItemPath(scheduledItemId)}
                      onClick={(e) => e.stopPropagation()}
                      data-testid="scheduled-run-badge"
                    >
                      <Badge variant="outline" className="text-[8px] border-accent/40 text-accent bg-accent/5 gap-0.5 hover:bg-accent/10">
                        <CalendarClock className="h-2.5 w-2.5" />
                        {t_i18n("Scheduled")}
                      </Badge>
                    </Link>
                  </Tip>
                  {scheduledTitle && <span className="content-compact font-semibold">{scheduledTitle}</span>}
                </div>
              ) : isDelegation ? (
                <div className="flex items-center gap-1.5">
                  <Badge variant="outline" className="text-[8px] border-accent/40 text-accent bg-accent/5">
                    <GitBranch className="h-2.5 w-2.5 mr-0.5" />
                    {t_i18n("Delegation")}
                  </Badge>
                  {run.parent_agent_name && (
                    <span className="content-caption text-default-secondary flex items-center gap-1">
                      {t_i18n("from")}
                      {ParentAgentIcon && <ParentAgentIcon className="h-3 w-3 text-accent/60 inline" />}
                      <span className="font-medium text-default-secondary">{run.parent_agent_name}</span>
                    </span>
                  )}
                </div>
              ) : isExternal ? (
                <Tip content={a2aProvider
                  ? t_i18n("Submitted by an external agent platform over A2A. It ran inside this exposure's run-as identity.")
                  : t_i18n("Submitted over A2A. Which partner can no longer be determined: its credential was shared between both A2A providers, or has since been deleted.")}>
                  <Badge variant="outline" className="text-[8px] border-accent/40 text-accent bg-accent/5 gap-1">
                    {a2aProvider && (
                      <img
                        src={providerLogoSrc(a2aProvider)}
                        alt=""
                        className="h-2.5 w-2.5"
                      />
                    )}
                    {a2aProvider
                      ? (INTEGRATION_PROVIDER_NAMES[a2aProvider] ?? a2aProvider)
                      : t_i18n("A2A")}
                  </Badge>
                </Tip>
              ) : (
                <>
                  {run.assignment_name && <span className="content-compact font-semibold">{run.assignment_name}</span>}
                </>
              )}
              {run.agent_name && <span className="content-caption text-default-secondary">· {run.agent_name}</span>}
            </div>
            )}
            <div
              className={cn("flex items-center gap-2 mt-0.5", compact ? "overflow-hidden whitespace-nowrap" : "flex-wrap")}
              data-testid="run-card-status-line"
            >
              {typeBadge && (
                <Badge variant="outline" className="shrink-0 text-[8px] border-accent/40 text-accent bg-accent/5">
                  <Bot className="h-2.5 w-2.5 mr-0.5" />
                  {t_i18n("Assignment")}
                </Badge>
              )}
              {/* Chips keep their size; only the text previews after them shrink
                  and truncate when the compact line runs out of room. */}
              <Badge variant={cfg.badge} className="shrink-0 text-[8px]">{t_i18n(cfg.label)}</Badge>
              {heldReason && <HeldBadge reason={heldReason} className="shrink-0 text-[8px]" />}
              {run.is_company_managed && (
                <span className="inline-flex shrink-0">
                  <CompanyManagedBadge size="sm" />
                </span>
              )}
              {/* Child run count indicator */}
              {hasChildren && (
                <Tip content={t_i18n("{count} delegation {noun}", { count: run.child_run_count ?? 0, noun: t_i18n(pluralize(run.child_run_count ?? 0, "step")) })}>
                  <span className="text-[9px] text-accent flex shrink-0 items-center gap-0.5 cursor-default">
                    <GitBranch className="h-2.5 w-2.5" />
                    {run.child_run_count} {t_i18n(pluralize(run.child_run_count ?? 0, "step"))}
                  </span>
                </Tip>
              )}
              {run.result_summary && !isAwaitingHuman && summaryPreview(run.result_summary, 80) && (
                <span className="min-w-0 text-[9px] text-default-secondary truncate max-w-[300px]">
                  {summaryPreview(run.result_summary, 80)}
                </span>
              )}
              {compact && isApprovalPending && (
                <span className={cn("min-w-0 content-caption truncate", WARNING_ON_HOVER_TEXT)}>
                  {t_i18n("Approval needed")}
                </span>
              )}
              {compact && isFreeTextPending && run.human_input_question && (
                <span className="min-w-0 text-[9px] text-default-secondary truncate max-w-[300px]">
                  {run.human_input_question.length > 80 ? `${run.human_input_question.slice(0, 80)}…` : run.human_input_question}
                </span>
              )}
              {run.error && (
                <span
                  data-tone={runErrorIsNeutral(run) ? "neutral" : "error"}
                  data-testid="run-error"
                  className={cn(
                    "min-w-0 truncate max-w-[200px]",
                    // A cancel is a deliberate act: caption size, neutral tone.
                    runErrorIsNeutral(run) ? "content-caption text-default-secondary" : "text-[9px] text-feedback-error-primary",
                  )}
                >
                  {(() => { const text = runErrorText(run, t_i18n); return text.length > 60 ? `${text.slice(0, 60)}…` : text; })()}
                </span>
              )}
              {hasActions && run.actions_taken && (
                <button
                  type="button"
                  className="text-[9px] text-accent hover:text-accent/80 flex shrink-0 items-center gap-0.5 transition-colors"
                  onClick={(e) => { e.stopPropagation(); setActionsExpanded(!actionsExpanded); }}
                >
                  {actionsExpanded ? <ChevronDown className="h-2.5 w-2.5" /> : <ChevronRight className="h-2.5 w-2.5" />}
                  {run.actions_taken.length} {t_i18n(pluralize(run.actions_taken.length, "step"))}
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Right: time + optional delete — always aligned right */}
        <div className="flex items-center gap-2 shrink-0">
          {compact ? (
            // The Live activity rows read a relative time, the absolute one in a
            // tooltip, as the CTEM feed rows beside them (#9594 A1).
            <Tip content={formatShortDateTimeIntl(intl, run.created_at)}>
              <span data-testid="run-card-time" className="content-caption text-default-secondary whitespace-nowrap">{formatTimeAgo(intl, run.created_at)}</span>
            </Tip>
          ) : (
            <span className="content-caption text-default-secondary whitespace-nowrap">{formatDateTime(run.created_at)}</span>
          )}
          {onDelete && canDelete && (
            <Tip content={t_i18n("Delete run")}>
              <button aria-label={t_i18n("Delete run")}
                className="hidden group-hover:flex h-6 w-6 items-center justify-center rounded-sm hover:bg-feedback-error-primary/10 text-feedback-error-primary"
                onClick={(e) => { e.stopPropagation(); onDelete(run.id); }}
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </Tip>
          )}
        </div>
      </div>

      {!compact && (
        <RunPauseDecision
          run={run}
          onRespond={onRespond}
          onDismiss={onDismiss}
          onApprove={onApprove}
          approveFailed={approveFailed}
        />
      )}

      {/* Inline action steps (collapsible) */}
      {actionsExpanded && hasActions && run.actions_taken && (
        <div className="mt-1 rounded-sm bg-elevation-hover py-1">
          {run.actions_taken.map((action, i) => (
            <ActionRow key={i} action={action} index={i} />
          ))}
        </div>
      )}

      {/* Delegation tree — flat descendant list (hidden in compact/dashboard mode) */}
      {expanded && hasChildren && !compact && (
        <DescendantList rootRunId={run.id} onDelete={onDelete} />
      )}
    </div>
  );

  if (card) {
    return (
      <Card
        className={cn(
          // `group` drives the delete button; `group/hover-row` drives WARNING_ON_HOVER_TEXT.
          "cursor-pointer hover:bg-elevation-hover transition-all group group/hover-row",
          run.status === "failed" && run.outcome !== "interrupted" && "border-feedback-error-primary/10",
          isHitlPending && "border-feedback-warning-primary/30",
          selected && "border-elevation-default bg-elevation-highlight/[0.04]",
          isDelegation && "border-accent/10",
          className,
        )}
        onClick={() => navigate(`/operations/executions/${run.id}`)}
      >
        <CardContent className={compact ? "p-3" : "p-4"}>{inner}</CardContent>
      </Card>
    );
  }

  // Lightweight row variant (no card wrapper)
  return (
    <div
      className={cn(
        "group group/hover-row p-3 cursor-pointer hover:bg-elevation-hover transition-all duration-300",
        isHitlPending && "bg-feedback-warning-primary/[0.03]",
        isDelegation && "bg-accent/[0.02]",
        className,
      )}
      onClick={() => navigate(`/operations/executions/${run.id}`)}
    >
      {inner}
    </div>
  );
}
