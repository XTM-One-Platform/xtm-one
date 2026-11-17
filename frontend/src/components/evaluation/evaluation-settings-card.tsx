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

import { useId, type ReactNode } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from "@/components/ui/select";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { cn } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { CRON_PRESETS } from "@/lib/constants";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { evaluationExecutionMode } from "@/components/evaluation/run-mode";
import { useEvaluationAlwaysAskTools, useEvaluationPromptGuardState } from "@/hooks/api/useEvaluationApi";
import { useFormatter } from "@/i18n";

interface EvaluationSettingsCardEvaluationSnapshot {
  execution_mode: string;
  run_mode: string;
  timeout_seconds: number;
  schedule: string | null;
  /** `enabled`: whether the schedule starts runs. */
  automatic_runs?: boolean;
  reuse_default: "off" | "on";
  apply_prompt_guards: boolean;
  require_tool_approval: boolean;
}

export interface EvaluationSettingsCardProps {
  editing: boolean;
  name: string;
  onNameChange: (v: string) => void;
  description: string;
  onDescriptionChange: (v: string) => void;
  executionMode: string;
  onExecutionModeChange: (v: string) => void;
  runMode: "parallel" | "sequential";
  onRunModeChange: (v: "parallel" | "sequential") => void;
  timeoutSeconds: number;
  onTimeoutSecondsChange: (v: number) => void;
  scheduleEnabled: boolean;
  onScheduleEnabledChange: (v: boolean) => void;
  schedule: string;
  onScheduleChange: (v: string) => void;
  /**
   * The evaluation's `enabled` flag, shown as "Automatic runs": off pauses
   * the schedule, never a run started by hand. Rendered only
   * when both props are passed (the Configuration tab; a new evaluation starts on).
   */
  automaticRuns?: boolean;
  onAutomaticRunsChange?: (v: boolean) => void;
  /**
   * "On agent change" starts no run yet, so it is not offered. An evaluation
   * that turned it on before keeps a switch to turn it off: both props are passed only
   * then (the Configuration tab, when the stored value is on).
   */
  onAgentChangeStored?: boolean;
  onOnAgentChangeStoredChange?: (v: boolean) => void;
  reuseDefault: boolean;
  onReuseDefaultChange: (v: boolean) => void;
  applyPromptGuards: boolean;
  onApplyPromptGuardsChange: (v: boolean) => void;
  requireToolApproval: boolean;
  onRequireToolApprovalChange: (v: boolean) => void;
  /** The selected agents, whose "always ask" tools the warning names. */
  agentIds: string[];
  /**
   * The creation page's "Judge Output Budget (tokens)" field, rendered only when
   * both props are passed (`ConfigTab` shows the budget in its Scoring card).
   */
  judgeMaxTokens?: number;
  onJudgeMaxTokensChange?: (v: number) => void;
  /** Read-only-mode display snapshot — ignored when `editing`. */
  evaluationSnapshot?: EvaluationSettingsCardEvaluationSnapshot;
}

/**
 * The "Settings" card of the evaluation creation page and of `EvaluationDetail.tsx`'s
 * Configuration tab, so both render the same fields: what a run does (name,
 * description, execution and run mode, timeout, triggers, reuse, guards, approval).
 */
