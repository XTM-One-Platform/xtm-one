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
 * Run history + run trace.
 *
 * Two levels in one rail, mirroring `VersionHistoryPanel`'s shell: the run
 * list, and — once a run is selected — its per-node step journal.  Selecting
 * a run also drives the canvas execution overlay through `onInspectRun`, so
 * the trace reads on the graph itself, not just in this list.
 *
 * Live behaviour comes from the hooks, not from here: `useFlowRuns` and
 * `useFlowRunDetail` poll fast only while something is active, and the global
 * activity stream invalidates `flow-runs` on every backend event.
 */

import { useState } from "react";
import { Library, Play, StepForward } from "lucide-react";
import { Ban, X } from "lucide-react";
import { FlowDecisionCard } from "@/components/decisions/FlowDecisionCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { STATUS_CONFIG } from "@/lib/constants";
import { cn, formatDateTime, formatDurationShort } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import {
  useCancelFlowRun,
  useFlowRunDetail,
  useFlowRunDebug,
  useFlowRuns,
} from "@/hooks/api/useFlowApi";
import { useWorkset, useWorksetItems, useWorksets } from "@/hooks/api/useWorksetApi";
import type { FlowRunStatus, FlowRunStep, Workset } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

/** Flow-run statuses reuse the platform's run vocabulary where it exists;
 * `awaiting` (parked on a delay/sub-flow/assignment) maps to the pending
 * clock — visually "waiting" — with its own label. */
const RUN_STATUS_LABELS: Record<FlowRunStatus, string> = {
  pending: "Pending",
  running: "Running",
  awaiting: "Waiting",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  skipped: "Skipped",
};

const ACTIVE_STATUSES: FlowRunStatus[] = ["pending", "running", "awaiting"];

function statusConfig(status: FlowRunStatus) {
  const key = status === "awaiting" ? "pending" : status;
  return STATUS_CONFIG[key] || STATUS_CONFIG.pending;
}

/** "1m 30s" between two journal timestamps, or null while still open. */
function spanDuration(start: string | null, end: string | null): string | null {
  if (!start || !end) return null;
  const seconds = (new Date(end).getTime() - new Date(start).getTime()) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return formatDurationShort(seconds);
}

function RunStatusBadge({ status }: { status: FlowRunStatus }) {
  const { t_i18n } = useFormatter();
  const cfg = statusConfig(status);
  return (
    <Badge variant={cfg.badge} className="gap-1 text-[9px]">
      <cfg.icon
        className={cn("h-2.5 w-2.5", status === "running" && "animate-spin")}
      />
      {t_i18n(RUN_STATUS_LABELS[status])}
    </Badge>
  );
}

interface RunsPanelProps {
  flowId: string;
  /** The run whose trace is open (also drives the canvas overlay). */
  inspectedRunId: string | null;
  onInspectRun: (runId: string | null) => void;
  onClose: () => void;
}

export function RunsPanel({
  flowId,
  inspectedRunId,
  onInspectRun,
  onClose,
}: RunsPanelProps) {
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading } = useFlowRuns(flowId);
  const cancelRun = useCancelFlowRun();

  return (
    <aside className="flex w-80 shrink-0 flex-col border-l border-elevation-subtle layer-1 bg-elevation-default">
      <div className="flex items-center gap-2 border-b border-elevation-subtle p-3">
        <p className="flex-1 content-base font-medium">{t_i18n("Run history")}</p>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label={t_i18n("Close")}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {isLoading ? (
          <p className="px-2 py-6 text-center content-compact text-default-secondary">
            {t_i18n("Loading…")}
          </p>
        ) : !data || data.runs.length === 0 ? (
          <p className="px-2 py-6 text-center content-compact text-default-secondary">
            {t_i18n("No runs yet: press Run to try this flow.")}
          </p>
        ) : (
          data.runs.map((run) => {
            const inspected = run.id === inspectedRunId;
            return (
              <div
                key={run.id}
                className={cn(
                  "mb-1 rounded-sm border",
                  inspected ? "border-accent/40 bg-accent/[0.04]" : "border-elevation-subtle",
                )}
              >
                <button
                  type="button"
                  className="w-full p-2.5 text-left"
                  onClick={() => onInspectRun(inspected ? null : run.id)}
                >
                  <div className="flex items-center gap-2">
                    <RunStatusBadge status={run.status} />
                    <span className="content-caption capitalize text-default-secondary">
                      {t_i18n(run.trigger_type.replace("_", " "))}
                    </span>
                    <span
                      className="ml-auto content-caption text-default-secondary"
                      title={formatDateTime(run.created_at)}
                    >
                      {(() => {
                        const duration = spanDuration(
                          run.started_at,
                          run.completed_at,
                        );
                        return duration
                          ? `${formatTimeAgo(intl, run.created_at)} · ${duration}`
                          : formatTimeAgo(intl, run.created_at);
                      })()}
                    </span>
                  </div>
                  {run.error && (
                    <p className="mt-1 line-clamp-2 content-caption text-feedback-error-primary">
                      {run.error}
                    </p>
                  )}
                  {!run.error && run.result_summary && (
                    <p className="mt-1 line-clamp-2 content-caption text-default-secondary">
                      {run.result_summary}
                    </p>
                  )}
                </button>

                {ACTIVE_STATUSES.includes(run.status) && (
                  <div className="border-t border-elevation-subtle-soft px-2.5 py-1.5">
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-6 gap-1 content-caption text-feedback-error-primary hover:text-feedback-error-primary"
                      disabled={cancelRun.isPending}
                      onClick={() =>
                        cancelRun.mutate({ runId: run.id, flowId })
                      }
                    >
                      <Ban className="h-3 w-3" /> {t_i18n("Cancel run")}
                    </Button>
                  </div>
                )}

                {inspected && <RunTrace runId={run.id} />}
              </div>
            );
          })
        )}
      </div>
    </aside>
  );
}

