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

import { useState, useRef } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Database, Plus, Upload, Download, ExternalLink,
} from "lucide-react";
import { Slider, Spinner } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { SearchInput } from "@/components/ui/search-input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { EmptyState } from "@/components/ui/empty-state";
import { NeedsFamilyBadge } from "@/components/ui/needs-family-badge";
import { FamilyBadge } from "@/components/ui/family-badge";
import { useToast } from "@/components/ui/toast";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  useDatasets,
  useCreateDataset,
  useImportDatasetZip,
  exportDataset,
  useHFSplits,
  useHFPreview,
  importHFDataset,
  MAX_HF_ROW_LIMIT,
  type HFImportProgress,
} from "@/hooks/api/useDatasetApi";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { BackButton } from "@/components/ui/back-button";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import type { Dataset } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Tip } from "@/components/ui/tooltip";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { formatTimeAgo } from "@/lib/relative-time";

/**
 * What the library page and its two creation dialogs explain in place: the
 * concepts shown on this page only. Cross-page concepts come from `EVALUATION_TERMS`.
 * Read by `t_i18n`: every value is a catalog key. @i18n-keys
 */
const DATASETS_TERMS = {
  // A dataset made by "New Dataset" or the HuggingFace import has no family (datasets.py).
  newDatasetFamilyLabel: "What a new dataset still needs",
  datasetCount: "The number of datasets you can see. The search does not change it.",
  huggingFace:
    "Creates a dataset from a split of a HuggingFace dataset: each row becomes an entry, its columns mapped to the entry's fields.",
  importZip:
    "Creates a dataset from a ZIP exported from a dataset's page, with its entries, files, judge instructions, output family, JSON schema and label vocabulary.",
  split:
    "A part of the HuggingFace dataset, such as train or test. A name before the slash is the subset the split belongs to.",
  splitLabel: "What a split is",
  preview: "The split's first 3 rows and first 6 columns. The column pickers on the next step list every column.",
  previewLabel: "What the preview shows",
  inputColumn: "The column whose text becomes each entry's input prompt, the message sent to the agent.",
  inputColumnLabel: "What the input prompt column fills",
  expectedColumn:
    "The column holding each entry's expected answer, the answer the agent should give. With none, the entries are imported without one; add it to each entry before running an evaluation.",
  expectedColumnLabel: "What the expected answer column fills",
  judgeColumn:
    "A column fills each entry's own judge instructions. Text written below instead fills the dataset's default judge instructions, used for every entry.",
  judgeColumnLabel: "What the judge instructions column fills",
  rowsToImport: "The split's first rows, in their order, not a random sample.",
  rowsToImportLabel: "Which rows are imported",
  judgeInstructionsDatasetLabel: "What the default judge instructions do",
} as const;

// ---------------------------------------------------------------------------
// Dataset Card
// ---------------------------------------------------------------------------

/**
 * One dataset of the library. The name is the card's link, stretched over the whole card
 * (`after:inset-0`), so the badges' explanations and the export button are tab stops of
 * their own instead of controls nested in a link.
 */
