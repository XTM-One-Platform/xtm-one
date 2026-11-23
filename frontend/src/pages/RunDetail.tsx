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

import { workItemPath } from "@/lib/task-routes";
import { useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { useTick } from "@/hooks/useTick";
import {
  CheckCircle2,
  Bot,
  Send,
  X,
  Play,
  Globe,
  Clock,
  Calendar,
  Zap,
  AlertCircle,
  Trash2,
  Link2,
  User,
  Mail,
  FileText,
  Wrench,
  ChevronDown,
  ChevronRight,
  MessageSquare,
  ArrowRight,
  Hand,
  Mic,
  MicOff,
  GitBranch,
  CornerLeftUp,
  Cpu,
  Copy,
  Check,
  ExternalLink,
  Activity,
  Scan,
  Info,
  CalendarClock,
  Radio,
  RotateCcw,
  AlertTriangle,
} from "lucide-react";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Tip } from "@/components/ui/tooltip";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { TruncatedText } from "@/components/ui/truncated-text";
import { DelegationIndent } from "@/components/ui/delegation-indent";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { RunCancelledCard } from "@/components/ui/run-cancelled-card";
import { RunFrictionsCard } from "@/components/ui/run-frictions-card";
import { HeldBadge } from "@/components/ui/held-badge";
import { runHeldReason } from "@/lib/held";
import { orderRunTree } from "@/lib/run-tree";
import { isTriggerMetadataKey } from "@/lib/trigger-context";
import { formatFailureCause } from "@/lib/failure-causes";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { LocationBackButton } from "@/components/ui/back-button";
import { AnnotationButtons } from "@/components/ui/annotation-buttons";
import { OutcomeReview } from "@/components/ui/outcome-review";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { useAssignmentRunDetail, useRespondToHumanInput, useDismissRun, useDeleteAssignmentRun, useFollowupRun, useFollowupChat, useDescendantRuns, useSubmitApprovalDecisions } from "@/hooks/api/useAssignmentApi";
import { RunActionRequests } from "@/components/runs/RunActionRequests";
import { A2ARunExchange } from "@/components/runs/A2ARunExchange";
import { a2aNoResultText } from "@/components/runs/a2a-no-result-text";
import { RunDecisionCard } from "@/components/decisions/RunDecisionCard";
import { useLangfuseConfig } from "@/hooks/api/useSettingsApi";
import { useSpeechToText } from "@/hooks/useSpeechToText";
import { useChatSendEnabled } from "@/hooks/usePlatformConfig";
import { useAuth } from "@/hooks/useAuth";
import { FileChip, WorkingFilesDisclosure } from "@/components/chat-panel/FileChips";
import { InlineFileContent } from "@/components/chat-panel/InlineFileContent";
import { conversationRevisions, fileTag } from "@/pages/chat/file-revisions";
import { FileRevisionsContext } from "@/pages/chat/file-revisions-context";
import { answerProse, summaryPreview } from "@/pages/chat/answerMarkers";
import RouteNotFound from "@/pages/RouteNotFound";
import type { ChatMessageAttachment } from "@/types";
import { cn, formatDateTime, formatCompactNumber, formatDurationShort, pluralize, attachmentTypeLabel } from "@/lib/utils";
import { runErrorIsNeutral, runErrorText } from "@/lib/run-error-text";
import { assignmentRunTracesPath } from "@/lib/trace-links";
import { primeChatDraft } from "@/lib/chat-launch";
import { ICON_MAP, INTEGRATION_PROVIDER_NAMES, runStatusConfig, TRIGGER_LABELS, REFETCH_INTERVAL_SLOW, DELEGATION_TOOL_NAMES, COPY_FEEDBACK_DELAY, HITL_CONTINUATION_MESSAGE } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { AgenticStep, AssignmentRunAttachment, AssignmentRunDetail, HumanInputHistoryEntry } from "@/types";
import { Spinner } from "@filigran/design-system";

// ── Trigger type icon + color mapping ──────────────────────────────────────
const TRIGGER_TYPE_CONFIG: Record<string, { icon: React.ElementType; color: string; bg: string }> = {
  manual: { icon: Play, color: "text-feedback-info-primary", bg: "bg-feedback-info-primary/10" },
  schedule: { icon: Clock, color: "text-filigran-ia-primary", bg: "bg-filigran-ia-primary/10" },
  webhook: { icon: Globe, color: "text-feedback-warning-primary", bg: "bg-feedback-alert-primary/10" },
  integration_event: { icon: Zap, color: "text-feedback-success-primary", bg: "bg-feedback-success-primary/10" },
  chain: { icon: Link2, color: "text-accent", bg: "bg-accent/10" },
  followup: { icon: MessageSquare, color: "text-filigran-tonic-primary", bg: "bg-filigran-tonic-primary/10" },
  platform_event: { icon: Radio, color: "text-accent", bg: "bg-accent/10" },
};

// ── Trigger context key labels for pretty display ─────────────────────────
const KEY_LABELS: Record<string, string> = {
  type: "Trigger Type",
  user_id: "User ID",
  user_email: "User Email",
  user_name: "User Name",
  triggered_at: "Triggered At",
  cron: "Cron Expression",
  scheduled_at: "Scheduled At",
  url: "URL",
  method: "Method",
  provider: "Provider",
  event_key: "Event",
  event: "Event",
  integration_id: "Integration ID",
  assignment_id: "Assignment ID",
  scope: "Scope",
  path: "Path",
  agent_id: "Agent ID",
  change_type: "Change Type",
  resource: "Resource",
  subscription_id: "Subscription ID",
  channel_id: "Channel ID",
  message_id: "Message ID",
  chained_from_assignment_id: "From Assignment ID",
  chained_from_assignment_name: "From Assignment",
  chained_from_run_id: "From Run ID",
  parent_run_id: "Parent Run ID",
  parent_assignment_name: "Parent Assignment",
  // platform_event runs: the event that started the run.
  event_id: "Event ID",
  object_type: "Object type",
  object_id: "Object ID",
  occurred_at: "Occurred at",
  once: "One-off watch",
  // A2A runs: who submitted the task, and with which credential form.
  source: "Source",
  client_id: "Client ID",
  a2a_provider: "A2A Provider",
};

// ── Key icon mapping for visual richness ───────────────────────────────────
const KEY_ICONS: Record<string, React.ElementType> = {
  user_email: Mail,
  user_name: User,
  user_id: User,
  url: Globe,
  provider: Zap,
  cron: Clock,
  triggered_at: Calendar,
  scheduled_at: Calendar,
  occurred_at: Calendar,
};

// ── Run attachment → chat attachment adapter ──────────────────────────────

/**
 * Convert an ``AssignmentRunAttachment`` into the ``ChatMessageAttachment``
 * shape the shared chat components (``FileChip`` / ``InlineFileContent``)
 * expect, so the visual + auth'd download flow stays identical between
 * channels (chat / run detail).
 */
function toChipAttachment(att: AssignmentRunAttachment): ChatMessageAttachment {
  return {
    filename: att.filename,
    // ``type`` is the short extension label the chip renders under the
    // filename. ``attachmentTypeLabel`` mirrors the backend
    // ``_attachment_meta_from_file_attachment`` helper (drops ``+xml`` /
    // ``+json`` suffixes + ``;`` params and shortens ``vnd.*`` subtypes to
    // the last dotted segment) so an extension-less vendor file doesn't
    // render an unwieldy label like ``VND.OPENXMLFORMATS-…SHEET``.
    type: attachmentTypeLabel(att.filename, att.content_type),
    size: att.size,
    file_id: att.file_id,
    download_url: att.download_url,
    content_type: att.content_type,
    // ``file_tag`` now comes from the backend (mirrors the chat path):
    // ``working_file`` renders a compact chip, anything else a full
    // download card. Fall back to ``download_file`` for older payloads
    // that predate the field.
    file_tag: att.file_tag ?? "download_file",
    created_at: att.created_at ?? null,
    sha256: att.sha256 ?? null,
    deleted_at: att.deleted_at ?? null,
    deleted_reason: att.deleted_reason ?? null,
  };
}

// ── Agentic Steps Display ────────────────────────────────────────────────

/** Max chars of tool result to show inline (collapsed). */
const TOOL_RESULT_PREVIEW_CHARS = 500;

