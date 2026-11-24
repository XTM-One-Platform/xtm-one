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

import { useId, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  FlaskConical, Bot, Cpu, Database, Plus, Play, Copy, ChevronDown, Check, Upload, Search, Star, Tag,
} from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FilterClearButton } from "@/components/ui/filter-pill";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { SearchInput } from "@/components/ui/search-input";
import { SelectionBox } from "@/components/ui/list-control-bar";
import { FamilyBadge } from "@/components/ui/family-badge";
import { Icon } from "@filigran/design-system";
import { comparisonEligibility } from "@/lib/evaluation-compare";
import { EmptyState } from "@/components/ui/empty-state";
import { Tip } from "@/components/ui/tooltip";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { HEADLINE_LABELS, headlines } from "@/lib/evaluation-headlines";
import { headlineTip } from "@/components/evaluation/headline-tip";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { PlaceboArmTag } from "@/components/evaluation/PlaceboArmTag";
import { PlatformErrorBadge } from "@/components/evaluation/platform-error-badge";
import { RunMenuModelRow } from "@/components/evaluation/unavailable-model";
import { useTriggerFailedToast } from "@/components/evaluation/use-trigger-failed-toast";
import { AutomaticRunsPausedBadge } from "@/components/evaluation/automatic-runs-badge";
import { hasAutomaticRuns } from "@/components/evaluation/automatic-runs";
import { useToast } from "@/components/ui/toast";
import {
  useEvaluationAvailableModels,
  useEvaluations,
  useTriggerEvaluation,
  useCopyEvaluation,
  useImportEvaluation,
  useSearchRuns,
} from "@/hooks/api/useEvaluationApi";
import { useClickOutside } from "@/hooks/useClickOutside";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useDebounce } from "@/hooks/useDebounce";
import { useAuth } from "@/hooks/useAuth";
import { cn, isEntityShared, formatModelLabel } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { EVALUATION_STATUS_CONFIG, PROVIDER_DISPLAY_NAMES, RUN_TAG_BASELINE_KEY, headlineColorClass, headlineDef } from "@/lib/constants";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import type { Evaluation, RunSearchFilters } from "@/types";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { formatTimeAgo } from "@/lib/relative-time";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

/**
 * The evaluation list's own explanations, read by `t_i18n`. The run menu's
 * sentences are the evaluation page's, word for word. @i18n-keys
 */
const LIST_NOTES = {
  runOptions: "Choose the models to run on.",
  copy: "Creates a private copy with the same settings, agents and dataset links, without its schedule, runs or results.",
  agents: "The agents this evaluation runs.",
  models: "The models configured on this evaluation, disabled ones included.",
  entries: "The entries of the datasets linked to this evaluation, all together.",
  lastLaunched: "When this evaluation was last launched.",
  minScore:
    "Keeps the runs whose chosen score is at least this value. Overall Performance is comparative within each evaluation, so one value means different things across evaluations.",
  tags: "Matches a run's tags, merged from its evaluation's, its agent's and its own. Leave the value empty to match every run that has the key.",
  baselinesOnly: "Keeps the runs of each evaluation's baseline agent.",
  resultCap: "The 50 most recent matching runs at most, across the evaluations you can see.",
} as const;

