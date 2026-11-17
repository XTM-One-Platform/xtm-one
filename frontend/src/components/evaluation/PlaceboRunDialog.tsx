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

import { useState } from "react";
import { AlertTriangle, FlaskConical, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { InfoLabel } from "@/components/ui/info-tip";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { usePlaceboEstimate, useTriggerEvaluation } from "@/hooks/api/useEvaluationApi";
import { useTriggerFailedToast } from "./use-trigger-failed-toast";
import { useSkills } from "@/hooks/api/useSkillApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { PLACEBO_TARGET_LABELS, placeboWarningLabel, targetFromKey } from "@/lib/placebo";
import { placeboWarningEffect } from "./placebo-warning-effects";
import { Spinner } from "@filigran/design-system";

/**
 * "Run placebo comparison" (FR-11 to FR-14): what the comparison would run -
 * its runs, agent executions, judge and probe calls, a token estimate - the
 * targets it leaves out, its warnings, every refusal, and the confirmation an
 * agent whose tools may change data needs, before triggering it. The trigger
 * sends the estimate's seed back, so it builds the placebos the estimate checked.
 */
export function PlaceboRunDialog({
  open,
  onOpenChange,
  evaluationId,
  modelIds,
  includeDefault,
  onTriggered,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  evaluationId: string;
  /** The models to run on; left out, every enabled model (like "Run All"). */
  modelIds?: string[];
  includeDefault?: boolean;
  onTriggered?: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const trigger = useTriggerEvaluation();
  const triggerFailed = useTriggerFailedToast();
  // A comparison runs agents: it cannot start while AI is off.
  const aiOff = useAiActionDisabled();
  const [confirmWriteTools, setConfirmWriteTools] = useState(false);
  // Each opening estimates afresh: an earlier opening's figures, refusals or
  // seed never show, nor launch, while the new estimate loads.
  const [opening, setOpening] = useState(0);
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setConfirmWriteTools(false);
      setOpening((count) => count + 1);
    }
  }
  const estimate = usePlaceboEstimate(evaluationId, { modelIds, includeDefault }, open, opening);

  const data = estimate.data;
  const skippedTargets = (data?.skipped ?? []).map((skipped) => ({ ...skipped, parsed: targetFromKey(skipped.target) }));
  const { data: skills = [] } = useSkills({ scope: "all" });
  const targetText = (target: ReturnType<typeof targetFromKey>) =>
    target.kind === "skill"
      ? `${t_i18n(PLACEBO_TARGET_LABELS.skill)} · ${skills.find((skill) => skill.id === target.skill_id)?.name ?? target.skill_id}`
      : t_i18n(PLACEBO_TARGET_LABELS[target.kind] ?? target.kind);
  const writeTools = Object.entries(data?.write_tool_sources ?? {}).sort(([a], [b]) => a.localeCompare(b));
  const blocked =
    aiOff || !data || estimate.isFetching || data.refusals.length > 0 || (writeTools.length > 0 && !confirmWriteTools);

  const run = async () => {
    try {
      const runs = await trigger.mutateAsync({
        evaluationId,
        modelIds,
        includeDefault,
        placebo: true,
        confirmWriteTools: writeTools.length > 0 && confirmWriteTools,
        placeboSeed: data?.seed,
      });
      toast({
        title: t_i18n("Placebo comparison started"),
        description: t_i18n("{count, plural, one {# run} other {# runs}} started.", { count: runs.length }),
        variant: "success",
      });
      onOpenChange(false);
      onTriggered?.();
    } catch (error) {
      triggerFailed(error);
    }
  };

  const stat = (label: string, tip: string, value: string) => (
    <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2">
      <p className="content-caption font-medium uppercase tracking-wider text-default-secondary">
        <InfoLabel content={tip}>{label}</InfoLabel>
      </p>
      <p className="content-base font-semibold">{value}</p>
    </div>
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-filigran-ia-primary/10">
              <FlaskConical className="h-5 w-5 text-filigran-ia-primary" />
            </div>
            <div>
              <DialogTitle>{t_i18n("Run placebo comparison")}</DialogTitle>
              <DialogDescription className="mt-1">
                {t_i18n(
                  "Each agent runs, for the configured number of trials, next to a copy of itself whose target is replaced by a neutral text of the same length, and, when the no-target arm is on, a copy with the target removed.",
                )}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-3 py-2">
          {estimate.isLoading && (
            <p className="flex items-center gap-2 content-compact text-default-secondary">
              <Spinner size="sm" tone="inherit" /> {t_i18n("Estimating...")}
            </p>
          )}
          {estimate.isError && (
            <p role="alert" className="content-compact text-feedback-error-primary">
              {estimate.error instanceof Error ? estimate.error.message : t_i18n("Unknown error")}
            </p>
          )}
          {data && (
            <>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {stat(t_i18n("Runs"), t_i18n("One run per agent, model, arm and trial."), String(data.runs))}
                {stat(
                  t_i18n("Agent executions"),
                  t_i18n("Runs times entries: how many answers the agents give. A comparison above the limit cannot run."),
                  t_i18n("{count, number} (limit {max, number})", { count: data.agent_executions, max: data.execution_cap }),
                )}
                {stat(
                  t_i18n("Judge calls"),
                  t_i18n("One LLM judge call per agent execution when the judge is enabled, none otherwise."),
                  String(data.judge_calls),
                )}
                {stat(
                  t_i18n("Probe calls"),
                  t_i18n("Calls to each run's model that measure a placebo's length in its tokens, so it matches its original's. A text already measured is not probed again."),
                  t_i18n("{count} at most", { count: data.probe_calls }),
                )}
                {stat(
                  t_i18n("Estimated tokens"),
                  t_i18n("The agent tokens the comparison would use: each agent and model's mean tokens per entry on its last completed run, times the entries of each run."),
                  data.estimated_tokens === null ? t_i18n("Not measured yet") : intl.formatNumber(data.estimated_tokens),
                )}
              </div>
              {data.estimated_tokens === null && (
                <p className="text-[11px] text-default-secondary">
                  {t_i18n("Tokens are estimated from each agent and model's last measured run; one of them has none yet.")}
                </p>
              )}

              {skippedTargets.length > 0 && (
                <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2">
                  <p className="mb-1 content-caption font-medium uppercase tracking-wider text-default-secondary">{t_i18n("Targets left out")}</p>
                  <ul className="space-y-0.5">
                    {skippedTargets.map((skipped) => (
                      <li key={`${skipped.agent_id}:${skipped.target}`} className="content-compact text-default-secondary">
                        {skipped.agent_name} · {targetText(skipped.parsed)}: {skipped.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {data.warnings.length > 0 && (
                <ul className="space-y-0.5">
                  {data.warnings.map((code) => {
                    const label = placeboWarningLabel(code);
                    const text = label.known ? t_i18n(label.key) : label.key;
                    const effect = placeboWarningEffect(code);
                    return (
                      <li key={code} className="flex items-center gap-1.5 content-compact text-feedback-warning-primary">
                        <AlertTriangle className="h-3 w-3 shrink-0" /> {effect ? <InfoLabel content={t_i18n(effect)}>{text}</InfoLabel> : text}
                      </li>
                    );
                  })}
                </ul>
              )}

              {data.refusals.length > 0 && (
                <div role="alert" className="rounded-sm border border-feedback-error-primary/35 bg-feedback-error-primary/[0.08] px-3 py-2">
                  <p className="mb-1 flex items-center gap-1.5 content-compact font-medium text-feedback-error-primary">
                    <AlertTriangle className="h-3.5 w-3.5" /> {t_i18n("This comparison cannot run")}
                  </p>
                  <ul className="list-disc space-y-0.5 pl-5">
                    {data.refusals.map((refusal) => (
                      <li key={refusal} className="content-compact text-default-primary">{refusal}</li>
                    ))}
                  </ul>
                </div>
              )}

              {writeTools.length > 0 && (
                <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] px-3 py-2">
                  <p className="mb-1 flex items-center gap-1.5 content-compact font-medium text-feedback-warning-primary">
                    <AlertTriangle className="h-3.5 w-3.5" /> {t_i18n("Tools that may change data")}
                  </p>
                  <p className="mb-1.5 content-compact text-default-secondary">
                    {t_i18n("No one approves these agents' tool calls, and each tool may run once per arm and trial ({count} agent executions).", {
                      count: data.agent_executions,
                    })}
                  </p>
                  <ul className="mb-2 space-y-0.5">
                    {writeTools.map(([agent, sources]) => (
                      <li key={agent} className="content-compact">
                        <span className="font-medium">{agent}</span>: {sources.join(", ")}
                      </li>
                    ))}
                  </ul>
                  <label className="flex items-center justify-between gap-3">
                    <span className="content-compact font-medium">{t_i18n("Run them anyway")}</span>
                    <Switch aria-label={t_i18n("Run them anyway")} checked={confirmWriteTools} onCheckedChange={setConfirmWriteTools} />
                  </label>
                </div>
              )}
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button className="gap-1.5" onClick={run} disabled={blocked || trigger.isPending} aria-busy={trigger.isPending || undefined}>
            {trigger.isPending && <Spinner size="sm" tone="inherit" />}{!trigger.isPending && <Play className="h-3.5 w-3.5" />}
            {t_i18n("Run comparison")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