function ToolResultContent({ content }: { content: string }) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  const isLong = content.length > TOOL_RESULT_PREVIEW_CHARS;
  const displayContent = expanded || !isLong ? content : content.slice(0, TOOL_RESULT_PREVIEW_CHARS) + "…";

  return (
    <div>
      <pre className="text-[11px] text-default-secondary font-mono-plex whitespace-pre-wrap break-all leading-relaxed">
        {displayContent}
      </pre>
      {isLong && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="mt-1.5 content-caption text-accent hover:text-accent/80 flex items-center gap-1"
        >
          {expanded ? (
            <><ChevronDown className="h-3 w-3" /> {t_i18n("Collapse")}</>
          ) : (
            <><ChevronRight className="h-3 w-3" /> {t_i18n("Show full output ({size}K chars)", { size: (content.length / 1000).toFixed(1) })}</>
          )}
        </button>
      )}
    </div>
  );
}

function AgenticStepsCard({ steps }: { steps: AgenticStep[] }) {
  const { t_i18n } = useFormatter();
  const [stepsOpen, setStepsOpen] = useState(false);
  const [flowOpen, setFlowOpen] = useState(false);

  // Group steps into iterations: each iteration = tool_calls + their results
  const iterations: { toolCalls: AgenticStep[]; toolResults: AgenticStep[] }[] = [];
  let finalResponse: AgenticStep | null = null;
  let currentIteration: { toolCalls: AgenticStep[]; toolResults: AgenticStep[] } | null = null;

  for (const step of steps) {
    if (step.type === "tool_calls") {
      if (currentIteration) iterations.push(currentIteration);
      currentIteration = { toolCalls: [step], toolResults: [] };
    } else if (step.type === "tool_result" && currentIteration) {
      currentIteration.toolResults.push(step);
    } else if (step.type === "response") {
      finalResponse = step;
    }
  }
  if (currentIteration) iterations.push(currentIteration);

  const totalToolCalls = iterations.reduce(
    (sum, it) => sum + it.toolCalls.reduce((s, tc) => s + (tc.type === "tool_calls" ? tc.tool_calls.length : 0), 0),
    0,
  );

  // Check for delegation tool calls (agent-to-agent interactions)
  const hasDelegations = iterations.some((it) =>
    it.toolCalls.some((tc) =>
      tc.type === "tool_calls" && tc.tool_calls.some((t) => DELEGATION_TOOL_NAMES.has(t.name)),
    ),
  );

  // Build delegation flow entries from the steps
  const delegationEntries: { type: string; toolName: string; args: Record<string, unknown>; result?: string }[] = [];
  if (hasDelegations) {
    for (const it of iterations) {
      for (const tc of it.toolCalls) {
        if (tc.type === "tool_calls") {
          for (const t of tc.tool_calls) {
            if (DELEGATION_TOOL_NAMES.has(t.name)) {
              const matchingResult = it.toolResults.find((r) => r.type === "tool_result" && r.tool_call_id === t.id);
              delegationEntries.push({
                type: t.name === "consult_agent" ? "Consult" : (t.name === "transfer_to_agent" || t.name === "handover_to_agent") ? "Transfer" : "Background Task",
                toolName: t.name,
                args: t.arguments,
                result: matchingResult?.type === "tool_result" ? matchingResult.content : undefined,
              });
            }
          }
        }
      }
    }
  }

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="content-base flex items-center gap-2">
              <Wrench className="h-4 w-4 text-accent" />
              {t_i18n("Agent Reasoning")}
              {iterations.length > 0 && (
                <Badge variant="outline" className="text-[9px] font-mono-plex text-default-secondary">
                  {iterations.length} {t_i18n(pluralize(iterations.length, "iteration"))} · {totalToolCalls} {t_i18n(pluralize(totalToolCalls, "tool call"))}
                </Badge>
              )}
            </CardTitle>
            <div className="flex items-center gap-1.5">
              {hasDelegations && (
                <Button
                  variant="ghost" size="sm"
                  className="h-7 content-caption text-default-secondary hover:text-default-primary gap-1.5"
                  onClick={() => setFlowOpen(true)}
                >
                  <ArrowRight className="h-3 w-3" />
                  {t_i18n("View Agent Flow")}
                </Button>
              )}
              <Button
                variant="ghost" size="sm"
                className="h-7 content-caption text-default-secondary hover:text-default-primary gap-1.5"
                onClick={() => setStepsOpen(true)}
              >
                <MessageSquare className="h-3 w-3" />
                {t_i18n("View Full Log")}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {/* Compact summary view */}
          <div className="space-y-2">
            {iterations.map((iteration, idx) => (
              <div key={idx} className="flex items-start gap-3">
                <div className="flex flex-col items-center">
                  <div className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/10 text-accent content-caption font-bold shrink-0">
                    {idx + 1}
                  </div>
                  {idx < iterations.length - 1 && (
                    <div className="w-px h-full min-h-[12px] bg-elevation-hover mt-1" />
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-1.5 py-0.5">
                  {iteration.toolCalls.map((tc) =>
                    tc.type === "tool_calls"
                      ? tc.tool_calls.map((call, ci) => (
                          <Badge key={ci} variant="outline" className="content-caption font-mono-plex gap-1 bg-elevation-hover">
                            <Wrench className="h-2.5 w-2.5 text-accent" />
                            {call.name}
                          </Badge>
                        ))
                      : null,
                  )}
                </div>
              </div>
            ))}
            {finalResponse && (
              <div className="flex items-start gap-3">
                <div className="flex h-6 w-6 items-center justify-center rounded-full bg-feedback-success-primary/10 shrink-0">
                  <CheckCircle2 className="h-3.5 w-3.5 text-feedback-success-primary" />
                </div>
                <span className="content-compact text-default-secondary py-1">{t_i18n("Final response generated")}</span>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Full agentic conversation dialog */}
      <Dialog open={stepsOpen} onOpenChange={setStepsOpen}>
        <DialogContent className="max-w-4xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <MessageSquare className="h-4 w-4 text-accent" />
              {t_i18n("Agent Reasoning Log")}
            </DialogTitle>
            <DialogDescription>
              {t_i18n("Full step-by-step log of the agentic loop: each LLM iteration, tool calls, and their results.")}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            {iterations.map((iteration, idx) => (
              <div key={idx} className="space-y-2">
                {/* Iteration header */}
                <div className="flex items-center gap-2">
                  <div className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/10 text-accent content-caption font-bold shrink-0">
                    {idx + 1}
                  </div>
                  <span className="content-compact font-medium text-default-secondary">{t_i18n("Iteration {number}", { number: idx + 1 })}</span>
                  <div className="flex-1 h-px bg-elevation-hover" />
                </div>

                {/* Tool calls from this iteration */}
                {iteration.toolCalls.map((tc) =>
                  tc.type === "tool_calls" ? (
                    <div key={`tc-${idx}`} className="space-y-2 ml-8">
                      {/* LLM thinking (if any content before tool calls) */}
                      {tc.content && (
                        <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-3">
                          <div className="flex items-center gap-1.5 mb-1.5">
                            <Bot className="h-3 w-3 text-accent" />
                            <span className="text-[9px] text-accent uppercase tracking-wider font-medium">{t_i18n("Agent Thinking")}</span>
                          </div>
                          <p className="content-compact text-default-secondary whitespace-pre-wrap">{tc.content}</p>
                        </div>
                      )}

                      {/* Individual tool calls */}
                      {tc.tool_calls.map((call, ci) => {
                        // Find matching result
                        const result = iteration.toolResults.find(
                          (r) => r.type === "tool_result" && r.tool_call_id === call.id,
                        );
                        return (
                          <div key={ci} className="rounded-sm border border-elevation-subtle-soft overflow-hidden">
                            {/* Tool call header */}
                            <div className="flex items-center gap-2 px-3 py-2 bg-accent/[0.03] border-b border-elevation-subtle-soft">
                              <ArrowRight className="h-3 w-3 text-accent" />
                              <span className="content-caption font-mono-plex font-medium text-accent">{call.name}</span>
                            </div>
                            {/* Arguments */}
                            {call.arguments && Object.keys(call.arguments).length > 0 && (
                              <div className="px-3 py-2 border-b border-elevation-subtle-soft bg-elevation-hover">
                                <p className="text-[9px] text-default-secondary uppercase tracking-wider font-medium mb-1">{t_i18n("Arguments")}</p>
                                <pre className="text-[11px] text-default-secondary font-mono-plex whitespace-pre-wrap">
                                  {JSON.stringify(call.arguments, null, 2)}
                                </pre>
                              </div>
                            )}
                            {/* Result */}
                            {result && result.type === "tool_result" && (
                              <div className="px-3 py-2 bg-elevation-hover">
                                <p className="text-[9px] text-default-secondary uppercase tracking-wider font-medium mb-1">{t_i18n("Result")}</p>
                                <ToolResultContent content={result.content} />
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  ) : null,
                )}
              </div>
            ))}

            {/* Final response */}
            {finalResponse && finalResponse.type === "response" && finalResponse.content && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <div className="flex h-6 w-6 items-center justify-center rounded-full bg-feedback-success-primary/10 shrink-0">
                    <CheckCircle2 className="h-3.5 w-3.5 text-feedback-success-primary" />
                  </div>
                  <span className="content-compact font-medium text-default-secondary">{t_i18n("Final Response")}</span>
                  <div className="flex-1 h-px bg-elevation-hover" />
                </div>
                <div className="ml-8 rounded-sm bg-feedback-success-primary/[0.03] border border-feedback-success-primary/10 p-3">
                  {/* Markers are stripped (not rendered as chips) here — the
                      Result card + Generated Files card already surface the
                      downloadable chips; the log is a raw-trace view. */}
                  <ChatMarkdown content={answerProse(finalResponse.content)} className="chat-markdown" />
                </div>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Agent Flow Dialog — shows delegation interactions extracted from agentic steps */}
      {hasDelegations && (
        <Dialog open={flowOpen} onOpenChange={setFlowOpen}>
          <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <ArrowRight className="h-4 w-4 text-accent" />
                {t_i18n("Agent Delegation Flow")}
              </DialogTitle>
              <DialogDescription>
                {t_i18n("{count} agent-to-agent {noun} detected in this assignment run.", { count: delegationEntries.length, noun: t_i18n(pluralize(delegationEntries.length, "delegation")) })}
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-3 mt-2">
              {delegationEntries.map((entry, idx) => {
                const agentId = String(entry.args.agent_id ?? entry.args.target_agent_id ?? "");
                const question = String(entry.args.question ?? entry.args.task ?? entry.args.message ?? "");
                return (
                  <div key={idx} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 space-y-2">
                    <div className="flex items-center gap-2">
                      <div className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/10 text-accent content-caption font-bold shrink-0">
                        {idx + 1}
                      </div>
                      <Badge variant="outline" className="text-[9px]">{t_i18n(entry.type)}</Badge>
                      {agentId && (
                        <span className="content-caption text-default-secondary font-mono-plex">→ {agentId.slice(0, 8)}...</span>
                      )}
                    </div>
                    {question && (
                      <p className="content-compact text-default-secondary ml-8 line-clamp-3">{question}</p>
                    )}
                    {entry.result && (
                      <div className="ml-8 rounded-sm bg-elevation-hover p-2">
                        <p className="text-[9px] text-default-secondary uppercase tracking-wider mb-1">{t_i18n("Result")}</p>
                        <p className="content-compact text-default-secondary line-clamp-5 whitespace-pre-wrap">{entry.result}</p>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

// ── Run metadata header ────────────────────────────────────────────────────

/**
 * Parse a timestamp that may be an ISO string, an epoch-seconds number, or an
 * epoch-milliseconds number. Schedule-recovery metadata historically stored a
 * raw epoch-seconds value which `new Date(n)` interprets as milliseconds —
 * rendering "Jan 1970". Treat sub-1e12 numbers as seconds to fix that.
 */
function parseFlexibleTimestamp(value: unknown): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    const ms = Math.abs(value) < 1e12 ? value * 1000 : value;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const d = new Date(String(value));
  return Number.isNaN(d.getTime()) ? null : d;
}

// Callers must pass a non-null, non-empty value (the tile loop skips
// null/undefined/"" before calling), so no null/undefined guard is needed here.
function formatMetaValue(key: string, value: unknown): string {
  if (key === "triggered_at" || key === "scheduled_at" || key === "occurred_at") {
    const d = parseFlexibleTimestamp(value);
    return d ? formatDateTime(d.toISOString()) : String(value);
  }
  // An A2A run's origin and credential form, by their names rather than their codes.
  if (key === "source" && value === "a2a") return "A2A";
  if (key === "a2a_provider" && typeof value === "string") return INTEGRATION_PROVIDER_NAMES[value] ?? value;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/**
 * Pick a column count that divides the tile count evenly so every row has the
 * same number of columns (no lonely trailing tile). Falls back to the layout
 * with the fullest last row for prime counts. Classes are static so Tailwind
 * keeps them in the build.
 */
const META_GRID_COLS: Record<number, string> = {
  1: "grid-cols-1",
  2: "grid-cols-2",
  3: "grid-cols-1 sm:grid-cols-3",
  4: "grid-cols-2 sm:grid-cols-4",
};

function balancedGridCols(n: number): string {
  if (n <= 1) return META_GRID_COLS[1];
  for (const c of [4, 3, 2]) {
    if (n % c === 0) return META_GRID_COLS[c];
  }
  // No even division (odd count, not a multiple of 3): choose 3 vs 4 columns by
  // whichever leaves the fuller last row (fewest empty trailing slots); tie → 4
  // for fewer rows. e.g. 13 → 3 cols (last row 1/3 full) instead of 4 (1/4).
  const empty4 = 4 - (n % 4);
  const empty3 = 3 - (n % 3);
  return empty4 <= empty3 ? META_GRID_COLS[4] : META_GRID_COLS[3];
}

interface MetaTile {
  key: string;
  label: string;
  icon: React.ElementType;
  value: string;
  subtitle?: string;
}

/** Build the Langfuse trace deep link, rejecting hosts that are not
 *  well-formed http(s) URLs (the host is persisted from admin input and
 *  rendered as an `href` for all authenticated users — never allow
 *  `javascript:` or other unexpected schemes). */
function buildTraceUrl(host: string, traceId: string): string | null {
  try {
    const url = new URL(host);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    return `${url.origin}${url.pathname.replace(/\/+$/, "")}/trace/${encodeURIComponent(traceId)}`;
  } catch {
    return null;
  }
}

/** Copyable Langfuse trace id row, with an external deep link when the
 *  Langfuse host is configured. The `GET /settings/langfuse` endpoint is
 *  admin-only, so the config query is gated on `isAdmin` — non-admins still
 *  get the copyable trace id, just without the "Open in Langfuse" deep link.
 *  Querying it as a non-admin would 403 and crash the page via the global
 *  `throwOnError`. */
function LangfuseTraceRow({ traceId }: { traceId: string }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { data: langfuse } = useLangfuseConfig({ enabled: isAdmin });
  const [copied, setCopied] = useState(false);
  const traceUrl =
    langfuse?.enabled && langfuse.host ? buildTraceUrl(langfuse.host, traceId) : null;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(traceId);
      setCopied(true);
      setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
    } catch {
      console.warn("Clipboard write failed");
    }
  };

  return (
    <div className="mt-2 flex items-center gap-2 rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-3 py-2.5">
      <div className="flex items-center gap-1.5 shrink-0">
        <Activity className="h-2.5 w-2.5 text-default-secondary" />
        <span className="text-[9px] text-default-secondary uppercase tracking-wider font-medium">
          {t_i18n("Langfuse Trace")}
        </span>
      </div>
      <code className="flex-1 truncate font-mono-plex text-[11px] text-default-secondary">{traceId}</code>
      <Tip content={copied ? t_i18n("Copied!") : t_i18n("Copy trace ID")}>
        <button
          type="button"
          aria-label={t_i18n("Copy trace ID")}
          onClick={handleCopy}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm text-default-secondary transition-colors hover:bg-elevation-hover hover:text-default-primary"
        >
          {copied ? <Check className="h-3 w-3 text-feedback-success-primary" /> : <Copy className="h-3 w-3" />}
        </button>
      </Tip>
      {traceUrl && (
        <Tip content={t_i18n("Open in Langfuse")}>
          <a
            href={traceUrl}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={t_i18n("Open trace in Langfuse")}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm text-default-secondary transition-colors hover:bg-accent/10 hover:text-accent"
          >
            <ExternalLink className="h-3 w-3" />
          </a>
        </Tip>
      )}
    </div>
  );
}

function RunOverviewCard({ run }: { run: AssignmentRunDetail }) {
  const { t_i18n } = useFormatter();
  const [payloadOpen, setPayloadOpen] = useState(false);
  // The local trace page (/traces/:id) is admin-only (AdminRoute), so only
  // show the trace link to admins to avoid a dead link for regular users.
  const { isAdmin } = useAuth();
  const ctx = (run.trigger_context || {}) as Record<string, unknown>;

  const triggerType = typeof ctx.type === "string" ? ctx.type : null;
  const cfg = triggerType
    ? TRIGGER_TYPE_CONFIG[triggerType] || TRIGGER_TYPE_CONFIG.webhook
    : null;
  const TriggerIcon = cfg?.icon;
  const triggerLabel = triggerType ? TRIGGER_LABELS[triggerType] || triggerType : null;

  const bodyKey = Object.keys(ctx).find((k) => k === "body" || k === "payload");
  const headersKey = Object.keys(ctx).find((k) => k === "headers");
  // Base payload presence on actual data, not just the key existing — an
  // empty/null `body`/`headers` must not surface a "Full Payload" button that
  // opens an empty dialog.
  const hasHeaders = Boolean(headersKey && ctx[headersKey]);
  const hasBody = Boolean(bodyKey && ctx[bodyKey]);

  const isRunning = run.status === "running" || run.status === "pending";
  const duration =
    run.started_at && run.completed_at
      ? Math.round(
          (new Date(run.completed_at).getTime() - new Date(run.started_at).getTime()) / 1000,
        )
      : null;

  // ── Build one unified, uniform tile list: run timeline + trigger metadata ──
  const tiles: MetaTile[] = [
    { key: "created", label: "Created", icon: Calendar, value: formatDateTime(run.created_at) },
    {
      key: "started",
      label: "Started",
      icon: Zap,
      value: run.started_at ? formatDateTime(run.started_at) : t_i18n("Waiting…"),
    },
    {
      key: "completed",
      label: "Completed",
      icon: CheckCircle2,
      value: run.completed_at
        ? formatDateTime(run.completed_at)
        : isRunning
          ? t_i18n("In progress…")
          : t_i18n("Pending"),
      subtitle: duration !== null ? t_i18n("Duration: {duration}", { duration: formatDurationShort(duration) }) : undefined,
    },
  ];

  const triggeredAt = parseFlexibleTimestamp(ctx.triggered_at);
  for (const key of Object.keys(ctx)) {
    if (!isTriggerMetadataKey(key)) continue;
    const val = ctx[key];
    if (val === null || val === undefined || val === "") continue;
    // De-dupe: for schedule triggers `scheduled_at` is usually identical to
    // `triggered_at` — drop the redundant tile.
    if (key === "scheduled_at" && triggeredAt) {
      const sched = parseFlexibleTimestamp(val);
      if (sched && Math.abs(sched.getTime() - triggeredAt.getTime()) < 120_000) continue;
    }
    tiles.push({
      key,
      label: KEY_LABELS[key] || key,
      icon: KEY_ICONS[key] || Link2,
      value: typeof val === "boolean" ? t_i18n(val ? "Yes" : "No") : formatMetaValue(key, val),
    });
  }

  // Token usage tile — only when accounting captured usage for this run.
  const inputTokens = run.total_input_tokens ?? 0;
  const outputTokens = run.total_output_tokens ?? 0;
  if (inputTokens > 0 || outputTokens > 0) {
    tiles.push({
      key: "tokens",
      label: "Tokens",
      icon: Cpu,
      value: formatCompactNumber(inputTokens + outputTokens),
      subtitle: t_i18n("{input} in · {output} out", { input: formatCompactNumber(inputTokens), output: formatCompactNumber(outputTokens) }),
    });
  }

  const gridCols = balancedGridCols(tiles.length);
  const hasPayload = hasHeaders || hasBody;

  return (
    <Card className="border-elevation-subtle-soft">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="content-base flex items-center gap-2">
            <FileText className="h-4 w-4 text-accent" />
            <span>{t_i18n("Overview")}</span>
            {cfg && TriggerIcon && triggerLabel && (
              <Badge variant="outline" className={cn("gap-1 text-[9px] font-medium", cfg.color)}>
                <TriggerIcon className={cn("h-2.5 w-2.5", cfg.color)} />
                {t_i18n(triggerLabel)}
              </Badge>
            )}
          </CardTitle>
          {hasPayload && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 content-caption text-default-secondary hover:text-default-primary gap-1.5"
              onClick={() => setPayloadOpen(true)}
            >
              <Globe className="h-3 w-3" />
              {t_i18n("Full Payload")}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <div className={cn("grid gap-2", gridCols)}>
          {tiles.map((tile) => {
            const TileIcon = tile.icon;
            return (
              <div
                key={tile.key}
                className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-3 py-2.5"
              >
                <div className="flex items-center gap-1.5 mb-0.5">
                  <TileIcon className="h-2.5 w-2.5 text-default-secondary" />
                  <span className="text-[9px] text-default-secondary uppercase tracking-wider font-medium">
                    {t_i18n(tile.label)}
                  </span>
                </div>
                <Tip content={tile.value}>
                  <p className="content-compact text-default-secondary truncate">{tile.value}</p>
                </Tip>
                {tile.subtitle && (
                  <p className="content-caption text-default-secondary mt-0.5">{tile.subtitle}</p>
                )}
              </div>
            );
          })}
        </div>
        {run.llm_trace_id && isAdmin ? (
          <Link
            to={`/observability/traces/${run.llm_trace_id}`}
            className="mt-2 flex items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2.5 content-base transition-colors hover:border-accent/30 hover:bg-accent/5"
          >
            <Scan className="h-4 w-4 text-accent" />
            <span>{t_i18n("Local Trace")}</span>
            <code className="content-compact font-mono-plex text-default-disabled">{run.llm_trace_id.slice(0, 8)}…</code>
            <ChevronRight className="ml-auto h-3 w-3 text-default-disabled" />
          </Link>
        ) : null}
        {run.langfuse_trace_id && <LangfuseTraceRow traceId={run.langfuse_trace_id} />}
      </CardContent>

      {/* Full Payload Dialog */}
      <Dialog open={payloadOpen} onOpenChange={setPayloadOpen}>
        <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Globe className="h-4 w-4 text-feedback-warning-primary" />
              {t_i18n("Full Trigger Payload")}
            </DialogTitle>
            <DialogDescription>{t_i18n("Complete trigger context including headers and body.")}</DialogDescription>
          </DialogHeader>

          {hasHeaders && headersKey ? (
            <div className="space-y-1.5">
              <p className="content-caption text-default-secondary uppercase tracking-wider font-medium">{t_i18n("Headers")}</p>
              <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-3 max-h-[200px] overflow-auto">
                <pre className="text-[11px] text-default-secondary font-mono-plex whitespace-pre-wrap">
                  {JSON.stringify(ctx[headersKey], null, 2)}
                </pre>
              </div>
            </div>
          ) : null}

          {hasBody && bodyKey ? (
            <div className="space-y-1.5">
              <p className="content-caption text-default-secondary uppercase tracking-wider font-medium">{t_i18n("Request Body")}</p>
              <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-3 max-h-[60vh] overflow-auto">
                <pre className="text-[11px] text-default-secondary font-mono-plex whitespace-pre-wrap">
                  {JSON.stringify(ctx[bodyKey], null, 2)}
                </pre>
              </div>
            </div>
          ) : null}

          {!hasHeaders && !hasBody && (
            <p className="content-base text-default-secondary italic py-4 text-center">
              {t_i18n("No payload or headers available for this trigger.")}
            </p>
          )}
        </DialogContent>
      </Dialog>
    </Card>
  );
}

/**
 * What the Result card says about a `completed` run that stopped before its
 * task was done, from its `stop_reason`; null when it ended on its own
 * terms. The run keeps its answer, so this reads as partial, never as failed.
 */
function unfinishedRunNotice(
  stopReason: string | null | undefined,
  t_i18n: ReturnType<typeof useFormatter>["t_i18n"],
): string | null {
  switch (stopReason) {
    case null:
    case undefined:
    case "":
      return null;
    case "max_iterations":
      return t_i18n("This run used all its Max iterations before the agent finished: the result is a partial summary.");
    case "deadline":
      return t_i18n("This run reached its timeout before the agent finished: the result is a partial summary.");
    case "budget_exhausted":
      return t_i18n("The run budget cut this result short: it is partial.");
    case "degenerate_repetition":
      return t_i18n("The model got stuck repeating the same text, so this run was stopped: the result is partial.");
    case "timeout":
      return t_i18n("The model did not answer in time, so this run stopped before the agent finished.");
    case "waiting_on_tasks":
      return t_i18n("This run stopped while tasks it delegated were still running: the result is partial.");
    case "integrations_unreadable":
      return t_i18n("The agent finished, but none of its integrations could be read: the result is not assessable.");
    default:
      return t_i18n("This run stopped before the agent finished: the result is partial.");
  }
}

export default function RunDetail() {
  const { t_i18n } = useFormatter();
  useTick(REFETCH_INTERVAL_SLOW); // keep relative timestamps fresh
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: run, isLoading } = useAssignmentRunDetail(id || "", { notFoundIsOrdinary: true });
  const { isAdmin } = useAuth();
  const respondHitl = useRespondToHumanInput();
  const submitApprovals = useSubmitApprovalDecisions();
  const dismissRun = useDismissRun();
  const deleteRun = useDeleteAssignmentRun();
  const followupRun = useFollowupRun();
  const followupChat = useFollowupChat();
  const chatSendEnabled = useChatSendEnabled();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const [followupPrompt, setFollowupPrompt] = useState("");
  // How a follow-up runs: a new background run (default) or an interactive
  // chat seeded with this run's history (for troubleshooting).
  const [followupMode, setFollowupMode] = useState<"background" | "chat">("background");
  const [promptOpen, setPromptOpen] = useState(false);
  const followupSpeech = useSpeechToText({
    onTranscript: (text) =>
      setFollowupPrompt((prev) => (prev ? prev + " " : "") + text.trim()),
  });

  const hasChildren = (run?.child_run_count ?? 0) > 0;
  const { data: descendants, isLoading: descendantsLoading } = useDescendantRuns(hasChildren ? id ?? null : null);

  if (isLoading) return <PageSkeleton cards={3} />;
  // An unknown id (or one you cannot read) is an address no page answers (#797 S4).
  if (!run) return <RouteNotFound />;

  const statusCfg = runStatusConfig(run);
  // A failed run the platform ended: its error block reads neutral, as its badge does.
  const runInterrupted = run.outcome === "interrupted";
  const StatusIcon = statusCfg.icon;
  const AgentIcon = ICON_MAP[run.agent_icon] ?? Bot;
  const isRunning = run.status === "running" || run.status === "pending";
  const isAwaitingHuman = run.status === "awaiting_human";
  const isTerminal = ["completed", "failed", "skipped"].includes(run.status);
  // A completed run that ended on a limit is shown as partial.
  const unfinishedNotice = run.status === "completed" ? unfinishedRunNotice(run.stop_reason, t_i18n) : null;
  // Waiting while agentic AI is unavailable here: held, not failed.
  const heldReason = runHeldReason(run);
  // A completed external run whose A2A task waits on its caller's answer.
  const waitingOnCaller = run.a2a_exchange?.asked?.state === "waiting";
  const a2aNoResult = a2aNoResultText(run.a2a_exchange, t_i18n);
  // Pending only while nobody has responded yet: the respond endpoint stores
  // the response immediately, so the panel flips to the answered state even
  // before the engine picks the run back up.
  const isHitlPending = isAwaitingHuman && !run.human_input_response;
  // A gated tool call pauses with structured proposals; request_human_input
  // pauses with a free-text question. Both land in awaiting_human and both set
  // ``human_input_question``, so the presence of proposals is what decides
  // which prompt to render — same rule as the run card.
  const pendingApprovals = run.pending_approvals ?? [];
  // Same rule as the run card, and for the same reason: the proposals outlive
  // the decision, because resuming is queued rather than immediate. Without
  // this the panel stayed actionable after an answer and a reload could submit
  // a second one (T225).
  const isApprovalPending =
    isHitlPending && pendingApprovals.length > 0 && !run.approval_answered;
  /* The free-text form is for a run that asked a QUESTION, never for one
     awaiting structured decisions — `/respond` refuses free text while
     proposals are outstanding. Keyed on the proposals rather than on
     `isApprovalPending`: that goes false the moment `approval_answered` is
     recorded, while the proposals survive until the worker resumes, so the
     window between the two offered a form the API would reject. */
  const isFreeTextPending = isHitlPending && pendingApprovals.length === 0;
  // Company-managed runs: everyone sees the pause, only admins can resolve it.
  const canInteractHitl = !run.is_company_managed || isAdmin;
  // Resolved HITL rounds — from the durable history, with a legacy fallback
  // for runs resolved before the history column existed (their live fields
  // survived because they never went through the resume-clear path).
  const hitlEntries: HumanInputHistoryEntry[] =
    run.human_input_history && run.human_input_history.length > 0
      ? run.human_input_history
      : run.human_input_question && run.human_input_response && !isHitlPending
        ? [{
            question: run.human_input_question,
            response: run.human_input_response === "[Dismissed by user]" ? null : run.human_input_response,
            resolution: run.human_input_response === "[Dismissed by user]" ? "dismissed" : "answered",
            responded_at: "",
            responded_by: null,
            responded_by_id: null,
          }]
        : [];
  const lastHitl = hitlEntries.length > 0 ? hitlEntries[hitlEntries.length - 1] : null;
  const chipAttachments = (run.attachments ?? []).map(toChipAttachment);
  // The run of a scheduled work item: titled and linked by its item.
  const runContext = (run.trigger_context || {}) as Record<string, unknown>;
  const scheduledItemId =
    runContext.type === "scheduled_work_item" ? String(runContext.work_item_id ?? "") : "";
  // A run that wrote one file name twice: the chips number the revisions.
  const fileRevisions = conversationRevisions([{ created_at: run.created_at, attachments: chipAttachments }]);

  return (
    <div className="space-y-6">
      {/* Back + Header */}
      <div>
        {/* The opener may name where Back goes (the fleet timeline: "Back to Command Center"). */}
        <LocationBackButton to={-1} label="Back" className="mb-4" />
        <div className="flex items-center justify-between gap-4">
          {/* The title block shrinks before the header actions; its badges stay on one line
              and the agent name is cut with a tooltip (UX r1 B1 on #1590). */}
          <div className="flex min-w-0 flex-1 items-center gap-4">
            <div className={cn(
              "flex h-14 w-14 shrink-0 items-center justify-center rounded-sm",
              isRunning ? "bg-accent/10" : unfinishedNotice || waitingOnCaller ? "bg-feedback-warning-primary/10" : run.status === "completed" ? "bg-feedback-success-primary/10" : "bg-feedback-error-primary/10",
            )}>
              <AgentIcon className={cn("h-7 w-7", isRunning ? "text-accent" : unfinishedNotice ? "text-feedback-warning-primary" : statusCfg.color)} />
            </div>
            <div className="min-w-0">
              <h1 className="text-2xl font-bold">
                {run.assignment_name
                  || (scheduledItemId
                    ? String(runContext.title || t_i18n("Scheduled work item"))
                    : t_i18n("Delegation Run"))}
              </h1>
              <div className="flex items-center gap-2 mt-1">
                <Badge variant={statusCfg.badge} className="shrink-0 gap-1 whitespace-nowrap">
                  <StatusIcon className={cn("h-3 w-3", isRunning && "animate-spin")} />
                  {t_i18n(statusCfg.label)}
                </Badge>
                {heldReason && <HeldBadge reason={heldReason} />}
                {unfinishedNotice && (
                  <Badge variant="outline" className="shrink-0 gap-1 whitespace-nowrap text-feedback-warning-primary border-feedback-warning-primary/40">
                    <AlertTriangle className="h-3 w-3" />
                    {run.stop_reason === "integrations_unreadable" ? t_i18n("Not assessable") : t_i18n("Partial result")}
                  </Badge>
                )}
                {run.parent_run_id && !run.assignment_name && run.parent_agent_name && (
                  <span className="content-compact text-accent flex shrink-0 items-center gap-1 whitespace-nowrap">
                    {t_i18n("Delegated from")} <span className="font-medium">{run.parent_agent_name}</span>
                  </span>
                )}
                <TruncatedText text={run.agent_name} className="content-compact text-default-secondary" />
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {run.assignment_id && (
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => navigate(`/agents/assignments/${run.assignment_id}`)}>
                <Zap className="h-3.5 w-3.5" /> {t_i18n("View Assignment")}
              </Button>
            )}
            {scheduledItemId && (
              <Button
                variant="secondary"
                size="sm"
                className="gap-1.5"
                onClick={() => navigate(workItemPath(scheduledItemId))}
              >
                <CalendarClock className="h-3.5 w-3.5" /> {t_i18n("View the scheduled work item")}
              </Button>
            )}
            {/* The Trace Explorer is admin-only (AdminRoute), like the Local
                Trace row below. */}
            {isAdmin && (
              <Tip content={t_i18n("Investigate this run in the Trace Explorer")}>
                <Button asChild variant="secondary" size="sm" className="gap-1.5">
                  <Link to={assignmentRunTracesPath(run.id)}>
                    <Scan className="h-3.5 w-3.5" /> {t_i18n("View Traces")}
                  </Link>
                </Button>
              </Tip>
            )}
            {run.parent_run_id && (
              <Button variant="default" size="sm" className="gap-1.5" onClick={() => navigate(`/operations/executions/${run.parent_run_id}`)}>
                <CornerLeftUp className="h-3.5 w-3.5" /> {t_i18n("Parent Run")}
              </Button>
            )}
            <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => navigate(`/agents/${run.agent_id}`)}>
              <AgentIcon className="h-3.5 w-3.5" /> {t_i18n("View Agent")}
            </Button>
            <Button aria-label={t_i18n("Delete run")} variant="ghost" size="sm" className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={async () => { if (await confirm({ title: "Delete this run?", description: "This action cannot be undone.", confirmLabel: "Delete" })) { deleteRun.mutate(run.id, { onSuccess: () => navigate(-1) }); } }}>
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>

      {/* Unified run metadata header — timeline + trigger context in one
          balanced, equal-column grid (replaces the old 3-card row + separate
          Trigger Event card). */}
      <RunOverviewCard run={run} />

      {/* An external run's exchange with the agent that submitted its A2A
          task: the question it asked, and the run the answer started. */}
      <A2ARunExchange exchange={run.a2a_exchange} />

      {/* Someone who cannot answer the run asked those who can, from the card of
          its pasted link. Listed to the people who may answer it. */}
      <RunActionRequests runId={run.id} enabled={isHitlPending && canInteractHitl} />

      {/* The pause on the one decision card (#842, S3): a tool approval
          when proposals are pending (they take precedence, exactly as on the
          run card: a gated pause sets ``human_input_question`` too), else the
          run's question with its free text. */}
      {(isApprovalPending || (isFreeTextPending && run.human_input_question)) && (
        <RunDecisionCard
          // A failed approval remounts the card so its controls answer again.
          key={`${run.id}-${submitApprovals.isError ? "failed" : "ok"}`}
          runId={run.id}
          question={run.human_input_question}
          proposals={isApprovalPending ? pendingApprovals : []}
          canAnswer={canInteractHitl}
          pending={submitApprovals.isPending || respondHitl.isPending || dismissRun.isPending}
          onApprove={(runId, decisions) =>
            submitApprovals.mutate(
              { runId, decisions },
              {
                onSuccess: () => toast({ title: t_i18n("Decision recorded: assignment resuming"), variant: "success" }),
                onError: (err) =>
                  toast({ title: t_i18n("Could not submit decisions"), description: err.message, variant: "error" }),
              },
            )
          }
          onRespond={(runId, response) =>
            respondHitl.mutate(
              { runId, response },
              { onSuccess: () => toast({ title: t_i18n("Response sent: assignment resuming"), variant: "success" }) },
            )
          }
          onDismiss={(runId) =>
            dismissRun.mutate(runId, { onSuccess: () => toast({ title: t_i18n("Run dismissed"), variant: "success" }) })
          }
        />
      )}

      {/* Resolved HITL rounds — persisted history: answered/dismissed state,
          the user's reply (identity + timestamp), and the continuation system
          notice. Survives panel re-opens because it renders stored data, and
          stays visible while a LATER round is pending (multi-round context). */}
      {hitlEntries.length > 0 && (
        <Card className={cn(lastHitl?.resolution === "answered" ? "border-feedback-success-primary/20" : "border-elevation-subtle-soft")}>
          <CardHeader>
            <CardTitle className="content-base flex items-center gap-2">
              <Hand className="h-4 w-4 text-default-secondary" />
              {t_i18n("Human Input")}
              {lastHitl?.resolution === "answered" ? (
                <Badge variant="success" className="gap-1 text-[9px]">
                  <CheckCircle2 className="h-2.5 w-2.5" />
                  {t_i18n("Answered")}
                </Badge>
              ) : (
                <Badge variant="outline" className="gap-1 text-[9px] text-default-secondary">
                  <X className="h-2.5 w-2.5" />
                  {t_i18n("Dismissed")}
                </Badge>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {hitlEntries.map((entry, idx) => (
              <div key={idx} className="space-y-2">
                {entry.question && (
                  <div className="rounded-sm bg-elevation-hover p-3">
                    <p className="content-caption text-default-secondary uppercase tracking-wider mb-1">{t_i18n("Agent asked")}</p>
                    <div className="content-compact text-default-secondary">
                      <ChatMarkdown content={entry.question} />
                    </div>
                  </div>
                )}
                {entry.resolution === "answered" ? (
                  <div className="rounded-sm bg-feedback-success-primary/5 p-3">
                    <div className="flex items-center gap-2 mb-1.5">
                      <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-feedback-success-primary/10">
                        <User className="h-3.5 w-3.5 text-feedback-success-primary" />
                      </div>
                      <span className="content-compact font-medium text-default-secondary">{entry.responded_by ?? t_i18n("You")}</span>
                      {entry.responded_at && (
                        <span className="content-caption text-default-secondary">{formatDateTime(entry.responded_at)}</span>
                      )}
                    </div>
                    <p className="content-compact text-default-secondary whitespace-pre-wrap">{entry.response}</p>
                  </div>
                ) : (
                  <div className="rounded-sm bg-elevation-hover p-3">
                    <p className="content-compact text-default-secondary">
                      {t_i18n("Dismissed")}{entry.responded_by ? ` ${t_i18n("by {name}", { name: entry.responded_by })}` : ""}
                      {entry.responded_at ? ` · ${formatDateTime(entry.responded_at)}` : ""} — {t_i18n("no action taken.")}
                    </p>
                  </div>
                )}
              </div>
            ))}
            {/* Continuation notice only for the CURRENT answered transition —
                never while a later HITL round is waiting for input again. */}
            {lastHitl?.resolution === "answered" && !isHitlPending && (
              <div className="flex items-start gap-2 rounded-sm bg-accent/[0.04] border border-accent/10 p-3">
                <Info className="h-3.5 w-3.5 text-accent shrink-0 mt-0.5" />
                <p className="content-compact text-default-secondary">{t_i18n(HITL_CONTINUATION_MESSAGE)}</p>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Result summary — always show the card */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="content-base flex items-center gap-2">
              {isRunning ? (
                <><Spinner size="sm" tone="inherit" className="text-accent" /> {t_i18n("Result")}</>
              ) : run.status === "failed" ? (
                <><StatusIcon className={cn("h-4 w-4", statusCfg.color)} /> {t_i18n("Result")}</>
              ) : unfinishedNotice ? (
                <><AlertTriangle className="h-4 w-4 text-feedback-warning-primary" /> {t_i18n("Result")}</>
              ) : waitingOnCaller ? (
                <><StatusIcon className={cn("h-4 w-4", statusCfg.color)} /> {t_i18n("Result")}</>
              ) : (
                <><CheckCircle2 className="h-4 w-4 text-feedback-success-primary" /> {t_i18n("Result")}</>
              )}
            </CardTitle>
            <div className="flex items-center gap-2">
              {(run.status === "completed" || run.status === "failed") && (
                <AnnotationButtons targetType="assignment_run" targetId={run.id} label="Was this result useful?" />
              )}
              <Button
                variant="ghost" size="sm"
                className="h-7 content-caption text-default-secondary hover:text-default-primary gap-1.5"
                onClick={() => setPromptOpen(true)}
              >
                <FileText className="h-3 w-3" />
                {t_i18n("Show Full Prompt")}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {unfinishedNotice && (
            <div className="flex items-start gap-2 rounded-sm bg-feedback-warning-primary/5 p-3">
              <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
              <p className="content-compact text-default-secondary">{unfinishedNotice}</p>
            </div>
          )}
          {run.result_summary ? (
            // ``InlineFileContent`` replaces ``[[FILE:uuid]]`` markers in the
            // summary with the same inline file chips the chat view renders,
            // instead of leaking raw ids into the prose. The full "Generated
            // Files" card below stays as the at-a-glance list of all
            // deliverables.
            <FileRevisionsContext.Provider value={fileRevisions}>
              <InlineFileContent content={run.result_summary} attachments={chipAttachments} />
            </FileRevisionsContext.Provider>
          ) : isRunning ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              {/* The ring is sized from the 32 px (xl) glyph's own box, so the
                  pulse stays centred on it; it stops under reduced motion. */}
              <div className="relative mb-4 flex size-8 items-center justify-center">
                <Spinner size="xl" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
                <div aria-hidden className="absolute inset-0 rounded-full bg-accent/10 animate-ping motion-reduce:animate-none" />
              </div>
              <p className="content-base text-default-secondary">{t_i18n("Assignment is running...")}</p>
              <p className="content-compact text-default-secondary mt-1">{t_i18n("The agent is processing this assignment. Results will appear here automatically when complete.")}</p>
            </div>
          ) : (
            <p className="content-base text-default-secondary italic py-4 text-center">{a2aNoResult ?? t_i18n("No result produced.")}</p>
          )}
          {/* The ROI review row: under the result, never beside the thumbs (#9365). */}
          <OutcomeReview block={run.roi} />
        </CardContent>
      </Card>

      {/* Generated files — produced by the agent's ``generate_file`` /
          custom-tool ``$output_files`` paths during this run. Shown right
          after the Result card so they read as the deliverables that go
          with the summary. We reuse the same ``FileChip`` component the
          chat view uses so the visual + auth'd download flow stays
          identical between channels (chat / run detail). */}
      {run.attachments && run.attachments.length > 0 && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="content-base flex items-center gap-2">
                <FileText className="h-4 w-4 text-accent" />
                {t_i18n("Generated Files ({count})", { count: run.attachments.length })}
              </CardTitle>
              {/* The same files in the cross-context repository (Work > Files),
                  scoped to this run — where they can be grouped, searched and
                  deleted alongside every other run's output. */}
              <Link
                to={`/files?run=${encodeURIComponent(run.id)}`}
                className="flex items-center gap-1 content-compact text-default-secondary transition-colors hover:text-default-primary"
              >
                {t_i18n("Open in Files")}
                <ExternalLink className="h-3 w-3" />
              </Link>
            </div>
          </CardHeader>
          <CardContent>
            <FileRevisionsContext.Provider value={fileRevisions}>
              {/* Deliverables as cards; working files (scratch files, the
                  pictures a tool made to check the work) behind one
                  collapsed line. */}
              <div className="flex flex-wrap items-center gap-2">
                {chipAttachments
                  .filter((att) => fileTag(att) !== "working_file")
                  .map((att) => (
                    <FileChip key={att.file_id} att={att} />
                  ))}
              </div>
              <WorkingFilesDisclosure files={chipAttachments.filter((att) => fileTag(att) === "working_file")} />
            </FileRevisionsContext.Provider>
          </CardContent>
        </Card>
      )}

      {/* Error */}
      {run.error && runInterrupted && (
        <Card className="border-elevation-subtle">
          <CardHeader>
            <CardTitle className="content-base flex items-center gap-2 text-default-primary"><RotateCcw className="h-4 w-4 text-default-secondary" /> {t_i18n("Interrupted")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <p className="content-compact text-default-secondary">{t_i18n("It was interrupted: the platform restarted or stopped the process running it.")}</p>
            {/* The platform's own words, in English: behind a disclosure, under the translated line. */}
            <details data-testid="run-interrupted-details">
              <DisclosureSummary>{t_i18n("Details")}</DisclosureSummary>
              <pre className="mt-1.5 content-compact text-default-secondary whitespace-pre-wrap bg-elevation-hover rounded-sm p-3">{run.error}</pre>
            </details>
          </CardContent>
        </Card>
      )}
      {/* A cancelled run reads neutral, its reason localised. */}
      {run.error && runErrorIsNeutral(run) && !runInterrupted && (
        <RunCancelledCard run={run} />
      )}
      {run.error && !runErrorIsNeutral(run) && (
        <Card className="border-feedback-error-primary/20">
          <CardHeader>
            <CardTitle className="content-base flex items-center gap-2 text-feedback-error-primary"><AlertCircle className="h-4 w-4" /> {t_i18n("Error")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {/* The classified cause above the raw text: it answers
                "whose problem is this" at a glance, which the error string
                rarely does. Absent on runs that failed before the classifier
                existed — those show the text alone rather than a misleading
                "unattributed" label. */}
            {run.failure_cause && (
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="text-feedback-error-primary border-feedback-error-primary/40">
                  {formatFailureCause(run.failure_cause)}
                </Badge>
                {run.failure_determination === "heuristic" && (
                  <Tip content={t_i18n("Inferred from the error text rather than read from a status code or an exception type: indicative, not authoritative.")}>
                    <span className="text-[11px] text-default-secondary italic">{t_i18n("inferred")}</span>
                  </Tip>
                )}
              </div>
            )}
            <pre className="content-compact text-feedback-error-primary whitespace-pre-wrap bg-feedback-error-primary/5 rounded-sm p-3">{run.error}</pre>
          </CardContent>
        </Card>
      )}

      {/* Follow-up — available on terminal runs */}
      {isTerminal && (
        <Card className="border-accent/20">
          <CardHeader>
            <CardTitle className="content-base flex items-center gap-2">
              <MessageSquare className="h-4 w-4 text-accent" /> {t_i18n("Follow-up")}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const trimmed = followupPrompt.trim();
                if (followupMode === "chat") {
                  // Interactive: open a chat seeded with this run's history and
                  // carry the typed prompt over as the composer draft so the
                  // user lands in the conversation ready to continue.
                  followupSpeech.stop();
                  followupChat.mutate(
                    { runId: run.id, prompt: trimmed || undefined },
                    {
                      onSuccess: (res) => {
                        if (trimmed) primeChatDraft(trimmed, { conversationId: res.conversation_id });
                        setFollowupPrompt("");
                        navigate(`/chat?c=${res.conversation_id}`);
                      },
                      onError: (err) => {
                        toast({ title: err.message || t_i18n("Failed to open chat"), variant: "error" });
                      },
                    },
                  );
                  return;
                }
                // Background: create a new run from this result.
                if (trimmed) {
                  followupSpeech.stop();
                  followupRun.mutate(
                    { runId: run.id, prompt: trimmed },
                    {
                      onSuccess: (newRun) => {
                        toast({ title: t_i18n("Follow-up run created"), variant: "success" });
                        setFollowupPrompt("");
                        navigate(`/operations/executions/${newRun.id}`);
                      },
                      onError: (err) => {
                        toast({ title: err.message || t_i18n("Failed to create follow-up"), variant: "error" });
                      },
                    },
                  );
                }
              }}
              className="space-y-3"
            >
              {/* Mode selector — only when chat is available on this platform */}
              {chatSendEnabled && (
                <div>
                  <div className="inline-flex rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-0.5 content-compact">
                    <button
                      type="button"
                      aria-pressed={followupMode === "background"}
                      onClick={() => setFollowupMode("background")}
                      className={cn(
                        "flex items-center gap-1.5 rounded-sm px-3 py-1.5 transition-colors",
                        followupMode === "background"
                          ? "bg-accent/15 text-accent"
                          : "text-default-secondary hover:text-default-primary",
                      )}
                    >
                      <Play className="h-3.5 w-3.5" /> {t_i18n("Background run")}
                    </button>
                    <button
                      type="button"
                      aria-pressed={followupMode === "chat"}
                      onClick={() => setFollowupMode("chat")}
                      className={cn(
                        "flex items-center gap-1.5 rounded-sm px-3 py-1.5 transition-colors",
                        followupMode === "chat"
                          ? "bg-accent/15 text-accent"
                          : "text-default-secondary hover:text-default-primary",
                      )}
                    >
                      <MessageSquare className="h-3.5 w-3.5" /> {t_i18n("Interactive chat")}
                    </button>
                  </div>
                  <p className="mt-1.5 text-[11px] text-default-secondary">
                    {followupMode === "chat"
                      ? t_i18n("Opens a chat seeded with this run's task, result and tool trace so you can troubleshoot what happened.")
                      : t_i18n("Creates a new background run that continues from this run's result.")}
                  </p>
                </div>
              )}
              <div className="relative">
                <textarea
                  className="flex min-h-[80px] w-full rounded-sm border border-accent/20 bg-elevation-default px-3 py-2 pr-10 content-base placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                  placeholder={
                    followupMode === "chat"
                      ? t_i18n("Optional: type your first question, or just open the chat to review this run…")
                      : t_i18n("Ask the agent for a follow-up based on this run's result...")
                  }
                  value={followupPrompt}
                  onChange={(e) => setFollowupPrompt(e.target.value)}
                />
                {followupSpeech.isSupported && (
                  <Tip content={followupSpeech.isListening ? t_i18n("Stop dictation") : t_i18n("Dictate follow-up")}>
                    <button
                      type="button"
                      onClick={followupSpeech.toggle}
                      className={cn(
                        "absolute right-2 top-2 flex h-7 w-7 items-center justify-center rounded-sm transition-colors",
                        followupSpeech.isListening
                          ? "bg-accent/20 text-accent animate-pulse"
                          : "text-default-secondary hover:text-accent hover:bg-accent/10",
                      )}
                    >
                      {followupSpeech.isListening ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
                    </button>
                  </Tip>
                )}
              </div>
              <Button
                type="submit"
                disabled={
                  followupRun.isPending ||
                  followupChat.isPending ||
                  (followupMode === "background" && !followupPrompt.trim())
                }
                className="gap-1.5 bg-accent/20 text-accent hover:bg-accent/30"
                onClick={() => followupSpeech.stop()}
              >
                {followupMode === "chat" ? (
                  <>
                    <MessageSquare className="h-3.5 w-3.5" />
                    {followupChat.isPending ? t_i18n("Opening…") : t_i18n("Open in Chat")}
                  </>
                ) : (
                  <>
                    <Send className="h-3.5 w-3.5" />
                    {followupRun.isPending ? t_i18n("Sending...") : t_i18n("Send Follow-up")}
                  </>
                )}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}

      {/* Sub-Runs (delegation children) */}
      {hasChildren && (
        <Card>
          <CardHeader>
            <CardTitle className="content-base flex items-center gap-2">
              <GitBranch className="h-4 w-4 text-accent" />
              {t_i18n("Sub-Runs")}
              <Badge variant="outline" className="text-[9px] font-mono-plex text-default-secondary">
                {run.child_run_count} {t_i18n(pluralize(run.child_run_count, "delegation"))}
              </Badge>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {descendantsLoading ? (
              <div className="flex items-center gap-2 content-compact text-default-secondary animate-pulse py-4 justify-center">
                <Spinner size="sm" tone="inherit" />
                {t_i18n("Loading sub-runs…")}
              </div>
            ) : descendants && descendants.length > 0 ? (
              <div className="space-y-1">
                {orderRunTree(run.id, descendants).map(({ run: desc, depth }) => {
                  const cfg = runStatusConfig(desc);
                  const SIcon = cfg.icon;
                  const AIcon = ICON_MAP[desc.agent_icon] ?? Bot;
                  const descRunning = desc.status === "running";
                  return (
                    <div
                      key={desc.id}
                      className="group flex items-center gap-3 p-3 rounded-sm cursor-pointer hover:bg-elevation-hover transition-colors"
                      onClick={() => navigate(`/operations/executions/${desc.id}`)}
                    >
                      <DelegationIndent depth={depth} />
                      <SIcon className={cn("h-4 w-4 shrink-0", cfg.color, descRunning && "animate-spin")} />
                      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">
                        <AIcon className="h-3.5 w-3.5 text-accent" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="content-compact font-medium">{desc.agent_name}</span>
                          {desc.parent_run_id !== run.id && desc.parent_agent_name && (
                            <span className="content-caption text-default-secondary">
                              {t_i18n("from")} <span className="font-medium text-default-disabled">{desc.parent_agent_name}</span>
                            </span>
                          )}
                          {desc.assignment_name && (
                            <span className="content-caption text-default-secondary">· {desc.assignment_name}</span>
                          )}
                        </div>
                        <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                          <Badge variant={cfg.badge} className="text-[8px]">{t_i18n(cfg.label)}</Badge>
                          {desc.result_summary && summaryPreview(desc.result_summary, 100) && (
                            <span className="text-[9px] text-default-secondary truncate max-w-[400px]">
                              {summaryPreview(desc.result_summary, 100)}
                            </span>
                          )}
                          {desc.error && (
                            <span className={cn("truncate max-w-[200px]", runErrorIsNeutral(desc) ? "content-caption text-default-secondary" : "text-[9px] text-feedback-error-primary")}>
                              {(() => { const text = runErrorText(desc, t_i18n); return text.length > 60 ? `${text.slice(0, 60)}…` : text; })()}
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        <span className="content-caption text-default-secondary whitespace-nowrap">{formatDateTime(desc.created_at)}</span>
                        <ChevronRight className="h-3.5 w-3.5 text-default-secondary opacity-0 group-hover:opacity-100 transition-opacity" />
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="content-compact text-default-secondary italic py-4 text-center">{t_i18n("No sub-runs found.")}</p>
            )}
          </CardContent>
        </Card>
      )}

      {/* Friction ledger: what the run struggled with, and what carries each fix. */}
      {run.frictions && run.frictions.length > 0 && (
        <RunFrictionsCard frictions={run.frictions} undisposed={run.frictions_undisposed} />
      )}

      {/* Agentic Conversation Log */}
      {run.agentic_steps && run.agentic_steps.length > 0 && (
        <AgenticStepsCard steps={run.agentic_steps} />
      )}

      {/* Legacy: Actions taken (fallback for old runs without agentic_steps) */}
      {!run.agentic_steps && run.actions_taken && run.actions_taken.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="content-base flex items-center gap-2"><Zap className="h-4 w-4 text-feedback-warning-primary" /> {t_i18n("Actions Taken ({count})", { count: run.actions_taken.length })}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {run.actions_taken.map((action, i) => (
                <div key={i} className="rounded-sm bg-elevation-hover p-3">
                  <pre className="content-compact text-default-secondary whitespace-pre-wrap">{typeof action === "string" ? action : JSON.stringify(action, null, 2)}</pre>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
      {/* Full Prompt Dialog */}
      <Dialog open={promptOpen} onOpenChange={setPromptOpen}>
        <DialogContent className="max-w-3xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <FileText className="h-4 w-4 text-accent" />
              {t_i18n("Full Rendered Prompt")}
            </DialogTitle>
            <DialogDescription>{t_i18n("The complete prompt sent to the LLM for this assignment run.")}</DialogDescription>
          </DialogHeader>
          {run.rendered_prompt ? (
            <div className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4 max-h-[65vh] overflow-auto">
              <pre className="text-[11px] text-default-secondary font-mono-plex whitespace-pre-wrap">{run.rendered_prompt}</pre>
            </div>
          ) : (
            <p className="content-base text-default-secondary italic py-4 text-center">
              {t_i18n("No rendered prompt available for this run.")}
            </p>
          )}
        </DialogContent>
      </Dialog>

      {ConfirmEl}
    </div>
  );
}