/** The per-node journal of one run, in execution order. */
function RunTrace({ runId }: { runId: string }) {
  const { t_i18n } = useFormatter();
  const { data } = useFlowRunDetail(runId);
  const debugRun = useFlowRunDebug();
  if (!data) {
    return (
      <p className="border-t border-elevation-subtle-soft px-2.5 py-2 content-caption text-default-secondary">
        {t_i18n("Loading…")}
      </p>
    );
  }
  return (
    <ol className="border-t border-elevation-subtle-soft px-2.5 py-1.5">
      {/* Human-in-the-loop: an Ask-user node waits — answer inline, the
          response becomes the node's output and the run resumes. */}
      {data.awaiting_input_question && (
        <FlowDecisionCard
          runId={runId}
          question={data.awaiting_input_question}
          ask={data.awaiting_input_ask}
        />
      )}
      {/* Debugger: paused on a breakpoint — resume or step to the next node. */}
      {data.paused_node_id && (
        <li className="mb-1 rounded-sm border border-feedback-error-primary/40 bg-feedback-error-primary/[0.06] px-2 py-1.5">
          <p className="content-caption font-medium text-feedback-error-primary">
            {t_i18n("Paused at")}{" "}
            <span className="font-mono-plex">{data.paused_node_id}</span>
            {data.step_mode ? " · step" : ""}
          </p>
          <div className="mt-1 flex gap-1.5">
            <button
              type="button"
              className="flex items-center gap-1 rounded-sm border border-elevation-subtle px-1.5 py-0.5 content-caption hover:bg-elevation-hover"
              disabled={debugRun.isPending}
              onClick={() => debugRun.mutate({ runId, action: "continue" })}
            >
              <Play className="h-2.5 w-2.5" /> {t_i18n("Continue")}
            </button>
            <button
              type="button"
              className="flex items-center gap-1 rounded-sm border border-elevation-subtle px-1.5 py-0.5 content-caption hover:bg-elevation-hover"
              disabled={debugRun.isPending}
              onClick={() => debugRun.mutate({ runId, action: "step" })}
            >
              <StepForward className="h-2.5 w-2.5" /> {t_i18n("Step")}
            </button>
          </div>
        </li>
      )}
      {/* The corpus this run built, if it built one. */}
      <RunWorksets runId={runId} active={ACTIVE_STATUSES.includes(data.status)} />
      {data.steps.map((step) => (
        <RunTraceStep key={step.id} step={step} />
      ))}
      {data.steps.length === 0 && (
        <p className="py-1 content-caption text-default-secondary">
          {t_i18n("No steps recorded.")}
        </p>
      )}
    </ol>
  );
}

/**
 * The working corpora a run built, watched while it builds them.
 *
 * A run-scoped workset is invisible everywhere else on this canvas — it is not
 * a node output, and by the time the run ends it may have been purged by its
 * own TTL. So the one moment an operator can see what the run actually
 * collected is while it is collecting: items in, items summarised, items
 * evicted, and what it currently thinks matters most.
 *
 * The honesty of the tools is carried verbatim: the coverage sentence is the
 * service's own, so what the operator reads is what the agents read — an
 * unsealed corpus is never shown as complete, and an evicted item is named as
 * no longer searchable.
 */
function RunWorksets({ runId, active }: { runId: string; active: boolean }) {
  // Scoped to THIS run, not to the flow: a concurrent run of the same flow has
  // its own corpus, and mixing them is the isolation the scope promises.
  const { data: worksets } = useWorksets(
    { scopeRef: runId, limit: 5 },
    // Polls only while the run can still add to it.
    { refetchInterval: active ? 5000 : false },
  );
  if (!worksets || worksets.length === 0) return null;
  return (
    <>
      {worksets.map((workset) => (
        <RunWorksetCard key={workset.id} workset={workset} active={active} />
      ))}
    </>
  );
}

