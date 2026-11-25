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

import { Link, useParams, useNavigate, useSearchParams } from "react-router-dom";
import { FlaskConical, Bot, Cpu, ChevronDown, ChevronRight, Paperclip, Scale, ExternalLink, Star, Tag } from "lucide-react";
import { useState, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import { Icon } from "@filigran/design-system";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { useEvaluation, useMultipleEvaluationRuns, useRunDiff } from "@/hooks/api/useEvaluationApi";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { Badge } from "@/components/ui/badge";
import { RunModeBadge } from "@/components/evaluation/run-mode-badge";
import { PromptGuardBadge } from "@/components/evaluation/prompt-guard-badge";
import { PlatformErrorBadge } from "@/components/evaluation/platform-error-badge";
import { ToolApprovalBadge } from "@/components/evaluation/tool-approval-badge";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { PageSkeleton } from "@/components/ui/skeleton";
import { NotFoundState } from "@/components/ui/not-found-state";
import { Tip } from "@/components/ui/tooltip";
import { InfoLabel } from "@/components/ui/info-tip";
import { RunVersionChips } from "@/components/evaluation/run-version-chips";
import { headlineTip } from "@/components/evaluation/headline-tip";
import { PlaceboArmTag } from "@/components/evaluation/PlaceboArmTag";
import { judgeHeadline, judgeNaNote, performanceHeadline, qualityHeadline } from "@/lib/evaluation-headlines";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { headlineColorClass } from "@/lib/constants";
import { cn, formatModelLabel } from "@/lib/utils";
import { withBasePath } from "@/lib/base-path";
import { evaluationSubjectPath } from "@/lib/evaluation-subject";
import { useFormatter } from "@/i18n";
import type { EvaluationExecutionTrace, EvaluationResult, EvaluationRunDetail } from "@/types";

type OutputFormat = "json" | "markdown" | "plain";

function detectOutputFormat(text: string): OutputFormat {
  const trimmed = text.trim();
  if ((trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
    try { JSON.parse(trimmed); return "json"; } catch { /* not valid JSON */ }
  }
  if (/^#{1,6}\s|^\*\*|^- |\*\s|^\d+\.\s|```|^\|.*\|$/m.test(trimmed)) return "markdown";
  return "plain";
}

function FormattedText({ text }: { text: string }) {
  const format = useMemo(() => detectOutputFormat(text), [text]);
  if (format === "json") {
    let formatted: string;
    try { formatted = JSON.stringify(JSON.parse(text.trim()), null, 2); } catch { formatted = text; }
    return <pre className="content-compact font-mono-plex text-feedback-success-primary whitespace-pre-wrap break-words">{formatted}</pre>;
  }
  if (format === "markdown") {
    return (
      <div className="prose prose-invert prose-sm max-w-none prose-p:text-default-secondary prose-p:my-1 prose-p:whitespace-pre-line prose-headings:text-default-primary prose-headings:mt-2 prose-headings:mb-1 prose-strong:text-default-primary prose-li:text-default-secondary prose-li:whitespace-pre-line prose-ul:my-1 prose-ol:my-1">
        <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{text}</ReactMarkdown>
      </div>
    );
  }
  return <pre className="content-base text-default-secondary whitespace-pre-wrap break-words">{text}</pre>;
}

function traceColor(type: EvaluationExecutionTrace["type"]) {
  if (type === "tool") return "border-feedback-alert-primary/30 text-feedback-warning-primary";
  if (type === "judge" || type === "judge_skipped") return "border-filigran-ia-primary/30 text-filigran-ia-primary";
  if (type === "wait") return "border-elevation-subtle-soft text-default-secondary";
  return "border-accent/30 text-accent";
}

function ExecutionTimeline({ result }: { result: EvaluationResult | undefined }) {
  const { t_i18n } = useFormatter();
  const traces = useMemo(() => result?.execution_details?.traces ?? [], [result?.execution_details?.traces]);
  const timeline = useMemo(
    () => traces.reduce(
      (acc, trace) => ({
        elapsed: acc.elapsed + (trace.duration_ms || 0),
        items: [...acc.items, { trace, start: acc.elapsed }],
      }),
      { elapsed: 0, items: [] as Array<{ trace: EvaluationExecutionTrace; start: number }> },
    ).items,
    [traces],
  );

  if (!result?.execution_details || traces.length === 0) {
    return <span className="content-compact text-default-secondary">{t_i18n("No execution trace")}</span>;
  }

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5 content-caption text-default-secondary">
        <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.agentTime)}>
          <Badge variant="outline" className="text-[9px]">{t_i18n("Agent {duration}s", { duration: ((result.execution_details.agent_duration_ms ?? 0) / 1000).toFixed(2) })}</Badge>
        </InfoLabel>
        {result.execution_details.judge_duration_ms != null && (
          <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.judgeTime)}>
            <Badge variant="outline" className="text-[9px]">{t_i18n("Judge {duration}s", { duration: (result.execution_details.judge_duration_ms / 1000).toFixed(2) })}</Badge>
          </InfoLabel>
        )}
        <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.totalTime)}>
          <Badge variant="outline" className="text-[9px]">{t_i18n("Total {duration}s", { duration: ((result.execution_details.total_duration_ms ?? 0) / 1000).toFixed(2) })}</Badge>
        </InfoLabel>
        {result.execution_details.ttft_ms != null && (
          <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.ttft)}>
            <Badge variant="outline" className="text-[9px]">{t_i18n("TTFT {duration}ms", { duration: result.execution_details.ttft_ms })}</Badge>
          </InfoLabel>
        )}
      </div>

      <div className="space-y-1.5">
        {timeline.map(({ trace: t, start }, idx) => {
          const phase = t.phase ? String(t.phase).toUpperCase() : null;
          return (
            <div key={`${t.type}-${idx}`} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-1.5">
              <div className="flex items-start gap-1.5">
                <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-sm bg-elevation-hover px-1 text-[9px] font-mono-plex text-default-secondary">{idx + 1}</span>
                <div className="min-w-0 flex-1">
                  <p className="text-[11px] text-default-primary break-words leading-snug">{t.description}</p>
                  <div className="mt-1 flex flex-wrap items-center gap-1">
                    <InfoLabel
                      className="inline-flex"
                      content={t_i18n(EVALUATION_TERMS.stepStart)}
                    >
                      <Badge variant="outline" className="text-[9px]">T+{start}ms</Badge>
                    </InfoLabel>
                    <InfoLabel className="inline-flex" content={t_i18n("How long this step took.")}>
                      <Badge variant="outline" className="text-[9px]">{t.duration_ms}ms</Badge>
                    </InfoLabel>
                    <TraceTypeBadge type={t.type} />
                    {phase && <Badge variant="outline" className="text-[9px] text-accent border-accent/30">{phase}</Badge>}
                    {t.tool_name && <Badge variant="outline" className="text-[9px]">{t.tool_name}</Badge>}
                    {t.model && <Badge variant="outline" className="text-[9px]">{t.model}</Badge>}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** A score's tooltip that names it, then defines it: a value shown alone says nothing of which score it is. */
function namedNote(name: string, definition: string): string {
  return `${name}: ${definition}`;
}

/** What each kind of execution step is, read by `t_i18n`. @i18n-keys */
const TRACE_TYPE_NOTES = {
  llm: "A call to the agent's model.",
  tool: "A tool call the agent made.",
  judge: "The LLM judge scoring the answer.",
  judge_skipped: "The LLM judge did not score this entry: it was not enabled for the run, or the entry was left out as an execution anomaly.",
  wait: "A wait for tasks the agent spawned to finish.",
} as const satisfies Record<EvaluationExecutionTrace["type"], string>;

/** A step's type, explained; a type this build does not know gets no tooltip, never an empty one. */
function TraceTypeBadge({ type }: { type: EvaluationExecutionTrace["type"] }) {
  const { t_i18n } = useFormatter();
  const badge = <Badge variant="outline" className={cn("text-[9px]", traceColor(type))}>{type.toUpperCase()}</Badge>;
  const note = Object.prototype.hasOwnProperty.call(TRACE_TYPE_NOTES, type) ? TRACE_TYPE_NOTES[type] : null;
  return note ? <InfoLabel className="inline-flex" content={t_i18n(note)}>{badge}</InfoLabel> : badge;
}

/** Label for a run column: "AgentName · model" */
function runLabel(run: EvaluationRunDetail, t_i18n: (id: string) => string) {
  return `${run.agent_name || run.agent_slug || "Agent"} · ${formatModelLabel(run.model_id, t_i18n)}`;
}

function AgentLink({ run, className, iconClassName }: { run: EvaluationRunDetail; className?: string; iconClassName?: string }) {
  const { t_i18n } = useFormatter();
  const label = run.agent_name || run.agent_slug || "Agent";
  const path = evaluationSubjectPath(run);
  if (!path) {
    return <span className={className}>{label}</span>;
  }

  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <span>{label}</span>
      <a
        href={withBasePath(path)}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center hover:text-default-primary transition-colors"
        onClick={(e) => e.stopPropagation()}
        aria-label={t_i18n(run.flow_id ? "Open flow {name} in a new tab" : "Open agent {name} in a new tab", { name: label })}
      >
        <ExternalLink className={cn("h-2.5 w-2.5 opacity-70", iconClassName)} />
      </a>
    </span>
  );
}

/** Find the result for a given entry_id in a run */
function findResult(run: EvaluationRunDetail, entryId: string): EvaluationResult | undefined {
  return run.results?.find((r) => r.entry_id === entryId);
}

/** Unique dataset entries across all runs, ordered by first appearance */
function collectEntries(runs: EvaluationRunDetail[]) {
  const seen = new Map<string, { entryId: string; description: string; inputPrompt: string; files: EvaluationResult["files"]; expectedAnswer: string }>();
  for (const run of runs) {
    for (const r of run.results || []) {
      if (!r.entry_id) continue;
      if (!seen.has(r.entry_id)) {
        seen.set(r.entry_id, {
          entryId: r.entry_id,
          description: r.description || "",
          inputPrompt: r.input_prompt || "",
          files: r.files,
          expectedAnswer: r.expected_answer || "",
        });
      }
    }
  }
  return [...seen.values()];
}

// ─── Entry Row ──────────────────────────────────────────────────────────────
interface EntryRowProps {
  entry: ReturnType<typeof collectEntries>[number];
  index: number;
  runs: EvaluationRunDetail[];
  /** A STIX bundle evaluation: the entry's answers open side by side in the bundle review. */
  bundleReview?: boolean;
}

/** The most answers the bundle review lays side by side. */
const MAX_REVIEWED_ANSWERS = 4;

function EntryRow({ entry, index, runs, bundleReview = false }: EntryRowProps) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  const [reasoningOpen, setReasoningOpen] = useState<string | null>(null);
  const results = runs.map((run) => findResult(run, entry.entryId));
  const openResult = reasoningOpen ? results.find((_, i) => runs[i].id === reasoningOpen) : null;
  const openRun = reasoningOpen ? runs.find((r) => r.id === reasoningOpen) : null;
  const reviewIds = results.flatMap((r) => (r ? [r.id] : [])).slice(0, MAX_REVIEWED_ANSWERS);

  return (
    <>
      <tr
        className="border-b border-elevation-subtle-soft last:border-0 hover:bg-elevation-highlight cursor-pointer transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <td className="pl-4 pr-1 py-3">
          {/* The row toggles on a click anywhere; this button is its keyboard trigger. */}
          <button
            type="button"
            aria-expanded={expanded}
            aria-label={t_i18n("Entry {index}: show the answers", { index: index + 1 })}
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            className="flex items-center gap-3 whitespace-nowrap rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4 text-default-secondary shrink-0" />
            ) : (
              <ChevronRight className="h-4 w-4 text-default-secondary shrink-0" />
            )}
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm bg-elevation-hover content-caption font-mono-plex text-default-secondary">{index + 1}</span>
          </button>
        </td>
        <td className="px-2 py-3">
          <div className="flex items-center gap-2 min-w-0">
            {entry.files && entry.files.length > 0 && (
              <div className="flex items-center gap-1 shrink-0">
                {entry.files.map((f) => (
                  <span key={f.id} className="inline-flex items-center gap-1 rounded-sm border border-transparent bg-elevation-hover px-2 py-0.5 text-[11px] text-default-secondary">
                    <Paperclip className="h-2.5 w-2.5" />
                    <span className="max-w-[100px] truncate">{f.filename}</span>
                  </span>
                ))}
              </div>
            )}
            <p className="min-w-0 content-base line-clamp-1">{entry.description || entry.inputPrompt || "—"}</p>
            {/* On the entry's own line: the answers open side by side without unfolding it. */}
            {bundleReview && reviewIds.length > 0 && (
              <Link
                to={`/evaluations/review?results=${reviewIds.join(",")}`}
                onClick={(e) => e.stopPropagation()}
                className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-sm content-compact font-medium text-accent hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
              >
                <Icon name="file-search" size={16} />
                {t_i18n("Review bundles")}
              </Link>
            )}
          </div>
        </td>
        {results.map((r, i) => {
          // Per column: a rescore can turn the judge off for one run alone, and a
          // judge-off entry stores 0.0 in the NOT NULL `score`, which must not
          // print as a real zero.
          const judgeOn = judgeHeadline(runs[i]).value !== null;
          const qualVal = r && judgeOn ? r.score : null;
          // Why the score is N/A: no result, or the run's judge outcome.
          const naNote = !r
            ? t_i18n("This run has no result for this entry.")
            : t_i18n(...judgeNaNote(runs[i]));
          return (
            <td key={runs[i].id} className="px-2 py-2 text-center">
              <div className="flex items-center justify-center gap-2">
                {qualVal == null ? (
                  <InfoLabel content={naNote} className="content-compact font-semibold text-default-secondary">
                    {t_i18n("N/A")}
                  </InfoLabel>
                ) : (
                  <span className={cn("content-compact font-semibold", headlineColorClass(qualVal))}>{qualVal}</span>
                )}
                <span className={cn("content-compact font-medium", r?.duration_seconds != null ? "text-default-primary" : "text-default-secondary")}>
                  {r?.duration_seconds != null ? `${r.duration_seconds.toFixed(1)}s` : "—"}
                </span>
              </div>
            </td>
          );
        })}
      </tr>
      {expanded && (
        <tr className="border-b border-elevation-subtle-soft">
          {/* First col: empty (chevron + index) */}
          <td className="px-3 pt-1 pb-4 align-top" />
          {/* Second col: expected answer */}
          <td className="px-3 pt-1 pb-4 align-top">
            <h4 className="content-caption font-medium text-default-secondary uppercase tracking-wider mb-1.5">{t_i18n("Expected Answer")}</h4>
            <div className="bg-elevation-hover rounded-sm p-3 max-h-72 overflow-auto">
              {entry.expectedAnswer ? <FormattedText text={entry.expectedAnswer} /> : <span className="content-compact text-default-secondary">—</span>}
            </div>
          </td>
          {/* One col per run: agent output + reasoning */}
          {runs.map((run, i) => {
            const r = results[i];
            return (
              <td key={run.id} className="px-3 pt-1 pb-4 align-top">
                {r?.evaluator_reasoning && (
                  <button
                    className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 mb-1.5 content-caption text-default-secondary hover:text-default-primary hover:bg-elevation-highlight transition-colors"
                    onClick={(e) => { e.stopPropagation(); setReasoningOpen(run.id); }}
                  >
                    <Scale className="h-3 w-3" />
                    <span>{t_i18n("Judge")}</span>
                  </button>
                )}
                <div className="bg-elevation-hover rounded-sm p-3 max-h-72 overflow-auto">
                  {r?.agent_output ? <FormattedText text={r.agent_output} /> : <span className="content-compact text-default-secondary">{t_i18n("No result")}</span>}
                </div>
                <div className="mt-2">
                  <h5 className="content-caption font-medium text-default-secondary uppercase tracking-wider mb-1">{t_i18n("Execution Timeline")}</h5>
                  <div className="max-h-72 overflow-auto rounded-sm bg-elevation-hover p-2">
                    <ExecutionTimeline result={r} />
                  </div>
                </div>
              </td>
            );
          })}
        </tr>
      )}
      <Dialog open={!!reasoningOpen} onOpenChange={() => setReasoningOpen(null)}>
        <DialogContent className="max-w-3xl max-h-[90vh] flex flex-col overflow-hidden">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Scale className="h-4 w-4" />
              {t_i18n("Judge Reasoning")}
              {openRun && (
                <Badge variant="outline" className="ml-2 content-compact font-normal">
                  <AgentLink run={openRun} iconClassName="h-2.5 w-2.5" /> · {formatModelLabel(openRun.model_id, t_i18n)}
                </Badge>
              )}
            </DialogTitle>
          </DialogHeader>
          {openResult?.evaluator_reasoning && (
            <div className="flex-1 overflow-y-auto prose prose-invert prose-sm max-w-none prose-p:text-default-secondary prose-p:my-1 prose-p:whitespace-pre-line prose-headings:text-default-primary prose-headings:mt-3 prose-headings:mb-1 prose-strong:text-default-primary prose-li:text-default-secondary prose-ul:my-1 prose-ol:my-1">
              <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{openResult.evaluator_reasoning}</ReactMarkdown>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

// ─── Parameter Diff ─────────────────────────────────────────────────────────
function RunParamDiffCard({ runA, runB }: { runA: EvaluationRunDetail; runB: EvaluationRunDetail }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useRunDiff(runA.id, runB.id);
  const [showIdentical, setShowIdentical] = useState(false);

  if (isLoading || !data) return null;
  const { changed, identical } = data;
  if (changed.length === 0 && identical.length === 0) return null;

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardContent className="p-4 space-y-3">
        <div className="flex items-center gap-2">
          <Scale className="h-4 w-4 text-accent" />
          <h3 className="content-base font-semibold">
            <InfoLabel
              content={t_i18n(
                "The conditions each run logged when it started: its model, execution mode, scoring settings, pinned dataset versions and agent setting versions, plus the evaluation's custom parameters.",
              )}
            >
              {t_i18n("Parameter Diff")}
            </InfoLabel>
          </h3>
          <Badge variant="outline" className="text-[9px]">{t_i18n("{count} changed", { count: changed.length })}</Badge>
        </div>

        {changed.length === 0 ? (
          <p className="content-compact text-default-secondary">{t_i18n("No parameter differences between these two runs.")}</p>
        ) : (
          <div className="space-y-1">
            <div className="grid grid-cols-[1fr_1fr_1fr] gap-2 px-2.5 content-caption font-medium uppercase tracking-wider text-default-secondary">
              <span>{t_i18n("Parameter")}</span>
              <span className="truncate text-feedback-warning-primary">{runLabel(runA, t_i18n)}</span>
              <span className="truncate text-accent">{runLabel(runB, t_i18n)}</span>
            </div>
            {changed.map((d) => (
              <div key={d.key} className="grid grid-cols-[1fr_1fr_1fr] gap-2 rounded-sm border border-feedback-alert-primary/20 bg-feedback-alert-primary/[0.04] px-2.5 py-1.5">
                <span className="text-[11px] font-mono-plex text-default-secondary break-all">{d.key}</span>
                <span className="content-caption font-mono-plex text-feedback-warning-primary break-all">{d.value_a ?? "—"}</span>
                <span className="text-[11px] font-mono-plex text-accent break-all">{d.value_b ?? "—"}</span>
              </div>
            ))}
          </div>
        )}

        {identical.length > 0 && (
          <div>
            <button
              onClick={() => setShowIdentical((v) => !v)}
              className="flex items-center gap-1 text-[11px] text-default-secondary hover:text-default-primary"
            >
              {showIdentical ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              {t_i18n("{count, plural, one {# identical parameter} other {# identical parameters}}", { count: identical.length })}
            </button>
            {showIdentical && (
              <div className="mt-1 space-y-1">
                {identical.map((d) => (
                  <div key={d.key} className="grid grid-cols-[1fr_2fr] gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2.5 py-1.5">
                    <span className="text-[11px] font-mono-plex text-default-secondary break-all">{d.key}</span>
                    <span className="text-[11px] font-mono-plex break-all">{d.value_a ?? "—"}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Main Page ──────────────────────────────────────────────────────────────
export default function EvaluationRunCompare() {
  const { t_i18n } = useFormatter();
  const { evaluationId } = useParams<{ evaluationId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const runIds = useMemo(() => (searchParams.get("runs") || "").split(",").filter(Boolean), [searchParams]);
  const { data: evaluation, isLoading: evalLoading } = useEvaluation(evaluationId || "");
  const runQueries = useMultipleEvaluationRuns(runIds);

  const isLoading = evalLoading || runQueries.some((q) => q.isLoading);
  const runs = runQueries.map((q) => q.data).filter((d): d is EvaluationRunDetail => !!d);
  const entries = useMemo(() => collectEntries(runs), [runs]);

  if (isLoading) return <PageSkeleton cards={4} />;
  if (!evaluation) return (
    <NotFoundState
      icon={FlaskConical}
      title="Evaluation not found"
      description="This evaluation may have been deleted."
      actionLabel="View Evaluations"
      onAction={() => navigate("/evaluations")}
      backTo="/evaluations"
      backLabel="Back to Evaluations"
    />
  );

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo={`/evaluations/${evaluationId}/activity`}
        backLabel="Back to Activity"
        icon={FlaskConical}
        title={t_i18n("Run Comparison")}
        description={`${evaluation.name} — ${t_i18n("{runs, plural, one {# run} other {# runs}} · {entries, plural, one {# entry} other {# entries}}", { runs: runs.length, entries: entries.length })}`}
      />

      {/* Run legend */}
      <div className="flex flex-wrap items-center gap-2">
        {runs.map((run) => (
          <Badge key={run.id} variant="outline" className="gap-1 content-compact py-1">
            {run.is_baseline && (
              <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.baselineRun)}>
                <Star role="img" aria-label={t_i18n("Baseline")} className="h-3 w-3 text-feedback-warning-primary fill-feedback-alert-primary" />
              </InfoLabel>
            )}
            <Bot className="h-3 w-3" />
            <AgentLink run={run} />
            <span className="text-default-secondary">·</span>
            <RunVersionChips run={run} />
            <span className="text-default-secondary">·</span>
            <Cpu className="h-3 w-3" />
            <span className="text-default-secondary">{formatModelLabel(run.model_id, t_i18n)}</span>
            <PlaceboArmTag run={run} />
            <RunModeBadge run={run} />
            <PromptGuardBadge run={run} />
            <ToolApprovalBadge run={run} />
            <PlatformErrorBadge run={run} />
            {run.score != null && (
              <>
                <span className="text-default-secondary">·</span>
                <InfoLabel
                  content={namedNote(t_i18n(judgeHeadline(run).label), headlineTip(judgeHeadline(run), t_i18n))}
                  className={cn("font-medium", headlineColorClass(run.score))}
                >
                  {run.score}
                </InfoLabel>
              </>
            )}
            {/* The other two headlines, through the shared resolver so all
                evaluation pages agree; Overall Performance keeps its
                comparative-scope disclosure. */}
            {run.quality_score != null && (
              <>
                <span className="text-default-secondary">·</span>
                <InfoLabel
                  content={namedNote(t_i18n(qualityHeadline(run).label), headlineTip(qualityHeadline(run), t_i18n))}
                  className={cn("font-medium", headlineColorClass(run.quality_score))}
                >
                  {run.quality_score}
                </InfoLabel>
              </>
            )}
            {(() => {
              const perf = performanceHeadline(run);
              if (perf.value == null) return null;
              return (
                <>
                  <span className="text-default-secondary">·</span>
                  <InfoLabel
                    content={namedNote(t_i18n(perf.label), headlineTip(perf, t_i18n))}
                    className={cn("font-medium", perf.legacy ? "text-default-secondary" : headlineColorClass(perf.value))}
                  >
                    {perf.value}
                  </InfoLabel>
                </>
              );
            })()}
            {run.effective_tags && Object.entries(run.effective_tags).slice(0, 3).map(([k, v]) => (
              <InfoLabel key={k} className="inline-flex" content={t_i18n(EVALUATION_TERMS.runTags)}>
                <span className="inline-flex items-center gap-0.5 font-mono-plex content-caption bg-elevation-hover text-default-secondary border border-elevation-subtle-soft rounded-sm px-1 py-0.5 leading-none">
                  <Tag className="h-2.5 w-2.5" />{k}: {v}
                </span>
              </InfoLabel>
            ))}
          </Badge>
        ))}
      </div>

      {/* Parameter diff: only meaningful for a two-run comparison */}
      {runs.length === 2 && <RunParamDiffCard runA={runs[0]} runB={runs[1]} />}

      <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full content-base table-fixed">
              <colgroup>
                <col style={{ width: "72px" }} />
                <col />
                {runs.map((run) => (
                  <col key={run.id} style={{ width: `${Math.floor(50 / runs.length)}%` }} />
                ))}
              </colgroup>
              <thead>
                <tr className="border-b border-elevation-subtle-soft bg-elevation-hover">
                  <th className="text-left content-caption font-medium text-default-secondary uppercase tracking-wider pl-4 pr-1 py-2.5" />
                  <th className="text-left content-caption font-medium text-default-secondary uppercase tracking-wider px-2 py-2.5">{t_i18n("Dataset Entry")}</th>
                  {runs.map((run) => (
                    <th key={run.id} className="text-center px-2 py-2.5">
                      <Tip content={runLabel(run, t_i18n)}>
                        <div className="content-caption font-medium text-default-secondary uppercase tracking-wider truncate">
                          <AgentLink run={run} className="justify-center" />
                        </div>
                      </Tip>
                      <p className="text-[9px] text-default-secondary truncate font-normal">{formatModelLabel(run.model_id, t_i18n)}</p>
                      <div className="flex justify-center gap-1 mt-0.5 empty:hidden">
                        <RunModeBadge run={run} />
                        <PromptGuardBadge run={run} />
                        <ToolApprovalBadge run={run} />
                        <PlatformErrorBadge run={run} />
                      </div>
                      <div className="flex flex-wrap justify-center gap-1 mt-0.5 empty:hidden">
                        <RunVersionChips run={run} />
                      </div>
                      {/* Only what a dataset entry measures; run-level
                          aggregates do not belong on an entry row. */}
                      <div className="flex items-center justify-center gap-2 mt-1 text-[9px] text-default-secondary">
                        <InfoLabel content={t_i18n(EVALUATION_TERMS.entryJudgeScore)}>{t_i18n("Judge Score")}</InfoLabel>
                        <InfoLabel content={t_i18n(EVALUATION_TERMS.entryDuration)}>{t_i18n("Time")}</InfoLabel>
                      </div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {entries.length === 0 ? (
                  <tr>
                    <td colSpan={runs.length + 2} className="text-center py-12 content-base text-default-secondary">{t_i18n("No results to compare")}</td>
                  </tr>
                ) : (
                  entries.map((entry, idx) => (
                    <EntryRow key={entry.entryId} entry={entry} index={idx} runs={runs} bundleReview={evaluation.family === "STIX_BUNDLE"} />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