function EvaluationCard({
  evaluation,
  userId,
  selected,
  onToggleSelect,
}: {
  evaluation: Evaluation;
  userId?: string;
  /** Selected for a comparison (#12282). */
  selected: boolean;
  onToggleSelect: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const isShared = isEntityShared(evaluation, userId);
  const agentCount = evaluation.agent_ids.length;
  const modelCount = evaluation.model_ids.length;
  const trigger = useTriggerEvaluation();
  const triggerFailed = useTriggerFailedToast();
  const copy = useCopyEvaluation();
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();
  const { toast } = useToast();
  const [runMenuOpen, setRunMenuOpen] = useState(false);
  const [pendingModelId, setPendingModelId] = useState<string | null>(null);
  const [pickedModelIds, setPickedModelIds] = useState<string[]>([]);
  // The owner's models, the extra ones a launch accepts: read when the menu opens,
  // not once per card.
  const { data: availableModels = [] } = useEvaluationAvailableModels(evaluation.id, { enabled: runMenuOpen });
  const runMenuRef = useRef<HTMLDivElement>(null);
  const enabledModels = (evaluation.model_ids || []).filter(
    (mid) => !(evaluation.disabled_model_ids || []).includes(mid),
  );
  const unavailableModelIds = useMemo(
    () => new Set(evaluation.unavailable_model_ids ?? []),
    [evaluation.unavailable_model_ids],
  );
  // A model flagged after it was picked leaves the selection: Run Selected would be refused.
  const selectedModelIds = useMemo(
    () => pickedModelIds.filter((mid) => !unavailableModelIds.has(mid)),
    [pickedModelIds, unavailableModelIds],
  );
  const extraSelectedModels = selectedModelIds.filter(
    (mid) => mid !== "__default__" && !enabledModels.includes(mid),
  );
  const runOnModels = [...enabledModels, ...extraSelectedModels];
  const availableModelsToAdd = useMemo(() => {
    const configured = new Set(evaluation.model_ids || []);
    const unique = new Map<string, { model: string; provider: string }>();
    for (const item of availableModels) {
      if (
        !configured.has(item.model)
        && !selectedModelIds.includes(item.model)
        && !unique.has(item.model)
      ) {
        unique.set(item.model, { model: item.model, provider: item.provider });
      }
    }
    return Array.from(unique.values()).sort((a, b) => a.model.localeCompare(b.model));
  }, [availableModels, evaluation.model_ids, selectedModelIds]);
  useClickOutside(runMenuRef, () => setRunMenuOpen(false), runMenuOpen);

  const toggleModelSelection = (modelId: string) => {
    setPickedModelIds((prev) =>
      prev.includes(modelId)
        ? prev.filter((id) => id !== modelId)
        : [...prev, modelId],
    );
  };

  const clearSelection = () => {
    setPickedModelIds([]);
  };

  const handleTrigger = async (e: React.MouseEvent, modelIds?: string[]) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      const runs = await trigger.mutateAsync({ evaluationId: evaluation.id, modelIds });
      toast({
        title: t_i18n("Evaluation triggered"),
        description: t_i18n("{count, plural, one {# run} other {# runs}} started.", { count: runs.length }),
        variant: "success",
      });
      clearSelection();
      setRunMenuOpen(false);
    } catch (error) {
      triggerFailed(error);
    }
  };

  const handleTriggerSelected = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (selectedModelIds.length === 0) return;
    setPendingModelId("__selected__");
    try {
      const includeDefault = selectedModelIds.includes("__default__");
      const additionalModelIds = selectedModelIds.filter((id) => id !== "__default__");
      const runs = await trigger.mutateAsync({
        evaluationId: evaluation.id,
        modelIds: additionalModelIds,
        includeDefault,
      });
      toast({
        title: t_i18n("Evaluation triggered"),
        description: t_i18n("{runs, plural, one {# run} other {# runs}} started on {models, plural, one {# model} other {# models}}.", {
          runs: runs.length,
          models: selectedModelIds.length,
        }),
        variant: "success",
      });
      clearSelection();
      setRunMenuOpen(false);
    } catch (error) {
      triggerFailed(error);
    } finally {
      setPendingModelId(null);
    }
  };

  const handleAddAvailableModelSelection = (e: React.MouseEvent, modelId: string) => {
    e.preventDefault();
    e.stopPropagation();
    setPickedModelIds((prev) => {
      const next = prev.includes(modelId) ? prev : [...prev, modelId];
      if (evaluation.use_agent_default_model && !next.includes("__default__")) {
        return ["__default__", ...next];
      }
      return next;
    });
    toast({
      title: t_i18n("Model added to selection"),
      description: evaluation.use_agent_default_model
        ? t_i18n("Default model + selected model ready. Click Run Selected.")
        : t_i18n("Selected model ready. Click Run Selected."),
      variant: "info",
    });
  };

  const handleCopy = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      const created = await copy.mutateAsync(evaluation.id);
      toast({ title: t_i18n("Evaluation copied"), description: t_i18n("\"{name}\" created.", { name: created.name }), variant: "success" });
      // A model no provider of the copy's owner offers, from the server.
      for (const warning of created.warnings ?? []) {
        toast({ title: t_i18n("Evaluation copied"), description: warning, variant: "info" });
      }
      navigate(`/evaluations/${created.id}`);
    } catch {
      toast({ title: t_i18n("Failed to copy evaluation"), variant: "error" });
    }
  };

  // The name is the card's link, stretched over it, so the buttons and explanations
  // stay their own tab stops instead of controls nested inside a link.
  return (
    <Card className={cn(
      "relative transition-all hover:bg-elevation-hover border-elevation-subtle h-full backdrop-blur-sm layer-1 bg-elevation-default hover:border-accent/30 focus-within:border-accent/30",
      // The blur makes each card its own stacking context: raised while its run menu is
      // open, or the next cards would paint over the menu.
      runMenuOpen && "z-20",
    )}>
      <CardContent className="p-4 flex flex-col h-full">
        {/* The header wraps: once the name (at least 8rem) and the controls and badges do
            not fit on one row, the controls and badges move under the name, so a card with
            every badge keeps its name visible and nothing runs past its border. */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            <FlaskConical className="h-5 w-5 text-accent" />
          </div>
          <div className="min-w-32 flex-1">
            {/* A name too long for the card is cut, its full text on hover (title) and
                unfolded on keyboard focus, as a title never shows on focus. */}
            <Link
              to={`/evaluations/${evaluation.id}`}
              title={evaluation.name}
              className="block content-base font-medium truncate rounded-sm after:absolute after:inset-0 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus-visible:whitespace-normal focus-visible:break-words"
            >
              {evaluation.name}
            </Link>
            <p className="content-caption text-default-secondary line-clamp-1">{evaluation.description || " "}</p>
          </div>
          {/* Above the stretched link and above the footer below (z-20 over z-10), so the
              run menu is never covered; only the controls take clicks, the gaps open the card. */}
          <div className="relative z-20 flex flex-wrap items-center gap-2 max-w-full pointer-events-none [&>*]:pointer-events-auto">
            <SelectionBox
              checked={selected}
              onToggle={onToggleSelect}
              label={t_i18n("Select {name} to compare", { name: evaluation.name })}
            />
            <Tip content={aiOff ? aiReason : t_i18n(LIST_NOTES.copy)} wrap="auto">
              <button
                aria-label={t_i18n("Copy evaluation")}
                className="flex h-7 w-7 items-center justify-center rounded-sm text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-colors disabled:opacity-40"
                onClick={handleCopy}
                disabled={copy.isPending || aiOff}
                aria-busy={copy.isPending || undefined}
              >
                {copy.isPending ? (
                  <Spinner size="sm" tone="inherit" label={t_i18n("Copy evaluation")} />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
              </button>
            </Tip>
            <div className="relative" ref={runMenuRef}>
              <div className="flex items-center overflow-hidden rounded-sm border border-accent/20">
                <Tip content={aiOff ? aiReason : t_i18n(EVALUATION_TERMS.runEvaluation)} wrap="auto">
                  <button
                    aria-label={t_i18n("Run evaluation now")}
                    className="flex h-7 w-7 items-center justify-center text-accent hover:bg-accent/10 transition-colors disabled:opacity-40"
                    onClick={(e) => handleTrigger(e)}
                    disabled={trigger.isPending || aiOff}
                    aria-busy={trigger.isPending || undefined}
                  >
                    {trigger.isPending ? (
                      <Spinner size="sm" tone="inherit" label={t_i18n("Running...")} />
                    ) : (
                      <Play className="h-3.5 w-3.5" />
                    )}
                  </button>
                </Tip>
                <Tip content={t_i18n(LIST_NOTES.runOptions)} wrap="auto">
                  <button
                    className="flex h-7 w-6 items-center justify-center text-accent hover:bg-accent/10 transition-colors disabled:opacity-40 border-l border-accent/20"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      if (!runMenuOpen) {
                        clearSelection();
                      }
                      setRunMenuOpen((v) => !v);
                    }}
                    disabled={trigger.isPending}
                    aria-label={t_i18n("Open run options")}
                    aria-expanded={runMenuOpen}
                  >
                    <ChevronDown className="h-3.5 w-3.5" />
                  </button>
                </Tip>
              </div>

              {runMenuOpen && (
                <div
                  className="absolute right-0 z-50 mt-1 w-80 overflow-hidden rounded-sm border border-accent/20 layer-1 bg-elevation-default shadow-overlay backdrop-blur-sm"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                  }}
                >
                  <div className="border-b border-elevation-subtle-soft px-3 py-2.5">
                    <Tip content={aiOff ? aiReason : t_i18n(EVALUATION_TERMS.runAll)} wrap="auto" wrapClassName="inline-flex w-full">
                      <button
                        className="w-full rounded-sm bg-accent/15 px-3 py-2 text-left content-compact font-medium text-accent hover:bg-accent/20 transition-colors disabled:opacity-40"
                        onClick={(e) => handleTrigger(e)}
                        disabled={trigger.isPending || aiOff}
                      >
                        {t_i18n("Run All")}
                      </button>
                    </Tip>
                  </div>

                  <div className="px-3 pb-3 pt-2">
                    <div className="mb-1.5 flex items-center justify-between">
                      <p className="content-caption font-medium uppercase tracking-wider text-default-secondary">{t_i18n("Run on :")}</p>
                      <span className="content-caption text-default-secondary">
                        {t_i18n("{count} selected", { count: selectedModelIds.length })}
                      </span>
                    </div>
                    <div className="max-h-44 overflow-y-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default">
                      {!evaluation.use_agent_default_model && runOnModels.length === 0 ? (
                        <p className="px-2 py-2 content-compact text-default-secondary">{t_i18n("No enabled models.")}</p>
                      ) : (
                        <>
                          {evaluation.use_agent_default_model && (() => {
                            const isSelected = selectedModelIds.includes("__default__");
                            return (
                              <Tip key="__default__" content={`${t_i18n(EVALUATION_TERMS.agentDefaultModel)} ${t_i18n(EVALUATION_TERMS.defaultModelInSelection)}`}>
                              <button
                                aria-pressed={isSelected}
                                className="flex w-full items-center gap-2 border-b border-elevation-subtle-soft px-2 py-1.5 text-left content-compact hover:bg-accent/10 transition-colors"
                                onClick={(e) => {
                                  e.preventDefault();
                                  e.stopPropagation();
                                  toggleModelSelection("__default__");
                                }}
                                disabled={trigger.isPending || pendingModelId === "__selected__"}
                              >
                                <span className={isSelected ? "flex h-4 w-4 items-center justify-center rounded-sm border border-accent/50 bg-accent/20 text-accent" : "flex h-4 w-4 items-center justify-center rounded-sm border border-elevation-subtle-soft"}>
                                  {isSelected ? <Check className="h-2.5 w-2.5" /> : null}
                                </span>
                                <span className="truncate italic text-default-secondary/80">{t_i18n("Agent default model")}</span>
                              </button>
                              </Tip>
                            );
                          })()}
                          {runOnModels.map((modelId) => (
                            <RunMenuModelRow
                              key={modelId}
                              modelId={modelId}
                              selected={selectedModelIds.includes(modelId)}
                              unavailable={unavailableModelIds.has(modelId)}
                              disabled={trigger.isPending || pendingModelId === "__selected__"}
                              onToggle={(e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                toggleModelSelection(modelId);
                              }}
                            />
                          ))}
                        </>
                      )}
                    </div>

                    {availableModelsToAdd.length > 0 && (
                      <div className="mt-2">
                        <div className="mb-1.5 flex items-center justify-between">
                          <p className="flex items-center gap-1 content-caption font-medium uppercase tracking-wider text-default-secondary">
                            {t_i18n("Available Models")}
                            <InfoTip label={t_i18n("What adding an available model does")} content={t_i18n(EVALUATION_TERMS.availableModels)} />
                          </p>
                          <span className="content-caption text-default-secondary">{t_i18n("Click to add to selection")}</span>
                        </div>
                        <div className="max-h-36 overflow-y-auto rounded-sm border border-elevation-subtle-soft bg-elevation-default">
                          {availableModelsToAdd.map((m) => (
                            <button
                              key={m.model}
                              className="flex w-full items-center justify-between gap-2 border-b border-elevation-subtle-soft px-2 py-1.5 text-left content-compact hover:bg-accent/10 transition-colors last:border-b-0"
                              onClick={(e) => handleAddAvailableModelSelection(e, m.model)}
                              disabled={trigger.isPending}
                            >
                              <span className="min-w-0 flex-1">
                                <span className="block truncate font-mono-plex">{m.model}</span>
                                <span className="block truncate content-caption text-default-secondary">{PROVIDER_DISPLAY_NAMES[m.provider] || m.provider}</span>
                              </span>
                              <Plus className="h-3.5 w-3.5 text-accent" />
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    <div className="mt-2 flex items-center gap-2">
                      <AiDisabledTip wrapClassName="flex flex-1">
                        <button
                          className="flex-1 rounded-sm bg-accent px-3 py-2 content-compact font-medium text-negative-primary hover:bg-accent/90 transition-colors disabled:opacity-40"
                          onClick={handleTriggerSelected}
                          disabled={selectedModelIds.length === 0 || trigger.isPending || aiOff}
                          aria-busy={pendingModelId === "__selected__" || undefined}
                        >
                          {pendingModelId === "__selected__" ? (
                            <span className="flex items-center justify-center gap-1.5">
                              <Spinner size="sm" tone="inherit" />
                              {t_i18n("Running...")}
                            </span>
                          ) : (
                            t_i18n("Run Selected ({count})", { count: selectedModelIds.length })
                          )}
                        </button>
                      </AiDisabledTip>
                      <button
                        className="rounded-sm px-2.5 py-2 content-compact text-default-secondary hover:text-default-primary hover:bg-elevation-hover transition-colors disabled:opacity-40"
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          clearSelection();
                        }}
                        disabled={selectedModelIds.length === 0 || trigger.isPending}
                      >
                        {t_i18n("Clear")}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
            {evaluation.has_active_runs && (
              <Badge variant="cyan" className="content-caption gap-1 animate-pulse pointer-events-none!">
                <Spinner size="sm" tone="inherit" />
                {t_i18n("Evaluating...")}
              </Badge>
            )}
            <AutomaticRunsPausedBadge evaluation={evaluation} />
            {evaluation.is_company_managed && (
              <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.companyManagedEvaluation)}>
                <CompanyManagedBadge />
              </InfoLabel>
            )}
            {isShared && (
              <InfoLabel className="inline-flex" content={t_i18n(EVALUATION_TERMS.sharedEvaluation)}>
                <SharedBadge />
              </InfoLabel>
            )}
          </div>
        </div>

        {/* Stats -- pushed to bottom */}
        <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
          <div className="relative z-10 flex items-center gap-2 text-default-secondary pointer-events-none [&>*]:pointer-events-auto">
            <InfoLabel className="flex items-center gap-0.5 content-caption" content={t_i18n(LIST_NOTES.agents)}>
              <Bot className="h-3 w-3" aria-label={t_i18n("Agents")} />
              {agentCount}
            </InfoLabel>
            <InfoLabel className="flex items-center gap-0.5 content-caption" content={t_i18n(LIST_NOTES.models)}>
              <Cpu className="h-3 w-3" aria-label={t_i18n("Models")} />
              {modelCount}
            </InfoLabel>
            <InfoLabel className="flex items-center gap-0.5 content-caption" content={t_i18n(LIST_NOTES.entries)}>
              <Database className="h-3 w-3" aria-label={t_i18n("Entries")} />
              {evaluation.entry_count}
            </InfoLabel>
            <FamilyBadge family={evaluation.family} size="sm" />
          </div>
          {evaluation.last_triggered_at && (
            <InfoLabel className="relative z-10 ml-auto content-caption text-default-secondary" content={t_i18n(LIST_NOTES.lastLaunched)}>
              {formatTimeAgo(intl, evaluation.last_triggered_at)}
            </InfoLabel>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// Cross-run search panel: filter runs by score, tags, baseline
// ---------------------------------------------------------------------------

function RunSearchPanel() {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const [minScore, setMinScore] = useState("");
  const [tagKey, setTagKey] = useState("");
  const [tagValue, setTagValue] = useState("");
  const [baselineOnly, setBaselineOnly] = useState(false);
  const fieldId = useId();
  // Defaults to a live headline: `overall_score` is null on every recent run, so
  // filtering on it returns nothing.
  const [scoreField, setScoreField] = useState<"quality_score" | "performance_score" | "score">("quality_score");

  const debouncedMinScore = useDebounce(minScore, 300);
  const debouncedTagKey = useDebounce(tagKey, 300);
  const debouncedTagValue = useDebounce(tagValue, 300);

  const filters: RunSearchFilters = useMemo(() => {
    const f: RunSearchFilters = { limit: 50 };
    const score = parseFloat(debouncedMinScore);
    if (!Number.isNaN(score)) {
      f.scores = [{ field: scoreField, op: "gte", value: score }];
    }
    if (baselineOnly) f.baseline_only = true;
    const tags: Record<string, string> = {};
    if (debouncedTagKey.trim()) tags[debouncedTagKey.trim()] = debouncedTagValue.trim();
    if (Object.keys(tags).length > 0) f.tags = tags;
    return f;
  }, [debouncedMinScore, debouncedTagKey, debouncedTagValue, baselineOnly, scoreField]);

  const hasFilters =
    !!filters.scores || !!filters.tags || !!filters.baseline_only;
  const { data, isLoading } = useSearchRuns(filters, hasFilters);
  const runs = data?.runs || [];

  const clear = () => {
    setMinScore("");
    setTagKey("");
    setTagValue("");
    setBaselineOnly(false);
  };

  return (
    <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
      <CardContent className="p-4 space-y-4">
        <div className="flex flex-wrap items-end gap-4">
          <div className="space-y-1.5">
            <div className="flex items-center gap-1">
              <Label htmlFor={`${fieldId}-min-score`}>{t_i18n("Min score")}</Label>
              <InfoTip label={t_i18n("What Min score keeps")} content={t_i18n(LIST_NOTES.minScore)} />
            </div>
            <div className="flex items-center gap-1.5">
              <Input aria-label={t_i18n("Min score")}
                id={`${fieldId}-min-score`}
                type="number"
                value={minScore}
                onChange={(e) => setMinScore(e.target.value)}
                placeholder={t_i18n("e.g. 70")}
                className="w-24"
              />
              {/* The three headlines are decoupled, so a threshold must name
                  which one it applies to. */}
              <Select value={scoreField} onValueChange={(v) => setScoreField(v as typeof scoreField)}>
                <SelectTrigger className="w-[168px]" aria-label={t_i18n("Score")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="quality_score">{t_i18n(HEADLINE_LABELS.quality)}</SelectItem>
                  <SelectItem value="performance_score">{t_i18n(HEADLINE_LABELS.performance)}</SelectItem>
                  <SelectItem value="score">{t_i18n(HEADLINE_LABELS.judge)}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center gap-1">
              <Label htmlFor={`${fieldId}-tag-key`}>{t_i18n("Tag key")}</Label>
              <InfoTip label={t_i18n("How tags match runs")} content={t_i18n(LIST_NOTES.tags)} />
            </div>
            <Input aria-label={t_i18n("Tag key")}
              id={`${fieldId}-tag-key`}
              value={tagKey}
              onChange={(e) => setTagKey(e.target.value)}
              placeholder={t_i18n("key")}
              className="w-32"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${fieldId}-tag-value`} className="block">{t_i18n("Tag value")}</Label>
            <Input aria-label={t_i18n("Tag value")}
              id={`${fieldId}-tag-value`}
              value={tagValue}
              onChange={(e) => setTagValue(e.target.value)}
              placeholder={t_i18n("value")}
              className="w-32"
            />
          </div>
          <div className="flex items-center gap-2 pb-2">
            <Star className={cn("h-3.5 w-3.5", baselineOnly ? "text-feedback-warning-primary fill-feedback-alert-primary" : "text-default-secondary")} />
            <span className="content-compact text-default-secondary">{t_i18n("Baselines only")}</span>
            <InfoTip
              label={t_i18n("What Baselines only keeps")}
              content={`${t_i18n(LIST_NOTES.baselinesOnly)} ${t_i18n(EVALUATION_TERMS.baselineAgent)}`}
            />
            <Switch aria-label={t_i18n("Baselines only")} checked={baselineOnly} onCheckedChange={setBaselineOnly} />
          </div>
          {hasFilters && (
            <FilterClearButton onClick={clear}>{t_i18n("Clear")}</FilterClearButton>
          )}
        </div>

        {!hasFilters ? (
          <p className="content-compact text-default-secondary">{t_i18n("Enter a filter above to search runs across all evaluations.")}</p>
        ) : isLoading ? (
          <p role="status" className="content-compact text-default-secondary flex items-center gap-1.5"><Spinner size="sm" tone="inherit" /> {t_i18n("Searching…")}</p>
        ) : runs.length === 0 ? (
          <p className="content-compact text-default-secondary">{t_i18n("No runs match these filters.")}</p>
        ) : (
          <div className="space-y-1.5">
            <div className="content-caption font-medium uppercase tracking-wider text-default-secondary">
              <InfoLabel content={t_i18n(LIST_NOTES.resultCap)}>{t_i18n("{count, plural, one {# run} other {# runs}}", { count: runs.length })}</InfoLabel>
            </div>
            {runs.map((run) => {
              const statusCfg = EVALUATION_STATUS_CONFIG[run.status];
              const isBaseline = !!run.is_baseline;
              const effTags = run.effective_tags || run.tags || {};
              return (
                // A row, not a button: the explained values inside must be tab stops of their own.
                // The model link is its keyboard entry; a click anywhere opens the run.
                <div
                  key={run.id}
                  onClick={() => navigate(`/evaluations/runs/${run.id}`)}
                  className="w-full flex items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 text-left hover:bg-elevation-highlight transition-colors cursor-pointer"
                >
                  {isBaseline && (
                    <InfoLabel className="inline-flex shrink-0" content={t_i18n(EVALUATION_TERMS.baselineRun)}>
                      <Star role="img" aria-label={t_i18n("Baseline")} className="h-3.5 w-3.5 text-feedback-warning-primary fill-feedback-alert-primary" />
                    </InfoLabel>
                  )}
                  <Link
                    to={`/evaluations/runs/${run.id}`}
                    onClick={(e) => e.stopPropagation()}
                    className="flex items-center gap-1 content-compact font-mono-plex text-default-secondary min-w-0 rounded-sm hover:text-default-primary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                  >
                    <Cpu className="h-3 w-3 shrink-0" />
                    <span className="truncate">{formatModelLabel(run.model_id, t_i18n)}</span>
                  </Link>
                  {statusCfg && (
                    <Badge variant="outline" className={cn("content-caption shrink-0", statusCfg.color)}>{t_i18n(statusCfg.label)}</Badge>
                  )}
                  <PlaceboArmTag run={run} />
                  <PlatformErrorBadge run={run} />
                  <span className="ml-auto flex items-center gap-3 content-compact shrink-0">
                    {/* The shared resolver keeps headlines consistent with the
                        other evaluation pages. Only measured ones show: an absent
                        headline is N/A, never a zero. */}
                    {headlines(run).map((h) =>
                      h.value === null ? null : (
                        // Named, then defined: the number alone says nothing of which score it is.
                        // The dot says which headline it is, the number's colour its band.
                        // A legacy speed is neither: it stays grey and unmarked, as on the other pages.
                        <InfoLabel
                          key={h.id}
                          content={`${t_i18n(h.label)}: ${headlineTip(h, t_i18n)}`}
                          className={cn(
                            "inline-flex items-center gap-1 font-mono-plex font-semibold",
                            h.legacy ? "text-default-secondary" : headlineColorClass(h.value),
                          )}
                        >
                          {!h.legacy && (
                            <span
                              aria-hidden
                              className="inline-block w-2 h-2 rounded-full"
                              style={{ backgroundColor: headlineDef(h.id).color }}
                            />
                          )}
                          {h.value.toFixed(1)}
                        </InfoLabel>
                      ),
                    )}
                    {Object.entries(effTags).filter(([k]) => k !== RUN_TAG_BASELINE_KEY).slice(0, 2).map(([k, v]) => (
                      <InfoLabel key={k} className="inline-flex" content={t_i18n(EVALUATION_TERMS.runTags)}>
                        <Badge variant="outline" className="content-caption gap-1 font-mono-plex">
                          <Tag className="h-2.5 w-2.5" />{k}: {v}
                        </Badge>
                      </InfoLabel>
                    ))}
                    <span className="content-caption text-default-secondary">{formatTimeAgo(intl, run.created_at)}</span>
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function Evaluations() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: evaluations, isLoading } = useEvaluations();
  const { user } = useAuth();
  const { toast } = useToast();
  const importEvaluation = useImportEvaluation();
  const importInputRef = useRef<HTMLInputElement>(null);
  const [showRunSearch, setShowRunSearch] = useState(false);
  // Evaluations picked for a comparison (#12282); kept across searches, dropped when deleted.
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const selectedEvaluations = useMemo(
    () => (evaluations ?? []).filter((e) => selectedIds.includes(e.id)),
    [evaluations, selectedIds],
  );
  const eligibility = comparisonEligibility(selectedEvaluations);
  const toggleSelected = (id: string) =>
    setSelectedIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  const { search, setSearch, allItems: allEvaluations, filtered } =
    // No visibility control on this page yet: every row it lists stays shown (dev:visibility-filters-consistency).
    useEntityFilter({ items: evaluations, withVisibility: false });

  const handleImportFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;

    try {
      const created = await importEvaluation.mutateAsync(file);
      // An import arrives with automatic runs paused: say so when it has a
      // schedule to pause.
      toast({
        title: t_i18n("Evaluation imported"),
        description: hasAutomaticRuns(created) && !created.enabled
          ? t_i18n("\"{name}\" created with linked resources. Its automatic runs are paused: turn them on in its configuration.", { name: created.name })
          : t_i18n("\"{name}\" created with linked resources.", { name: created.name }),
        variant: "success",
      });
      // What the package held that this instance cannot use (a placebo
      // target naming a skill of the exporting instance, a model no provider
      // of yours offers), from the server.
      for (const warning of created.warnings ?? []) {
        toast({ title: t_i18n("Evaluation imported"), description: warning, variant: "info" });
      }
      navigate(`/evaluations/${created.id}`);
    } catch (error) {
      toast({
        title: t_i18n("Import failed"),
        description: error instanceof Error ? error.message : t_i18n("Invalid evaluation bundle"),
        variant: "error",
      });
    }
  };

  return (
    <div className="space-y-6">
      <input
        ref={importInputRef}
        type="file"
        accept="application/zip,.zip"
        className="hidden"
        onChange={handleImportFile}
      />
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Evaluations")}</h1>
          <p className="mt-2 text-default-secondary">
            {t_i18n("Evaluate agent performance across models and datasets.")}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            className="gap-1.5"
            onClick={() => setShowRunSearch((v) => !v)}
          >
            <Search className="h-4 w-4" /> {t_i18n("Search Runs")}
          </Button>
          <Button
            variant="outline"
            className="gap-1.5"
            onClick={() => importInputRef.current?.click()}
            disabled={importEvaluation.isPending}
            aria-busy={importEvaluation.isPending || undefined}
          >
            {importEvaluation.isPending ? (
              <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} />
            ) : (
              <Upload className="h-4 w-4" />
            )}
            {t_i18n("Import Evaluation")}
          </Button>
          <Button variant="outline" className="gap-1.5" onClick={() => navigate("/evaluations/datasets")}>
            <Database className="h-4 w-4" /> {t_i18n("Datasets")}
          </Button>
          <Button className="gap-1.5" onClick={() => navigate("/evaluations/new")}>
            <Plus className="h-4 w-4" /> {t_i18n("New Evaluation")}
          </Button>
        </div>
      </div>

      {showRunSearch && <RunSearchPanel />}

      {isLoading ? (
        <CardGridSkeleton count={cardSkeletonCount()} label={t_i18n("Loading")} />
      ) : (
        <>
          {(allEvaluations ?? []).length > 0 && (
            <div className="flex flex-wrap items-center gap-3">
              <SearchInput toolbar placeholder={t_i18n("Search evaluations...")} value={search} onChange={setSearch} />
              {selectedEvaluations.length > 0 && (
                <div className="flex items-center gap-2" role="group" aria-label={t_i18n("Selected evaluations")}>
                  <span className="content-compact text-default-secondary tabular-nums">
                    {t_i18n("{count, plural, one {# selected} other {# selected}}", { count: selectedEvaluations.length })}
                  </span>
                  <Button variant="ghost" size="sm" onClick={() => setSelectedIds([])}>
                    {t_i18n("Clear selection")}
                  </Button>
                  <Tip content={eligibility.reason ? t_i18n(eligibility.reason) : t_i18n("Compare the selected evaluations on the entries their datasets share.")} wrap="auto">
                    <Button
                      size="sm"
                      className="gap-1.5"
                      disabled={!eligibility.canCompare}
                      onClick={() => navigate(`/evaluations/compare?ids=${selectedEvaluations.map((e) => e.id).join(",")}`)}
                    >
                      <Icon name="columns-3" size={16} /> {t_i18n("Compare")}
                    </Button>
                  </Tip>
                </div>
              )}
              <span className="ml-auto rounded-sm bg-elevation-hover px-2.5 py-1 content-compact font-medium tabular-nums text-default-secondary">{allEvaluations.length}</span>
            </div>
          )}

          {allEvaluations.length === 0 ? (
            <EmptyState
              icon={FlaskConical}
              title="No evaluations yet"
              requiresAi
              description="Create your first evaluation to test agent performance across different models and datasets."
              actionLabel="New Evaluation"
              onAction={() => navigate("/evaluations/new")}
            />
          ) : filtered.length > 0 ? (
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {filtered.map((evaluation) => (
                <EvaluationCard
                  key={evaluation.id}
                  evaluation={evaluation}
                  userId={user?.id}
                  selected={selectedIds.includes(evaluation.id)}
                  onToggleSelect={() => toggleSelected(evaluation.id)}
                />
              ))}
            </div>
          ) : (
            <p className="content-base text-default-secondary py-8 text-center">{t_i18n("No evaluations matching your search")}</p>
          )}
        </>
      )}
    </div>
  );
}
