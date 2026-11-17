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

import { useState, useMemo, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { X, Database, Plus, ChevronDown, ChevronRight, FileText, Upload, Trash2, Paperclip, Lock } from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { cn } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { BackButton } from "@/components/ui/back-button";
import { Badge } from "@/components/ui/badge";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { NeedsFamilyBadge } from "@/components/ui/needs-family-badge";
import { Tip } from "@/components/ui/tooltip";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { useToast } from "@/components/ui/toast";
import { useCreateEvaluation, useEvaluationFamilies, useFamilyMetricCatalog } from "@/hooks/api/useEvaluationApi";
import { useDataset, useCreateDataset, useAddDatasetEntry, useDeleteDatasetEntry, useUploadDatasetEntryFile, useRemoveDatasetEntryFile } from "@/hooks/api/useDatasetApi";
import type { CreateEvaluationRequest, EvaluationFamily, ExtractorSpec } from "@/types";
import { useAvailableModels } from "@/hooks/api/useSettingsApi";
import { EVALUATION_SUB_SCORES } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { EvaluationSettingsCard } from "@/components/evaluation/evaluation-settings-card";
import { EvaluationScoringCard } from "@/components/evaluation/evaluation-scoring-card";
import { EvaluationAgentPickerCard } from "@/components/evaluation/evaluation-agent-picker-card";
import { EvaluationModelPickerCard } from "@/components/evaluation/evaluation-model-picker-card";
import { EvaluationDatasetLinkPicker } from "@/components/evaluation/evaluation-dataset-link-picker";
import { EvaluationExtractorCard } from "@/components/evaluation/evaluation-extractor-card";
import { OverallScoreComposition } from "@/components/evaluation/metric-selector";
import { creationExtras } from "@/components/evaluation/creation-payload";

/** A dataset linked on this page, as far as its row shows it. */
interface LinkedDataset {
  id: string;
  name: string;
  entry_count: number;
  is_read_only: boolean;
  needs_family: boolean;
}

/**
 * What the Datasets card's own concepts mean: the picker offers only the chosen
 * family's datasets and those with none (`useDatasets(search, family)`), and a dataset
 * made here takes the chosen family: one with none would refuse every launch
 * (`launch.py`, `check_dataset_families`). @i18n-keys
 */
const DATASET_NOTES = {
  datasets:
    "The test cases every run answers. Only datasets of the chosen output family, or with none yet, are offered; one with no family must get one before the evaluation can run.",
  newDatasetFamily:
    "The dataset is created with the output family chosen in Scoring, so this evaluation can run on it.",
  chooseFamilyFirst: "Choose an output family in Scoring first: the new dataset is created with it.",
} as const;

/** What attaching a file to an entry does (`evaluation_engine.py` loads the entry's files
 * for the agent). @i18n-keys */
const ENTRY_FILE_NOTE = {
  attach: "Attach a file to this entry: it is sent to the agent with the input prompt.",
} as const;

export default function NewEvaluation() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const createEvaluation = useCreateEvaluation();
  const aiOff = useAiActionDisabled();
  const { data: availableModels } = useAvailableModels();
  const { data: familyCatalog } = useEvaluationFamilies();
  const { toast } = useToast();
  // Output family and LLM Judge toggle. Declared early because the
  // dataset picker below filters by family.
  const [family, setFamily] = useState<EvaluationFamily | "">("");
  // The overall metric selection and each agent's output extraction, set here as the
  // Configuration tab sets them. `null` = this family's default metrics.
  const [qualitySelection, setQualitySelection] = useState<string[] | null>(null);
  const [performanceSelection, setPerformanceSelection] = useState<string[] | null>(null);
  const [agentExtractors, setAgentExtractors] = useState<Record<string, ExtractorSpec | null>>({});
  const { data: familyMetricCatalog } = useFamilyMetricCatalog(family || null);
  // Quality metrics belong to a family: another family's selection would name metrics
  // this one does not measure. The performance half is the same for every family.
  // So do extractors: each family scores its own output kinds, so one made for the
  // previous family would be refused on save.
  const changeFamily = (next: EvaluationFamily | "") => {
    if (next !== family) {
      setQualitySelection(null);
      setAgentExtractors({});
    }
    setFamily(next);
  };
  const [selectedDatasetIds, setSelectedDatasetIds] = useState<string[]>([]);
  // `needs_family` is kept so a linked dataset still says it blocks the launch.
  const [selectedDatasetCache, setSelectedDatasetCache] = useState<Map<string, LinkedDataset>>(new Map());
  const selectedDatasets = useMemo(
    () => selectedDatasetIds.map((id) => selectedDatasetCache.get(id)).filter(Boolean) as LinkedDataset[],
    [selectedDatasetIds, selectedDatasetCache],
  );

  const [showNewDatasetForm, setShowNewDatasetForm] = useState(false);
  const [newDsName, setNewDsName] = useState("");
  const [newDsDescription, setNewDsDescription] = useState("");
  const [newDsInstructions, setNewDsInstructions] = useState("");
  const [expandedDatasetId, setExpandedDatasetId] = useState<string | null>(null);
  const [showAddEntry, setShowAddEntry] = useState(false);
  const [entryDescription, setEntryDescription] = useState("");
  const [entryInputPrompt, setEntryInputPrompt] = useState("");
  const [entryExpectedAnswer, setEntryExpectedAnswer] = useState("");
  const [entryEvalPrompt, setEntryEvalPrompt] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [pendingUploadEntryId, setPendingUploadEntryId] = useState<string | null>(null);
  const newEntryFileInputRef = useRef<HTMLInputElement>(null);
  const [newEntryFile, setNewEntryFile] = useState<File | null>(null);

  const createDataset = useCreateDataset();
  const addEntry = useAddDatasetEntry();
  const deleteEntry = useDeleteDatasetEntry();
  const uploadFile = useUploadDatasetEntryFile();
  const removeFile = useRemoveDatasetEntryFile();
  const { data: expandedDataset, isLoading: expandedLoading } = useDataset(expandedDatasetId ?? "");

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selectedAgentIds, setSelectedAgentIds] = useState<string[]>([]);
  const [selectedAgentCache, setSelectedAgentCache] = useState<Map<string, { id: string; name: string; slug: string | null; icon: string }>>(new Map());

  const selectedAgents = useMemo(
    () => selectedAgentIds.map((id) => selectedAgentCache.get(id)).filter(Boolean) as { id: string; name: string; slug: string | null; icon: string }[],
    [selectedAgentIds, selectedAgentCache],
  );
  const [useAgentDefaultModel, setUseAgentDefaultModel] = useState(true);
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([]);
  const canDisableDefaultModel = selectedModelIds.length > 0;
  const [executionMode, setExecutionMode] = useState<string>("full_loop");
  const [runMode, setRunMode] = useState<"parallel" | "sequential">("parallel");
  const [timeoutSeconds, setTimeoutSeconds] = useState(3600);
  const [judgeMaxTokens, setJudgeMaxTokens] = useState(8192);
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [schedule, setSchedule] = useState<string>("*/5 * * * *");
  const [performanceTolerance, setPerformanceTolerance] = useState(5);
  const [penaltyFactor, setPenaltyFactor] = useState(0.5);
  const [judgeEnabled, setJudgeEnabled] = useState(true);
  const [reuseDefault, setReuseDefault] = useState<boolean>(true);
  // Disabled by default: the guard's checks cost model calls on every entry.
  const [applyPromptGuards, setApplyPromptGuards] = useState<boolean>(false);
  // Disabled by default: nobody can approve during an evaluation.
  const [requireToolApproval, setRequireToolApproval] = useState<boolean>(false);
  const [subScoreWeights, setSubScoreWeights] = useState<Record<string, number>>(
    () => Object.fromEntries(EVALUATION_SUB_SCORES.map((s) => [s.key, 1]))
  );
  const subScoreTotal = useMemo(
    () => Object.values(subScoreWeights).reduce((a, b) => a + b, 0) || 1,
    [subScoreWeights],
  );

  const availableModelsToAdd = useMemo(
    () => (availableModels ?? []).filter((m) => !selectedModelIds.includes(m.model)),
    [availableModels, selectedModelIds],
  );

  const selectedModels = useMemo(
    () => selectedModelIds.map((mid) => {
      const av = (availableModels ?? []).find((m) => m.model === mid);
      return { model: mid, provider: av?.provider ?? "" };
    }),
    [availableModels, selectedModelIds],
  );

  const handleCreateDataset = async () => {
    if (!newDsName.trim() || !family) return;
    try {
      const ds = await createDataset.mutateAsync({
        name: newDsName.trim(),
        description: newDsDescription.trim() || null,
        evaluation_instructions: newDsInstructions.trim() || null,
        family,
      });
      setSelectedDatasetIds((prev) => [...prev, ds.id]);
      setSelectedDatasetCache((prev) => new Map(prev).set(ds.id, { id: ds.id, name: ds.name, entry_count: 0, is_read_only: !!ds.is_read_only, needs_family: !!ds.needs_family }));
      setExpandedDatasetId(ds.id);
      setShowNewDatasetForm(false);
      setNewDsName("");
      setNewDsDescription("");
      setNewDsInstructions("");
    } catch (error: unknown) {
      toast({ variant: "error", title: t_i18n("Failed to create dataset"), description: (error as Error).message });
    }
  };

  const handleAddEntry = async () => {
    if (!expandedDatasetId || !entryInputPrompt.trim() || !entryExpectedAnswer.trim()) return;
    if (expandedDataset?.is_read_only) {
      toast({ variant: "error", title: t_i18n("This dataset is read-only") });
      return;
    }
    try {
      const created = await addEntry.mutateAsync({
        datasetId: expandedDatasetId,
        description: entryDescription.trim() || null,
        input_prompt: entryInputPrompt.trim(),
        expected_answer: entryExpectedAnswer.trim(),
        evaluation_prompt: entryEvalPrompt.trim() || null,
      });
      if (newEntryFile) {
        await uploadFile.mutateAsync({ entryId: created.id, file: newEntryFile });
        setNewEntryFile(null);
      }
      setEntryDescription("");
      setEntryInputPrompt("");
      setEntryExpectedAnswer("");
      setEntryEvalPrompt("");
      setShowAddEntry(false);
      setSelectedDatasetCache((prev) => {
        const cached = prev.get(expandedDatasetId);
        if (!cached) return prev;
        return new Map(prev).set(expandedDatasetId, { ...cached, entry_count: cached.entry_count + 1 });
      });
    } catch (error: unknown) {
      toast({ variant: "error", title: t_i18n("Failed to add entry"), description: (error as Error).message });
    }
  };

  const handleDeleteEntry = async (entryId: string) => {
    if (expandedDataset?.is_read_only) {
      toast({ variant: "error", title: t_i18n("This dataset is read-only") });
      return;
    }
    try {
      await deleteEntry.mutateAsync(entryId);
      if (expandedDatasetId) {
        setSelectedDatasetCache((prev) => {
          const cached = prev.get(expandedDatasetId);
          if (!cached) return prev;
          return new Map(prev).set(expandedDatasetId, { ...cached, entry_count: Math.max(0, cached.entry_count - 1) });
        });
      }
    } catch (error: unknown) {
      toast({ variant: "error", title: t_i18n("Failed to delete entry"), description: (error as Error).message });
    }
  };

  const handleSubmit = () => {
    if (!name.trim()) {
      toast({ variant: "error", title: t_i18n("Name is required") });
      return;
    }

    if (selectedAgentIds.length === 0) {
      toast({ variant: "error", title: t_i18n("At least one agent must be selected") });
      return;
    }

    if (!useAgentDefaultModel && selectedModelIds.length === 0) {
      toast({ variant: "error", title: t_i18n("Enable agent default model or add at least one model") });
      return;
    }

    if (!family) {
      toast({ variant: "error", title: t_i18n("Select an output family") });
      return;
    }

    const request: CreateEvaluationRequest = {
      name: name.trim(),
      description: description.trim() || null,
      family,
      agent_ids: selectedAgentIds,
      use_agent_default_model: useAgentDefaultModel,
      model_ids: selectedModelIds,
      execution_mode: executionMode,
      run_mode: runMode,
      timeout_seconds: timeoutSeconds,
      judge_max_tokens: judgeMaxTokens,
      performance_tolerance: performanceTolerance,
      quality_source: "judge",
      penalty_factor: penaltyFactor,
      // Judge on → ["judge"]; off → explicit [] so the gate reads it as disabled.
      scorer_selection: judgeEnabled ? ["judge"] : [],
      reuse_default: reuseDefault ? "on" : "off",
      apply_prompt_guards: applyPromptGuards,
      require_tool_approval: requireToolApproval,
      sub_score_weights: Object.fromEntries(
        Object.entries(subScoreWeights).map(([k, v]) => [k, v / subScoreTotal])
      ),
      trigger_config: {
        schedule: scheduleEnabled && schedule.trim() ? schedule.trim() : null,
        // Off: on agent change starts no run yet, so it is not offered.
        on_agent_change: false,
      },
      dataset_ids: selectedDatasetIds.length > 0 ? selectedDatasetIds : undefined,
      ...creationExtras({ agentIds: selectedAgentIds, agentExtractors, qualitySelection, performanceSelection }),
    };

    createEvaluation.mutateAsync(request).then((data) => {
      toast({ variant: "success", title: t_i18n("Evaluation created successfully") });
      navigate(`/evaluations/${data.id}`);
    }).catch((error: Error) => {
      toast({ variant: "error", title: t_i18n("Failed to create evaluation"), description: error.message });
    });
  };

  return (
    <div className="space-y-6">
      <BackButton to="/evaluations" label="Back to Evaluations" className="mb-4" />

      <div>
        <h1 className="text-3xl font-sans-geologica font-bold">{t_i18n("Create Evaluation")}</h1>
        <p className="mt-2 text-default-secondary">{t_i18n("Set up a new evaluation to test agent performance.")}</p>
      </div>

      {/* Settings */}
      <EvaluationSettingsCard
        editing
        name={name}
        onNameChange={setName}
        description={description}
        onDescriptionChange={setDescription}
        executionMode={executionMode}
        onExecutionModeChange={setExecutionMode}
        runMode={runMode}
        onRunModeChange={setRunMode}
        timeoutSeconds={timeoutSeconds}
        onTimeoutSecondsChange={setTimeoutSeconds}
        scheduleEnabled={scheduleEnabled}
        onScheduleEnabledChange={setScheduleEnabled}
        schedule={schedule}
        onScheduleChange={setSchedule}
        reuseDefault={reuseDefault}
        onReuseDefaultChange={setReuseDefault}
        applyPromptGuards={applyPromptGuards}
        requireToolApproval={requireToolApproval}
        agentIds={selectedAgentIds}
        onRequireToolApprovalChange={setRequireToolApproval}
        onApplyPromptGuardsChange={setApplyPromptGuards}
        judgeMaxTokens={judgeMaxTokens}
        onJudgeMaxTokensChange={setJudgeMaxTokens}
      />

      {/* Scoring */}
      <EvaluationScoringCard
        editing
        familyCatalog={familyCatalog}
        family={family}
        onFamilyChange={changeFamily}
        judgeEnabled={judgeEnabled}
        onJudgeEnabledChange={setJudgeEnabled}
        performanceTolerance={performanceTolerance}
        onPerformanceToleranceChange={setPerformanceTolerance}
        penaltyFactor={penaltyFactor}
        onPenaltyFactorChange={setPenaltyFactor}
        subScoreWeights={subScoreWeights}
        onSubScoreWeightChange={(key, v) => setSubScoreWeights((prev) => ({ ...prev, [key]: v }))}
        subScoreTotal={subScoreTotal}
        aggregationExtra={
          family ? (
            <OverallScoreComposition
              catalog={familyMetricCatalog ?? []}
              quality={qualitySelection}
              performance={performanceSelection}
              onQualityChange={setQualitySelection}
              onPerformanceChange={setPerformanceSelection}
            />
          ) : undefined
        }
      />

      {/* Agents Selection */}
      <EvaluationAgentPickerCard
        agents={selectedAgents}
        boundIds={selectedAgentIds}
        canManage
        editing
        onAdd={(agent) => {
          setSelectedAgentIds((prev) => [...prev, agent.id]);
          setSelectedAgentCache((prev) => new Map(prev).set(agent.id, agent));
        }}
        onRemove={(agentId) => {
          setSelectedAgentIds((prev) => prev.filter((id) => id !== agentId));
          // A removed agent takes its extractor with it, as on the Configuration tab.
          setAgentExtractors(({ [agentId]: _removed, ...rest }) => rest);
        }}
      />

      {/* Per-agent output extraction, once the family it is checked against is chosen */}
      {family && (
        <EvaluationExtractorCard
          target={{ family }}
          outputKinds={familyCatalog?.find((entry) => entry.id === family)?.extractor_output_kinds}
          agents={selectedAgents}
          extractors={agentExtractors}
          canManage
          onChange={(patch) => setAgentExtractors((current) => ({ ...current, ...patch }))}
        />
      )}

      {/* Models Selection */}
      <EvaluationModelPickerCard
        editing
        useAgentDefaultModel={useAgentDefaultModel}
        onUseAgentDefaultModelChange={setUseAgentDefaultModel}
        canDisableDefaultModel={canDisableDefaultModel}
        selectedModels={selectedModels}
        availableModelsToAdd={availableModelsToAdd}
        onAddModel={(model) => setSelectedModelIds((prev) => [...prev, model])}
        onRemoveModel={(model) => {
          setSelectedModelIds((prev) => {
            const next = prev.filter((id) => id !== model);
            if (next.length === 0) setUseAgentDefaultModel(true);
            return next;
          });
        }}
        evaluationUseAgentDefaultModel={useAgentDefaultModel}
      />

      {/* Datasets */}
      <Card className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
        <CardHeader className="pb-2">
          <div className="flex items-center gap-2">
            <Database className="h-3.5 w-3.5 text-default-secondary" />
            <CardTitle className="flex items-center gap-1 text-base font-medium">
              {t_i18n("Datasets")}
              <InfoTip content={t_i18n(DATASET_NOTES.datasets)} label={t_i18n("Which datasets can be linked")} />
            </CardTitle>
            <Badge variant="outline" className="content-caption">{t_i18n("{count} selected", { count: selectedDatasets.length })}</Badge>
            <div className="ml-auto">
              <Button
                type="button"
                variant="secondary"
                data-testid="new-evaluation.new-dataset"
                className={cn(showNewDatasetForm && "border-accent/30 bg-accent/10 text-accent")}
                onClick={() => setShowNewDatasetForm((v) => !v)}
              >
                <Plus className="h-3.5 w-3.5 mr-1" />
                {t_i18n("New Dataset")}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {/* New Dataset creation form */}
          {showNewDatasetForm && (
            <div className="rounded-sm border border-accent/20 bg-accent/5 p-4 space-y-3">
              <p className="flex items-center gap-1 content-compact font-medium text-accent">
                {t_i18n("Create New Dataset")}
                <InfoTip content={t_i18n(DATASET_NOTES.newDatasetFamily)} label={t_i18n("Which output family the new dataset gets")} />
              </p>
              {!family && (
                <p className="content-compact text-default-secondary">{t_i18n(DATASET_NOTES.chooseFamilyFirst)}</p>
              )}
              <div className="space-y-1.5">
                <Label id="new-dataset-name-label" htmlFor="new-dataset-name">{t_i18n("Name *")}</Label>
                <Input id="new-dataset-name" aria-labelledby="new-dataset-name-label"
                  value={newDsName}
                  onChange={(e) => setNewDsName(e.target.value)}
                  placeholder={t_i18n("Dataset name")}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="new-dataset-description">{t_i18n("Description")}</Label>
                <textarea
                  id="new-dataset-description"
                  className={`w-full min-h-[60px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y ${FIELD_FOCUS_RING}`}
                  value={newDsDescription}
                  onChange={(e) => setNewDsDescription(e.target.value)}
                  placeholder={t_i18n("Optional description")}
                />
              </div>
              <div className="space-y-1.5">
                <div className="flex items-center gap-1">
                  <Label htmlFor="new-dataset-judge-instructions">{t_i18n("Default Judge Instructions")}</Label>
                  <InfoTip content={t_i18n(EVALUATION_TERMS.judgeInstructionsDataset)} label={t_i18n("When the dataset's judge instructions apply")} />
                </div>
                <textarea
                  id="new-dataset-judge-instructions"
                  className={`w-full min-h-[60px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y ${FIELD_FOCUS_RING}`}
                  value={newDsInstructions}
                  onChange={(e) => setNewDsInstructions(e.target.value)}
                  placeholder={t_i18n("Instructions for the LLM judge when evaluating entries from this dataset")}
                />
              </div>
              <div className="flex justify-end gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => { setShowNewDatasetForm(false); setNewDsName(""); setNewDsDescription(""); setNewDsInstructions(""); }}
                >
                  {t_i18n("Cancel")}
                </Button>
                <Button
                  type="button"
                  onClick={handleCreateDataset}
                  disabled={!newDsName.trim() || !family || createDataset.isPending || aiOff}
                  aria-busy={createDataset.isPending || undefined}
                >
                  {createDataset.isPending && <Spinner size="sm" tone="inherit" />}
                  {!createDataset.isPending && <Plus className="h-3 w-3" />}
                  {t_i18n("Create")}
                </Button>
              </div>
            </div>
          )}

          <p className="content-compact text-default-secondary">
            {t_i18n("Link existing datasets with test cases. You can also add datasets after creation from the evaluation's Dataset tab.")}
          </p>

          {/* Existing dataset search picker */}
          <EvaluationDatasetLinkPicker
            family={family || undefined}
            linkedIds={selectedDatasetIds}
            onLink={(ds) => {
              setSelectedDatasetIds((prev) => [...prev, ds.id]);
              setSelectedDatasetCache((prev) => new Map(prev).set(ds.id, { id: ds.id, name: ds.name, entry_count: ds.entry_count, is_read_only: !!ds.is_read_only, needs_family: !!ds.needs_family }));
            }}
          />

          {/* Hidden file input for entry uploads */}
          <input
            type="file"
            ref={fileInputRef}
            className="hidden"
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (!file || !pendingUploadEntryId) return;
              e.target.value = "";
              try {
                await uploadFile.mutateAsync({ entryId: pendingUploadEntryId, file });
              } catch (error: unknown) {
                toast({ variant: "error", title: t_i18n("Upload failed"), description: (error as Error).message });
              } finally {
                setPendingUploadEntryId(null);
              }
            }}
          />

          {/* Selected datasets list */}
          {selectedDatasets.length === 0 ? (
            <div className="rounded-sm border border-dashed border-elevation-subtle-soft p-6 text-center">
              <Database className="h-6 w-6 text-default-secondary mx-auto mb-2" />
              <p className="content-compact text-default-secondary">{t_i18n("No datasets selected. Search and link datasets above or create a new one.")}</p>
            </div>
          ) : (
            <div className="space-y-1.5">
              {selectedDatasets.map((ds) => {
                const isExpanded = expandedDatasetId === ds.id;
                const expandedReadOnly = isExpanded && expandedDataset ? expandedDataset.is_read_only : ds.is_read_only;

                return (
                  <div key={ds.id} className="rounded-sm border border-elevation-subtle-soft overflow-hidden">
                  {/* Dataset header row */}
                  <div className="flex items-center gap-3 bg-elevation-default px-4 py-2.5 hover:bg-elevation-hover transition-colors">
                    <Database className="h-4 w-4 text-accent shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="content-base font-medium truncate">{ds.name}</p>
                    </div>
                    {/* The badge explains itself on hover and focus. */}
                    {ds.needs_family && <NeedsFamilyBadge size="sm" />}
                    {expandedReadOnly && (
                      <InfoLabel content={t_i18n(EVALUATION_TERMS.datasetReadOnly)}>
                        <Badge variant="outline" className="text-[9px]">
                          <Lock className="h-2.5 w-2.5 mr-1" /> {t_i18n("Read-only")}
                        </Badge>
                      </InfoLabel>
                    )}
                    <span className="content-caption text-default-secondary shrink-0">
                      {t_i18n("{count, plural, one {# entry} other {# entries}}", { count: isExpanded && expandedDataset ? expandedDataset.entries.length : ds.entry_count })}
                    </span>
                    <Tip content={isExpanded ? t_i18n("Hide entries") : t_i18n("Show entries")}>
                      <button
                        type="button"
                        aria-label={isExpanded ? t_i18n("Hide entries") : t_i18n("Show entries")}
                        aria-expanded={isExpanded}
                        className="h-7 w-7 flex items-center justify-center rounded-sm text-default-secondary hover:text-default-primary transition-all"
                        onClick={() => { setExpandedDatasetId(expandedDatasetId === ds.id ? null : ds.id); setShowAddEntry(false); }}
                      >
                        {isExpanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                      </button>
                    </Tip>
                    <Tip content={t_i18n("Remove dataset")}>
                      <button aria-label={t_i18n("Remove dataset")}
                        type="button"
                        className="h-7 w-7 flex items-center justify-center rounded-sm text-default-secondary hover:text-feedback-error-primary transition-all"
                        onClick={() => { setSelectedDatasetIds((prev) => prev.filter((id) => id !== ds.id)); if (expandedDatasetId === ds.id) setExpandedDatasetId(null); }}
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </Tip>
                  </div>

                  {/* Inline entry management panel */}
                  {isExpanded && (
                    <div className="border-t border-elevation-subtle-soft bg-elevation-hover p-3 space-y-2">
                      {expandedLoading ? (
                        <div className="flex items-center gap-2 content-compact text-default-secondary py-2">
                          <Spinner size="sm" tone="inherit" />
                          {t_i18n("Loading entries...")}
                        </div>
                      ) : (
                        <>
                          {/* Entries list */}
                          {expandedDataset && expandedDataset.entries.length > 0 ? (
                            <div className="space-y-1.5">
                              {expandedDataset.entries.map((entry, idx) => (
                                <div key={entry.id} className="rounded-sm border border-elevation-subtle-soft bg-elevation-default p-2.5">
                                  <div className="flex items-start gap-2">
                                    <span className="content-caption text-default-secondary font-mono-plex w-5 shrink-0 mt-0.5">#{idx + 1}</span>
                                    <div className="min-w-0 flex-1 space-y-1">
                                      {entry.description && (
                                        <p className="content-compact text-default-secondary italic truncate">{entry.description}</p>
                                      )}
                                      <p className="content-compact font-medium truncate">
                                        {entry.input_prompt.length > 90 ? entry.input_prompt.slice(0, 90) + "…" : entry.input_prompt}
                                      </p>
                                      <p className="content-compact text-default-secondary truncate">
                                        <InfoLabel content={t_i18n("Expected Answer")}>→</InfoLabel>{" "}{entry.expected_answer.length > 70 ? entry.expected_answer.slice(0, 70) + "…" : entry.expected_answer}
                                      </p>
                                      {entry.files && entry.files.length > 0 && (
                                        <div className="flex flex-wrap gap-1 pt-0.5">
                                          {entry.files.map((f) => (
                                            <span key={f.id} className="inline-flex items-center gap-1 content-caption bg-elevation-hover rounded-sm px-1.5 py-0.5">
                                              <FileText className="h-2.5 w-2.5 shrink-0" />
                                              <span className="max-w-[120px] truncate">{f.filename}</span>
                                              {!expandedReadOnly && (
                                                <Tip content={t_i18n("Remove file")}>
                                                  <button aria-label={t_i18n("Remove file")}
                                                    type="button"
                                                    className="hover:text-feedback-error-primary transition-colors"
                                                    onClick={() => removeFile.mutate({ entryId: entry.id, fileId: f.id })}
                                                  >
                                                    <X className="h-2 w-2" />
                                                  </button>
                                                </Tip>
                                              )}
                                            </span>
                                          ))}
                                        </div>
                                      )}
                                    </div>
                                    {!expandedReadOnly && (
                                      <div className="flex items-center gap-0.5 shrink-0">
                                        <Tip content={t_i18n(ENTRY_FILE_NOTE.attach)}>
                                          <button
                                            type="button"
                                            aria-label={t_i18n("Attach a file to this entry")}
                                            aria-busy={(uploadFile.isPending && pendingUploadEntryId === entry.id) || undefined}
                                            className="h-6 w-6 flex items-center justify-center rounded-sm text-default-secondary hover:text-accent transition-colors"
                                            onClick={() => { setPendingUploadEntryId(entry.id); fileInputRef.current?.click(); }}
                                          >
                                            {uploadFile.isPending && pendingUploadEntryId === entry.id ? (
                                              <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} />
                                            ) : (
                                              <Upload className="h-3 w-3" />
                                            )}
                                          </button>
                                        </Tip>
                                        <Tip content={t_i18n("Delete entry")} wrap="auto">
                                          <button aria-label={t_i18n("Delete entry")}
                                            type="button"
                                            className="h-6 w-6 flex items-center justify-center rounded-sm text-default-secondary hover:text-feedback-error-primary transition-colors"
                                            onClick={() => handleDeleteEntry(entry.id)}
                                            disabled={deleteEntry.isPending || aiOff}
                                          >
                                            <Trash2 className="h-3 w-3" />
                                          </button>
                                        </Tip>
                                      </div>
                                    )}
                                  </div>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <p className="content-compact text-default-secondary text-center py-2">
                              {expandedReadOnly ? t_i18n("No entries yet. This dataset is read-only.") : t_i18n("No entries yet. Add your first entry below.")}
                            </p>
                          )}

                          {/* Add Entry form / button */}
                          {!expandedReadOnly && showAddEntry ? (
                            <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 space-y-2.5">
                              <p className="content-compact font-medium">{t_i18n("New Entry")}</p>
                              <div className="space-y-1">
                                <Label id="new-entry-description-label" htmlFor="new-entry-description">{t_i18n("Description")}</Label>
                                <Input id="new-entry-description" aria-labelledby="new-entry-description-label"
                                  value={entryDescription}
                                  onChange={(e) => setEntryDescription(e.target.value)}
                                  placeholder={t_i18n("Optional description")}
                                />
                              </div>
                              <div className="space-y-1">
                                <Label htmlFor="new-entry-input-prompt">{t_i18n("Input Prompt *")}</Label>
                                <textarea
                                  id="new-entry-input-prompt"
                                  className={`w-full min-h-[64px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y ${FIELD_FOCUS_RING}`}
                                  value={entryInputPrompt}
                                  onChange={(e) => setEntryInputPrompt(e.target.value)}
                                  placeholder={t_i18n("The prompt to send to the agent")}
                                />
                              </div>
                              <div>
                                <input
                                  ref={newEntryFileInputRef}
                                  type="file"
                                  className="hidden"
                                  onChange={(e) => { const f = e.target.files?.[0]; if (f) setNewEntryFile(f); e.target.value = ""; }}
                                />
                                {newEntryFile ? (
                                  <div className="flex items-center gap-1.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2 py-1">
                                    <FileText className="h-3 w-3 text-accent shrink-0" />
                                    <span className="content-compact text-default-primary truncate flex-1">{newEntryFile.name}</span>
                                    <Tip content={t_i18n("Remove file")}>
                                      <button aria-label={t_i18n("Remove file")} type="button" onClick={() => setNewEntryFile(null)} className="text-default-secondary hover:text-default-primary shrink-0">
                                        <X className="h-3 w-3" />
                                      </button>
                                    </Tip>
                                  </div>
                                ) : (
                                  <Tip content={t_i18n(ENTRY_FILE_NOTE.attach)}>
                                    <Button
                                      type="button"
                                      variant="secondary"
                                      data-testid="new-evaluation.attach-file"
                                      onClick={() => newEntryFileInputRef.current?.click()}
                                    >
                                      <Paperclip className="h-3.5 w-3.5 mr-1" />
                                      {t_i18n("Attach File")}
                                    </Button>
                                  </Tip>
                                )}
                              </div>
                              <div className="space-y-1">
                                <div className="flex items-center gap-1">
                                  <Label htmlFor="new-entry-expected-answer">{t_i18n("Expected Answer *")}</Label>
                                  <InfoTip content={t_i18n(EVALUATION_TERMS.expectedAnswer)} label={t_i18n("What the expected answer is")} />
                                </div>
                                <textarea
                                  id="new-entry-expected-answer"
                                  className={`w-full min-h-[64px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y ${FIELD_FOCUS_RING}`}
                                  value={entryExpectedAnswer}
                                  onChange={(e) => setEntryExpectedAnswer(e.target.value)}
                                  placeholder={t_i18n("The expected or reference answer")}
                                />
                              </div>
                              <div className="space-y-1">
                                <div className="flex items-center gap-1">
                                  <Label htmlFor="new-entry-judge-instructions">{t_i18n("Judge Instructions")}</Label>
                                  <InfoTip content={t_i18n(EVALUATION_TERMS.judgeInstructionsEntry)} label={t_i18n("When the entry's judge instructions apply")} />
                                </div>
                                <p id="new-entry-judge-instructions-help" className="content-compact text-default-secondary">{t_i18n("(optional: fully replaces dataset & evaluation defaults, not appended)")}</p>
                                <textarea
                                  id="new-entry-judge-instructions"
                                  aria-describedby="new-entry-judge-instructions-help"
                                  className={`w-full min-h-[50px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y ${FIELD_FOCUS_RING}`}
                                  value={entryEvalPrompt}
                                  onChange={(e) => setEntryEvalPrompt(e.target.value)}
                                  placeholder={t_i18n("Entry-specific judge instructions. Fully replaces (not appended to) the dataset-level and evaluation-level instructions when set.")}
                                />
                              </div>
                              <div className="flex justify-end gap-2">
                                <Button
                                  type="button"
                                  variant="secondary"
                                  onClick={() => { setShowAddEntry(false); setEntryDescription(""); setEntryInputPrompt(""); setEntryExpectedAnswer(""); setEntryEvalPrompt(""); setNewEntryFile(null); }}
                                >
                                  {t_i18n("Cancel")}
                                </Button>
                                <Button
                                  type="button"
                                  onClick={handleAddEntry}
                                  disabled={!entryInputPrompt.trim() || !entryExpectedAnswer.trim() || addEntry.isPending || aiOff}
                                  aria-busy={addEntry.isPending || undefined}
                                >
                                  {addEntry.isPending && <Spinner size="sm" tone="inherit" />}
                                  {!addEntry.isPending && <Plus className="h-3 w-3" />}
                                  {t_i18n("Add Entry")}
                                </Button>
                              </div>
                            </div>
                          ) : !expandedReadOnly ? (
                            <Button
                              type="button"
                              variant="secondary"
                              data-testid="new-evaluation.add-entry-open"
                              className="w-full"
                              onClick={() => setShowAddEntry(true)}
                            >
                              <Plus className="h-3.5 w-3.5 mr-1" />
                              {t_i18n("Add Entry")}
                            </Button>
                          ) : (
                            <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-default p-2 content-compact text-default-secondary flex items-center gap-1.5">
                              <Lock className="h-3 w-3" /> {t_i18n("This dataset is read-only. Disable read-only in dataset settings to add or edit entries.")}
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Submit */}
      <div className="flex justify-end gap-3" data-testid="new-evaluation.footer-actions">
        <Button variant="secondary" onClick={() => navigate("/evaluations")}>
          {t_i18n("Cancel")}
        </Button>
        <Button
          onClick={handleSubmit}
          disabled={!name.trim() || createEvaluation.isPending || aiOff}
          aria-busy={createEvaluation.isPending || undefined}
        >
          {createEvaluation.isPending ? (
            <>
              <Spinner size="sm" tone="inherit" />
              {t_i18n("Creating...")}
            </>
          ) : (
            t_i18n("Create Evaluation")
          )}
        </Button>
      </div>
    </div>
  );
}