export function EvaluationSettingsCard({
  editing,
  name,
  onNameChange,
  description,
  onDescriptionChange,
  executionMode,
  onExecutionModeChange,
  runMode,
  onRunModeChange,
  timeoutSeconds,
  onTimeoutSecondsChange,
  scheduleEnabled,
  onScheduleEnabledChange,
  schedule,
  onScheduleChange,
  automaticRuns,
  onAutomaticRunsChange,
  onAgentChangeStored,
  onOnAgentChangeStoredChange,
  reuseDefault,
  onReuseDefaultChange,
  applyPromptGuards,
  onApplyPromptGuardsChange,
  requireToolApproval,
  onRequireToolApprovalChange,
  agentIds,
  judgeMaxTokens,
  onJudgeMaxTokensChange,
  evaluationSnapshot,
}: EvaluationSettingsCardProps) {
  const { t_i18n } = useFormatter();
  const showJudgeMaxTokens = judgeMaxTokens !== undefined && onJudgeMaxTokensChange !== undefined;
  // Read here so both pages that edit an evaluation show the warning.
  const { data: promptGuardState } = useEvaluationPromptGuardState(editing);
  const departsFromChat = !!promptGuardState?.connected && !applyPromptGuards;
  // Fetched only while the setting is off, the one case it warns about.
  const { data: alwaysAsk, isError: alwaysAskUnknown } = useEvaluationAlwaysAskTools(agentIds, editing && !requireToolApproval);
  const unaskedTools = !requireToolApproval ? (alwaysAsk?.agents ?? []) : [];
  const fieldId = useId();
  const ids = {
    name: `${fieldId}-name`,
    description: `${fieldId}-description`,
    executionMode: `${fieldId}-execution-mode`,
    runMode: `${fieldId}-run-mode`,
    timeout: `${fieldId}-timeout`,
    judgeMaxTokens: `${fieldId}-judge-max-tokens`,
    judgeMaxTokensHelp: `${fieldId}-judge-max-tokens-help`,
    schedule: `${fieldId}-schedule`,
  };

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base font-medium">{t_i18n("Settings")}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {editing ? (
          <div className="space-y-4">
            <div>
              <Label htmlFor={ids.name} className={cn(HEADING, "mb-1.5")}>{t_i18n("Name")}</Label>
              <Input id={ids.name} aria-label={t_i18n("Name")} value={name} onChange={(e) => onNameChange(e.target.value)} className="h-9" placeholder={t_i18n("Evaluation name")} />
            </div>
            <div>
              <Label htmlFor={ids.description} className={cn(HEADING, "mb-1.5")}>{t_i18n("Description")}</Label>
              <textarea
                id={ids.description}
                value={description}
                onChange={(e) => onDescriptionChange(e.target.value)}
                placeholder={t_i18n("Optional description")}
                className={`w-full min-h-[80px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary resize-y ${FIELD_FOCUS_RING}`}
              />
            </div>
            <div className="grid grid-cols-3 gap-4">
              <div>
                <ExecutionModeHeading htmlFor={ids.executionMode} className="mb-1.5" />
                <Select value={executionMode} onValueChange={onExecutionModeChange}>
                  <SelectTrigger id={ids.executionMode} className="h-9"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="llm_only">{t_i18n("LLM Only")}</SelectItem>
                    <SelectItem value="full_loop">{t_i18n("Full Loop with Tools")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <RunModeHeading htmlFor={ids.runMode} className="mb-1.5" />
                <Select value={runMode} onValueChange={(v) => onRunModeChange(v as "parallel" | "sequential")}>
                  <SelectTrigger id={ids.runMode} className="h-9"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="parallel">{t_i18n("Parallel")}</SelectItem>
                    <SelectItem value="sequential">{t_i18n("Sequential")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <TimeoutHeading label={t_i18n("Execution Timeout (s)")} htmlFor={ids.timeout} className="mb-1.5" />
                <Input id={ids.timeout} aria-label={t_i18n("Execution Timeout (s)")}
                  type="number"
                  min={60}
                  step={1}
                  value={timeoutSeconds}
                  onChange={(e) => {
                    const parsed = parseInt(e.target.value, 10);
                    if (!Number.isNaN(parsed)) {
                      onTimeoutSecondsChange(Math.max(60, parsed));
                    }
                  }}
                  className="h-9 font-mono-plex"
                />
              </div>
            </div>
            {showJudgeMaxTokens && (
              <div>
                <Label htmlFor={ids.judgeMaxTokens} className={cn(HEADING, "mb-1.5")}>{t_i18n("Judge Output Budget (tokens)")}</Label>
                <p id={ids.judgeMaxTokensHelp} className="content-compact text-default-secondary mb-1.5">
                  {t_i18n("Maximum tokens the LLM judge may use for its verdict. Raise this if complex outputs produce truncated (unscored) verdicts.")}
                </p>
                <Input id={ids.judgeMaxTokens} aria-label={t_i18n("Judge Output Budget (tokens)")} aria-describedby={ids.judgeMaxTokensHelp}
                  type="number"
                  min={256}
                  max={131072}
                  step={256}
                  value={judgeMaxTokens}
                  onChange={(e) => {
                    const parsed = parseInt(e.target.value, 10);
                    if (!Number.isNaN(parsed)) {
                      onJudgeMaxTokensChange!(Math.min(131072, Math.max(256, parsed)));
                    }
                  }}
                  className="h-9 w-40 font-mono-plex"
                />
              </div>
            )}
            <div className="flex items-center justify-between rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
              <div>
                <p className="content-base font-medium">{t_i18n("Scheduled Run")}</p>
                <p className="content-compact text-default-secondary mt-0.5">{t_i18n("Starts a launch on a cron schedule, read in UTC.")}</p>
              </div>
              <Switch aria-label={t_i18n("Scheduled Run")} checked={scheduleEnabled} onCheckedChange={onScheduleEnabledChange} />
            </div>
            {scheduleEnabled && (
              <div>
                <div className={cn(HEADING, "mb-1.5")}>
                  <Label htmlFor={ids.schedule}>{t_i18n("Schedule (Cron)")}</Label>
                  <InfoTip
                    content={t_i18n("The cron expression is read in UTC, so a preset such as Daily at 8 AM starts at 08:00 UTC, not in your time zone.")}
                    label={t_i18n("Which time zone the schedule uses")}
                  />
                </div>
                <Input id={ids.schedule} aria-label={t_i18n("Schedule (Cron)")} value={schedule} onChange={(e) => onScheduleChange(e.target.value)} className="h-9 font-mono-plex" placeholder={t_i18n("e.g. 0 0 * * 1")} />
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {CRON_PRESETS.map((p) => (
                    <button
                      key={p.value}
                      type="button"
                      className={cn("content-caption px-2 py-1 rounded-sm transition-colors", schedule === p.value ? "bg-accent/20 text-accent" : "bg-elevation-hover text-default-secondary hover:bg-elevation-highlight")}
                      onClick={() => onScheduleChange(p.value)}
                    >
                      {t_i18n(p.label)}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {onAgentChangeStored !== undefined && onOnAgentChangeStoredChange !== undefined && (
              <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                <div className="min-w-0">
                  <p className="flex items-center gap-1 content-base font-medium">
                    {t_i18n("On Agent Change")}
                    <InfoTip
                      content={t_i18n(
                        "This evaluation turned it on earlier, but it starts no run yet. It is no longer offered; turn it off to clear it.",
                      )}
                      label={t_i18n("Why On Agent Change is shown")}
                    />
                  </p>
                </div>
                <Switch aria-label={t_i18n("On Agent Change")} checked={onAgentChangeStored} onCheckedChange={onOnAgentChangeStoredChange} />
              </div>
            )}
            {automaticRuns !== undefined && onAutomaticRunsChange !== undefined && (
              <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                <div className="min-w-0">
                  <p className="content-base font-medium">{t_i18n("Automatic runs")}</p>
                  <p className="content-compact text-default-secondary mt-0.5">
                    {t_i18n("Off pauses the schedule without removing it. A run you start still runs.")}
                  </p>
                </div>
                <Switch aria-label={t_i18n("Automatic runs")} checked={automaticRuns} onCheckedChange={onAutomaticRunsChange} />
              </div>
            )}
            <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
              <div className="min-w-0">
                <PredictionReuseHeading className="content-base font-medium" />
              </div>
              <Switch aria-label={t_i18n("Prediction Reuse")} checked={reuseDefault} onCheckedChange={onReuseDefaultChange} />
            </div>
            <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
              <div className="min-w-0">
                <PromptGuardsHeading className="content-base font-medium" />
                <p className="content-compact text-default-secondary mt-0.5">
                  {t_i18n("Check entries' prompts and the agent's answers with the platform's prompt guard, as chat does. Disabled by default: the checks cost model calls on every entry.")}
                </p>
                {departsFromChat && (
                  <p className="content-caption text-feedback-warning-primary mt-1">
                    {t_i18n("A prompt guard is connected on this platform. With this disabled, a score may count what chat would refuse or withhold.")}
                  </p>
                )}
              </div>
              <Switch aria-label={t_i18n("Apply prompt guards")} checked={applyPromptGuards} onCheckedChange={onApplyPromptGuardsChange} />
            </div>
            <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
              <div className="min-w-0">
                <ToolApprovalHeading className="content-base font-medium" />
                <p className="content-compact text-default-secondary mt-0.5">
                  {t_i18n("Hold tool calls that need approval, as chat does. Disabled by default: nobody can approve during an evaluation, so a held call ends the entry on the approval message.")}
                </p>
                {unaskedTools.length > 0 && (
                  <div className="content-caption text-feedback-warning-primary mt-1 space-y-0.5">
                    <p>{t_i18n("With this disabled, these tools an administrator marked \"always ask\" run without approval:")}</p>
                    {unaskedTools.map((a) => (
                      <p key={a.agent_id} className="font-mono-plex">
                        {a.agent_name}: {a.tools.join(", ")}
                      </p>
                    ))}
                    <p className="text-default-secondary">{t_i18n("Based on each agent's tools as last known.")}</p>
                  </div>
                )}
                {!requireToolApproval && alwaysAskUnknown && !alwaysAsk && (
                  <p className="content-caption text-feedback-warning-primary mt-1">{t_i18n("Could not check which tools are marked \"always ask\".")}</p>
                )}
              </div>
              <Switch aria-label={t_i18n("Require tool approval")} checked={requireToolApproval} onCheckedChange={onRequireToolApprovalChange} />
            </div>
          </div>
        ) : (
          evaluationSnapshot && (
            <div className="space-y-4">
              <div className="grid grid-cols-4 gap-4">
                <div>
                  <ExecutionModeHeading className="mb-1.5" />
                  {/* Every stored value but ``full_loop`` runs as LLM Only. */}
                  <p className="content-base font-medium">{evaluationExecutionMode(evaluationSnapshot.execution_mode) === "full_loop" ? t_i18n("Full Loop with Tools") : t_i18n("LLM Only")}</p>
                </div>
                <div>
                  <RunModeHeading className="mb-1.5" />
                  <p className="content-base font-medium">{evaluationSnapshot.run_mode === "sequential" ? t_i18n("Sequential") : t_i18n("Parallel")}</p>
                </div>
                <div>
                  <TimeoutHeading label={t_i18n("Execution Timeout")} className="mb-1.5" />
                  <p className="content-base font-medium font-mono-plex">{evaluationSnapshot.timeout_seconds}s</p>
                </div>
                <div>
                  <p className={cn(HEADING, "mb-1.5")}>{t_i18n("Schedule")}</p>
                  <div className="flex items-center gap-2">
                    {evaluationSnapshot.schedule ? (
                      // The raw expression stays visible; what it means, and that it is UTC, is its tooltip.
                      <InfoLabel
                        content={scheduleNote(evaluationSnapshot.schedule, t_i18n)}
                        className="content-base font-medium font-mono-plex"
                      >
                        {evaluationSnapshot.schedule} {t_i18n("UTC")}
                      </InfoLabel>
                    ) : (
                      <Badge variant="outline" className="content-caption">{t_i18n("Disabled")}</Badge>
                    )}
                    {evaluationSnapshot.automatic_runs === false &&
                      evaluationSnapshot.schedule && (
                        <InfoLabel content={t_i18n("Automatic runs are off: the schedule starts no launch until they are turned back on. A run you start still runs.")}>
                          <Badge variant="outline" className="content-caption">{t_i18n("Paused")}</Badge>
                        </InfoLabel>
                      )}
                  </div>
                </div>
              </div>
              <div className="grid grid-cols-4 gap-4 pt-3 border-t border-elevation-subtle-soft">
                <div>
                  <PredictionReuseHeading className={cn(HEADING, "mb-1.5")} />
                  <Badge variant={evaluationSnapshot.reuse_default === "on" ? "cyan" : "outline"} className="content-caption">
                    {evaluationSnapshot.reuse_default === "on" ? t_i18n("Enabled") : t_i18n("Disabled")}
                  </Badge>
                </div>
                <div>
                  <PromptGuardsHeading className={cn(HEADING, "mb-1.5")} />
                  <Badge variant={evaluationSnapshot.apply_prompt_guards ? "cyan" : "outline"} className="content-caption">
                    {evaluationSnapshot.apply_prompt_guards ? t_i18n("Enabled") : t_i18n("Disabled")}
                  </Badge>
                </div>
                <div>
                  <ToolApprovalHeading className={cn(HEADING, "mb-1.5")} />
                  <Badge variant={evaluationSnapshot.require_tool_approval ? "cyan" : "outline"} className="content-caption">
                    {evaluationSnapshot.require_tool_approval ? t_i18n("Enabled") : t_i18n("Disabled")}
                  </Badge>
                </div>
              </div>
            </div>
          )
        )}
      </CardContent>
    </Card>
  );
}

/** A field label: body size, sentence case, as the shared form kit renders it. */
const HEADING = "flex items-center gap-1 content-base font-medium text-default-primary";

/**
 * A field heading with its info bubble: a `Label` tied to the control when `htmlFor` is
 * given (edit mode), the same text as a heading otherwise (read mode).
 */
function FieldHeading({ htmlFor, className, children, tip }: { htmlFor?: string; className?: string; children: ReactNode; tip: ReactNode }) {
  return (
    <div className={cn(HEADING, className)}>
      {htmlFor ? <Label htmlFor={htmlFor}>{children}</Label> : <span>{children}</span>}
      {tip}
    </div>
  );
}

/** The "Execution Mode" heading, with what each mode runs. */
function ExecutionModeHeading({ htmlFor, className }: { htmlFor?: string; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <FieldHeading htmlFor={htmlFor} className={className} tip={
      <InfoTip
        content={t_i18n(
          "LLM Only sends only the agent's persona and the entry to the model: its handovers, tools and knowledge bases do not run. Full Loop with Tools runs the agent as it is configured.",
        )}
        label={t_i18n("What each execution mode runs")}
      />
    }>
      {t_i18n("Execution Mode")}
    </FieldHeading>
  );
}

/** The "Run Mode" heading, with how the runs of one launch are scheduled. */
function RunModeHeading({ htmlFor, className }: { htmlFor?: string; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <FieldHeading htmlFor={htmlFor} className={className} tip={
      <InfoTip
        content={t_i18n(
          "Parallel starts a launch's runs together, a limited number at a time; Sequential runs them one after another.",
        )}
        label={t_i18n("What each run mode does")}
      />
    }>
      {t_i18n("Run Mode")}
    </FieldHeading>
  );
}

/** The "Execution Timeout" heading, with what the budget covers. */
function TimeoutHeading({ label, htmlFor, className }: { label: string; htmlFor?: string; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <FieldHeading htmlFor={htmlFor} className={className} tip={
      <InfoTip
        content={t_i18n(
          "How long one launch of this evaluation may take, all its runs together. A placebo comparison gets this time for each wave of runs.",
        )}
        label={t_i18n("What the execution timeout covers")}
      />
    }>
      {label}
    </FieldHeading>
  );
}

/** The "Prediction Reuse" label, with what is copied and when. */
function PredictionReuseHeading({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <p className={cn("flex items-center gap-1", className)}>
      {t_i18n("Prediction Reuse")}
      <InfoTip content={t_i18n(EVALUATION_TERMS.predictionReuse)} label={t_i18n("What prediction reuse copies")} />
    </p>
  );
}

/** The "Apply prompt guards" label, with the modes it applies to. */
function PromptGuardsHeading({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <p className={cn("flex items-center gap-1", className)}>
      {t_i18n("Apply prompt guards")}
      <InfoTip
        content={t_i18n("Applies to Full Loop with Tools only. LLM Only and pipeline agents run no prompt guard.")}
        label={t_i18n("Where prompt guards apply")}
      />
    </p>
  );
}

/** The "Require tool approval" label, with where it applies. */
function ToolApprovalHeading({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <p className={cn("flex items-center gap-1", className)}>
      {t_i18n("Require tool approval")}
      <InfoTip
        content={t_i18n(
          "Applies to Full Loop with Tools only, to the evaluated agent and the agents it consults, transfers to or delegates to.",
        )}
        label={t_i18n("Where tool approval applies")}
      />
    </p>
  );
}

/**
 * What a stored schedule does, every word translated: a preset by its own label, any other
 * expression without a description (the cron describer is English-only).
 */
function scheduleNote(schedule: string, t_i18n: (id: string, values?: Record<string, string>) => string): string {
  const preset = CRON_PRESETS.find((p) => p.value === schedule.trim());
  return preset
    ? t_i18n("Starts a launch on this schedule: {schedule}, read in UTC.", { schedule: t_i18n(preset.label) })
    : t_i18n("Starts a launch on this cron schedule, read in UTC.");
}