function RunWorksetCard({
  workset,
  active,
}: {
  workset: Workset;
  active: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { data: detail } = useWorkset(workset.id);
  // The top priorities — the same ranking the digest a synthesis is written
  // from uses, so the operator sees what the agent will lead with.
  const { data: items } = useWorksetItems(workset.id, { limit: 5 });
  const coverage = detail?.coverage;

  return (
    <li className="mb-1 rounded-sm border border-elevation-subtle bg-elevation-hover px-2 py-1.5">
      <p className="flex items-center gap-1 content-caption font-medium">
        <Library className="h-2.5 w-2.5 shrink-0 text-accent" />
        <span className="truncate">{workset.label || workset.key}</span>
        {active && !workset.sealed_at && (
          <Badge variant="outline" className="ml-auto text-[9px]">
            {t_i18n("Collecting")}
          </Badge>
        )}
      </p>

      <div className="mt-1 flex flex-wrap gap-x-2.5 gap-y-0.5 content-caption text-default-secondary">
        <span>
          {t_i18n("Collected")}: <b className="text-default-primary">{workset.item_count}</b>
        </span>
        <span>
          {t_i18n("Summarised")}:{" "}
          <b className="text-default-primary">{coverage?.ready ?? "—"}</b>
        </span>
        {/* Evicted is shown whenever it is non-zero, never folded into a
            total: an operator reading "120 collected" over a corpus that
            reclaimed 70 of them is reading the wrong number. */}
        {workset.evicted_count > 0 && (
          <span className="text-feedback-warning-primary">
            {t_i18n("Evicted")}: <b>{workset.evicted_count}</b>
          </span>
        )}
        {(coverage?.failed ?? 0) > 0 && (
          <span className="text-feedback-error-primary">
            {t_i18n("Failed")}: <b>{coverage?.failed}</b>
          </span>
        )}
      </div>

      {items && items.items.length > 0 && (
        <ol className="mt-1 space-y-0.5">
          {items.items.map((item) => (
            <li
              key={item.id}
              className="flex items-baseline gap-1 content-caption text-default-secondary"
            >
              <span className="w-5 shrink-0 text-right font-mono-plex text-default-primary">
                {item.priority_score ?? "—"}
              </span>
              <span className="truncate">{item.title}</span>
              {item.corroboration_count > 0 && (
                <span className="shrink-0 text-accent">
                  +{item.corroboration_count}
                </span>
              )}
            </li>
          ))}
        </ol>
      )}

      {/* Verbatim from the service — the operator and the agent read one
          sentence, not two descriptions that can disagree. */}
      {coverage && (
        <p className="mt-1 text-[9px] leading-snug text-default-secondary">
          {coverage.description}
        </p>
      )}
    </li>
  );
}

function RunTraceStep({ step }: { step: FlowRunStep }) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  const hasDetails =
    (step.input && Object.keys(step.input).length > 0) ||
    (step.output && Object.keys(step.output).length > 0);
  const cfg =
    step.status === "running"
      ? STATUS_CONFIG.running
      : step.status === "failed"
        ? STATUS_CONFIG.failed
        : step.status === "skipped"
          ? STATUS_CONFIG.dismissed || STATUS_CONFIG.pending
          : STATUS_CONFIG.completed;
  return (
    <li className="flex items-start gap-1.5 py-0.5">
      <cfg.icon
        className={cn(
          "mt-0.5 h-3 w-3 shrink-0",
          cfg.color,
          step.status === "running" && "animate-spin",
        )}
      />
      <div className="min-w-0 flex-1">
        <button
          type="button"
          className={cn(
            "block w-full truncate text-left content-caption",
            hasDetails && "cursor-pointer hover:text-default-primary",
          )}
          disabled={!hasDetails}
          onClick={() => setExpanded((prev) => !prev)}
          title={hasDetails ? t_i18n("Show inputs and outputs") : undefined}
        >
          <span className="font-medium">{step.node_id}</span>
          <span className="text-default-secondary"> · {step.node_kind}</span>
          {step.iteration > 0 && (
            <span className="text-default-secondary"> · #{step.iteration}</span>
          )}
          {(() => {
            const duration = spanDuration(step.started_at, step.completed_at);
            return duration ? (
              <span className="text-default-secondary"> · {duration}</span>
            ) : null;
          })()}
        </button>
        {step.error && (
          <p className="line-clamp-2 content-caption text-feedback-error-primary">{step.error}</p>
        )}
        {!step.error && step.warning && (
          <p className="line-clamp-2 content-caption text-feedback-warning-primary">
            {t_i18n("Completed, but the operation answered with an error:")} {step.warning}
          </p>
        )}
        {/* Troubleshooting detail: what the node computed WITH (rendered
            config) and what it produced — the journal's input/output. */}
        {expanded && step.input && Object.keys(step.input).length > 0 && (
          <div className="mt-1">
            <p className="text-[9px] font-semibold uppercase tracking-wide text-default-secondary">
              {t_i18n("Input")}
            </p>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-sm bg-elevation-hover p-1.5 font-mono-plex text-[9px] text-default-secondary">
              {JSON.stringify(step.input, null, 1)}
            </pre>
          </div>
        )}
        {expanded && step.output && Object.keys(step.output).length > 0 && (
          <div className="mt-1">
            <p className="text-[9px] font-semibold uppercase tracking-wide text-default-secondary">
              {t_i18n("Output")}
            </p>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-sm bg-elevation-hover p-1.5 font-mono-plex text-[9px] text-default-secondary">
              {JSON.stringify(step.output, null, 1)}
            </pre>
          </div>
        )}
      </div>
    </li>
  );
}