function DatasetCard({ dataset }: { dataset: Dataset }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const [exporting, setExporting] = useState(false);

  const handleExport = async () => {
    setExporting(true);
    try {
      await exportDataset(dataset.id, dataset.name);
    } catch {
      toast({ title: t_i18n("Export failed"), variant: "error" });
    } finally {
      setExporting(false);
    }
  };

  return (
    <Card className="relative transition-all hover:bg-elevation-hover border-elevation-subtle h-full backdrop-blur-sm layer-1 bg-elevation-default hover:border-accent/30 focus-within:border-accent/30">
      <CardContent className="p-4 flex flex-col h-full">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            <Database className="h-5 w-5 text-accent" />
          </div>
          <div className="min-w-0 flex-1">
            <Link
              to={`/evaluations/datasets/${dataset.id}`}
              className="block content-base font-medium truncate rounded-sm after:absolute after:inset-0 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            >
              {dataset.name}
            </Link>
            <p className="content-caption text-default-secondary line-clamp-2 mt-0.5">
              {dataset.description || " "}
            </p>
          </div>
          <div className="relative z-10 flex items-center gap-1.5 shrink-0 pointer-events-none [&>*]:pointer-events-auto">
            {dataset.is_read_only && (
              <InfoLabel content={t_i18n(EVALUATION_TERMS.datasetReadOnly)}>
                <Badge variant="outline" className="content-caption">{t_i18n("Read-only")}</Badge>
              </InfoLabel>
            )}
            {dataset.needs_family ? (
              <NeedsFamilyBadge size="sm" />
            ) : (
              <FamilyBadge family={dataset.family} size="sm" />
            )}
            <Tip content={t_i18n("Export as ZIP")}>
              <button
                type="button"
                aria-label={t_i18n("Export as ZIP")}
                className="flex h-7 w-7 items-center justify-center rounded-sm text-default-secondary hover:text-accent hover:bg-accent/10 transition-colors"
                onClick={handleExport}
                disabled={exporting}
                aria-busy={exporting || undefined}
              >
                {exporting && <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} />}
                {!exporting && <Download className="h-3.5 w-3.5" />}
              </button>
            </Tip>
          </div>
        </div>

        <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
          <span className="content-caption text-default-secondary">
            {t_i18n("{count, plural, one {# entry} other {# entries}}", { count: dataset.entry_count })}
          </span>
          {dataset.evaluation_count > 0 && (
            <InfoLabel
              content={t_i18n(EVALUATION_TERMS.datasetEvaluationCount)}
              className="relative z-10 content-caption text-default-secondary"
            >
              {t_i18n("{count, plural, one {# evaluation} other {# evaluations}}", { count: dataset.evaluation_count })}
            </InfoLabel>
          )}
          <span className="ml-auto content-caption text-default-secondary">
            {t_i18n("Updated {time}", { time: formatTimeAgo(intl, dataset.updated_at) })}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Create Dataset Dialog
// ---------------------------------------------------------------------------

function CreateDatasetDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const create = useCreateDataset();
  const aiOff = useAiActionDisabled();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [evalInstructions, setEvalInstructions] = useState("");

  const handleCreate = () => {
    if (!name.trim()) {
      toast({ title: t_i18n("Name is required"), variant: "error" });
      return;
    }
    create.mutateAsync({
      name: name.trim(),
      description: description.trim() || null,
      evaluation_instructions: evalInstructions.trim() || null,
    }).then((ds) => {
      toast({ title: t_i18n("Dataset created"), variant: "success" });
      onClose();
      navigate(`/evaluations/datasets/${ds.id}`);
    }).catch((err: Error) => {
      toast({ title: t_i18n("Failed to create dataset"), description: err.message, variant: "error" });
    });
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <div className="flex items-center gap-1.5">
            <DialogTitle>{t_i18n("New Dataset")}</DialogTitle>
            <InfoTip content={t_i18n(EVALUATION_TERMS.newDatasetNoFamily)} label={t_i18n(DATASETS_TERMS.newDatasetFamilyLabel)} />
          </div>
        </DialogHeader>
        <div className="space-y-4 pt-2">
          <div className="space-y-1.5">
            <label className="content-base font-medium">{t_i18n("Name *")}</label>
            <Input aria-label={t_i18n("Name *")} value={name} onChange={(e) => setName(e.target.value)} placeholder={t_i18n("Dataset name")} autoFocus />
          </div>
          <div className="space-y-1.5">
            <label className="content-base font-medium">{t_i18n("Description")}</label>
            <textarea
              className={`w-full min-h-[80px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y ${FIELD_FOCUS_RING}`}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t_i18n("Optional description")}
            />
          </div>
          <div className="space-y-1.5">
            <label className="flex items-center gap-1 content-base font-medium">
              {t_i18n("Default Judge Instructions")}
              <InfoTip
                content={t_i18n(EVALUATION_TERMS.judgeInstructionsDataset)}
                label={t_i18n(DATASETS_TERMS.judgeInstructionsDatasetLabel)}
              />
            </label>
            <textarea
              aria-label={t_i18n("Default Judge Instructions")}
              className={`w-full min-h-[80px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y ${FIELD_FOCUS_RING}`}
              value={evalInstructions}
              onChange={(e) => setEvalInstructions(e.target.value)}
              placeholder={t_i18n("Instructions for the LLM judge on every entry of this dataset...")}
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={onClose}>{t_i18n("Cancel")}</Button>
            <AiDisabledTip>
            <Button onClick={handleCreate} disabled={create.isPending || aiOff} aria-busy={create.isPending || undefined}>
              {create.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Create")}
            </Button>
            </AiDisabledTip>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// HuggingFace Import Dialog
// ---------------------------------------------------------------------------

const HF_STEPS = ["Dataset", "Split", "Columns", "Options"] as const;
/** Column picker value for "no column". */
const NO_COLUMN = "__none__";
type HFStep = 0 | 1 | 2 | 3;

function HFStepIndicator({ current }: { current: HFStep }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center gap-0 pt-1 pb-3">
      {HF_STEPS.map((label, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <div key={label} className="flex items-center flex-1 last:flex-none">
            <div className="flex items-center gap-1.5">
              <div className={`flex h-5 w-5 items-center justify-center rounded-full content-caption font-semibold transition-colors ${
                done ? "bg-accent text-negative-primary" : active ? "bg-accent/20 text-accent border border-accent/40" : "bg-elevation-hover text-default-secondary"
              }`}>
                {done ? "✓" : i + 1}
              </div>
              <span className={`content-compact ${active ? "text-default-primary font-medium" : done ? "text-accent" : "text-default-secondary"}`}>
                {t_i18n(label)}
              </span>
            </div>
            {i < HF_STEPS.length - 1 && (
              <div className={`flex-1 h-px mx-2 ${i < current ? "bg-accent/40" : "bg-elevation-hover"}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function HFImportDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t_i18n, intl } = useFormatter();
  const figures = useNumberFormat();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [step, setStep] = useState<HFStep>(0);
  const [hfDatasetId, setHfDatasetId] = useState("");
  const [selectedConfig, setSelectedConfig] = useState("");
  const [selectedSplit, setSelectedSplit] = useState("");
  const [inputColumn, setInputColumn] = useState("");
  const [expectedColumn, setExpectedColumn] = useState("");
  const [evaluationColumn, setEvaluationColumn] = useState("");
  const [evaluationInstruction, setEvaluationInstruction] = useState("");
  const [descriptionColumn, setDescriptionColumn] = useState("");
  const [rowLimit, setRowLimit] = useState<number | "">(100);
  const [dsName, setDsName] = useState("");
  const [dsDescription, setDsDescription] = useState("");

  const splits = useHFSplits(hfDatasetId);
  const preview = useHFPreview(hfDatasetId, selectedConfig, selectedSplit);
  const [importProgress, setImportProgress] = useState<HFImportProgress | null>(null);

  const previewColumns = preview.data?.features.map((f) => f.name) ?? [];
  const previewRows = preview.data?.rows.slice(0, 3) ?? [];
  const splitOptions = splits.data?.splits ?? [];
  const selectedSplitMeta = splitOptions.find((s) => s.config === selectedConfig && s.split === selectedSplit);
  const maxRows = Math.min(selectedSplitMeta?.num_rows ?? 10000, MAX_HF_ROW_LIMIT);

  const handleFetch = () => {
    if (!hfDatasetId.trim()) return;
    splits.refetch().then((res) => {
      if (res.data?.splits.length) {
        const first = res.data.splits[0];
        setSelectedConfig(first.config);
        setSelectedSplit(first.split);
        setRowLimit(Math.min(first.num_rows ?? 100, MAX_HF_ROW_LIMIT));
        setStep(1);
      }
    }).catch(() => {
      toast({ title: t_i18n("Failed to fetch dataset splits"), variant: "error" });
    });
  };

  const handleImport = async () => {
    setImportProgress({ fetched: 0, total: rowLimit !== "" ? rowLimit : 0, saving: false });
    try {
      const ds = await importHFDataset(
        {
          hf_dataset_id: hfDatasetId,
          config: selectedConfig || undefined,
          split: selectedSplit,
          input_column: inputColumn,
          expected_answer_column: expectedColumn || undefined,
          evaluation_prompt_column: evaluationColumn || undefined,
          evaluation_instructions: !evaluationColumn ? (evaluationInstruction.trim() || undefined) : undefined,
          description_column: descriptionColumn || undefined,
          row_limit: rowLimit !== "" ? rowLimit : undefined,
          name: dsName || undefined,
          description: dsDescription || undefined,
        },
        setImportProgress,
      );
      toast({
        title: ds.reused ? t_i18n("This dataset already exists: it was opened instead of imported again") : t_i18n("Dataset imported from HuggingFace"),
        variant: "success",
      });
      onClose();
      navigate(`/evaluations/datasets/${ds.id}`);
    } catch (err) {
      setImportProgress(null);
      toast({ title: t_i18n("Import failed"), description: (err as Error).message, variant: "error" });
    }
  };

  const handleClose = () => {
    setStep(0);
    setHfDatasetId("");
    setSelectedConfig("");
    setSelectedSplit("");
    setInputColumn("");
    setExpectedColumn("");
    setEvaluationColumn("");
    setEvaluationInstruction("");
    setDescriptionColumn("");
    setRowLimit(100);
    setDsName("");
    setDsDescription("");
    setImportProgress(null);
    onClose();
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <div className="flex items-center gap-1.5">
            <DialogTitle>{t_i18n("Import from HuggingFace")}</DialogTitle>
            <InfoTip content={t_i18n(EVALUATION_TERMS.newDatasetNoFamily)} label={t_i18n(DATASETS_TERMS.newDatasetFamilyLabel)} />
          </div>
        </DialogHeader>

        <HFStepIndicator current={step} />

        {/* Step 0 — Dataset ID */}
        {step === 0 && (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <label className="content-base font-medium">{t_i18n("HuggingFace Dataset ID")}</label>
              <p className="text-[11px] text-default-secondary">{t_i18n("e.g. squad, rajpurkar/squad, username/my-dataset")}</p>
              <div className="flex gap-2">
                <Input aria-label={t_i18n("HuggingFace Dataset ID")}
                  value={hfDatasetId}
                  onChange={(e) => setHfDatasetId(e.target.value)}
                  placeholder="dataset-owner/dataset-name"
                  onKeyDown={(e) => e.key === "Enter" && handleFetch()}
                  autoFocus
                />
                <Button onClick={handleFetch} disabled={splits.isFetching || !hfDatasetId.trim()} aria-busy={splits.isFetching || undefined} className="shrink-0">
                  {splits.isFetching && <Spinner size="sm" tone="inherit" />}
                  {!splits.isFetching && <ExternalLink className="h-4 w-4" />}
                  {t_i18n("Fetch")}
                </Button>
              </div>
            </div>
          </div>
        )}

        {/* Step 1 — Split */}
        {step === 1 && (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <label className="flex items-center gap-1 content-base font-medium">
                {t_i18n("Split")}
                <InfoTip content={t_i18n(DATASETS_TERMS.split)} label={t_i18n(DATASETS_TERMS.splitLabel)} />
              </label>
              <SearchableSelect
                aria-label={t_i18n("Split")}
                searchable="auto"
                sort={false}
                options={splitOptions.map((s, i) => ({
                  value: String(i),
                  label: s.config !== "default" ? `${s.config}/${s.split}` : s.split,
                  description: s.num_rows != null ? t_i18n("{count} rows", { count: intl.formatNumber(s.num_rows) }) : undefined,
                }))}
                value={String(splitOptions.findIndex((s) => s.config === selectedConfig && s.split === selectedSplit))}
                onValueChange={(v) => {
                  const s = splitOptions[parseInt(v, 10)];
                  if (s) {
                    setSelectedConfig(s.config);
                    setSelectedSplit(s.split);
                    setRowLimit(Math.min(s.num_rows ?? 100, MAX_HF_ROW_LIMIT));
                  }
                }}
              />
            </div>

            {/* Preview table */}
            {preview.isFetching && (
              <p className="content-compact text-default-secondary flex items-center gap-1.5">
                <Spinner size="sm" tone="inherit" /> {t_i18n("Loading preview...")}
              </p>
            )}
            {previewRows.length > 0 && (
              <div className="space-y-1.5">
                <p className="flex items-center gap-1 content-compact font-medium text-default-secondary uppercase tracking-wider">
                  {t_i18n("Preview (first 3 rows)")}
                  <InfoTip content={t_i18n(DATASETS_TERMS.preview)} label={t_i18n(DATASETS_TERMS.previewLabel)} />
                </p>
                <div className="overflow-x-auto rounded-sm border border-elevation-subtle-soft">
                  <table className="w-full content-compact">
                    <thead>
                      <tr className="border-b border-elevation-subtle-soft layer-1 bg-elevation-default">
                        {previewColumns.slice(0, 6).map((col) => (
                          <th key={col} className="text-left px-3 py-2 font-medium text-default-secondary">{col}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {previewRows.map((row, i) => (
                        <tr key={i} className="border-b border-elevation-subtle-soft">
                          {previewColumns.slice(0, 6).map((col) => (
                            <td key={col} className="px-3 py-2 text-default-secondary max-w-[200px] truncate">
                              {String(row[col] ?? "")}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <div className="flex justify-between pt-2">
              <Button variant="ghost" size="sm" onClick={() => setStep(0)}>{t_i18n("Back")}</Button>
              <Button onClick={() => setStep(2)} disabled={!selectedSplit}>{t_i18n("Next")}</Button>
            </div>
          </div>
        )}

        {/* Step 2 — Column mapping */}
        {step === 2 && (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <p className="content-base font-medium">{t_i18n("Column Mapping")}</p>
              <p className="text-[11px] text-default-secondary">{t_i18n("Map the dataset columns to the evaluation entry fields.")}</p>
            </div>
            {preview.isFetching ? (
              <p className="content-compact text-default-secondary flex items-center gap-1.5">
                <Spinner size="sm" tone="inherit" /> {t_i18n("Loading columns...")}
              </p>
            ) : (
              <div className="space-y-3">
                {[
                  {
                    key: "input",
                    label: t_i18n("Input Prompt"),
                    tip: { content: t_i18n(DATASETS_TERMS.inputColumn), label: t_i18n(DATASETS_TERMS.inputColumnLabel) },
                    required: true,
                    value: inputColumn,
                    set: setInputColumn,
                  },
                  {
                    key: "expected",
                    label: t_i18n("Expected Answer"),
                    tip: { content: t_i18n(DATASETS_TERMS.expectedColumn), label: t_i18n(DATASETS_TERMS.expectedColumnLabel) },
                    required: false,
                    value: expectedColumn,
                    set: setExpectedColumn,
                  },
                  { key: "description", label: t_i18n("Description"), tip: null, required: false, value: descriptionColumn, set: setDescriptionColumn },
                ].map((f) => (
                  <div key={f.key} className="space-y-1">
                    <label className="flex items-center gap-1 content-compact text-default-secondary">
                      {f.label}{f.required && <span className="text-accent">*</span>}
                      {f.tip && <InfoTip content={f.tip.content} label={f.tip.label} />}
                    </label>
                    <SearchableSelect
                      aria-label={f.label}
                      searchable="auto"
                      sort={false}
                      options={previewColumns.map((col) => ({ value: col, label: col }))}
                      pinned={[{ value: NO_COLUMN, label: t_i18n("-- none --") }]}
                      value={f.value || NO_COLUMN}
                      onValueChange={(v) => f.set(v === NO_COLUMN ? "" : v)}
                    />
                  </div>
                ))}

                {/* Judge instructions: a column fills each entry's, manual text the dataset default */}
                <div className="space-y-1.5">
                  <label className="flex items-center gap-1 content-compact text-default-secondary">
                    {t_i18n("Judge Instructions")}
                    <InfoTip content={t_i18n(DATASETS_TERMS.judgeColumn)} label={t_i18n(DATASETS_TERMS.judgeColumnLabel)} />
                  </label>
                  <SearchableSelect
                    aria-label={t_i18n("Judge Instructions")}
                    searchable="auto"
                    sort={false}
                    options={previewColumns.map((col) => ({ value: col, label: col }))}
                    pinned={[{ value: NO_COLUMN, label: t_i18n("-- write manually below --") }]}
                    value={evaluationColumn || NO_COLUMN}
                    onValueChange={(v) => { setEvaluationColumn(v === NO_COLUMN ? "" : v); setEvaluationInstruction(""); }}
                  />
                  {!evaluationColumn && (
                    <textarea
                      aria-label={t_i18n("Default Judge Instructions")}
                      className={`w-full min-h-[80px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y ${FIELD_FOCUS_RING}`}
                      placeholder={t_i18n("Default judge instructions for every entry of this dataset…")}
                      value={evaluationInstruction}
                      onChange={(e) => setEvaluationInstruction(e.target.value)}
                    />
                  )}
                </div>
              </div>
            )}

            <div className="flex justify-between pt-2">
              <Button variant="ghost" size="sm" onClick={() => setStep(1)}>{t_i18n("Back")}</Button>
              <Button onClick={() => setStep(3)} disabled={!inputColumn}>{t_i18n("Next")}</Button>
            </div>
          </div>
        )}

        {/* Step 3 — Options + Import */}
        {step === 3 && (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <label className="content-base font-medium">{t_i18n("Dataset Name")}</label>
              {/* The name the backend gives an unnamed import (`hf_import_dataset`). */}
              <Input
                aria-label={t_i18n("Dataset Name")}
                value={dsName}
                onChange={(e) => setDsName(e.target.value)}
                placeholder={`${hfDatasetId}${selectedConfig ? ` / ${selectedConfig}` : ""} / ${selectedSplit}`}
              />
            </div>
            <div className="space-y-1.5">
              <label className="content-base font-medium">{t_i18n("Description")}</label>
              <Input aria-label={t_i18n("Description")} value={dsDescription} onChange={(e) => setDsDescription(e.target.value)} placeholder={t_i18n("Optional")} />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className="flex items-center gap-1 content-base font-medium">
                  {t_i18n("Rows to import")}
                  <InfoTip content={t_i18n(DATASETS_TERMS.rowsToImport)} label={t_i18n(DATASETS_TERMS.rowsToImportLabel)} />
                </label>
                <span className="content-base font-mono-plex font-medium text-accent tabular-nums">
                  {typeof rowLimit === "number" ? figures.exactCount(rowLimit) : "—"}
                  {selectedSplitMeta && (
                    <span className="text-default-secondary font-normal content-compact" title={figures.exactCount(maxRows)}> / {figures.count(maxRows)}</span>
                  )}
                </span>
              </div>
              <Slider
                aria-label={t_i18n("Rows to import")}
                min={5}
                max={maxRows}
                step={5}
                value={[typeof rowLimit === "number" ? rowLimit : maxRows]}
                onValueChange={([val]) => {
                  setRowLimit(val + 5 > maxRows ? maxRows : val);
                }}
              />
              <div className="flex justify-between content-caption text-default-secondary tabular-nums">
                <span>5</span>
                <span title={figures.exactCount(maxRows)}>{figures.count(maxRows)}</span>
              </div>
            </div>

            {importProgress && (
              <div className="space-y-1.5">
                <div className="flex justify-between content-compact text-default-secondary">
                  <span>{importProgress.saving ? t_i18n("Saving to database…") : t_i18n("Fetching rows from HuggingFace…")}</span>
                  {!importProgress.saving && importProgress.total > 0 && (
                    <span className="font-mono-plex tabular-nums text-accent" title={`${figures.exactCount(importProgress.fetched)} / ${figures.exactCount(importProgress.total)}`}>
                      {figures.count(importProgress.fetched)} / {figures.count(importProgress.total)}
                    </span>
                  )}
                </div>
                <div className="h-1.5 w-full rounded-full bg-elevation-hover overflow-hidden">
                  <div
                    className="h-full rounded-full bg-accent transition-all duration-300"
                    style={{
                      width: importProgress.saving || importProgress.total === 0
                        ? "100%"
                        : `${Math.round((importProgress.fetched / importProgress.total) * 100)}%`,
                    }}
                  />
                </div>
              </div>
            )}

            <div className="flex justify-between pt-2">
              <Button variant="ghost" size="sm" onClick={() => setStep(2)} disabled={!!importProgress}>{t_i18n("Back")}</Button>
              <Button onClick={handleImport} disabled={!!importProgress} aria-busy={!!importProgress || undefined}>
                {!!importProgress && <Spinner size="sm" tone="inherit" />}
                {importProgress ? t_i18n("Importing…") : t_i18n("Import Dataset")}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Datasets() {
  const { t_i18n } = useFormatter();
  const { data: datasets, isLoading } = useDatasets();
  // No visibility control on this page yet: every row it lists stays shown (dev:visibility-filters-consistency).
  const { search, setSearch, allItems, filtered } = useEntityFilter({ items: datasets, withVisibility: false });
  const { toast } = useToast();
  const importZip = useImportDatasetZip();
  const aiOff = useAiActionDisabled();
  const zipInputRef = useRef<HTMLInputElement>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [hfOpen, setHfOpen] = useState(false);

  const handleZipImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    importZip.mutateAsync(file).then(() => {
      toast({ title: t_i18n("Dataset imported from ZIP"), variant: "success" });
    }).catch((err: Error) => {
      toast({ title: t_i18n("Import failed"), description: err.message, variant: "error" });
    });
    if (zipInputRef.current) zipInputRef.current.value = "";
  };

  return (
    <div className="space-y-6">
      <CreateDatasetDialog open={createOpen} onClose={() => setCreateOpen(false)} />
      <HFImportDialog open={hfOpen} onClose={() => setHfOpen(false)} />
      <input ref={zipInputRef} type="file" accept=".zip" className="hidden" onChange={handleZipImport} />

      <BackButton to="/evaluations" label="Back to Evaluations" />

      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Datasets")}</h1>
          <p className="mt-2 text-default-secondary">{t_i18n("Manage test datasets for your evaluations.")}</p>
        </div>
        <div className="flex items-center gap-2">
          {/* Both imports are AI actions the server refuses with AI off (`require_ai_enabled`). */}
          <AiDisabledTip>
            <Tip content={t_i18n(DATASETS_TERMS.huggingFace)}>
              <Button variant="outline" size="sm" className="gap-1.5" onClick={() => setHfOpen(true)} disabled={aiOff}>
                <ExternalLink className="h-3.5 w-3.5" /> HuggingFace
              </Button>
            </Tip>
          </AiDisabledTip>
          <AiDisabledTip>
            <Tip content={t_i18n(DATASETS_TERMS.importZip)}>
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5"
                onClick={() => zipInputRef.current?.click()}
                disabled={importZip.isPending || aiOff}
                aria-busy={importZip.isPending || undefined}
              >
                {importZip.isPending && <Spinner size="sm" tone="inherit" />}
                {!importZip.isPending && <Upload className="h-3.5 w-3.5" />}
                {t_i18n("Import ZIP")}
              </Button>
            </Tip>
          </AiDisabledTip>
          <Button className="gap-1.5" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" /> {t_i18n("New Dataset")}
          </Button>
        </div>
      </div>

      {isLoading ? (
        <CardGridSkeleton count={cardSkeletonCount()} label={t_i18n("Loading")} />
      ) : (
        <>
          {(allItems ?? []).length > 0 && (
            <div className="flex flex-wrap items-center gap-3">
              <SearchInput toolbar placeholder={t_i18n("Search datasets...")} value={search} onChange={setSearch} />
              <InfoLabel
                content={t_i18n(DATASETS_TERMS.datasetCount)}
                className="ml-auto rounded-sm bg-elevation-hover px-2.5 py-1 content-compact font-medium tabular-nums text-default-secondary"
              >
                {allItems.length}
              </InfoLabel>
            </div>
          )}

          {allItems.length === 0 ? (
            <EmptyState
              icon={Database}
              title="No datasets yet"
              requiresAi
              description="Create your first dataset to define test cases for your evaluations."
              actionLabel="New Dataset"
              onAction={() => setCreateOpen(true)}
            />
          ) : filtered.length > 0 ? (
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {filtered.map((ds) => (
                <DatasetCard key={ds.id} dataset={ds} />
              ))}
            </div>
          ) : (
            <p className="content-base text-default-secondary py-8 text-center">{t_i18n("No datasets matching your search")}</p>
          )}
        </>
      )}
    </div>
  );
}
