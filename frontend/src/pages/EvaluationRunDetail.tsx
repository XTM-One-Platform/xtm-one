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

import { Link, useParams, useNavigate } from "react-router-dom";
import { FlaskConical, Bot, Cpu, ChevronDown, ChevronRight, Clock, Zap, Brain, Wrench, Copy, Check, Paperclip, Scale, Download, RotateCcw, AlertTriangle, Hash, StopCircle, ExternalLink, Repeat, History, Tag, BarChart3, Plus, X, Star, Gavel, Hourglass, Workflow } from "lucide-react";
import { Icon, Slider, Spinner } from "@filigran/design-system";
import { useState, useCallback, useMemo, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import {
  downloadEvaluationRunResultTrace,
  useEvaluation,
  useEvaluationComparison,
  useEvaluationRun,
  useRunParams,
  useReplayJudgeResult,
  useReplayEvaluationRun,
  useRescoreRun,
  useResultMetrics,
  useRunMetricsAggregate,
  useRunTags,
  usePatchRunTags,
} from "@/hooks/api/useEvaluationApi";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { FallbackEntryBadge, RunModeBadge } from "@/components/evaluation/run-mode-badge";
import { PromptGuardBadge } from "@/components/evaluation/prompt-guard-badge";
import { MetricName } from "@/components/evaluation/metric-name";
import { PlatformErrorBadge } from "@/components/evaluation/platform-error-badge";
import { NeededApprovalBadge, ToolApprovalBadge } from "@/components/evaluation/tool-approval-badge";
import { HeldBadge } from "@/components/ui/held-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { PageSkeleton } from "@/components/ui/skeleton";
import { NotFoundState } from "@/components/ui/not-found-state";
import { Input } from "@/components/ui/input";
import { Tip } from "@/components/ui/tooltip";
import { RunVersionChips } from "@/components/evaluation/run-version-chips";
import { PlaceboArmTag } from "@/components/evaluation/PlaceboArmTag";
import { PlaceboSnapshotCard } from "@/components/evaluation/PlaceboSnapshotCard";
import { useToast } from "@/components/ui/toast";
import { isLegacyRun, judgeHeadline, judgeNaNote, metricReasonLabel, naReason, partialCoverage, performanceHeadline, qualityHeadline, runJudged } from "@/lib/evaluation-headlines";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import {
  EVALUATION_TERMS,
  extractionFailureLabel,
  FIELD_STATUS_DESCRIPTIONS,
  fieldModeCopy,
  judgeSubScoreDescription,
  METRIC_COLUMN_DESCRIPTIONS,
  METRIC_MEAN_BASIS_LABELS,
  METRIC_MEAN_BASIS_NOTES,
  triggerNote,
} from "@/lib/evaluation-glossary";
import { headlineTip } from "@/components/evaluation/headline-tip";
import { EXTRACTED_PREVIEW_CHARS, renderExtractedPrediction } from "@/lib/extracted-prediction";
import { cn, formatModelLabel, formatPercentValue, pluralize, formatMetricValue as formatCountValue } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { COPY_FEEDBACK_DELAY, EVALUATION_STATUS_CONFIG, EVALUATION_SUB_SCORES, EVALUATION_TRIGGER_LABELS, metricEntryDisplayName, MODEL_INDEPENDENT, PARAM_SOURCE_COLORS, PARAM_SOURCE_LABELS, REUSE_BADGE_CONFIG, RUN_TAG_BASELINE_KEY, headlineColorClass, headlineDef } from "@/lib/constants";
import { withBasePath } from "@/lib/base-path";
import { evaluationSubjectPath } from "@/lib/evaluation-subject";
import { formatRunMetricValue } from "@/lib/run-metric-format";
import type {
  EvaluationResult,
  EvaluationRun,
  JsonFieldAccuracyDetail,
  JsonFieldScore,
  KnownExclusionReason,
  MetricAggregate,
  MetricExclusion,
  ExtractedPrediction,
  ExtractionFailure,
  PersistedMetricResult,
  RunMetric,
  StixByTypePrfDetail,
  StixPerTypeEntry,
  StixPrfDetail,
} from "@/types";
import { EXTRACTION_FAILED_KEY } from "@/types";
import { RUN_LEVEL_EXCLUSION_KEY } from "@/types";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

const JUDGE_PARSE_FAILURE_PREFIX = "Failed to parse judge response";
const JUDGE_TRUNCATION_PREFIX = "Judge output was truncated at max_tokens";
const JUDGE_FAILURE_PREFIX = "Judge failure:";

// Hard cap enforced by the backend replay schema
// (ReplayJudgeRequest.judge_max_tokens, le=131072).
const REPLAY_JUDGE_MAX_TOKENS_CAP = 131_072;

function inferModelMaxOutputTokens(modelId: string | null | undefined): number | null {
  if (!modelId) return null;
  // Strip gateway/vendor prefixes (openrouter/openai/gpt-6-astra) and variant
  // suffixes (gpt-6-astra:free) so the family checks below match. Mirrors the
  // backend's _normalize_family_key() in model_limits.py.
  const lowered = modelId.toLowerCase().trim();
  const mid = (lowered.includes("/") ? lowered.slice(lowered.lastIndexOf("/") + 1) : lowered).split(":")[0];

  // Best-effort local mapping, no network call. It bounds the judge_max_tokens
  // slider, a completion limit, so it mirrors the backend's
  // _FAMILY_MAX_OUTPUT_TOKENS floors, not the context windows.
  if (mid.startsWith("gpt-6") || mid.startsWith("gpt-5")) return 128_000;
  if (mid.startsWith("gpt-4.1")) return 32_768;
  if (mid.startsWith("gpt-4o")) return 16_384;
  if (mid.startsWith("o1") || mid.startsWith("o3") || mid.startsWith("o4")) return 100_000;
  // Claude 5 generation (fable / opus-5 / sonnet-5) supports 128k output;
  // earlier Claude models keep the conservative 64k floor.
  if (mid.includes("claude-fable-") || mid.includes("claude-opus-5") || mid.includes("claude-sonnet-5")) return 128_000;
  if (mid.includes("claude")) return 64_000;
  // includes(), not startsWith(): dot-prefixed ids survive normalization
  // (Bedrock us.anthropic.claude-..., azure-style deployments).
  if (mid.includes("gemini-2.5") || mid.includes("gemini-3")) return 65_536;
  if (mid.includes("grok-")) return 32_768;

  return null;
}

function ReplayJudgeButton({ runId, result }: { runId: string; result: EvaluationResult }) {
  const traces = result.execution_details?.traces || [];
  const judgeTrace = [...traces].reverse().find((t) => typeof t === "object" && t !== null && t.type === "judge");
  const judgeFailed = judgeTrace?.is_judge_failure === true;

  const reasoning = (result.evaluator_reasoning || "").trim();
  const isParseFailure =
    judgeFailed ||
    result.is_judge_failure === true ||
    reasoning.startsWith(JUDGE_FAILURE_PREFIX) ||
    reasoning.startsWith(JUDGE_PARSE_FAILURE_PREFIX) ||
    reasoning.startsWith(JUDGE_TRUNCATION_PREFIX);
  if (!isParseFailure) return null;
  return <ReplayJudgePanel runId={runId} result={result} />;
}

function ReplayJudgePanel({ runId, result }: { runId: string; result: EvaluationResult }) {
  const { t_i18n, intl } = useFormatter();
  const replayJudge = useReplayJudgeResult();
  const aiOff = useAiActionDisabled();
  const [open, setOpen] = useState(false);

  const traces = result.execution_details?.traces || [];
  const judgeTrace = [...traces].reverse().find((t) => t.type === "judge");
  const judgeModel = judgeTrace?.model || null;
  // `judge_max_tokens` is not part of the declared shape, so it is read structurally.
  const recordedLimit = Number(
    (result.execution_details as unknown as Record<string, unknown> | null)?.judge_max_tokens,
  );
  const currentLimit = recordedLimit > 0 ? recordedLimit : 2048;
  const inferredMaxOutput = inferModelMaxOutputTokens(judgeModel);
  const sliderMax = Math.min(inferredMaxOutput ?? currentLimit * 3, REPLAY_JUDGE_MAX_TOKENS_CAP);
  const sliderMin = currentLimit;
  const sliderStep = 256;
  const defaultValue = Math.min(sliderMax, Math.max(sliderMin, currentLimit * 2));
  const [tokenWindow, setTokenWindow] = useState(defaultValue);

  const replay = async () => {
    await replayJudge.mutateAsync({
      runId,
      resultId: result.id,
      judgeMaxTokens: tokenWindow,
    });
    setOpen(false);
  };

  return (
    <>
      <Tip content={t_i18n("Replay the LLM judge on this entry with a larger output budget.")}>
        <Button aria-label={t_i18n("Replay the LLM judge with a larger output budget")} variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => setOpen(true)}>
          <RotateCcw className="h-3 w-3 text-feedback-warning-primary" />
        </Button>
      </Tip>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <RotateCcw className="h-4 w-4 text-feedback-warning-primary" />
              {t_i18n("Replay Judge")}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-4">
            <p className="content-base text-default-secondary">
              {t_i18n("Calls the LLM judge again on this entry's stored answer with a larger output budget. The new verdict and score replace the current ones, and the run's overall scores are recomputed.")}
            </p>

            <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 space-y-3">
              <div className="flex items-center justify-between">
                <span className="content-compact font-medium uppercase tracking-wider text-default-secondary">{t_i18n("Judge Output Budget")}</span>
                <Badge variant="outline" className="content-caption">{t_i18n("{count} tokens", { count: tokenWindow })}</Badge>
              </div>
              <Slider
                aria-label={t_i18n("Judge Output Budget")}
                min={sliderMin}
                max={sliderMax}
                step={sliderStep}
                value={[tokenWindow]}
                onValueChange={([value]) => setTokenWindow(value)}
              />
              <div className="flex items-center justify-between text-[11px] text-default-secondary">
                <span>{t_i18n("Current: {count}", { count: currentLimit })}</span>
                <span>{t_i18n("Max: {count}", { count: sliderMax })}</span>
              </div>
            </div>

            {inferredMaxOutput ? (
              <p className="content-compact text-accent">
                {t_i18n("Estimated model output limit (best-effort, local): {count} tokens.", { count: intl.formatNumber(inferredMaxOutput) })}
              </p>
            ) : (
              <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 p-3 content-compact text-feedback-warning-tertiary">
                <p className="flex items-start gap-2">
                  <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                  {t_i18n("The judge model's output limit is not known here, so the slider stops at 3 times the current budget. Check the model's real output limit before choosing a higher value.")}
                </p>
              </div>
            )}

            <div className="flex items-center justify-end gap-2">
              <Button variant="outline" onClick={() => setOpen(false)} disabled={replayJudge.isPending}>{t_i18n("Cancel")}</Button>
              <AiDisabledTip>
                <Button onClick={replay} disabled={replayJudge.isPending || aiOff}>
                  {replayJudge.isPending ? t_i18n("Replaying...") : t_i18n("Replay Judge")}
                </Button>
              </AiDisabledTip>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function DownloadButton({ text, format, filename }: { text: string; format: OutputFormat; filename: string }) {
  const { t_i18n } = useFormatter();
  const ext = format === "json" ? "json" : "md";
  const mime = format === "json" ? "application/json" : "text/markdown";
  const handleDownload = useCallback(() => {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${filename}.${ext}`;
    a.click();
    URL.revokeObjectURL(url);
  }, [text, mime, ext, filename]);

  return (
    <Tip content={t_i18n("Download")}>
      <button aria-label={t_i18n("Download")}
        type="button"
        onClick={handleDownload}
        className="h-6 w-6 flex items-center justify-center rounded-sm text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-colors"
      >
        <Download className="h-3 w-3" />
      </button>
    </Tip>
  );
}

function CopyButton({ text }: { text: string }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  }, [text]);

  return (
    <Tip content={t_i18n("Copy")}>
      <button
        aria-label={t_i18n("Copy")}
        type="button"
        onClick={handleCopy}
        className="h-6 w-6 flex items-center justify-center rounded-sm text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-colors"
      >
        {copied ? <Check className="h-3 w-3 text-feedback-success-primary" /> : <Copy className="h-3 w-3" />}
      </button>
    </Tip>
  );
}

type OutputFormat = "json" | "markdown" | "plain";

function detectOutputFormat(text: string): OutputFormat {
  const trimmed = text.trim();
  if ((trimmed.startsWith("{") && trimmed.endsWith("}")) || (trimmed.startsWith("[") && trimmed.endsWith("]"))) {
    try { JSON.parse(trimmed); return "json"; } catch { /* not valid JSON */ }
  }
  if (/^#{1,6}\s|^\*\*|^- |\*\s|^\d+\.\s|```|^\|.*\|$/m.test(trimmed)) return "markdown";
  return "plain";
}

function AgentOutput({ text }: { text: string }) {
  const format = useMemo(() => detectOutputFormat(text), [text]);

  if (format === "json") {
    let formatted: string;
    try { formatted = JSON.stringify(JSON.parse(text.trim()), null, 2); } catch { formatted = text; }
    return (
      <pre className="bg-elevation-hover rounded-sm p-3 max-h-48 overflow-auto content-compact font-mono-plex text-feedback-success-primary whitespace-pre-wrap break-words">
        {formatted}
      </pre>
    );
  }

  if (format === "markdown") {
    return (
      <div className="bg-elevation-hover rounded-sm p-3 max-h-48 overflow-auto prose prose-invert prose-sm max-w-none prose-p:text-default-secondary prose-p:my-2 prose-p:whitespace-pre-line prose-headings:text-default-primary prose-headings:mt-3 prose-headings:mb-1 prose-strong:text-default-primary prose-li:text-default-secondary prose-li:whitespace-pre-line prose-ul:my-2 prose-ol:my-2">
        <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{text}</ReactMarkdown>
      </div>
    );
  }

  return (
    <pre className="bg-elevation-hover rounded-sm p-3 max-h-48 overflow-auto content-base text-default-secondary whitespace-pre-wrap break-words">
      {text}
    </pre>
  );
}

/** The extracted value, pretty-printed when it is JSON and bounded either way. */
function ExtractedPredictionPanel({
  extracted,
  filename,
}: {
  extracted: ExtractedPrediction;
  filename: string;
}) {
  const { t_i18n } = useFormatter();
  const { text, hidden } = useMemo(
    () => renderExtractedPrediction(extracted.prediction),
    [extracted.prediction],
  );

  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-between mb-1 gap-2">
        <h4 className="content-compact font-medium text-accent/90 uppercase tracking-wider truncate">
          <InfoLabel content={t_i18n(RUN_PAGE_NOTES.extracted)}>{t_i18n("Extracted: scored")}</InfoLabel>
        </h4>
        <div className="flex items-center gap-2 shrink-0">
          {extracted.output_kind && (
            <InfoLabel content={t_i18n(outputKindNote(extracted.output_kind))}>
              <Badge variant="outline" className="text-[9px]">{extracted.output_kind}</Badge>
            </InfoLabel>
          )}
          {extracted.item_count !== null && extracted.item_count !== undefined && (
            <span className="content-caption text-default-secondary">
              {extracted.item_count} {pluralize(extracted.item_count, "item")}
            </span>
          )}
          <DownloadButton text={extracted.prediction} format="json" filename={filename} />
          <CopyButton text={extracted.prediction} />
        </div>
      </div>
      <div className="flex-1">
        <pre className="bg-accent/[0.04] border border-accent/20 rounded-sm p-3 max-h-48 overflow-auto content-compact font-mono-plex text-accent/90 whitespace-pre-wrap break-words">
          {text}
        </pre>
        {hidden > 0 && (
          <p className="content-caption text-default-secondary mt-1">
            {t_i18n("Bounded to the first {count} characters.", {
              count: EXTRACTED_PREVIEW_CHARS.toLocaleString(),
            })}{" "}
            {t_i18n("The stored value is whole: copy or download it.")}
          </p>
        )}
      </div>
    </div>
  );
}

function SubScoreBar({ subScoreKey, label, value, color }: { subScoreKey: string; label: string; value: number; color: string }) {
  const { t_i18n } = useFormatter();
  const description = judgeSubScoreDescription(subScoreKey);
  return (
    <div className="flex items-center gap-2 content-compact">
      <span className={cn("w-40 shrink-0", color)}>
        {description ? <InfoLabel content={t_i18n(description)}>{label}</InfoLabel> : label}
      </span>
      <div className="flex-1 h-1.5 bg-elevation-hover rounded-full overflow-hidden">
        <div
          className="h-full bg-accent rounded-full transition-all"
          style={{ width: `${value}%` }}
        />
      </div>
      <span className="w-8 text-right text-default-secondary">{value}</span>
    </div>
  );
}

/** Why a model call stopped, by the canonical `stop_reason` every provider maps onto
 * (`backend/app/llm/base.py`, `ChatResponse.stop_reason`), read by `t_i18n`. @i18n-keys */
const STOP_REASON_NOTES = {
  end_turn: "The model finished its answer.",
  tool_use: "The model stopped to call one or more tools.",
  max_tokens: "The model stopped at its output limit, so its answer may be cut.",
  finalize_tool: "A tool configured to end the turn returned its result, so no further model call was made.",
  waiting_on_tasks: "The agent stopped waiting for the background tasks it started before they finished; this is not a complete answer.",
  budget_exhausted: "The run's budget cut the answer short; this is not a complete answer.",
  degenerate_repetition: "The model got stuck repeating the same text, so the run was stopped; this is not a complete answer.",
  cancelled: "The call was cancelled.",
  timeout: "The call timed out.",
  unknown: "The reason the provider gave for stopping.",
} as const;

function stopReasonNote(reason: string): string {
  return Object.prototype.hasOwnProperty.call(STOP_REASON_NOTES, reason)
    ? STOP_REASON_NOTES[reason as keyof typeof STOP_REASON_NOTES]
    : STOP_REASON_NOTES.unknown;
}

/** The run page's own explanations, read by `t_i18n`: concepts no other page
 * shows. A concept shared with other pages lives in `lib/evaluation-glossary.ts`. @i18n-keys */
const RUN_PAGE_NOTES = {
  phase:
    "The step of the agent's pipeline this tool call ran in: prepare runs before the model answers, finalize after it.",
  waiting: "Time the agent spent waiting for the tasks it started to finish.",
  inputTokens: "Tokens the model read for this step.",
  outputTokens: "Tokens the model wrote for this step.",
  maxTokensBadge: "The model stopped at its output limit on this step. Open the step for what it means.",
  stepDetails: "Opens this step's prompts, answer, token usage and stop reason, when the run recorded them.",
  extracted:
    "What the evaluation's extractor took from the agent's answer. The quality metrics scored this value, not the raw answer.",
  outputFormat:
    "The format this page recognises in the text, to display it. It is not a setting of the agent or the dataset.",
  perFieldScoring:
    "How each field of the expected answer scored under Field Accuracy: its comparison mode, weight, score and result.",
  exactMatch: "Every compared field matches: none is missing, extra or incorrect.",
  fieldWeight: "This field's weight in Field Accuracy's weighted average; fields without a mark weigh 1.",
  fieldNotCompared: "Not compared, so it neither adds to nor lowers the score.",
  metricLeftOut:
    "This metric has no value on this entry, so the entry does not count in the metric's value for the run.",
  parameters:
    "The conditions this run was recorded under, one value per key, for comparing runs. A key set in several places keeps one value: given at launch, else the evaluation's, else captured automatically.",
} as const;

/** What the extractor's final value is, by its `output_kind`
 * (`services/evaluation/extraction/spec.py`), read by `t_i18n`. @i18n-keys */
const OUTPUT_KIND_NOTES = {
  object: "The extractor produced a JSON object.",
  array: "The extractor produced a JSON list.",
  label_set: "The extractor produced a set of labels, without duplicates and in a fixed order.",
  unknown: "The kind of value the extractor produced.",
} as const;

function outputKindNote(kind: string): string {
  return Object.prototype.hasOwnProperty.call(OUTPUT_KIND_NOTES, kind)
    ? OUTPUT_KIND_NOTES[kind as keyof typeof OUTPUT_KIND_NOTES]
    : OUTPUT_KIND_NOTES.unknown;
}

type TraceForDialog = {
  type?: string;
  description?: string;
  model?: string;
  provider?: string;
  tool_name?: string;
  phase?: string;
  tool_input?: string;
  tool_output?: string;
  stop_reason?: string | null;
  usage?: Record<string, number>;
  system_prompt?: string;
  user_prompt?: string;
  raw_response?: string;
  result_id?: string;
  trace_index?: number;
};

function JudgeTraceDialog({ runId, trace, open, onClose }: { runId: string; trace: TraceForDialog | null; open: boolean; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const [isDownloading, setIsDownloading] = useState(false);
  if (!trace) return null;
  const isTool = trace.type === "tool";
  const isJudge = !isTool && (trace.type === "judge" || !trace.type);
  const accentColor = isTool ? "text-feedback-warning-primary" : isJudge ? "text-filigran-ia-primary" : "text-accent";
  const TitleIcon = isTool ? Wrench : isJudge ? Zap : Brain;
  const titleLabel = isTool ? t_i18n("Tool Call Details") : isJudge ? t_i18n("Judge Execution Details") : t_i18n("LLM Call Details");

  // Token usage: OpenAI-style and Anthropic-style keys.
  const usage = trace.usage || {};
  const inputTokens = usage.input_tokens ?? usage.prompt_tokens ?? null;
  const outputTokens = usage.output_tokens ?? usage.completion_tokens ?? null;
  const totalTokens = usage.total_tokens ?? (inputTokens != null && outputTokens != null ? inputTokens + outputTokens : null);
  const hasUsage = inputTokens != null || outputTokens != null || totalTokens != null;
  const hasPromptPayload = !!(trace.system_prompt || trace.user_prompt || trace.raw_response);

  const isMaxTokens = trace.stop_reason === "max_tokens";

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-3xl max-h-[90vh] flex flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-2">
              <TitleIcon className={`h-4 w-4 ${accentColor}`} />
              {titleLabel}
            </span>
            <Button
              variant="outline"
              size="sm"
              className="h-7"
              disabled={
                isDownloading ||
                !trace.result_id ||
                trace.trace_index == null ||
                trace.trace_index < 0
              }
              onClick={async () => {
                if (!trace.result_id || trace.trace_index == null || trace.trace_index < 0) return;
                try {
                  setIsDownloading(true);
                  await downloadEvaluationRunResultTrace(runId, trace.result_id, trace.trace_index);
                } finally {
                  setIsDownloading(false);
                }
              }}
            >
              <Download className="h-3 w-3 mr-1" />
              {isDownloading ? t_i18n("Downloading...") : t_i18n("Download Full Trace")}
            </Button>
          </DialogTitle>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto space-y-5 pr-1">
          {/* Model / Provider / Stop reason row */}
          <div className="flex flex-wrap items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-4 py-3">
            <Cpu className={`h-4 w-4 ${accentColor} shrink-0`} />
            <div className="space-y-0.5">
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{isTool ? t_i18n("Tool") : t_i18n("Model")}</p>
              <p className="content-base font-mono-plex">{isTool ? (trace.tool_name || "—") : (trace.model || "—")}</p>
            </div>
            {isTool && trace.phase && (
              <>
                <div className="h-6 w-px bg-elevation-hover mx-2" />
                <div className="space-y-0.5">
                  <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">
                    <InfoLabel content={t_i18n(RUN_PAGE_NOTES.phase)}>{t_i18n("Phase")}</InfoLabel>
                  </p>
                  <p className="content-base font-mono-plex text-accent">{trace.phase}</p>
                </div>
              </>
            )}
            {trace.provider && (
              <>
                <div className="h-6 w-px bg-elevation-hover mx-2" />
                <div className="space-y-0.5">
                  <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Provider")}</p>
                  <p className={`content-base font-mono ${accentColor}`}>{trace.provider}</p>
                </div>
              </>
            )}
            {trace.stop_reason && (
              <>
                <div className="h-6 w-px bg-elevation-hover mx-2" />
                <div className="space-y-0.5">
                  <p className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Stop Reason")}</p>
                  <p className={`content-base font-mono ${isMaxTokens ? "text-feedback-warning-primary" : "text-default-secondary"}`}>
                    <InfoLabel content={t_i18n(stopReasonNote(trace.stop_reason))} className="inline-flex items-center gap-1">
                      {isMaxTokens && <StopCircle className="h-3.5 w-3.5" />}
                      {trace.stop_reason}
                    </InfoLabel>
                  </p>
                </div>
              </>
            )}
          </div>

          {/* Max-tokens warning */}
          {isMaxTokens && (
            <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] px-4 py-3">
              <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0 mt-0.5" />
              <p className="content-compact text-feedback-warning-primary">
                <strong>{t_i18n("Hard limit reached.")}</strong>{" "}
                {isJudge
                  ? t_i18n("The LLM judge stopped at its output budget, so its verdict is cut and counts as a judge failure. Replay the judge with a larger output budget, or raise the evaluation's Judge Output Budget.")
                  : t_i18n("The model stopped at its output limit. The agent asks it to continue a cut answer up to 3 times, so the answer is cut only if its last step stopped here too.")}
              </p>
            </div>
          )}

          {/* Token usage */}
          {hasUsage && (
            <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-4 py-3 space-y-2">
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider flex items-center gap-1"><Hash className="h-3 w-3" /> {t_i18n("Token Usage")}</p>
              <div className="flex flex-wrap gap-4">
                {inputTokens != null && (
                  <div className="space-y-0.5">
                    <p className="content-caption text-default-secondary">{t_i18n("Input")}</p>
                    <p className="content-base font-mono-plex text-accent">{inputTokens.toLocaleString()}</p>
                  </div>
                )}
                {outputTokens != null && (
                  <div className="space-y-0.5">
                    <p className="content-caption text-default-secondary">{t_i18n("Output")}</p>
                    <p className={`content-base font-mono ${isMaxTokens ? "text-feedback-warning-primary" : "text-feedback-success-primary"}`}>{outputTokens.toLocaleString()}</p>
                  </div>
                )}
                {totalTokens != null && (
                  <div className="space-y-0.5">
                    <p className="content-caption text-default-secondary">{t_i18n("Total")}</p>
                    <p className="content-base font-mono-plex">{totalTokens.toLocaleString()}</p>
                  </div>
                )}
              </div>
            </div>
          )}

          {!hasUsage && !hasPromptPayload && (
            <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-4 py-3">
              <p className="content-compact text-default-secondary">
                {isTool
                  ? t_i18n("This tool call is clickable for diagnosis, but this run did not capture input/output payload for this step.")
                  : t_i18n("This LLM step is clickable for diagnosis, but this run did not capture full prompt/response or token payload for this specific intermediate step.")}
              </p>
            </div>
          )}

          {/* Tool Input */}
          {trace.tool_input && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Tool Input")}</h4>
                <CopyButton text={trace.tool_input} />
              </div>
              <pre className="bg-elevation-hover rounded-sm p-3 max-h-64 overflow-auto content-compact text-default-secondary whitespace-pre-wrap break-words font-mono-plex">{trace.tool_input}</pre>
            </div>
          )}

          {/* Tool Output */}
          {trace.tool_output && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Tool Output")}</h4>
                <CopyButton text={trace.tool_output} />
              </div>
              <pre className="bg-elevation-hover rounded-sm p-3 max-h-64 overflow-auto content-compact text-feedback-success-primary whitespace-pre-wrap break-words font-mono-plex">{trace.tool_output}</pre>
            </div>
          )}

          {/* System Prompt */}
          {trace.system_prompt && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("System Prompt")}</h4>
                <CopyButton text={trace.system_prompt} />
              </div>
              <pre className="bg-elevation-hover rounded-sm p-3 max-h-48 overflow-auto content-compact text-default-secondary whitespace-pre-wrap break-words font-mono-plex">{trace.system_prompt}</pre>
            </div>
          )}

          {/* User Prompt */}
          {trace.user_prompt && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("User Prompt")}</h4>
                <CopyButton text={trace.user_prompt} />
              </div>
              <pre className="bg-elevation-hover rounded-sm p-3 max-h-64 overflow-auto content-compact text-default-secondary whitespace-pre-wrap break-words font-mono-plex">{trace.user_prompt}</pre>
            </div>
          )}

          {/* Raw Response */}
          {trace.raw_response && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Raw Response")}</h4>
                <CopyButton text={trace.raw_response} />
              </div>
              <pre className="bg-elevation-hover rounded-sm p-3 max-h-64 overflow-auto content-compact text-feedback-success-primary whitespace-pre-wrap break-words font-mono-plex">{trace.raw_response}</pre>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Status → label for the per-field JSON breakdown, read by `t_i18n`: catalog
 * keys `validate-i18n` checks. @i18n-keys */
const FIELD_STATUS_LABELS = {
  correct: "Correct",
  incorrect: "Incorrect",
  missing: "Missing",
  extra: "Extra",
  excluded: "Excluded",
} as const;

/** Status → colour, kept apart from the labels above, whose every value is a catalog key.
 *
 * `excluded` is neutral, not red: a field left out by configuration is not the
 * agent's failure. */
const FIELD_STATUS_CLASSES: Record<keyof typeof FIELD_STATUS_LABELS, string> = {
  correct: "border-feedback-success-primary/30 text-feedback-success-primary",
  incorrect: "border-feedback-error-primary/30 text-feedback-error-primary",
  missing: "border-feedback-alert-primary/30 text-feedback-warning-primary",
  extra: "border-filigran-ia-primary/30 text-filigran-ia-primary",
  excluded: "border-elevation-subtle-soft text-default-secondary",
};

function isFieldStatus(bucket: string): bucket is keyof typeof FIELD_STATUS_LABELS {
  return Object.prototype.hasOwnProperty.call(FIELD_STATUS_LABELS, bucket);
}

/** Which of the five buckets a reported field belongs to.
 *
 * `matched` splits into correct/incorrect on the score: a partially-credited
 * free-text field is matched but not correct, and that is the case a reader looks for. */
function fieldBucket(field: JsonFieldScore): string {
  if (field.status !== "matched") return field.status;
  return field.score === 1 ? "correct" : "incorrect";
}

/** Percent text for a field score, never rounding a partial score to a perfect one.
 *
 * `Math.round` alone would print a token-F1 of 0.9975 as "100%" beside an
 * "Incorrect" badge. The number is clamped instead of loosening the bucket: 100%
 * means perfect and 0% means nothing earned, both exactly. */
function formatFieldScore(score: number): string {
  const percent = Math.round(score * 100);
  if (percent === 100 && score < 1) return "99%";
  if (percent === 0 && score > 0) return "1%";
  return `${percent}%`;
}

/** A field result's badge, which says what the result means on focus or hover. */
function FieldStatusBadge({ bucket, className, children }: { bucket: string; className?: string; children: ReactNode }) {
  const { t_i18n } = useFormatter();
  const badge = (
    <Badge variant="outline" className={cn("text-[9px]", isFieldStatus(bucket) && FIELD_STATUS_CLASSES[bucket], className)}>
      {children}
    </Badge>
  );
  if (!isFieldStatus(bucket)) return badge;
  return <InfoLabel content={t_i18n(FIELD_STATUS_DESCRIPTIONS[bucket])}>{badge}</InfoLabel>;
}

function JsonFieldBreakdown({ detail }: { detail: JsonFieldAccuracyDetail | null }) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const fields = detail?.per_field ?? [];
  if (!detail || fields.length === 0) return null;

  const counts = ["correct", "incorrect", "missing", "extra", "excluded"]
    .map((bucket) => ({ bucket, n: fields.filter((f) => fieldBucket(f) === bucket).length }))
    .filter((c) => c.n > 0);

  return (
    <div className="rounded-sm border border-accent/20 bg-accent/[0.04] px-3 py-2 space-y-2">
      {/* The toggle holds the title only: the badges beside it explain themselves on
          focus, and a focusable element inside a button is unreachable. */}
      <div className="flex w-full flex-wrap items-center gap-2 content-base">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="flex items-center gap-2"
          aria-expanded={open}
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <span className="text-accent font-medium">{t_i18n("Per-Field Scoring")}</span>
          <span className="text-default-secondary">—</span>
          <span className="text-default-primary font-bold">{fields.length}</span>
          <span className="content-caption text-default-secondary">
            {fields.length === 1 ? t_i18n("field") : t_i18n("fields")}
          </span>
        </button>
        <InfoTip content={t_i18n(RUN_PAGE_NOTES.perFieldScoring)} label={t_i18n("What Per-Field Scoring shows")} />
        {detail.exact_object_match && (
          <InfoLabel content={t_i18n(RUN_PAGE_NOTES.exactMatch)}>
            <Badge variant="outline" className="text-[9px] border-feedback-success-primary/30 text-feedback-success-primary">
              {t_i18n("Exact match")}
            </Badge>
          </InfoLabel>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          {counts.map(({ bucket, n }) => (
            <FieldStatusBadge key={bucket} bucket={bucket}>
              {n} {isFieldStatus(bucket) ? t_i18n(FIELD_STATUS_LABELS[bucket]) : bucket}
            </FieldStatusBadge>
          ))}
        </div>
      </div>

      {open && (
        <div className="space-y-1 pt-1">
          {fields.map((field) => {
            const bucket = fieldBucket(field);
            const mode = fieldModeCopy(field.mode);
            return (
              <div
                key={field.pointer}
                className="flex items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2 py-1"
              >
                <span className="flex-1 truncate font-mono-plex text-[11px] text-default-primary">
                  {field.pointer}
                </span>
                {mode ? (
                  <InfoLabel content={t_i18n(mode.description)} className="content-caption whitespace-nowrap text-default-secondary">
                    {t_i18n(mode.label)}
                  </InfoLabel>
                ) : (
                  <span className="content-caption text-default-secondary">{field.mode}</span>
                )}
                {/* Only non-default weights show, so the changed ones stand out. */}
                {field.weight !== 1 && (
                  <InfoLabel content={t_i18n(RUN_PAGE_NOTES.fieldWeight)} className="content-caption font-mono-plex text-default-secondary">
                    ×{field.weight}
                  </InfoLabel>
                )}
                {/* `—`, never 0: a field that was not compared did not score zero. */}
                <span className="w-12 text-right font-mono-plex text-[11px] tabular-nums text-default-primary">
                  {field.score == null ? (
                    <InfoLabel content={t_i18n(RUN_PAGE_NOTES.fieldNotCompared)}>—</InfoLabel>
                  ) : (
                    formatFieldScore(field.score)
                  )}
                </span>
                <FieldStatusBadge bucket={bucket} className="shrink-0">
                  {isFieldStatus(bucket) ? t_i18n(FIELD_STATUS_LABELS[bucket]) : bucket}
                </FieldStatusBadge>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ResultRow({ runId, result, index, judgeEnabled, evaluationSlug, runDate, bundleReview = false, entryKey }: { runId: string; result: EvaluationResult; index: number; judgeEnabled: boolean; evaluationSlug: string; runDate: string; bundleReview?: boolean; entryKey?: string }) {
  const { t_i18n, intl } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  const [showJudge, setShowJudge] = useState(false);
  const [judgeTrace, setJudgeTrace] = useState<TraceForDialog | null>(null);
  const subScores = result.sub_scores as Record<string, number>;
  const executionDetails = result.execution_details;
  const executionTraces = executionDetails?.traces ?? [];
  const hasExecutionTimeline = executionTraces.length > 0 || executionDetails?.agent_duration_ms != null;

  // Read only from a `scored` outcome: an `unavailable` or `skipped` one carries no
  // fields, and an empty breakdown would suggest the answer was measured.
  const fieldAccuracyDetail = useMemo<JsonFieldAccuracyDetail | null>(() => {
    const outcome = result.execution_details?.metric_results?.["json.field_accuracy"];
    if (!outcome || outcome.status !== "scored") return null;
    return (outcome.detail as JsonFieldAccuracyDetail | undefined) ?? null;
  }, [result.execution_details]);

  // One of three mutually exclusive anomaly badges (confirmation-seeking,
  // platform error, transient error), or none.
  const anomalyBadge = useMemo(() => {
    if (!result.is_execution_anomaly) return null;
    if (result.execution_anomaly_kind === "confirmation_seeking") {
      return {
        label: t_i18n("Confirmation-seeking"),
        tooltip: judgeEnabled
          ? t_i18n("The agent asked for confirmation or offered options instead of completing the task autonomously. This result is still judged and counted toward run scores.")
          : t_i18n("The agent asked for confirmation or offered options instead of completing the task autonomously. This result still counts toward run scores (judging is off for this run)."),
      };
    }
    if (result.execution_anomaly_kind === "platform_error") {
      return {
        label: t_i18n("Anomaly"),
        tooltip: t_i18n("The platform could not run the agent (configuration, licence, quota or a platform error), so the entry says nothing about the agent. Excluded from run scores."),
      };
    }
    return {
      label: t_i18n("Anomaly"),
      tooltip: t_i18n("A transient execution or provider error surfaced as the output (timeout, tool crash). Excluded from run scores."),
    };
  }, [result.is_execution_anomaly, result.execution_anomaly_kind, judgeEnabled, t_i18n]);

  // Shown in the collapsed row header too: a failed extraction with no marker
  // reads like "not scored yet" or a genuine zero. The extracted value is
  // what was scored, so a surprising score can be traced to the answer or to the
  // extraction chain.
  const extractedPrediction = useMemo<ExtractedPrediction | null>(
    () => result.execution_details?.extracted_prediction ?? null,
    [result.execution_details],
  );

  const extractionFailure = useMemo<ExtractionFailure | null>(
    () =>
      (result.execution_details?.metric_results?.[EXTRACTION_FAILED_KEY]
        ?.error as ExtractionFailure | undefined) ?? null,
    [result.execution_details],
  );

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardContent className="p-4">
        {/* The toggle is a button, so the entry opens from the keyboard; the badges
            stay outside it, each a tab stop of its own. */}
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            aria-expanded={expanded}
            className="flex min-w-0 flex-1 items-center gap-3 text-left rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4 text-default-secondary shrink-0" />
            ) : (
              <ChevronRight className="h-4 w-4 text-default-secondary shrink-0" />
            )}
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm bg-elevation-hover content-caption font-mono-plex text-default-secondary">{index + 1}</span>
            {result.files && result.files.length > 0 && (
              <span className="flex items-center gap-1 shrink-0">
                {result.files.map((f) => (
                  <span key={f.id} className="inline-flex items-center gap-1 rounded-full bg-elevation-hover px-2 py-0.5 text-[11px] text-default-secondary">
                    <Paperclip className="h-2.5 w-2.5" />
                    <span className="max-w-[100px] truncate">{f.filename}</span>
                  </span>
                ))}
              </span>
            )}
            <span className="min-w-0 content-base line-clamp-1">{result.description || result.input_prompt}</span>
          </button>
          <div className="flex shrink-0 items-center gap-2">
            {/* A STIX bundle answer opens in the bundle review, object by object: on the
                entry's own line, so no entry has to be unfolded to find it. */}
            {bundleReview && (
              <Link
                to={entryKey ? `/evaluations/review?run=${runId}&entry=${encodeURIComponent(entryKey)}` : `/evaluations/review?results=${result.id}`}
                className="inline-flex items-center gap-1.5 rounded-sm content-compact font-medium text-accent hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
              >
                <Icon name="file-search" size={16} />
                {t_i18n("Review bundle")}
              </Link>
            )}
            {result.fell_back_to_llm_only && <FallbackEntryBadge />}
            <NeededApprovalBadge tools={result.execution_details?.approval_required_tools} />
            {result.is_reused && (
              <InfoLabel content={t_i18n(EVALUATION_TERMS.reusedEntry)}>
                <Badge className={cn("shrink-0 gap-1 text-[9px]", REUSE_BADGE_CONFIG.reused.color, REUSE_BADGE_CONFIG.reused.bg)}>
                  <Repeat className="h-2.5 w-2.5" />
                  {t_i18n("Reused")}
                </Badge>
              </InfoLabel>
            )}
            {anomalyBadge && (
              <InfoLabel content={anomalyBadge.tooltip}>
                <Badge className="shrink-0 gap-1 text-[9px] text-feedback-warning-tertiary bg-feedback-alert-primary/10">
                  <AlertTriangle className="h-2.5 w-2.5" />
                  {anomalyBadge.label}
                </Badge>
              </InfoLabel>
            )}
            {result.is_judge_failure && (
              <InfoLabel content={t_i18n("The LLM judge did not return a complete, gradeable verdict (parse failure, truncation, or missing sub-score). Excluded from run scores.")}>
                <Badge className="shrink-0 gap-1 text-[9px] text-feedback-error-primary bg-feedback-error-primary/10">
                  <Gavel className="h-2.5 w-2.5" />
                  {t_i18n("Judge failure")}
                </Badge>
              </InfoLabel>
            )}
            {extractionFailure && (
              <InfoLabel content={t_i18n("The configured extractor could not produce something to score. When the agent's answer caused it, the entry's quality metrics score 0; when the extractor's configuration did, the entry is left out of Overall Quality. Unfold the entry for the reason and the operation that failed.")}>
                <Badge className="shrink-0 gap-1 text-[9px] text-feedback-warning-tertiary bg-feedback-alert-primary/10">
                  <AlertTriangle className="h-2.5 w-2.5" />
                  {t_i18n("Extraction failed")}
                </Badge>
              </InfoLabel>
            )}
          </div>
          {/* An entry shows only what was measured on it. Overall
              Quality and Overall Performance aggregate across entries (and,
              for performance, across runs), so they have no per-entry value. */}
          <div className="flex items-stretch shrink-0">
            <div className="w-[120px] text-center border-l border-elevation-subtle-soft px-2.5 py-2.5">
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider mb-1">
                <InfoLabel content={t_i18n(EVALUATION_TERMS.entryJudgeScore)}>{t_i18n("Judge Score")}</InfoLabel>
              </p>
              {(() => {
                // `evaluation_results.score` is NOT NULL, so a judge-off entry
                // stores 0.0, which must not print as a real zero. `judgeEnabled`
                // is the run's own state: a rescore can turn the judge off.
                const excluded =
                  result.is_judge_failure ||
                  result.execution_anomaly_kind === "transient_error" ||
                  result.execution_anomaly_kind === "platform_error";
                const val = excluded || !judgeEnabled ? null : result.score;
                const reason = excluded
                  ? t_i18n("Excluded from scoring: see the badge on this entry.")
                  : !judgeEnabled
                    ? t_i18n("The LLM judge was not enabled for this run.")
                    : t_i18n("Not measured for this entry.");
                return (
                  <>
                    {val == null ? (
                      <InfoLabel content={reason} className="content-base leading-7 text-default-secondary italic font-sans-geologica">
                        {t_i18n("N/A")}
                      </InfoLabel>
                    ) : (
                      <>
                        <span className={cn("text-lg font-bold font-sans-geologica", headlineColorClass(val))}>{val}</span>
                        <span className="content-caption text-default-secondary ml-0.5">/100</span>
                      </>
                    )}
                  </>
                );
              })()}
            </div>
            <div className="w-[120px] text-center border-l border-elevation-subtle-soft px-2.5 py-2.5">
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider mb-1">
                <InfoLabel content={t_i18n(EVALUATION_TERMS.entryDuration)}>{t_i18n("Duration")}</InfoLabel>
              </p>
              {/* Elapsed seconds, not a score: performance is normalised
                  across runs, so an entry has no 0-100 rank. */}
              {result.duration_seconds != null ? (
                <>
                  <span className="text-lg font-bold text-default-primary font-sans-geologica">{result.duration_seconds.toFixed(1)}</span>
                  <span className="content-caption text-default-secondary ml-0.5">s</span>
                </>
              ) : (
                <span className="text-lg font-bold font-sans-geologica text-default-secondary">—</span>
              )}
            </div>
          </div>
        </div>

        {expanded && (
          <div className="mt-4 space-y-4 pl-14">
            {result.is_reused && (
              <div className="flex items-start gap-3 rounded-sm border border-feedback-success-primary/30 bg-feedback-success-primary/[0.06] px-3 py-2">
                <Repeat className="h-3.5 w-3.5 text-feedback-success-primary shrink-0 mt-0.5" />
                <div className="min-w-0 content-compact">
                  <p className="font-medium text-feedback-success-primary">{t_i18n("Reused prediction")}</p>
                  <p className="text-feedback-success-primary/70 mt-0.5 break-words">
                    {t_i18n(EVALUATION_TERMS.reusedEntry)}
                    {result.reused_from_result_id && (
                      <>
                        {" "}{t_i18n("Source result:")}{" "}
                        <code className="font-mono-plex text-feedback-success-primary/90">{result.reused_from_result_id}</code>
                      </>
                    )}
                  </p>
                </div>
              </div>
            )}
            <ExtractionFailureNotice
              failure={extractionFailure}
              metricResults={result.execution_details?.metric_results ?? null}
            />
            <QualityNotes
              metricResults={result.execution_details?.metric_results ?? null}
              runId={runId}
              resultId={result.id}
            />
            <ResultMetricsStrip
              runId={runId}
              resultId={result.id}
              metricResults={result.execution_details?.metric_results ?? null}
            />
            {/* Raw output, extracted value and ground truth side by side: it tells
                an author whether to reprompt the agent or repair the chain. */}
            <div
              className={cn(
                "grid gap-4 items-stretch",
                extractedPrediction ? "grid-cols-1 lg:grid-cols-3" : "grid-cols-2",
              )}
            >
              <div className="flex flex-col">
                <div className="flex items-center justify-between mb-1">
                  <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Agent Output")}</h4>
                  <div className="flex items-center gap-2">
                    <InfoLabel content={t_i18n(RUN_PAGE_NOTES.outputFormat)}>
                      <Badge variant="outline" className="text-[9px]">
                        {detectOutputFormat(result.agent_output).toUpperCase()}
                      </Badge>
                    </InfoLabel>
                    <DownloadButton text={result.agent_output} format={detectOutputFormat(result.agent_output)} filename={`${evaluationSlug}_${runDate}_agent-output-${index + 1}`} />
                    <CopyButton text={result.agent_output} />
                  </div>
                </div>
                <div className="flex-1"><AgentOutput text={result.agent_output} /></div>
              </div>
              {extractedPrediction && (
                <ExtractedPredictionPanel
                  extracted={extractedPrediction}
                  filename={`${evaluationSlug}_${runDate}_extracted-${index + 1}`}
                />
              )}
              <div className="flex flex-col">
                <div className="flex items-center justify-between mb-1">
                  <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">
                    <InfoLabel content={t_i18n(EVALUATION_TERMS.expectedAnswer)}>{t_i18n("Expected Answer")}</InfoLabel>
                  </h4>
                  <div className="flex items-center gap-2">
                    {result.expected_answer && (
                      <>
                        <InfoLabel content={t_i18n(RUN_PAGE_NOTES.outputFormat)}>
                          <Badge variant="outline" className="text-[9px]">
                            {detectOutputFormat(result.expected_answer).toUpperCase()}
                          </Badge>
                        </InfoLabel>
                        <DownloadButton text={result.expected_answer} format={detectOutputFormat(result.expected_answer)} filename={`${evaluationSlug}_${runDate}_expected-answer-${index + 1}`} />
                        <CopyButton text={result.expected_answer} />
                      </>
                    )}
                  </div>
                </div>
                <div className="flex-1">
                  {result.expected_answer ? (
                    <AgentOutput text={result.expected_answer} />
                  ) : (
                    <div className="bg-elevation-hover rounded-sm p-3 content-base text-default-secondary">—</div>
                  )}
                </div>
              </div>
            </div>

            {/* Per-field JSON breakdown: which field cost the points. */}
            <JsonFieldBreakdown detail={fieldAccuracyDetail} />

            <div className="space-y-2">
              <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">
                <InfoLabel content={t_i18n(EVALUATION_TERMS.judgeSubScores)}>{t_i18n("Sub-Scores")}</InfoLabel>
              </h4>
              {EVALUATION_SUB_SCORES.map((s) => (
                <SubScoreBar key={s.key} subScoreKey={s.key} label={t_i18n(s.label)} value={subScores[s.key]} color={s.color} />
              ))}
            </div>
            {result.evaluator_reasoning && (
              <div>
                <div className="flex items-center justify-between mb-1">
                  <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Judge Reasoning")}</h4>
                  <div className="flex items-center gap-1">
                    <Tip content={t_i18n("Open the judge reasoning in a larger view")}>
                      <Button aria-label={t_i18n("Open the judge reasoning in a larger view")} variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => setShowJudge(true)}>
                        <Scale className="h-3 w-3" />
                      </Button>
                    </Tip>
                    <ReplayJudgeButton runId={runId} result={result} />
                    <CopyButton text={result.evaluator_reasoning} />
                  </div>
                </div>
                <div className="bg-elevation-hover rounded-sm p-3 max-h-48 overflow-auto prose prose-invert prose-sm max-w-none prose-p:text-default-secondary prose-p:my-2 prose-p:whitespace-pre-line prose-headings:text-default-primary prose-headings:mt-3 prose-headings:mb-1 prose-strong:text-default-primary prose-li:text-default-secondary prose-li:whitespace-pre-line prose-ul:my-2 prose-ol:my-2">
                  <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{result.evaluator_reasoning}</ReactMarkdown>
                </div>
              </div>
            )}

            {/* Execution Timing: only on a result that recorded a timeline or an agent duration */}
            {executionDetails && hasExecutionTimeline && (() => {
              const d = executionDetails;
              const traces = executionTraces;
              const TRACE_COLORS: Record<string, { bg: string; text: string; icon: typeof Brain }> = {
                llm: { bg: "bg-accent/20", text: "text-accent", icon: Brain },
                tool: { bg: "bg-feedback-alert-primary/20", text: "text-feedback-warning-primary", icon: Wrench },
                judge: { bg: "bg-filigran-ia-primary/20", text: "text-filigran-ia-primary", icon: Zap },
                // The judge did not run on the entry: judge-side, never the agent's.
                judge_skipped: { bg: "bg-filigran-ia-primary/20", text: "text-filigran-ia-primary", icon: Zap },
                // The agent held on the tasks it spawned: no call, so no details.
                wait: { bg: "bg-elevation-hover", text: "text-default-secondary", icon: Hourglass },
              };

              return (
                <div className="space-y-3">
                  {/* Summary KPIs */}
                  <div className="flex items-center gap-4">
                    <h4 className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Execution")}</h4>
                    <div className="flex items-center gap-3 content-caption">
                      <InfoLabel content={t_i18n(EVALUATION_TERMS.agentTime)} className="flex items-center gap-1 text-accent"><Clock className="h-3 w-3" /> {t_i18n("Agent: {duration}s", { duration: ((d.agent_duration_ms ?? 0) / 1000).toFixed(2) })}</InfoLabel>
                      {d.judge_duration_ms != null && <InfoLabel content={t_i18n(EVALUATION_TERMS.judgeTime)} className="flex items-center gap-1 text-filigran-ia-primary"><Clock className="h-3 w-3" /> {t_i18n("Judge: {duration}s", { duration: (d.judge_duration_ms / 1000).toFixed(2) })}</InfoLabel>}
                      <InfoLabel content={t_i18n(EVALUATION_TERMS.totalTime)} className="text-default-secondary">{t_i18n("Total: {duration}s", { duration: ((d.total_duration_ms ?? 0) / 1000).toFixed(2) })}</InfoLabel>
                      {d.ttft_ms != null && <InfoLabel content={t_i18n(EVALUATION_TERMS.ttft)} className="flex items-center gap-1 text-feedback-success-primary"><Zap className="h-3 w-3" /> {t_i18n("TTFT: {duration}ms", { duration: d.ttft_ms })}</InfoLabel>}
                    </div>
                  </div>

                  {/* Gantt diagram */}
                  <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                    {/* Pointer-only on purpose: every segment is a step the Timeline below
                        lists, keyboard-reachable, with the same description and duration. */}
                    <div className="flex h-6 rounded-sm overflow-hidden" aria-hidden="true">
                      {(() => {
                        const agentTraces = traces.filter((t) => t.type !== "judge" && t.type !== "judge_skipped");
                        const agentTotal = agentTraces.reduce((s, t) => s + t.duration_ms, 0) || 1;
                        return agentTraces.map((t, i) => {
                          const pct = Math.max(1, (t.duration_ms / agentTotal) * 100);
                          const cfg = TRACE_COLORS[t.type] || TRACE_COLORS.llm;
                          return (
                            <Tip key={i} content={`${t.description} — ${t.duration_ms}ms`}>
                              <div className={cn("h-full transition-opacity hover:opacity-80 border-r border-elevation-subtle-soft last:border-0", cfg.bg)} style={{ width: `${pct}%` }} />
                            </Tip>
                          );
                        });
                      })()}
                    </div>
                    {/* Legend */}
                    <div className="flex items-center gap-3 mt-2">
                      <div className="flex items-center gap-1"><div className="w-3 h-2 rounded-sm bg-accent/20" /><span className="text-[9px] text-default-secondary">{t_i18n("LLM")}</span></div>
                      <div className="flex items-center gap-1"><div className="w-3 h-2 rounded-sm bg-feedback-alert-primary/20" /><span className="text-[9px] text-default-secondary">{t_i18n("Tool")}</span></div>
                      {traces.some((t) => t.type === "wait") && (
                        <div className="flex items-center gap-1"><div className="w-3 h-2 rounded-sm bg-elevation-hover" /><InfoLabel content={t_i18n(RUN_PAGE_NOTES.waiting)} className="text-[9px] text-default-secondary">{t_i18n("Waiting")}</InfoLabel></div>
                      )}
                    </div>
                  </div>

                  {/* Timeline detail list */}
                  <div className="space-y-2">
                    <h5 className="content-caption font-medium text-default-secondary uppercase tracking-wider">{t_i18n("Timeline")}</h5>
                    <div className="space-y-2">
                      {(() => {
                        let elapsed = 0;
                        return traces.map((t, i) => {
                          const cfg = TRACE_COLORS[t.type] || TRACE_COLORS.llm;
                          const Icon = cfg.icon;
                          const traceUsage = (t.usage || {}) as Record<string, number>;
                          const inTok = traceUsage.input_tokens ?? traceUsage.prompt_tokens ?? null;
                          const outTok = traceUsage.output_tokens ?? traceUsage.completion_tokens ?? null;
                          const isMaxTok = t.stop_reason === "max_tokens";
                          const hasDetails = !!(t.system_prompt || t.user_prompt || t.raw_response || t.tool_input || t.tool_output || inTok != null || outTok != null || t.stop_reason);
                          const canOpenDetails = t.type === "llm" || t.type === "tool" || hasDetails;
                          const startedAtMs = elapsed;
                          elapsed += t.duration_ms || 0;
                          const phase = t.phase ? String(t.phase).toUpperCase() : null;
                          const openDetails = () =>
                            setJudgeTrace({
                              ...(t as TraceForDialog),
                              result_id: result.id,
                              trace_index: i,
                            });
                          return (
                            // A click anywhere on the row opens its details, as before; the
                            // Details button inside is the keyboard's way to it.
                            <div
                              key={i}
                              onClick={
                                canOpenDetails
                                  ? (e) => {
                                      // Not from a control or an explanation inside the row (its
                                      // tooltip is a React child too), nor the end of a text selection.
                                      const target = e.target as Element;
                                      if (target.closest('button, a, [tabindex="0"], [role="tooltip"]')) return;
                                      if (window.getSelection()?.toString()) return;
                                      openDetails();
                                    }
                                  : undefined
                              }
                              className={cn(
                                "rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-2.5",
                                canOpenDetails && "cursor-pointer hover:bg-elevation-highlight transition-colors",
                                isMaxTok && "border-feedback-alert-primary/20"
                              )}
                            >
                              <div className="flex items-start gap-2">
                                <span className="mt-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-sm bg-elevation-hover px-1 content-caption font-mono-plex text-default-secondary">
                                  {i + 1}
                                </span>
                                <span className={cn("mt-0.5 flex h-5 w-5 items-center justify-center rounded-sm shrink-0", cfg.bg)}>
                                  <Icon className={cn("h-3 w-3", cfg.text)} />
                                </span>
                                <div className="min-w-0 flex-1">
                                  <p className="content-compact text-default-primary leading-snug break-words">{t.description}</p>
                                  <div className="mt-1 flex flex-wrap items-center gap-1.5">
                                    <InfoLabel content={t_i18n(EVALUATION_TERMS.stepStart)}>
                                      <Badge variant="outline" className="text-[9px]">T+{startedAtMs}ms</Badge>
                                    </InfoLabel>
                                    <Badge variant="outline" className="text-[9px]">{t.duration_ms}ms</Badge>
                                    {phase && (
                                      <InfoLabel content={t_i18n(RUN_PAGE_NOTES.phase)}>
                                        <Badge variant="outline" className="text-[9px] text-accent border-accent/30">{phase}</Badge>
                                      </InfoLabel>
                                    )}
                                    {t.tool_name && <Badge variant="outline" className="text-[9px]">{t.tool_name}</Badge>}
                                    {t.model && <Badge variant="outline" className="text-[9px]">{t.model}</Badge>}
                                    {inTok != null && (
                                      <InfoLabel content={t_i18n(RUN_PAGE_NOTES.inputTokens)}>
                                        <Badge variant="outline" className="text-[9px] text-accent border-accent/30"><Hash className="h-2.5 w-2.5 mr-0.5" />{t_i18n("{count} in", { count: intl.formatNumber(inTok) })}</Badge>
                                      </InfoLabel>
                                    )}
                                    {outTok != null && (
                                      <InfoLabel content={t_i18n(RUN_PAGE_NOTES.outputTokens)}>
                                        <Badge variant="outline" className={cn("text-[9px]", isMaxTok ? "text-feedback-warning-primary border-feedback-alert-primary/30" : "text-feedback-success-primary border-feedback-success-primary/30")}><Hash className="h-2.5 w-2.5 mr-0.5" />{t_i18n("{count} out", { count: intl.formatNumber(outTok) })}</Badge>
                                      </InfoLabel>
                                    )}
                                    {isMaxTok && (
                                      <InfoLabel content={t_i18n(RUN_PAGE_NOTES.maxTokensBadge)}>
                                        <Badge variant="outline" className="text-[9px] text-feedback-warning-primary border-feedback-alert-primary/30 flex items-center gap-0.5"><StopCircle className="h-2.5 w-2.5" />max_tokens</Badge>
                                      </InfoLabel>
                                    )}
                                    {canOpenDetails && (
                                      <Tip content={t_i18n(RUN_PAGE_NOTES.stepDetails)}>
                                        <button
                                          type="button"
                                          onClick={openDetails}
                                          className="rounded-sm border border-filigran-ia-primary/30 px-1.5 text-[9px] font-medium text-filigran-ia-primary hover:bg-elevation-highlight focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                                        >
                                          {t_i18n("Details")}
                                        </button>
                                      </Tip>
                                    )}
                                  </div>
                                </div>
                              </div>
                            </div>
                          );
                        });
                      })()}
                    </div>
                  </div>
                </div>
              );
            })()}
          </div>
        )}
      </CardContent>

      <JudgeTraceDialog runId={runId} trace={judgeTrace} open={!!judgeTrace} onClose={() => setJudgeTrace(null)} />

      <Dialog open={showJudge} onOpenChange={setShowJudge}>
        <DialogContent className="max-w-3xl max-h-[90vh] flex flex-col overflow-hidden">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Scale className="h-4 w-4" />
              {t_i18n("Judge Reasoning")}
            </DialogTitle>
          </DialogHeader>
          {result.evaluator_reasoning && (
            <div className="flex-1 overflow-y-auto prose prose-invert prose-sm max-w-none prose-p:text-default-secondary prose-p:my-1 prose-p:whitespace-pre-line prose-headings:text-default-primary prose-headings:mt-3 prose-headings:mb-1 prose-strong:text-default-primary prose-li:text-default-secondary prose-ul:my-1 prose-ol:my-1">
              <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{result.evaluator_reasoning}</ReactMarkdown>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Run Metrics panel: server-side per-metric stats, grouped by the headline each
// metric composes.
//
// Composition and measurement show together: the raw Mean does not reproduce the
// headline (quality penalises inconsistency, performance is normalised across the
// evaluation). The `Contribution` column is the missing term, read from persisted
// provenance, never recomputed here.
// ---------------------------------------------------------------------------

type MetricRowState =
  | "contributing"
  | "not_default"
  | "not_shared"
  | "not_applicable"
  | "failed"
  | "unclassified";

/** The state for a reason no consumer recognises. Named so the resolver and the
 * reachability check below cannot drift apart. */
const NEUTRAL_ROW_STATE = "unclassified" as const;

/** Exhaustive: an unrecognised reason must not fall through to a known state, since
 * a known state asserts a cause. Unknown reasons resolve to `NEUTRAL_ROW_STATE`.
 *
 * `satisfies` rather than a type annotation: a reason added to the vocabulary and not
 * mapped here fails to compile, and the literal value types `ReachableRowState` needs
 * are kept.
 */
const EXCLUSION_REASON_STATES = {
  failed: "failed",
  not_applicable: "not_applicable",
  not_a_default_contributor: "not_default",
  missing_from_other_runs: "not_shared",
  // Run-level: `buildMetricRows` drops them before any row is built. Mapped
  // only to satisfy the exhaustiveness check.
  no_performance_data: NEUTRAL_ROW_STATE,
  too_few_comparable_runs: NEUTRAL_ROW_STATE,
  no_metric_selected: NEUTRAL_ROW_STATE,
  only_opt_in_metrics_shared: NEUTRAL_ROW_STATE,
} as const satisfies Record<KnownExclusionReason, MetricRowState>;

function exclusionState(reason: string): MetricRowState {
  // Own-property check, not `??`: `EXCLUSION_REASON_STATES["toString"]` resolves up
  // the prototype chain to a function, which would skip the fallback.
  return Object.prototype.hasOwnProperty.call(EXCLUSION_REASON_STATES, reason)
    ? EXCLUSION_REASON_STATES[reason as KnownExclusionReason]
    : NEUTRAL_ROW_STATE;
}

/** Every state a row can actually be assigned: counted, mapped from a known reason, or
 * the neutral fallback. Keying the tables below on this rather than on `MetricRowState`
 * makes an unreachable state a compile error: an entry for a state no branch produces
 * is an excess property, and a reachable state with no entry is a missing one. */
type ReachableRowState =
  | "contributing"
  | (typeof EXCLUSION_REASON_STATES)[KnownExclusionReason]
  | typeof NEUTRAL_ROW_STATE;

/** Why a metric is in its state: distinct causes, each with a distinct remedy.
 *
 * `failed` may be recovered by a re-run, `not_applicable` never will be,
 * `not_default` is recovered by selecting the metric, and `not_shared` asks nothing
 * of this run: other runs lack the metric.
 *
 * There is no "de-selected" state: this page loads the run, not the evaluation, so it
 * cannot tell a de-selection from a metric of the other headline or an older run.
 * Those land in `unclassified`, which asserts no cause.
 *
 * The note under each state's badge, read by `t_i18n`: every value is a catalog key
 * `validate-i18n` checks. @i18n-keys
 */
const METRIC_ROW_NOTES = {
  contributing: "Counted: this metric contributed to the overall score above.",
  not_default: "Measured, but not counted: this metric is not counted by default, and this evaluation has not selected it. Select it under Overall Score Composition to count it.",
  not_shared: "Measured for this run, but not counted: some runs of this evaluation lack it, and Overall Performance counts only the metrics every run has.",
  not_applicable: "Not applicable to this run: a re-run cannot produce it.",
  failed: "The metric failed to compute for this run. A re-run may succeed.",
  unclassified: "Measured, but not counted towards the overall score above.",
} as const satisfies Record<ReachableRowState, string>;

/** The badge of each state, read by `t_i18n`: catalog keys too. Capitalized as the
 * badge shows them (`uppercase`): the lowercase `failed` key is a count suffix elsewhere
 * ("3 failed"). @i18n-keys */
const METRIC_ROW_LABELS = {
  contributing: "Counted",
  not_default: "Not counted",
  not_shared: "Not counted",
  not_applicable: "N/A",
  failed: "Failed",
  unclassified: "Not counted",
} as const satisfies Record<ReachableRowState, string>;

/** Each state's badge colours. Kept apart from the two tables above, whose every value is a catalog key. */
const METRIC_ROW_TONES: Record<ReachableRowState, string> = {
  contributing: "text-feedback-success-primary border-feedback-success-primary/25 bg-feedback-success-primary/10",
  not_default: "text-default-secondary border-elevation-subtle-soft bg-elevation-hover",
  not_shared: "text-default-secondary border-elevation-subtle-soft bg-elevation-hover",
  not_applicable: "text-default-secondary border-elevation-subtle-soft bg-elevation-hover",
  failed: "text-feedback-error-primary border-feedback-error-primary/25 bg-feedback-error-primary/10",
  unclassified: "text-default-secondary border-elevation-subtle-soft bg-elevation-hover",
};

interface MetricRow extends MetricAggregate {
  state: MetricRowState;
  contribution?: number;
}

function buildMetricRows(
  aggregates: MetricAggregate[],
  contributingKeys: string[],
  excluded: MetricExclusion[],
  contributions: Record<string, number>,
): MetricRow[] {
  const counted = new Set(contributingKeys);
  // Run-level exclusions describe a run, not a metric, so they never become a row.
  // Filtered explicitly rather than relying on the sentinel key never matching.
  const excludedByKey = new Map(
    excluded.filter((e) => e.key !== RUN_LEVEL_EXCLUSION_KEY).map((e) => [e.key, e]),
  );

  const rows: MetricRow[] = aggregates.map((a) => {
    const ex = excludedByKey.get(a.key);
    const state: MetricRowState = counted.has(a.key)
      ? "contributing"
      : ex
        ? exclusionState(ex.reason)
        : "unclassified";
    return { ...a, state, contribution: contributions[a.key] };
  });

  // A selected metric can be excluded without producing any aggregate; it still
  // gets a row, so the headline's disclosure covers the metric that yielded nothing.
  const measured = new Set(aggregates.map((a) => a.key));
  for (const e of excluded) {
    // Same guard as `excludedByKey`: otherwise the run-level sentinel renders as a
    // fake "__run__" metric with N=0.
    if (e.key === RUN_LEVEL_EXCLUSION_KEY) continue;
    if (measured.has(e.key)) continue;
    rows.push({
      key: e.key,
      half: null,
      mean: null,
      p50: null,
      p95: null,
      min: null,
      max: null,
      count: 0,
      state: exclusionState(e.reason),
    });
  }
  // One order whatever the state, so an unmeasured metric does not move to the end.
  // By code point, the order the aggregates arrive in.
  return rows.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
}

/** A column header that explains itself on focus or hover; `notes` are the
 * sentences only some tables need, each its own paragraph. */
function ColumnHeader({ column, className, notes = [], children }: { column: keyof typeof METRIC_COLUMN_DESCRIPTIONS; className: string; notes?: string[]; children: ReactNode }) {
  const { t_i18n } = useFormatter();
  const description = t_i18n(METRIC_COLUMN_DESCRIPTIONS[column]);
  return (
    <th className={cn("font-medium uppercase tracking-wider pb-2", className)}>
      <InfoLabel
        content={
          notes.length === 0 ? (
            description
          ) : (
            <span className="block space-y-1">
              {[description, ...notes].map((sentence) => (
                <span key={sentence} className="block">{sentence}</span>
              ))}
            </span>
          )
        }
      >
        {children}
      </InfoLabel>
    </th>
  );
}

type MeanBasis = keyof typeof METRIC_MEAN_BASIS_NOTES;

/** Whether the run's value is computed otherwise than the Mean column: the
 * package pools the entries' counts or macro-averages the classes. */
function meanBasis(row: MetricAggregate): MeanBasis | null {
  return row.macro ? "macro" : row.pooled ? "pooled" : null;
}

/** `showState` is off for telemetry, which composes no overall score: a "Not counted"
 * badge on each of its rows would say nothing the group's caption does not. */
function MetricRowsTable({ rows, showContribution, showState = true }: { rows: MetricRow[]; showContribution: boolean; showState?: boolean }) {
  const { t_i18n } = useFormatter();
  // Said once on the Mean header, for the kinds this table holds, rather than beside
  // each value, where it read as "this value is wrong".
  const bases = new Set(rows.map(meanBasis));
  const meanNotes = (Object.keys(METRIC_MEAN_BASIS_NOTES) as MeanBasis[])
    .filter((basis) => bases.has(basis))
    .map((basis) => t_i18n(METRIC_MEAN_BASIS_NOTES[basis]));
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-default-secondary text-left">
            <th className="font-medium uppercase tracking-wider pb-2 pr-3">{t_i18n("Metric")}</th>
            {showContribution && (
              <ColumnHeader column="contribution" className="px-3 text-right">{t_i18n("Contribution")}</ColumnHeader>
            )}
            <ColumnHeader column="mean" className="px-3 text-right" notes={meanNotes}>{t_i18n("Mean")}</ColumnHeader>
            <ColumnHeader column="p50" className="px-3 text-right">P50</ColumnHeader>
            <ColumnHeader column="p95" className="px-3 text-right">P95</ColumnHeader>
            <ColumnHeader column="min" className="px-3 text-right">{t_i18n("Min")}</ColumnHeader>
            <ColumnHeader column="max" className="px-3 text-right">{t_i18n("Max")}</ColumnHeader>
            <ColumnHeader column="n" className="pl-3 text-right">N</ColumnHeader>
          </tr>
        </thead>
        <tbody className="font-mono-plex">
          {rows.map((r) => {
            const basis = meanBasis(r);
            return (
              <tr key={r.key} className="border-t border-elevation-subtle-soft">
                <td className="py-1.5 pr-3 font-sans-plex">
                  <span className="flex items-center gap-1.5">
                    <MetricName metricKey={r.key} className={cn(r.state === "contributing" ? "text-default-primary" : "text-default-secondary")} />
                    {showState && (
                      <InfoLabel
                        content={t_i18n(METRIC_ROW_NOTES[r.state])}
                        className={cn("text-[9px] uppercase tracking-wider border rounded-sm px-1 py-px", METRIC_ROW_TONES[r.state])}
                      >
                        {t_i18n(METRIC_ROW_LABELS[r.state])}
                      </InfoLabel>
                    )}
                    {/* Plain text: the Mean header says what it means. */}
                    {basis && (
                      <span className="text-[9px] uppercase tracking-wider text-default-secondary">
                        {t_i18n(METRIC_MEAN_BASIS_LABELS[basis])}
                      </span>
                    )}
                  </span>
                </td>
                {showContribution && (
                  <td className={cn("py-1.5 px-3 text-right", r.contribution === undefined ? "text-default-secondary" : "text-feedback-success-primary")}>
                    {r.contribution === undefined ? "—" : r.contribution.toFixed(2)}
                  </td>
                )}
                <td className="py-1.5 px-3 text-right">{formatRunMetricValue(r.key, r.mean, r.unit)}</td>
                <td className="py-1.5 px-3 text-right">{formatRunMetricValue(r.key, r.p50, r.unit)}</td>
                <td className="py-1.5 px-3 text-right">{formatRunMetricValue(r.key, r.p95, r.unit)}</td>
                <td className="py-1.5 px-3 text-right">{formatRunMetricValue(r.key, r.min, r.unit)}</td>
                <td className="py-1.5 px-3 text-right">{formatRunMetricValue(r.key, r.max, r.unit)}</td>
                <td className="py-1.5 pl-3 text-right text-default-secondary">{r.count}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function RunMetricsPanel({ run }: { run: EvaluationRun }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useRunMetricsAggregate(run.id);
  const [open, setOpen] = useState(false);

  const aggregates = useMemo(() => data?.aggregates || [], [data]);

  // Grouped by headline, never flattened: which metrics produced a number
  // has a different answer for each headline.
  const groups = useMemo(() => {
    const halves = [
      {
        half: "quality" as const,
        headline: qualityHeadline(run),
        keys: run.quality_metric_keys ?? [],
        excluded: run.quality_aggregation_excluded ?? [],
        contributions: run.quality_contributions ?? {},
      },
      {
        half: "performance" as const,
        headline: performanceHeadline(run),
        keys: run.performance_metric_keys ?? [],
        excluded: run.performance_aggregation_excluded ?? [],
        contributions: run.performance_contributions ?? {},
      },
    ].map((g) => ({
      ...g,
      rows: buildMetricRows(
        aggregates.filter((a) => a.half === g.half),
        g.keys,
        g.excluded,
        g.contributions,
      ),
      // Older runs have no persisted contributions. The column is omitted rather
      // than shown all "—" or fabricated here.
      hasContributions: Object.keys(g.contributions).length > 0,
      // Runs the comparison basis set aside. Performance only: quality is
      // absolute and per-run.
      setAside:
        g.half === "performance"
          ? (
              g.excluded.find(
                (e) =>
                  e.key === RUN_LEVEL_EXCLUSION_KEY &&
                  e.reason === "no_performance_data",
              )?.run_ids ?? []
            ).length
          : 0,
    }));
    return halves.filter((g) => g.rows.length > 0);
  }, [aggregates, run]);

  // Execution telemetry composing neither headline: kept visible for debugging,
  // but apart, so it is not mistaken for a metric left out of a headline.
  //
  // Falsy rather than `=== null`: a row whose `half` is `undefined` (dropped at the
  // serialisation boundary) must still land somewhere instead of vanishing.
  const otherRows = useMemo(
    () => buildMetricRows(aggregates.filter((a) => !a.half), [], [], {}),
    [aggregates],
  );

  if (isLoading || aggregates.length === 0) return null;

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-4 py-3 text-left content-base font-semibold"
      >
        {open ? <ChevronDown className="h-4 w-4 text-default-secondary" /> : <ChevronRight className="h-4 w-4 text-default-secondary" />}
        <BarChart3 className="h-4 w-4 text-accent" />
        {t_i18n("Metrics")}
        <CountBadge variant="outline">{aggregates.length}</CountBadge>
      </button>
      {open && (
        <CardContent className="pt-0 space-y-5">
          {groups.map(({ headline, keys, rows, hasContributions, setAside }) => (
            <div key={headline.id} className="space-y-2">
              {/* The headline value sits on its group, so the rows below read as
                  the explanation of that number. */}
              <div className="flex items-baseline gap-2 flex-wrap">
                {!headline.legacy && (
                  <span aria-hidden className="inline-block w-2 h-2 rounded-full self-center" style={{ backgroundColor: headlineDef(headline.id).color }} />
                )}
                <p className="content-compact font-semibold">{t_i18n(headline.label)}</p>
                <span className={cn("content-base font-bold font-sans-geologica", headline.legacy ? "text-default-secondary" : headlineColorClass(headline.value))}>
                  {headline.value === null ? t_i18n("N/A") : headline.value}
                </span>
                <span className="content-caption text-default-secondary">
                  {headline.value === null
                    ? t_i18n("no metric contributed")
                    : t_i18n("mean of {count} {metrics}", {
                        count: keys.length,
                        metrics: keys.length === 1 ? t_i18n("metric") : t_i18n("metrics"),
                      })}
                </span>
                <InfoTip content={t_i18n(headline.note)} label={t_i18n("What {score} is", { score: t_i18n(headline.label) })} />
              </div>
              {/* Shown on a scored run too, not only when the headline is N/A: the
                  comparison behind the number excluded runs. */}
              {setAside > 0 && (
                <p className="content-caption text-default-secondary">
                  {t_i18n(
                    "{count, plural, one {# run} other {# runs}} recorded no performance metric and were left out of this comparison.",
                    { count: setAside },
                  )}
                </p>
              )}
              <MetricRowsTable rows={rows} showContribution={hasContributions} />
            </div>
          ))}
          {otherRows.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-baseline gap-2">
                <p className="content-compact font-semibold text-default-secondary">{t_i18n("Execution Telemetry")}</p>
                <span className="content-caption text-default-secondary">
                  {t_i18n("composes no overall score")}
                </span>
              </div>
              <MetricRowsTable rows={otherRows} showContribution={false} showState={false} />
            </div>
          )}
        </CardContent>
      )}
    </Card>
  );
}

// Run Tags panel: mutable run-level tags. Baseline is agent-scoped, so it shows
// as a read-only indicator from the run's agent-derived `is_baseline`.
// ---------------------------------------------------------------------------

function RunTagsPanel({
  runId,
  canManage,
  isBaseline = false,
  effectiveTags,
}: {
  runId: string;
  canManage: boolean;
  isBaseline?: boolean;
  effectiveTags?: Record<string, string>;
}) {
  const { t_i18n } = useFormatter();
  const { data } = useRunTags(runId);
  const patchTags = usePatchRunTags();
  const aiOff = useAiActionDisabled();
  const aiOffReason = useAiDisabledReason();
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");

  const tags = data?.tags || {};
  const entries = Object.entries(tags);
  const newKeyReserved = newKey.trim() === RUN_TAG_BASELINE_KEY;

  // Inherited tags: keys from the evaluation or agent level that the run does not
  // override. Read-only, apart from the editable run-level tags.
  const inheritedEntries = Object.entries(effectiveTags || {}).filter(
    ([key]) => !(key in tags),
  );

  const applyPatch = (patch: Record<string, string | null>) => {
    patchTags.mutate(
      { runId, tags: patch },
      { onError: (e) => toast({ title: t_i18n("Failed to update tags"), description: e.message, variant: "error" }) },
    );
  };

  const addTag = () => {
    const key = newKey.trim();
    if (!key || newKeyReserved) return;
    applyPatch({ [key]: newValue.trim() || "true" });
    setNewKey("");
    setNewValue("");
  };

  const removeTag = (key: string) => applyPatch({ [key]: null });

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      {/* Collapsible like Parameters and Metrics, which share its column.
          Never hidden when empty: it carries the editor that adds the first tag. */}
      {/* The toggle holds the title only: the explanations beside it are tab stops
          of their own, which a button cannot contain. */}
      <div className="flex items-center gap-2 px-4 py-3">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-2 text-left content-base font-semibold"
        >
          {open ? <ChevronDown className="h-4 w-4 text-default-secondary" /> : <ChevronRight className="h-4 w-4 text-default-secondary" />}
          <Tag className="h-4 w-4 text-accent" />
          {t_i18n("Run Tags")}
          <CountBadge variant="outline">{entries.length + inheritedEntries.length}</CountBadge>
        </button>
        <InfoTip content={t_i18n(EVALUATION_TERMS.runTags)} label={t_i18n("What run tags are")} />
        {/* Baseline stays in the collapsed header: it qualifies the run
            itself, not its tags. */}
        {isBaseline && (
          <InfoLabel content={t_i18n(EVALUATION_TERMS.baselineRun)}>
            <Badge variant="outline" className="gap-1 content-caption border-feedback-alert-primary/30 text-feedback-warning-primary">
              <Star className="h-2.5 w-2.5 fill-feedback-alert-primary text-feedback-warning-primary" />
              {t_i18n("Baseline")}
            </Badge>
          </InfoLabel>
        )}
      </div>
      {open && (
      <CardContent className="pt-0 space-y-3">
        {entries.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {entries.map(([key, value]) => (
              <Badge
                key={key}
                variant="outline"
                className="gap-1 content-caption font-mono-plex"
              >
                {`${key}: ${value}`}
                {canManage && (
                  <Tip content={aiOffReason ?? t_i18n("Remove tag {key}", { key })} wrap="auto">
                    <button
                      type="button"
                      onClick={() => removeTag(key)}
                      disabled={patchTags.isPending || aiOff}
                      className="hover:text-feedback-error-primary transition-colors"
                      aria-label={t_i18n("Remove tag {key}", { key })}
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </Tip>
                )}
              </Badge>
            ))}
          </div>
        ) : (
          <p className="content-compact text-default-secondary">{t_i18n("No tags.")}</p>
        )}
        {inheritedEntries.length > 0 && (
          <div className="space-y-1.5">
            <span className="content-caption font-medium uppercase tracking-wider text-default-secondary">
              {t_i18n("Inherited")}
            </span>
            <div className="flex flex-wrap gap-1.5">
              {inheritedEntries.map(([key, value]) => (
                <InfoLabel key={key} content={t_i18n("Inherited from the evaluation or agent: edit at that level.")}>
                  <Badge
                    variant="outline"
                    className="gap-1 content-caption font-mono-plex border-dashed text-default-secondary"
                  >
                    {`${key}: ${value}`}
                  </Badge>
                </InfoLabel>
              ))}
            </div>
          </div>
        )}
        {canManage && (
          <div className="flex items-center gap-2">
            <Tip content={newKeyReserved ? t_i18n("\"baseline\" is reserved: set the baseline agent on the evaluation.") : undefined}>
              <Input aria-label={t_i18n("key")}
                value={newKey}
                onChange={(e) => setNewKey(e.target.value)}
                placeholder={t_i18n("key")}
                className="h-8 content-compact"
                invalidTone="alert"
                aria-invalid={newKeyReserved || undefined}
                onKeyDown={(e) => e.key === "Enter" && !aiOff && addTag()}
              />
            </Tip>
            <Input aria-label={t_i18n("value (optional)")}
              value={newValue}
              onChange={(e) => setNewValue(e.target.value)}
              placeholder={t_i18n("value (optional)")}
              className="h-8 content-compact"
              onKeyDown={(e) => e.key === "Enter" && !aiOff && addTag()}
            />
            <Tip content={aiOffReason ?? t_i18n("Add tag")} wrap="auto">
              <Button aria-label={t_i18n("Add tag")} size="sm" variant="outline" onClick={addTag} disabled={!newKey.trim() || newKeyReserved || patchTags.isPending || aiOff} className="h-8 shrink-0">
                <Plus className="h-3.5 w-3.5" />
              </Button>
            </Tip>
          </div>
        )}
      </CardContent>
      )}
    </Card>
  );
}

// Per-result metrics strip: scalar metrics for one result
// ---------------------------------------------------------------------------

/** The three buckets an entry's metrics fall into, in the run panel's order.
 *
 * Named after the headline each metric feeds: an entry has no headline of its own.
 * The headlines are computed across entries (and, for performance, across runs), so
 * a value here is one input to them, not a copy, and averaging the columns by eye
 * does not give the headline.
 */
const RESULT_METRIC_GROUPS: {
  id: keyof typeof RESULT_METRIC_GROUP_COPY;
  half: "quality" | "performance" | null;
}[] = [
  { id: "quality", half: "quality" },
  { id: "performance", half: "performance" },
  { id: "execution", half: null },
];

/** Each group's title and note, read by `t_i18n`: catalog keys `validate-i18n` checks,
 * kept apart from the colours above. @i18n-keys */
const RESULT_METRIC_GROUP_COPY = {
  quality: {
    title: "Quality",
    note: "Measured on this entry. Overall Quality is aggregated over the counted entries, from these and from the metrics only the run's Metrics panel shows.",
  },
  performance: {
    title: "Performance",
    note: "Measured on this entry, in native units. Overall Performance is aggregated over the counted entries, then normalised across this evaluation's runs.",
  },
  execution: {
    title: "Execution",
    note: "Execution telemetry. Composes neither headline.",
  },
} as const;

// PRF metric breakdown — precision/recall and TP/FP/FN read from the
// outcome's persisted `detail`, never recomputed and never combined across
// results.
// ---------------------------------------------------------------------------

type MetricDetailKind =
  | { kind: "diagnostic" }
  | { kind: "by_type"; detail: StixByTypePrfDetail }
  | { kind: "prf"; detail: StixPrfDetail }
  | { kind: "none" };

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** `typeof [] === "object"`, so an array would otherwise be read as a record:
 *  an array `classes` classifies as by-type and renders rows keyed "0", "1". */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Decide which breakdown shape a metric outcome's `detail` carries.
 *
 * The diagnostic shape is tested first: a malformed STIX prediction is persisted
 * as `status: "scored"` with value 0.0, so the `status !== "scored"` guard
 * does not exclude it, and it would otherwise reach a PRF renderer.
 *
 * Detection is structural, never keyed on the metric id: payload shape is a package
 * fact, and a list of known keys would misfile a metric named differently. Each
 * branch verifies every field it renders, so a partial payload yields no disclosure
 * instead of a half-empty one.
 */
function classifyMetricDetail(detail: unknown): MetricDetailKind {
  if (!isPlainObject(detail)) return { kind: "none" };
  const d = detail;
  if (typeof d.reason === "string") return { kind: "diagnostic" };
  if (!isFiniteNumber(d.precision) || !isFiniteNumber(d.recall) || !isFiniteNumber(d.f1)) {
    return { kind: "none" };
  }
  // Per-class counts are `classes`, `per_type` on an older result.
  const perClass = d.classes !== undefined ? d.classes : d.per_type;
  if (perClass !== undefined) {
    if (!isPlainObject(perClass)) return { kind: "none" };
    const entries = Object.values(perClass);
    // Each class must carry its own F1 to be listed: a macro breakdown without one
    // (classification's) shows its overall scores only.
    const complete =
      entries.length > 0 &&
      entries.every(
        (entry) =>
          isPlainObject(entry) &&
          isFiniteNumber(entry.tp) &&
          isFiniteNumber(entry.fp) &&
          isFiniteNumber(entry.fn) &&
          isFiniteNumber(entry.f1),
      );
    return complete
      ? {
          kind: "by_type",
          detail: {
            precision: d.precision,
            recall: d.recall,
            f1: d.f1,
            classes: perClass as Record<string, StixPerTypeEntry>,
          },
        }
      : { kind: "none" };
  }
  if (isFiniteNumber(d.tp) && isFiniteNumber(d.fp) && isFiniteNumber(d.fn)) {
    return { kind: "prf", detail: d as unknown as StixPrfDetail };
  }
  return { kind: "none" };
}

/** The names of a precision/recall breakdown's figures, read by `t_i18n`. @i18n-keys */
const PRF_LABELS = {
  precision: "Precision",
  recall: "Recall",
  f1: "F1",
  tp: "TP",
  fp: "FP",
  fn: "FN",
} as const;

/** What each figure of a precision/recall breakdown means, read by `t_i18n`. @i18n-keys */
const PRF_NOTES = {
  precision: "The share of the items in the answer that are expected.",
  recall: "The share of the expected items that the answer contains.",
  f1: "Precision and recall combined into one score (their harmonic mean): high only when both are.",
  tp: "True positives: expected items the answer contains.",
  fp: "False positives: items in the answer that are not expected.",
  fn: "False negatives: expected items the answer misses.",
  perClass: "One class on this entry (an entity type or a label): its own F1, then its true positives, false positives and false negatives.",
} as const;

function PrfScoreGrid({ precision, recall, f1 }: { precision: number; recall: number; f1: number }) {
  const { t_i18n } = useFormatter();
  const scores: ["precision" | "recall" | "f1", number][] = [
    ["precision", precision],
    ["recall", recall],
    ["f1", f1],
  ];
  return (
    <div className="grid grid-cols-3 gap-1">
      {scores.map(([label, value]) => (
        <div key={label} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2 py-1">
          <p className="text-[9px] font-medium uppercase tracking-wider text-default-secondary">
            <InfoLabel content={t_i18n(PRF_NOTES[label])}>{t_i18n(PRF_LABELS[label])}</InfoLabel>
          </p>
          <p className="text-[11px] font-mono-plex">{formatPercentValue(value)}</p>
        </div>
      ))}
    </div>
  );
}

// The design system's feedback tokens, which read in both themes.
const PRF_COUNT_STYLE: Record<"tp" | "fp" | "fn", string> = {
  tp: "border-feedback-success-primary/30 text-feedback-success-primary",
  fp: "border-feedback-warning-primary/30 text-feedback-warning-primary",
  fn: "border-feedback-error-primary/30 text-feedback-error-primary",
};

function PrfCounts({ tp, fp, fn }: { tp: number; fp: number; fn: number }) {
  const { t_i18n } = useFormatter();
  const counts: ["tp" | "fp" | "fn", number][] = [
    ["tp", tp],
    ["fp", fp],
    ["fn", fn],
  ];
  return (
    <div className="flex items-center gap-1.5">
      {counts.map(([label, value]) => (
        <InfoLabel key={label} content={t_i18n(PRF_NOTES[label])}>
          <Badge variant="outline" className={cn("text-[9px]", PRF_COUNT_STYLE[label])}>
            {t_i18n(PRF_LABELS[label])} {formatCountValue(value)}
          </Badge>
        </InfoLabel>
      ))}
    </div>
  );
}

function ByTypePrfRows({ perType }: { perType: Record<string, StixPerTypeEntry> }) {
  const { t_i18n } = useFormatter();
  // Arrival order: the backend already sorts the classes, and reordering here would
  // not match the stored payload.
  return (
    <div className="space-y-1">
      {Object.entries(perType).map(([stixType, entry]) => (
        <div
          key={stixType}
          className="flex items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2 py-1"
        >
          <InfoLabel content={t_i18n(PRF_NOTES.perClass)} className="text-[11px] font-mono-plex break-all">{stixType}</InfoLabel>
          <span className="ml-auto text-[11px] font-mono-plex">{formatPercentValue(entry.f1)}</span>
          <PrfCounts tp={entry.tp} fp={entry.fp} fn={entry.fn} />
        </div>
      ))}
    </div>
  );
}

function MetricCell({ metric, detail }: { metric: RunMetric; detail: unknown }) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const classified = useMemo(() => classifyMetricDetail(detail), [detail]);

  const label = (
    <MetricName metricKey={metric.key} entryLabel={metric.entry_label} className="text-[11px] text-default-secondary break-all" />
  );
  const value = (
    <span className="text-[11px] font-mono-plex break-all text-right">{formatRunMetricValue(metric.key, metric.value, metric.unit)}</span>
  );

  if (classified.kind !== "prf" && classified.kind !== "by_type") {
    return (
      <div className="flex items-baseline justify-between gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2.5 py-1.5">
        {label}
        {value}
      </div>
    );
  }

  return (
    <div
      className={cn(
        "rounded-sm border border-elevation-subtle-soft bg-elevation-hover",
        // Full row while open: squeezed into one column of the two-column grid,
        // the breakdown is unreadable.
        open && "sm:col-span-2",
      )}
    >
      {/* The name stays outside the toggle: it explains itself on focus, and a
          focusable element inside a button is unreachable. */}
      <div className="flex w-full items-baseline justify-between gap-2 px-2.5 py-1.5">
        {label}
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-label={t_i18n("{value}: show the precision, recall and counts of {metric}", {
            value: formatRunMetricValue(metric.key, metric.value, metric.unit),
            metric: metricEntryDisplayName(metric.key, t_i18n, metric.entry_label),
          })}
          className="flex items-baseline gap-1.5 rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
        >
          {value}
          {open ? (
            <ChevronDown className="h-3 w-3 shrink-0 text-default-secondary" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0 text-default-secondary" />
          )}
        </button>
      </div>
      {open && (
        <div className="space-y-1.5 border-t border-elevation-subtle-soft px-2.5 py-2">
          <PrfScoreGrid
            precision={classified.detail.precision}
            recall={classified.detail.recall}
            f1={classified.detail.f1}
          />
          {classified.kind === "prf" ? (
            <PrfCounts tp={classified.detail.tp} fp={classified.detail.fp} fn={classified.detail.fn} />
          ) : (
            <ByTypePrfRows perType={classified.detail.classes} />
          )}
        </div>
      )}
    </div>
  );
}

function ResultMetricsStrip({
  runId,
  resultId,
  metricResults,
}: {
  runId: string;
  resultId: string;
  metricResults?: Record<string, PersistedMetricResult> | null;
}) {
  const { t_i18n } = useFormatter();
  const { data } = useResultMetrics(runId, resultId);
  // A metric whose entry value means nothing on its own stays at run level: the
  // package says so, the page does not decide by key.
  const metrics = useMemo(() => (data || []).filter((m) => m.shown_on_entry !== false), [data]);

  // Same split as the run-level Metrics panel, from the same server-side
  // classification, so the reader sees which metrics feed each headline.
  //
  // Falsy rather than `=== null` for the last bucket, as in the run panel: a metric
  // whose half cannot be read must land somewhere rather than vanish.
  const groups = useMemo(
    () =>
      RESULT_METRIC_GROUPS.map((g) => ({
        ...g,
        rows: metrics.filter((m) => (g.half === null ? !m.half : m.half === g.half)),
      })).filter((g) => g.rows.length > 0),
    [metrics],
  );

  if (metrics.length === 0) return null;

  // Exact key match only: the strip iterates the rows, so an orphaned payload key
  // cannot be adopted by a neighbouring metric.
  const detailFor = (key: string): unknown =>
    metricResults && Object.prototype.hasOwnProperty.call(metricResults, key)
      ? metricResults[key]?.detail
      : undefined;

  return (
    <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2">
      <div className="flex items-center gap-2 mb-2 content-compact font-medium text-default-secondary uppercase tracking-wider">
        <BarChart3 className="h-3.5 w-3.5 text-accent" />
        {t_i18n("Metrics")}
      </div>
      {/* A boxed cell per metric, like the Parameters panel: the border marks
          where one metric ends. */}
      <div className="space-y-4">
        {groups.map((g) => (
          <div key={g.id} className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              {/* The headline's own colour marks the group it feeds; execution feeds none. */}
              <span
                className={cn(
                  "content-caption font-medium uppercase tracking-wider",
                  g.half === null ? "text-default-secondary" : headlineDef(g.half).textClass,
                )}
              >
                {t_i18n(RESULT_METRIC_GROUP_COPY[g.id].title)}
              </span>
              <InfoTip
                content={t_i18n(RESULT_METRIC_GROUP_COPY[g.id].note)}
                label={t_i18n("What the {group} group shows", { group: t_i18n(RESULT_METRIC_GROUP_COPY[g.id].title) })}
              />
            </div>
            <div className="grid gap-1 sm:grid-cols-2">
              {g.rows.map((m) => (
                <MetricCell key={m.key} metric={m} detail={detailFor(m.key)} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// Run Parameters panel: grouped by source
// ---------------------------------------------------------------------------

/** Where a run parameter came from (`services/evaluation/params.py`), read by `t_i18n`.
 * @i18n-keys */
const PARAM_SOURCE_NOTES = {
  manual: "Given when the run was launched, for example prediction reuse turned off by Re-run full. It replaces the same key from the evaluation or captured automatically.",
  evaluation: "The evaluation's custom parameters. They replace the same key captured automatically.",
  auto: "Captured by the platform from the run, its agent and its evaluation when the run started.",
} as const;

/** What a parameter key the platform captures records (`build_auto_params` in
 * `services/evaluation/params.py`), read by `t_i18n`. A key ending in `:` is a prefix
 * followed by an id or a setting name. A key not listed is the author's own. @i18n-keys */
const PARAM_KEY_NOTES = {
  model: "The model the run's agent answered with.",
  execution_mode: "The evaluation's execution mode: full_loop runs the agent as in chat, with its tools, handovers and knowledge bases; llm_only sends only its persona and the entry to the model.",
  quality_source: "What scored quality when the run started: judge for the LLM judge, f1 for the retired token-level F1.",
  evaluator_model: "The LLM judge's model; platform_default is the platform's default model.",
  reuse: "Whether prediction reuse was on for this run: on copies a stored answer when nothing that shapes it changed.",
  kb_count: "How many knowledge bases the agent had.",
  tool_count: "How many tool sources the agent had: MCP servers, custom tools and integrations, not the tools each provides.",
  persona_version: "The persona version the run used; 0 means the agent had no persona.",
  "dataset_version:": "The pinned version of the linked dataset with this id that the run was scored against.",
  "config_version:": "The version of this agent setting the run used. An empty value means the platform default.",
  "prompt_version:": "The version of the prompt with this id that the run used.",
} as const;

function paramKeyNote(key: string): string | null {
  if (Object.prototype.hasOwnProperty.call(PARAM_KEY_NOTES, key)) {
    return PARAM_KEY_NOTES[key as keyof typeof PARAM_KEY_NOTES];
  }
  const colon = key.indexOf(":");
  const prefix = colon > 0 ? key.slice(0, colon + 1) : null;
  return prefix && Object.prototype.hasOwnProperty.call(PARAM_KEY_NOTES, prefix)
    ? PARAM_KEY_NOTES[prefix as keyof typeof PARAM_KEY_NOTES]
    : null;
}

function RunParametersPanel({ runId }: { runId: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useRunParams(runId);
  const [open, setOpen] = useState(false);

  const params = data?.params || [];
  if (isLoading || params.length === 0) return null;

  const order = ["manual", "evaluation", "auto"] as const;
  const grouped = order
    .map((source) => ({ source, items: params.filter((p) => p.source === source) }))
    .filter((g) => g.items.length > 0);

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <div className="flex items-center gap-2 px-4 py-3">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-2 text-left content-base font-semibold"
        >
          {open ? <ChevronDown className="h-4 w-4 text-default-secondary" /> : <ChevronRight className="h-4 w-4 text-default-secondary" />}
          <Wrench className="h-4 w-4 text-accent" />
          {t_i18n("Parameters")}
          <CountBadge variant="outline">{params.length}</CountBadge>
        </button>
        <InfoTip content={t_i18n(RUN_PAGE_NOTES.parameters)} label={t_i18n("What run parameters are")} />
      </div>
      {open && (
        <CardContent className="pt-0 space-y-4">
          {grouped.map(({ source, items }) => {
            return (
              <div key={source} className="space-y-1.5">
                <div className={cn("content-caption font-medium uppercase tracking-wider", PARAM_SOURCE_COLORS[source])}>
                  <InfoLabel content={t_i18n(PARAM_SOURCE_NOTES[source])}>{t_i18n(PARAM_SOURCE_LABELS[source])}</InfoLabel>
                </div>
                <div className="grid gap-1 sm:grid-cols-2">
                  {items.map((p) => (
                    <div
                      key={p.key}
                      className="flex items-baseline justify-between gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2.5 py-1.5"
                    >
                      {paramKeyNote(p.key) ? (
                        <InfoLabel content={t_i18n(paramKeyNote(p.key) as string)} className="text-[11px] font-mono-plex text-default-secondary break-all">
                          {p.key}
                        </InfoLabel>
                      ) : (
                        <span className="text-[11px] font-mono-plex text-default-secondary break-all">{p.key}</span>
                      )}
                      <span className="text-[11px] font-mono-plex break-all text-right">{p.value ?? "—"}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </CardContent>
      )}
    </Card>
  );
}

export default function EvaluationRunDetail() {
  const { t_i18n } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { data: run, isLoading } = useEvaluationRun(id || "");
  // The family decides whether an entry's answer opens in the STIX bundle review.
  const { data: evaluation } = useEvaluation(run?.evaluation_id ?? "");
  // A STIX bundle run's entries open in its run review, on their own report: the
  // comparison names each result's entry as the review does.
  const runReviewable = evaluation?.family === "STIX_BUNDLE" && run?.status === "completed";
  const { data: runComparison } = useEvaluationComparison(
    runReviewable && run ? [run.evaluation_id] : [],
    runReviewable && run ? [run.id] : [],
  );
  const entryKeyOf = useMemo(
    () => new Map((runComparison?.cells ?? []).filter((c) => c.run_id === run?.id).map((c) => [c.result_id, c.entry_key])),
    [runComparison, run?.id],
  );
  const replayRun = useReplayEvaluationRun({
    onSuccess: (newRun) => navigate(`/evaluations/runs/${newRun.id}`),
  });
  const aiOff = useAiActionDisabled();
  const aiOffReason = useAiDisabledReason();
  const rescoreRun = useRescoreRun();
  const { toast } = useToast();

  if (isLoading) return <PageSkeleton cards={4} />;
  if (!run) return (
    <NotFoundState
      icon={FlaskConical}
      title="Run not found"
      description="This evaluation run may have been deleted."
      actionLabel="View Evaluations"
      onAction={() => navigate("/evaluations")}
      backTo="/evaluations"
      backLabel="Back to Evaluations"
    />
  );

  const statusConfig = EVALUATION_STATUS_CONFIG[run.status];
  const StatusIcon = statusConfig?.icon;
  const replayBlocked = !run.can_replay;
  const replayBlockReason =
    run.replay_block_reason ||
    t_i18n("Replay impossible: missing version snapshots.");
  // A completed run was scored by the metric suite, so it always shows the recorded
  // version or an explicit unknown. Unscored runs get no chip.
  const showsMetricSuite = run.status === "completed";
  const hasVersionChips =
    showsMetricSuite ||
    run.persona_version_number != null ||
    Object.keys(run.agent_config_versions || {}).length > 0 ||
    Object.keys(run.prompt_versions || {}).length > 0 ||
    Object.keys(run.pinned_dataset_versions || {}).length > 0;

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo={`/evaluations/${run.evaluation_id}/activity`}
        backLabel="Back to Activity"
        icon={FlaskConical}
        title={t_i18n("Run Detail")}
        badges={
          <>
                <PlaceboArmTag run={run} />
                {run.placebo_comparison_id && (
                  <InfoLabel
                    content={t_i18n(EVALUATION_TERMS.placeboTrial, { n: (run.trial_index ?? 0) + 1 })}
                    className="content-caption text-default-secondary"
                  >
                    {t_i18n("Trial {n}", { n: (run.trial_index ?? 0) + 1 })}
                  </InfoLabel>
                )}
                <div className="flex items-center gap-1 content-base text-default-secondary">
                  {run.flow_id ? <Workflow className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
                  {evaluationSubjectPath(run) ? (
                    <span className="inline-flex items-center gap-1">
                      <span>{run.agent_name || run.agent_slug}</span>
                      <Tip content={t_i18n(run.flow_id ? "Open flow {name} in a new tab" : "Open agent {name} in a new tab", { name: run.agent_name || run.agent_slug })}>
                        <a
                          href={withBasePath(evaluationSubjectPath(run) ?? "")}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center hover:text-default-primary transition-colors"
                          aria-label={t_i18n(run.flow_id ? "Open flow {name} in a new tab" : "Open agent {name} in a new tab", { name: run.agent_name || run.agent_slug })}
                        >
                          <ExternalLink className="h-2.5 w-2.5 opacity-70" />
                        </a>
                      </Tip>
                    </span>
                  ) : (
                    <span>{run.agent_name || run.agent_slug}</span>
                  )}
                </div>
                <span className="text-default-secondary">·</span>
                <div className="flex items-center gap-1 content-base text-default-secondary">
                  <Cpu className="h-3.5 w-3.5" />
                  {run.model_id === MODEL_INDEPENDENT ? (
                    <InfoLabel content={t_i18n(EVALUATION_TERMS.modelIndependent)}>{formatModelLabel(run.model_id, t_i18n)}</InfoLabel>
                  ) : (
                    <span>{formatModelLabel(run.model_id, t_i18n)}</span>
                  )}
                  {run.is_default_model && (
                    <InfoLabel content={t_i18n(EVALUATION_TERMS.agentDefaultModel)} className="content-caption text-accent">
                      {t_i18n("(default)")}
                    </InfoLabel>
                  )}
                </div>
                {statusConfig && (
                  <Badge variant={statusConfig.badge}>
                    {StatusIcon && <StatusIcon className={cn("h-3 w-3 mr-1", run.status === "running" && "animate-spin")} />}
                    {t_i18n(statusConfig.label)}
                  </Badge>
                )}
                {run.held_reason && <HeldBadge reason={run.held_reason} />}
                <InfoLabel content={t_i18n(triggerNote(run.trigger_reason))}>
                  <Badge variant="outline" className="content-caption">
                    {t_i18n(EVALUATION_TRIGGER_LABELS[run.trigger_reason] || run.trigger_reason)}
                  </Badge>
                </InfoLabel>
                <RunModeBadge run={run} className="content-caption" />
                <PromptGuardBadge run={run} className="content-caption" />
                <ToolApprovalBadge run={run} className="content-caption" />
                <PlatformErrorBadge run={run} className="content-caption" />
          </>
        }
        /* Version provenance on its own line, so the header row stays
           legible: what ran above, which versions below. */
        links={
              (hasVersionChips || runReviewable) && (
              <div className="flex items-center gap-x-3 gap-y-1 flex-wrap">
              {hasVersionChips && (
              <div className="flex items-center gap-1 flex-wrap">
                  <RunVersionChips run={run} />
                  {!!run.pinned_dataset_versions && Object.keys(run.pinned_dataset_versions).length > 0 && (
                    <InfoLabel
                      content={t_i18n(
                        run.has_dataset_drift
                          ? "Pinned dataset version {versions}: the dataset snapshot this run was scored against. The dataset has changed since."
                          : "Pinned dataset version {versions}: the dataset snapshot this run was scored against.",
                        { versions: Object.values(run.pinned_dataset_versions).map((v) => `v${v}`).join(", ") },
                      )}
                      className={cn(
                        "inline-flex items-center gap-0.5 content-caption font-mono-plex rounded-sm px-1 py-0.5 leading-none border",
                        run.has_dataset_drift
                          ? "bg-feedback-alert-primary/10 text-feedback-warning-tertiary border-feedback-alert-primary/20"
                          : "bg-accent/10 text-accent border-accent/20",
                      )}
                    >
                      <History className="h-2.5 w-2.5" />{Object.entries(run.pinned_dataset_versions).map(([, v]) => `v${v}`).join(",")}
                    </InfoLabel>
                  )}
                  {/* Metric suite version. A null on a completed run shows
                      as an explicit unknown: the run predates the record, and its
                      scores cannot be attributed to a suite version. */}
                  {showsMetricSuite && (
                    <InfoLabel
                      content={run.metric_suite_version
                        ? t_i18n("The version of the metric suite that computed this run's quality and performance metrics. Judge Quality does not come from it.")
                        : t_i18n("The metric suite version was not recorded for this run, so its quality and performance metrics cannot be tied to a version.")}
                      className={cn(
                        "inline-flex items-center gap-0.5 content-caption font-mono-plex rounded-sm px-1 py-0.5 leading-none border",
                        run.metric_suite_version
                          ? "bg-accent/10 text-accent/80 border-accent/20"
                          : "bg-elevation-highlight text-default-secondary border-elevation-subtle-soft",
                      )}
                    >
                      <Scale className="h-2.5 w-2.5" />
                      {run.metric_suite_version ? `v${run.metric_suite_version}` : t_i18n("unknown")}
                    </InfoLabel>
                  )}
              </div>
              )}
              {/* A completed STIX bundle run opens in the run review: its figures over
                  every report, then each report object by object. */}
              {runReviewable && (
                <Link
                  to={`/evaluations/review?run=${run.id}`}
                  className="inline-flex items-center gap-1.5 rounded-sm content-compact font-medium text-accent hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                >
                  <Icon name="file-search" size={16} />
                  {t_i18n("Review this run")}
                </Link>
              )}
              </div>
              )
        }
        /* items-stretch, not items-center: cells differ in height (N/A vs a
           full score), and centring them misaligns the column labels. */
        actions={
          <div className="flex items-stretch pr-4">
            <div className="w-[150px] text-center border-l border-elevation-subtle-soft px-2.5 py-2.5">
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider whitespace-nowrap mb-1">
                <InfoLabel content={t_i18n(judgeHeadline(run).note)}>{t_i18n(judgeHeadline(run).label)}</InfoLabel>
              </p>
              {(() => {
                const val = run.score;
                const color = headlineColorClass(val);
                if (run.status === "running") {
                  return <div className="flex justify-center mt-1.5 text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>;
                }
                if (val != null) {
                  return (
                    <>
                      <span className={cn("text-lg font-bold font-sans-geologica", color)}>{val}</span>
                      <span className="content-caption text-default-secondary ml-0.5">/100</span>
                    </>
                  );
                }
                if (run.status === "completed") {
                  return (
                    <InfoLabel content={t_i18n(...judgeNaNote(run))} className="content-base leading-7 text-default-secondary italic font-sans-geologica">
                      {t_i18n("N/A")}
                    </InfoLabel>
                  );
                }
                return <span className="text-lg font-bold font-sans-geologica text-default-secondary">—</span>;
              })()}
            </div>
            {/* Overall Quality: composed from the agent family's
                deterministic quality metrics, so its N/A reason is about those
                metrics, not the judge, which has its own headline. */}
            <div className="w-[150px] text-center border-l border-elevation-subtle-soft px-2.5 py-2.5">
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider whitespace-nowrap mb-1">
                <InfoLabel content={t_i18n(qualityHeadline(run).note)}>{t_i18n(qualityHeadline(run).label)}</InfoLabel>
              </p>
              {(() => {
                const q = qualityHeadline(run);
                if (run.status === "running") {
                  return <div className="flex justify-center mt-1.5 text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>;
                }
                const coverage = partialCoverage(run);
                if (q.value !== null) {
                  return (
                    <>
                      <InfoLabel
                        content={headlineTip(q, t_i18n)}
                        className={cn("text-lg font-bold font-sans-geologica", headlineColorClass(q.value))}
                      >
                        {q.value}
                      </InfoLabel>
                      <span className="content-caption text-default-secondary ml-0.5">/100</span>
                      {coverage && (
                        <p className="content-caption leading-tight text-feedback-warning-primary">
                          <InfoLabel content={<CoverageReasons leftOut={coverage.left_out} />}>
                            {t_i18n("all metrics on {counted} of {entries} entries", { counted: coverage.counted, entries: coverage.entries })}
                          </InfoLabel>
                        </p>
                      )}
                    </>
                  );
                }
                if (run.status === "completed" && !isLegacyRun(run)) {
                  return (
                    <InfoLabel
                      content={coverage ? <CoverageReasons leftOut={coverage.left_out} /> : t_i18n(naReason("quality", run.status))}
                      className="content-base leading-7 text-default-secondary italic font-sans-geologica"
                    >
                      {t_i18n("N/A")}
                    </InfoLabel>
                  );
                }
                return <span className="text-lg font-bold font-sans-geologica text-default-secondary">—</span>;
              })()}
            </div>
            {/* Overall Performance: comparative within this evaluation,
                100 means "fastest of these runs", as the tooltip says. An older
                run falls back to its legacy `speed_score`, labelled as such; a
                run never shows both. */}
            <div className="w-[150px] text-center border-l border-elevation-subtle-soft px-2.5 py-2.5">
              {(() => {
                const perf = performanceHeadline(run);
                return (
                  <>
                    <p className="content-caption font-medium text-default-secondary uppercase tracking-wider whitespace-nowrap mb-1">
                      <InfoLabel content={t_i18n(perf.note)}>{t_i18n(perf.label)}</InfoLabel>
                    </p>
                    {run.status === "running" ? (
                      <div className="flex justify-center mt-1.5 text-default-secondary"><Spinner size="sm" tone="inherit" label={t_i18n("Running")} /></div>
                    ) : perf.value != null ? (
                      <InfoLabel content={headlineTip(perf, t_i18n)}>
                        <span className={cn("text-lg font-bold font-sans-geologica", perf.legacy ? "text-default-secondary" : headlineColorClass(perf.value))}>{perf.value}</span>
                        <span className="content-caption text-default-secondary ml-0.5">/100</span>
                      </InfoLabel>
                    ) : run.status === "completed" ? (
                      <InfoLabel
                        content={t_i18n(naReason("performance", run.status, run))}
                        className="content-base leading-7 text-default-secondary italic font-sans-geologica"
                      >
                        {t_i18n("N/A")}
                      </InfoLabel>
                    ) : (
                      <span className="text-lg font-bold font-sans-geologica text-default-secondary">—</span>
                    )}
                  </>
                );
              })()}
            </div>
            <div className="w-[150px] text-center border-l border-elevation-subtle-soft px-2.5 py-2.5">
              <p className="content-caption font-medium text-default-secondary uppercase tracking-wider whitespace-nowrap mb-1">
                <InfoLabel content={t_i18n(EVALUATION_TERMS.runDuration)}>{t_i18n("Duration")}</InfoLabel>
              </p>
              {run.duration_seconds != null ? (
                <>
                  <span className="text-lg font-bold text-default-primary font-sans-geologica">{run.duration_seconds.toFixed(1)}</span>
                  <span className="content-caption text-default-secondary ml-0.5">s</span>
                </>
              ) : (
                <span className="text-lg font-bold font-sans-geologica text-default-secondary">—</span>
              )}
            </div>
          </div>
        }
      />

      {run.status === "completed" && (
        <div className="flex items-center justify-end gap-2">
          {/* `wrap`: a disabled button takes no pointer or focus events, so the wrapper
              keeps the tip, and the reason it is disabled, reachable. */}
          <Tip content={aiOffReason ?? (replayBlocked ? replayBlockReason : t_i18n(EVALUATION_TERMS.rerunFull))} wrap="auto">
            <Button
              variant="outline"
              size="sm"
              disabled={replayRun.isPending || replayBlocked || aiOff}
              onClick={async () => {
                try {
                  await replayRun.mutateAsync({ runId: run.id, reuse: "off" });
                  toast({ title: t_i18n("Triggered full run"), variant: "success" });
                } catch {
                  toast({ title: t_i18n("Failed to re-run full"), variant: "error" });
                }
              }}
            >
              <Zap className={cn("h-3.5 w-3.5 mr-1.5", replayRun.isPending && "animate-pulse")} />
              {replayRun.isPending ? t_i18n("Triggering...") : t_i18n("Re-run full")}
            </Button>
          </Tip>
          <Tip
            content={
              aiOffReason ??
              (run.placebo_comparison_id
                ? t_i18n("A placebo comparison's run cannot be re-scored on its own: launch a new placebo comparison.")
                : t_i18n(EVALUATION_TERMS.rescore))
            }
            wrap="auto"
          >
            <Button
              variant="outline"
              size="sm"
              disabled={rescoreRun.isPending || aiOff || !!run.placebo_comparison_id}
              onClick={async () => {
                try {
                  await rescoreRun.mutateAsync({ runId: run.id });
                  toast({ title: t_i18n("Re-score started"), variant: "success" });
                } catch {
                  toast({ title: t_i18n("Failed to re-score run"), variant: "error" });
                }
              }}
            >
              <Repeat className={cn("h-3.5 w-3.5 mr-1.5", rescoreRun.isPending && "animate-spin")} />
              {rescoreRun.isPending ? t_i18n("Re-scoring...") : t_i18n("Re-score")}
            </Button>
          </Tip>
        </div>
      )}

      <PlaceboSnapshotCard run={run} />

      {/* A placebo comparison's run is not broken: its tag and the replay buttons' tips say why it is not re-run alone. */}
      {replayBlocked && !run.placebo_comparison_id && (
        <div className="flex items-start gap-3 rounded-sm border border-feedback-error-primary/35 bg-feedback-error-primary/[0.08] px-4 py-3">
          <AlertTriangle className="h-4 w-4 text-feedback-error-primary shrink-0 mt-0.5" />
          <div className="min-w-0">
            <p className="content-base font-medium text-feedback-error-primary">{t_i18n("Replay disabled for this run")}</p>
            <p className="content-compact text-feedback-error-primary/80 mt-0.5 break-words">{replayBlockReason}</p>
          </div>
        </div>
      )}

      {/* Dataset drift banner: the pinned dataset was edited after this run, so
          the live dataset differs from the scored snapshot. */}
      {run.has_dataset_drift && (
        <div className="flex items-start gap-3 rounded-sm border border-feedback-alert-primary/35 bg-feedback-alert-primary/[0.08] px-4 py-3">
          <History className="h-4 w-4 text-feedback-warning-primary shrink-0 mt-0.5" />
          <div className="min-w-0">
            <p className="content-base font-medium text-feedback-warning-primary">{t_i18n("Dataset updated since this run")}</p>
            <p className="content-compact text-feedback-warning-primary mt-0.5 break-words">
              {run.pinned_dataset_versions && Object.keys(run.pinned_dataset_versions).length > 0
                ? t_i18n("This run was scored against a pinned dataset snapshot ({versions}). The live dataset has changed since then, so results reflect the historical snapshot, not the current dataset.", {
                    versions: Object.values(run.pinned_dataset_versions).map((v) => `v${v}`).join(", "),
                  })
                : t_i18n("This run was scored against a pinned dataset snapshot. The live dataset has changed since then, so results reflect the historical snapshot, not the current dataset.")}
            </p>
          </div>
        </div>
      )}

      {/* Anomaly banner. run.anomaly_count counts both
          subtypes, so the copy must hold for a mixed count: transient-error
          entries are excluded from scores, confirmation-seeking entries are
          flagged but still counted. */}
      {(run.anomaly_count ?? 0) > 0 && (
        <div className="flex items-start gap-3 rounded-sm border border-feedback-alert-primary/35 bg-feedback-alert-primary/[0.08] px-4 py-3">
          <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0 mt-0.5" />
          <div className="min-w-0">
            <p className="content-base font-medium text-feedback-warning-primary">
              {t_i18n("Execution anomalies flagged: {count}", { count: run.anomaly_count ?? 0 })}
            </p>
            <p className="content-compact text-feedback-warning-primary mt-0.5 break-words">
              {t_i18n(
                "The agent asked for confirmation or offered options instead of completing the task, hit a transient execution error, or could not be run by the platform. Confirmation-seeking results still count toward the run's scores; the others are excluded. See each entry's badge for its case.",
              )}
            </p>
          </div>
        </div>
      )}

      {/* Judge failure banner: results with an un-gradeable judge
          verdict were excluded from scores. */}
      {(run.judge_failure_count ?? 0) > 0 && (
        <div className="flex items-start gap-3 rounded-sm border border-feedback-error-primary/35 bg-feedback-error-primary/[0.08] px-4 py-3">
          <Gavel className="h-4 w-4 text-feedback-error-primary shrink-0 mt-0.5" />
          <div className="min-w-0">
            <p className="content-base font-medium text-feedback-error-primary">
              {t_i18n("Judge failures excluded from scores: {count}", { count: run.judge_failure_count ?? 0 })}
            </p>
            <p className="content-compact text-feedback-error-primary/80 mt-0.5 break-words">
              {t_i18n(
                "The LLM judge did not return a complete, gradeable verdict (parse failure, truncation, or missing sub-score). Excluded from run scores.",
              )}
            </p>
          </div>
        </div>
      )}

      {/* A failed rescore: the run keeps its status and the scores from before it,
          so the failure is shown apart from the run's own. */}
      {run.rescore_error && run.status !== "running" && (
        <div className="flex items-start gap-3 rounded-sm border border-feedback-alert-primary/35 bg-feedback-alert-primary/[0.08] px-4 py-3">
          <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0 mt-0.5" />
          <div className="min-w-0">
            <p className="content-base font-medium text-feedback-warning-primary">{t_i18n("The last rescore failed")}</p>
            <p className="content-compact text-feedback-warning-primary mt-0.5 break-words">
              {t_i18n("The scores shown are from before it.")} {run.rescore_error}
            </p>
          </div>
        </div>
      )}

      {/* Failed run error banner */}
      {run.status === "failed" && (
        <div className="flex items-start gap-3 rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/[0.06] px-4 py-3">
          <AlertTriangle className="h-4 w-4 text-feedback-error-primary shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <p className="content-base font-medium text-feedback-error-primary">{t_i18n("This run failed")}</p>
            {run.error && (
              <p className="content-compact text-feedback-error-primary/70 mt-0.5 break-words">{run.error}</p>
            )}
          </div>
          <Tip content={aiOffReason ?? (replayBlocked ? replayBlockReason : t_i18n(EVALUATION_TERMS.replay))} wrap="auto">
            <Button
              variant="outline"
              size="sm"
              className="shrink-0 border-feedback-error-primary/30 text-feedback-error-primary hover:bg-feedback-error-primary/10 hover:text-feedback-error-primary"
              disabled={replayRun.isPending || replayBlocked || aiOff}
              onClick={() => replayRun.mutate({ runId: run.id })}
            >
              <RotateCcw className={cn("h-3.5 w-3.5 mr-1.5", replayRun.isPending && "animate-spin")} />
              {replayRun.isPending ? t_i18n("Replaying...") : t_i18n("Replay")}
            </Button>
          </Tip>
        </div>
      )}

      {/* Cancelled run banner */}
      {run.status === "cancelled" && (
        <div className="flex items-start gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-4 py-3">
          <AlertTriangle className="h-4 w-4 text-default-secondary shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <p className="content-base font-medium">{t_i18n("This run was cancelled")}</p>
            {run.error && (
              <p className="content-compact text-default-secondary mt-0.5 break-words">{run.error}</p>
            )}
          </div>
          <Tip content={aiOffReason ?? (replayBlocked ? replayBlockReason : t_i18n(EVALUATION_TERMS.replay))} wrap="auto">
            <Button
              variant="outline"
              size="sm"
              className="shrink-0"
              disabled={replayRun.isPending || replayBlocked || aiOff}
              onClick={() => replayRun.mutate({ runId: run.id })}
            >
              <RotateCcw className={cn("h-3.5 w-3.5 mr-1.5", replayRun.isPending && "animate-spin")} />
              {replayRun.isPending ? t_i18n("Replaying...") : t_i18n("Replay")}
            </Button>
          </Tip>
        </div>
      )}

      {/* Progress */}
      {run.status === "running" && (
        <div className="space-y-1">
          <div className="flex items-center justify-between content-compact text-default-secondary">
            <span>{t_i18n("Progress")}</span>
            <span>{run.completed_entries} / {run.total_entries}</span>
          </div>
          <div className="h-2 bg-elevation-hover rounded-full overflow-hidden">
            <div
              className="h-full bg-accent rounded-full transition-all"
              style={{ width: `${run.total_entries > 0 ? (run.completed_entries / run.total_entries) * 100 : 0}%` }}
            />
          </div>
        </div>
      )}

      {/* Parameters */}
      <RunParametersPanel runId={run.id} />

      {/* Metrics & Tags */}
      <RunMetricsPanel run={run} />
      <RunTagsPanel runId={run.id} canManage={true} isBaseline={!!run.is_baseline} effectiveTags={run.effective_tags} />

      {/* Results */}
      <div>
        <h2 className="text-lg font-semibold mb-4">
          {t_i18n("Dataset Results")}
          <span className="content-base text-default-secondary ml-2">
            ({t_i18n("{count, plural, one {# entry} other {# entries}}", { count: run.results?.length || 0 })})
          </span>
        </h2>
        <div className="space-y-3">
          {(() => {
            const evalSlug = (run.evaluation_name || "evaluation").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
            const runDate = run.created_at ? run.created_at.slice(0, 10) : "unknown-date";
            return run.results?.map((result, idx) => (
              <ResultRow key={result.id} runId={run.id} result={result} index={idx} judgeEnabled={runJudged(run)} evaluationSlug={evalSlug} runDate={runDate} bundleReview={evaluation?.family === "STIX_BUNDLE"} entryKey={entryKeyOf.get(result.id)} />
            ));
          })()}
          {(!run.results || run.results.length === 0) && run.status !== "running" && (
            <p className="content-base text-default-secondary text-center py-8">{t_i18n("No results yet")}</p>
          )}
        </div>
      </div>
    </div>
  );
}

/** Why Overall Quality leaves entries out, one line per reason. */
export function CoverageReasons({ leftOut }: { leftOut: Record<string, number> }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-0.5 text-left">
      <p>{t_i18n("Each metric is averaged over the entries where it has a value. These entries lack a value for at least one counted metric:")}</p>
      {Object.entries(leftOut).map(([reason, count]) => (
        <p key={reason}>
          {t_i18n("{count, plural, one {# entry} other {# entries}}: {reason}", {
            count,
            reason: reasonText(reason, t_i18n),
          })}
        </p>
      ))}
    </div>
  );
}

/**
 * Why an entry's quality metrics did not score, or scored 0: the reason and hint
 * stored with each outcome, which no other surface shows. Performance keys and the
 * extraction record have their own displays.
 */
export function QualityNotes({
  metricResults,
  runId,
  resultId,
}: {
  metricResults: Record<string, PersistedMetricResult> | null;
  /** The entry, to name its metrics as its Metrics strip does. */
  runId?: string;
  resultId?: string;
}) {
  const { t_i18n } = useFormatter();
  // The query the strip runs, shared: a metric not shown on the entry gets no note there
  // either (its reason is the shown metric's), and one with an entry name is named by it.
  const { data: entryMetrics } = useResultMetrics(runId ?? "", resultId);
  const onEntry = new Map((entryMetrics ?? []).map((m) => [m.key, m]));
  const notes = Object.entries(metricResults ?? {}).flatMap(([key, outcome]) => {
    if (!outcome || key.startsWith("performance.") || key === EXTRACTION_FAILED_KEY) return [];
    if (onEntry.get(key)?.shown_on_entry === false) return [];
    const detail = (outcome.detail ?? {}) as Record<string, unknown>;
    const reason = [detail.error_message, detail.message, detail.reason].find((text) => typeof text === "string");
    const hint = typeof detail.hint === "string" ? detail.hint : null;
    const notScored = outcome.status !== "scored";
    if (!notScored && outcome.value !== 0) return [];
    if (typeof reason !== "string" && !hint) return [];
    return [{ key, notScored, reason: typeof reason === "string" ? reason : null, hint }];
  });
  if (notes.length === 0) return null;
  return (
    <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 content-compact space-y-1">
      <p className="font-medium text-default-secondary">{t_i18n("Quality notes")}</p>
      {notes.map((note) => (
        <p key={note.key}>
          <MetricName metricKey={note.key} entryLabel={onEntry.get(note.key)?.entry_label} className="font-medium" />
          {" — "}
          {note.notScored ? (
            <InfoLabel content={t_i18n(RUN_PAGE_NOTES.metricLeftOut)}>{t_i18n("left out")}</InfoLabel>
          ) : (
            t_i18n("scored 0")
          )}
          {note.reason && `: ${reasonText(note.reason, t_i18n)}`}
          {note.hint && <span className="block text-default-secondary">{note.hint}</span>}
        </p>
      ))}
    </div>
  );
}

/** A reason as the reader sees it: a code's translated label, else the backend's sentence. */
function reasonText(reason: string, t_i18n: (key: string) => string): string {
  const label = metricReasonLabel(reason);
  return label ? t_i18n(label) : reason;
}

/** A failed prediction extraction.
 *
 * Read from `execution_details.metric_results` directly, NOT from the metrics
 * endpoint: an `invalid` outcome writes no `EvaluationMetric` row, so the strip
 * below never receives it and a disclosure built there would be silently empty.
 */
function ExtractionFailureNotice({
  failure,
  metricResults,
}: {
  failure: ExtractionFailure | null;
  metricResults: Record<string, PersistedMetricResult> | null;
}) {
  const { t_i18n } = useFormatter();
  if (!failure) return null;
  // The family's metrics say which side failed: scored 0 when the answer did. A
  // result recorded before those metrics existed stores none, so it says neither.
  const familyOutcomes = Object.entries(metricResults ?? {}).filter(
    ([key]) => !key.startsWith("performance.") && key !== EXTRACTION_FAILED_KEY,
  );
  const answerAtFault = familyOutcomes.some(([, outcome]) => outcome?.status === "scored");
  const where = failure.operation
    ? `${failure.operation}${failure.operation_index ? ` (step ${failure.operation_index})` : ""}`
    : null;
  return (
    <div className="rounded-sm border border-feedback-alert-primary/40 bg-feedback-alert-primary/5 p-3 content-base">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0 text-feedback-warning-primary" />
        <div>
          <p className="font-medium">
            {t_i18n("Extraction failed")} — {extractionFailureLabel(failure.reason, t_i18n)}
          </p>
          <p className="text-default-secondary">{failure.message}</p>
          {where && (
            <p className="content-compact text-default-secondary mt-1">
              {t_i18n("Operation")}: {where}
              {failure.path ? ` · ${t_i18n("Path")}: ${failure.path}` : ""}
              {failure.expected_kind && failure.actual_kind
                ? ` · ${t_i18n("Expected {expected}, got {actual}", {
                    expected: failure.expected_kind,
                    actual: failure.actual_kind,
                  })}`
                : ""}
            </p>
          )}
          {familyOutcomes.length > 0 && (
            <p className="content-compact text-default-secondary mt-1">
              {answerAtFault
                ? t_i18n("The agent's answer could not be extracted, so the entry's quality metrics score 0 and count in the run's average.")
                : t_i18n("The extractor's configuration caused this, so the entry is left out of Overall Quality.")}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
