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

import { Icon, Slider } from "@filigran/design-system";
import { useId, type ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { Switch } from "@/components/ui/switch";
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from "@/components/ui/select";
import { EVALUATION_SUB_SCORES } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { familyDescription, familyLabel } from "@/lib/evaluation-families";
import { judgeSubScoreDescription } from "@/lib/evaluation-glossary";
import type { EvaluationFamily } from "@/types";
import { MetricName } from "./metric-name";

interface EvaluationScoringCardFamilyMeta {
  id: string;
  label: string;
  description?: string;
  metric_keys?: string[];
}

interface EvaluationScoringCardEvaluationSnapshot {
  family: EvaluationFamily | null;
  penalty_factor: number;
  performance_tolerance: number;
  /**
   * The saved sub-score weights, shown read-only in both modes while the judge
   * is on: set at creation, never edited afterwards, since a change rescores nothing.
   */
  sub_score_weights?: Record<string, number> | null;
}

export interface EvaluationScoringCardProps {
  editing: boolean;
  familyCatalog?: EvaluationScoringCardFamilyMeta[];
  family: EvaluationFamily | "";
  onFamilyChange: (family: EvaluationFamily) => void;
  judgeEnabled: boolean;
  onJudgeEnabledChange: (v: boolean) => void;
  performanceTolerance: number;
  onPerformanceToleranceChange: (v: number) => void;
  penaltyFactor: number;
  onPenaltyFactorChange: (v: number) => void;
  /** Creation-page-only fields — omitted on the Configuration tab. */
  subScoreWeights?: Record<string, number>;
  onSubScoreWeightChange?: (key: string, v: number) => void;
  subScoreTotal?: number;
  /**
   * Configuration-tab-only slots for fields the creation page lacks: `judgeExtra`
   * inside the enabled LLM Judge block (Evaluator Model, Judge Output Budget,
   * Judge Instructions), `extraContent` after it (Per-Field Scoring),
   * `aggregationExtra` inside Aggregation (Overall Score Composition).
   */
  judgeExtra?: ReactNode;
  extraContent?: ReactNode;
  aggregationExtra?: ReactNode;
  /** Read-only-mode display — ignored when `editing`, except its `sub_score_weights`. */
  evaluationSnapshot?: EvaluationScoringCardEvaluationSnapshot;
  judgeConfigured?: boolean;
  readOnlyJudgeExtra?: ReactNode;
  /**
   * ConfigTab-only read-only "Judge Instructions", rendered below the LLM
   * Judge grid when `judgeConfigured`.
   */
  readOnlyExtraContent?: ReactNode;
  /** The read-only counterpart of `extraContent` (Per-Field Scoring), judge or not. */
  readOnlyFieldScoring?: ReactNode;
  readOnlyAggregationExtra?: ReactNode;
}

/**
 * The "Scoring" card of the evaluation creation page and of `EvaluationDetail.tsx`'s
 * Configuration tab, so both render the same fields: what is measured
 * (Output Family, LLM Judge), then how it is reduced into headlines (Aggregation).
 */
export function EvaluationScoringCard({
  editing,
  familyCatalog,
  family,
  onFamilyChange,
  judgeEnabled,
  onJudgeEnabledChange,
  performanceTolerance,
  onPerformanceToleranceChange,
  penaltyFactor,
  onPenaltyFactorChange,
  subScoreWeights,
  onSubScoreWeightChange,
  subScoreTotal,
  judgeExtra,
  extraContent,
  aggregationExtra,
  evaluationSnapshot,
  judgeConfigured,
  readOnlyJudgeExtra,
  readOnlyExtraContent,
  readOnlyFieldScoring,
  readOnlyAggregationExtra,
}: EvaluationScoringCardProps) {
  const { t_i18n } = useFormatter();
  const familyMeta = family ? familyCatalog?.find((f) => f.id === family) : undefined;
  const showSubScoreWeights = subScoreWeights !== undefined && onSubScoreWeightChange !== undefined && subScoreTotal !== undefined;
  const savedSubScoreWeights = evaluationSnapshot?.sub_score_weights;
  const fieldId = useId();
  const familyFieldId = `${fieldId}-family`;
  const weightsHelpId = `${fieldId}-weights-help`;

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base font-medium">{t_i18n("Scoring")}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {editing ? (
          <div className="space-y-5">
            <div>
              <OutputFamilyHeading htmlFor={familyFieldId} className="mb-1.5" />
              <Select value={family} onValueChange={(v) => onFamilyChange(v as EvaluationFamily)}>
                <SelectTrigger id={familyFieldId} className="h-9 w-full max-w-md content-base">
                  <SelectValue placeholder={t_i18n("Select an output family")} />
                </SelectTrigger>
                <SelectContent>
                  {(familyCatalog ?? []).map((f) => (
                    <SelectItem key={f.id} value={f.id}>
                      {t_i18n(f.label)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {family ? (
                <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 mt-1.5 space-y-2">
                  <p className="content-compact text-default-secondary">{familyDescription(familyMeta, t_i18n)}</p>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="content-compact font-medium">{t_i18n("Metrics:")}</span>
                    {(familyMeta?.metric_keys ?? []).map((k) => (
                      <Badge key={k} variant="secondary" className="content-caption"><MetricName metricKey={k} /></Badge>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
            <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
              <div className="min-w-0">
                <p className="content-base font-medium">{t_i18n("LLM Judge")}</p>
                <p className="content-compact text-default-secondary mt-0.5">
                  {t_i18n(SCORING_NOTES.llmJudge)}
                </p>
              </div>
              <Switch aria-label={t_i18n("LLM Judge")} checked={judgeEnabled} onCheckedChange={onJudgeEnabledChange} />
            </div>
            {judgeEnabled && judgeExtra}
            {extraContent}

            {/* Aggregation — ordered by pipeline stage: entries, then runs, then headlines. */}
            <div className="space-y-5 pt-3 border-t border-elevation-subtle-soft">
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="content-base font-medium">{t_i18n("Consistency Penalty")}</p>
                    <p className="content-compact text-default-secondary mt-0.5">{t_i18n("Across dataset entries, within a run")}</p>
                  </div>
                  <span className="content-base font-mono-plex font-medium tabular-nums">{penaltyFactor}</span>
                </div>
                <Slider
                  aria-label={t_i18n("Consistency Penalty")}
                  min={0}
                  max={1.5}
                  step={0.1}
                  value={[penaltyFactor]}
                  onValueChange={([value]) => onPenaltyFactorChange(value)}
                />
                <div className="flex justify-between content-caption text-default-secondary"><span>{t_i18n("0 (mean only)")}</span><span>{t_i18n("1.5 (strict)")}</span></div>
                <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 space-y-2">
                  <p className="content-compact text-default-secondary">
                    <span className="font-medium text-default-primary">{t_i18n("Formula:")}</span>{" "}
                    <code className="text-accent font-mono-plex">mean - k × std_dev</code>{" "}
                    {t_i18n(", reversed to")}{" "}
                    <code className="text-accent font-mono-plex">mean + k × std_dev</code>{" "}
                    {t_i18n("for lower-is-better metrics")}
                  </p>
                  <p className="content-compact text-default-secondary">
                    {t_i18n("Every metric is reduced across dataset entries to its mean, then shifted toward the worse side in proportion to the standard deviation: Judge Quality, each Overall Quality metric and each Overall Performance metric alike. A higher k rewards agents that perform consistently across all entries.")}
                  </p>
                  <p className="content-compact text-default-secondary">
                    <span className="font-medium text-default-primary">{t_i18n("Example:")}</span>{" "}
                    {t_i18n("A higher-is-better metric scored 85, 92, 70 across 3 entries → mean = 82.3, std = 9.2 → with k={k}:", { k: penaltyFactor })} <code className="font-mono-plex text-accent">{Math.max(0, 82.3 - penaltyFactor * 9.2).toFixed(1)}</code>
                  </p>
                </div>
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <PerformanceToleranceHeading className="content-base font-medium" />
                    <p className="content-compact text-default-secondary mt-0.5">{t_i18n("Across runs, within the evaluation")}</p>
                  </div>
                  <span className="content-base font-mono-plex font-medium tabular-nums">{performanceTolerance}%</span>
                </div>
                <Slider
                  aria-label={t_i18n("Performance Tolerance")}
                  min={0}
                  max={25}
                  step={1}
                  value={[performanceTolerance]}
                  onValueChange={([value]) => onPerformanceToleranceChange(value)}
                />
                <div className="flex justify-between content-caption text-default-secondary"><span>{t_i18n("0% (strict)")}</span><span>{t_i18n("25% (lenient)")}</span></div>
              </div>
              {showSubScoreWeights && (
                <div className="space-y-2 pt-3 border-t border-elevation-subtle-soft">
                  <div>
                    <p className="flex items-center gap-1 content-base font-medium">
                      {t_i18n("Sub-Score Weights")}
                      <InfoTip
                        content={t_i18n("How much each of the LLM judge's sub-scores counts in an entry's Judge Quality.")}
                        label={t_i18n("How the sub-score weights count")}
                      />
                    </p>
                    <p id={weightsHelpId} className="content-compact text-default-secondary mt-0.5">
                      {t_i18n("Set relative importance: values are normalized automatically")}
                    </p>
                  </div>
                  <div className="space-y-2">
                    {EVALUATION_SUB_SCORES.map((s) => (
                      <div key={s.key} className="flex items-center gap-3">
                        <div className={`content-base flex-1 ${s.color}`}>
                          <Label id={`${fieldId}-weight-${s.key}-label`} htmlFor={`${fieldId}-weight-${s.key}`} className={s.color}>
                            <SubScoreName subScoreKey={s.key} label={s.label} />
                          </Label>
                        </div>
                        <Input
                          id={`${fieldId}-weight-${s.key}`}
                          aria-labelledby={`${fieldId}-weight-${s.key}-label`}
                          aria-describedby={weightsHelpId}
                          data-testid={`evaluation-sub-score-weight-${s.key}`}
                          type="number"
                          min={0}
                          step={1}
                          value={(subScoreWeights as Record<string, number>)[s.key]}
                          onChange={(e) => onSubScoreWeightChange!(s.key, Math.max(0, Number(e.target.value) || 0))}
                          className="w-20 text-right font-mono-plex tabular-nums"
                        />
                        <span className="content-compact font-mono-plex text-default-secondary w-10 text-right tabular-nums">
                          {Math.round(((subScoreWeights as Record<string, number>)[s.key] / (subScoreTotal as number)) * 100)}%
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {!showSubScoreWeights && savedSubScoreWeights !== undefined && judgeEnabled && (
                <SavedSubScoreWeights weights={savedSubScoreWeights} />
              )}
              {aggregationExtra}
            </div>
          </div>
        ) : (
          evaluationSnapshot && (
            <div className="space-y-4">
              <div>
                <OutputFamilyHeading className="mb-1.5" />
                {evaluationSnapshot.family ? (
                  (() => {
                    const meta = familyCatalog?.find((f) => f.id === evaluationSnapshot.family);
                    return (
                      <div className="flex flex-wrap items-center gap-2">
                        <FamilyBadge label={familyLabel(evaluationSnapshot.family, familyCatalog, t_i18n)} description={familyDescription(meta, t_i18n)} />
                        {(meta?.metric_keys ?? []).map((mk) => (
                          <Badge key={mk} variant="secondary" className="content-caption"><MetricName metricKey={mk} /></Badge>
                        ))}
                      </div>
                    );
                  })()
                ) : (
                  <div className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] px-3 py-2">
                    <Icon name="triangle-alert" size={16} className="text-feedback-warning-primary shrink-0" />
                    <p className="content-caption text-feedback-warning-primary">
                      {t_i18n("No output family set. Edit this evaluation to select a family before running it.")}
                    </p>
                  </div>
                )}
              </div>

              <div className="grid grid-cols-4 gap-4 pt-3 border-t border-elevation-subtle-soft">
                <div>
                  <p className={cn(FIELD_HEADING, "mb-1.5")}>
                    {t_i18n("LLM Judge")}
                    <InfoTip content={t_i18n(SCORING_NOTES.llmJudge)} label={t_i18n("What the LLM judge scores")} />
                  </p>
                  <Badge variant={judgeConfigured ? "cyan" : "outline"} className="content-caption">
                    {judgeConfigured ? t_i18n("Enabled") : t_i18n("Disabled")}
                  </Badge>
                </div>
                {judgeConfigured && readOnlyJudgeExtra}
              </div>
              {judgeConfigured && readOnlyExtraContent}
              {readOnlyFieldScoring}

              <div className="pt-3 border-t border-elevation-subtle-soft">
                <div className="grid grid-cols-4 gap-4">
                  <div>
                    <p className={cn(FIELD_HEADING, "mb-1.5")}>{t_i18n("Consistency Penalty")}</p>
                    <p className="content-base font-medium font-mono-plex">{evaluationSnapshot.penalty_factor}</p>
                  </div>
                  <div>
                    <PerformanceToleranceHeading className={cn(FIELD_HEADING, "mb-1.5")} />
                    <p className="content-base font-medium font-mono-plex">{evaluationSnapshot.performance_tolerance}%</p>
                  </div>
                  {readOnlyAggregationExtra}
                </div>
                <div className="mt-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
                  <p className="content-compact text-default-secondary">
                    <code className="text-accent font-mono-plex">mean ∓ {evaluationSnapshot.penalty_factor} × std_dev</code>
                    <span className="mx-1.5">—</span>
                    {t_i18n("Applied to every metric, always shifted toward the worse side: Judge Quality, each Overall Quality metric and each Overall Performance metric.")}
                  </p>
                </div>
                {judgeConfigured && savedSubScoreWeights !== undefined && (
                  <SavedSubScoreWeights weights={savedSubScoreWeights} />
                )}
              </div>
            </div>
          )
        )}
      </CardContent>
    </Card>
  );
}

/**
 * What the LLM judge switch decides: the judge runs for every family when on, and
 * no family needs it (each has deterministic metrics, `families.py`). @i18n-keys
 */
const SCORING_NOTES = {
  llmJudge:
    "Scores each answer on five sub-scores, combined into Judge Quality. Off, no judge runs and Judge Quality reads N/A; the output family's metrics still score.",
  savedSubScoreWeights:
    "Set when the evaluation is created. They are not edited afterwards: a change would not rescore the runs already scored.",
} as const;

/**
 * The weight the judge gives each sub-score (`_compute_global_score`): the saved ones as
 * given, a sub-score they leave out counting nothing; none saved (or `{}`), 0.20 each.
 */
function appliedSubScoreWeight(saved: Record<string, number> | null, key: string, defaultPercent: number): number {
  if (!saved || Object.keys(saved).length === 0) return defaultPercent / 100;
  return saved[key] ?? 0;
}

/**
 * Whole percents for `weights` that add up to their rounded total (largest remainder):
 * three weights of 1/3 read 34%, 33%, 33%, not 33% three times. Ties go to the first.
 */
function wholePercents(weights: number[]): number[] {
  const exact = weights.map((w) => w * 100);
  const percents = exact.map(Math.floor);
  const missing = Math.round(exact.reduce((a, b) => a + b, 0)) - percents.reduce((a, b) => a + b, 0);
  const byRemainder = exact.map((e, i) => ({ i, remainder: e - percents[i] })).sort((a, b) => b.remainder - a.remainder || a.i - b.i);
  for (let k = 0; k < missing && k < byRemainder.length; k++) percents[byRemainder[k].i] += 1;
  return percents;
}

/** The saved sub-score weights, read-only: set at creation, a change would rescore nothing. */
function SavedSubScoreWeights({ weights }: { weights: Record<string, number> | null }) {
  const { t_i18n } = useFormatter();
  const percents = wholePercents(EVALUATION_SUB_SCORES.map((s) => appliedSubScoreWeight(weights, s.key, s.weight)));
  return (
    <div className="space-y-2 pt-3 mt-3 border-t border-elevation-subtle-soft">
      <div>
        <p className={FIELD_HEADING}>
          {t_i18n("Sub-Score Weights")}
          <InfoTip
            content={t_i18n("How much each of the LLM judge's sub-scores counts in an entry's Judge Quality.")}
            label={t_i18n("How the sub-score weights count")}
          />
        </p>
        <p className="content-compact text-default-secondary mt-0.5">{t_i18n(SCORING_NOTES.savedSubScoreWeights)}</p>
      </div>
      <dl className="grid grid-cols-5 gap-3">
        {EVALUATION_SUB_SCORES.map((s, i) => (
          <div key={s.key}>
            <dt className={cn("content-compact", s.color)}>
              <SubScoreName subScoreKey={s.key} label={s.label} />
            </dt>
            <dd className="content-base font-medium font-mono-plex tabular-nums">
              {percents[i]}%
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** A judge sub-score's name, with what the judge asks about it as its tooltip. */
function SubScoreName({ subScoreKey, label }: { subScoreKey: string; label: string }) {
  const { t_i18n } = useFormatter();
  const description = judgeSubScoreDescription(subScoreKey);
  if (!description) return <>{t_i18n(label)}</>;
  return <InfoLabel content={t_i18n(description)}>{t_i18n(label)}</InfoLabel>;
}

/** The read-only family, with its description as its tooltip, as the edit view shows it. */
function FamilyBadge({ label, description }: { label: string; description: string | null }) {
  const badge = <Badge variant="cyan" className="content-caption">{label}</Badge>;
  return description ? <InfoLabel content={description}>{badge}</InfoLabel> : badge;
}

/** A field label: body size, sentence case, as the shared form kit renders it. */
const FIELD_HEADING = "flex items-center gap-1 content-base font-medium text-default-primary";

/** The "Output Family" heading, with what choosing one decides; a `Label` tied to the picker when `htmlFor` is given. */
function OutputFamilyHeading({ htmlFor, className }: { htmlFor?: string; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className={cn(FIELD_HEADING, className)}>
      {htmlFor ? <Label htmlFor={htmlFor}>{t_i18n("Output Family")}</Label> : <span>{t_i18n("Output Family")}</span>}
      <InfoTip
        content={t_i18n(
          "The shape every entry's expected answer takes. It decides which metrics compare the agent's answer with it, and so what Overall Quality is composed from.",
        )}
        label={t_i18n("What an output family decides")}
      />
    </div>
  );
}

/** The "Performance Tolerance" heading, with how the band scores runs. */
function PerformanceToleranceHeading({ className }: { className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <p className={cn("flex items-center gap-1", className)}>
      {t_i18n("Performance Tolerance")}
      <InfoTip
        content={t_i18n(
          "On each performance metric, a run within this percentage of the best run's value (of the gap between the best and worst runs when the best is 0 or less) scores 100, as the best does; the others scale down to 0 at the worst run.",
        )}
        label={t_i18n("How the performance tolerance scores runs")}
      />
    </p>
  );
}
