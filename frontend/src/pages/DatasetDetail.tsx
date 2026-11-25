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

import React, { useId, useRef, useState } from "react";
import { Spinner } from "@filigran/design-system";
import { useParams, useNavigate } from "react-router-dom";
import {
  Database, Plus, Pencil, Trash2, X, Check, Download, Paperclip, FileText,
  Image as ImageIcon, Settings2, FlaskConical, History,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TagInput } from "@/components/ui/tag-input";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { NotFoundState } from "@/components/ui/not-found-state";
import { NeedsFamilyBadge } from "@/components/ui/needs-family-badge";
import { FamilyBadge } from "@/components/ui/family-badge";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { Pagination } from "@/components/ui/pagination";
import { useTabParam } from "@/hooks/useTabParam";
import {
  useDataset,
  useDatasetVersions,
  useDatasetVersion,
  useUpdateDataset,
  useSeedGroundTruth,
  useDeleteDataset,
  useAddDatasetEntry,
  useUpdateDatasetEntry,
  useDeleteDatasetEntry,
  useUploadDatasetEntryFile,
  useRemoveDatasetEntryFile,
  exportDataset,
} from "@/hooks/api/useDatasetApi";
import { useAuth } from "@/hooks/useAuth";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useEvaluationFamilies } from "@/hooks/api/useEvaluationApi";
import { cn, formatShortDateTime } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { useFormatter } from "@/i18n";
import type { DatasetEntry, EvaluationFamily, OutputSchema } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { EVALUATION_TERMS } from "@/lib/evaluation-glossary";
import { familyDescription } from "@/lib/evaluation-families";
import { formatTimeAgo } from "@/lib/relative-time";

/**
 * What the dataset page explains in place: the concepts shown on this page only.
 * Cross-page concepts come from `EVALUATION_TERMS`. Read by `t_i18n`: every value is a
 * catalog key. @i18n-keys
 */
const DATASET_TERMS = {
  // An empty prompt with files is replaced by `FILE_ONLY_FALLBACK_PROMPT` (framing.py).
  inputPrompt:
    "The message sent to the agent. It may stay empty when a file is attached: the agent is then asked to produce its deliverable for the attached files.",
  inputPromptLabel: "What the input prompt is",
  expectedAnswerLabel: "What the expected answer is",
  judgeInstructionsEntryLabel: "What an entry's judge instructions do",
  judgeInstructionsDatasetLabel: "What the default judge instructions do",
  // `seed_ground_truth`: a preview of the agent's `output_schema`, nothing written.
  seedFromAgent:
    "Fills the JSON schema below with the one an agent you pick declares for its output. Nothing is saved until you accept it and save.",
  familyLabel: "What this output family compares",
  // A run takes its output family from its evaluation (`evaluation_engine.py`).
  familyConflicts:
    "A run scores these entries with its evaluation's output family, not this one. Change one of the two families, or unlink the dataset from the evaluation.",
  familyConflictsLabel: "What a family conflict changes",
  // Default judge instructions are read live, never from a version (`load_pinned_entries`).
  versionHistory:
    "Every saved version of this dataset, newest first; Current is the one the next launch pins. A run is scored against its pinned version, but the default judge instructions are not versioned: runs and re-scores read the current ones.",
  versionHistoryLabel: "What a dataset version is",
  versionSnapshot:
    "This view shows each entry's input and expected answer. The version also keeps each entry's judge instructions, files and JSON schema, and the dataset's JSON schema and label vocabulary.",
  versionSnapshotLabel: "What a version keeps",
} as const;

type Tab = "entries" | "versions" | "settings";

const ENTRIES_PER_PAGE = 20;

// ---------------------------------------------------------------------------
// Entry Form
// ---------------------------------------------------------------------------

function EntryForm({
  idx,
  entry,
  datasetId,
  onCancel,
  isNew,
  globalInstructions,
  family,
}: {
  idx: number;
  entry: {
    description: string | null;
    input_prompt: string;
    expected_answer: string;
    evaluation_prompt: string | null;
    file_ids: string[] | null;
    files: { id: string; filename: string; size: number; content_type: string }[] | null;
    output_schema?: OutputSchema | null;
  };
  datasetId: string;
  onCancel: () => void;
  isNew: boolean;
  globalInstructions?: string | null;
  /** Gates the per-entry JSON schema override: only STRUCTURED_JSON has
   * per-field scoring for it to feed, as at dataset level. */
  family: EvaluationFamily | null;
}) {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const addEntry = useAddDatasetEntry();
  const updateEntry = useUpdateDatasetEntry();
  const uploadFile = useUploadDatasetEntryFile();
  const removeFile = useRemoveDatasetEntryFile();
  const [description, setDescription] = useState(entry.description ?? "");
  const [inputPrompt, setInputPrompt] = useState(entry.input_prompt);
  const [expectedAnswer, setExpectedAnswer] = useState(entry.expected_answer);
  const [evalPrompt, setEvalPrompt] = useState(entry.evaluation_prompt ?? "");
  const canHaveOutputSchema = family === "STRUCTURED_JSON";
  const [hasSchema, setHasSchema] = useState(canHaveOutputSchema && !!entry.output_schema);
  const [schemaDef, setSchemaDef] = useState<string>(() =>
    JSON.stringify(entry.output_schema?.definition ?? null, null, 2),
  );
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const fieldId = useId();
  const existingFiles = entry.files ?? [];
  const hasFiles = existingFiles.length > 0 || pendingFiles.length > 0;
  const isPending = addEntry.isPending || updateEntry.isPending || uploading;

  const handleSave = async () => {
    if (!inputPrompt.trim() && !hasFiles) {
      toast({ title: t_i18n("Input Prompt or a file attachment is required"), variant: "error" });
      return;
    }
    if (!expectedAnswer.trim()) {
      toast({ title: t_i18n("Expected Answer is required"), variant: "error" });
      return;
    }
    let outputSchema: OutputSchema | null = null;
    if (canHaveOutputSchema && hasSchema) {
      let definition: unknown;
      try {
        definition = JSON.parse(schemaDef);
      } catch {
        toast({ title: t_i18n("Schema definition must be valid JSON"), variant: "error" });
        return;
      }
      outputSchema = { type: "json_schema", definition };
    }
    try {
      if (isNew) {
        const created = await addEntry.mutateAsync({
          datasetId,
          description: description || null,
          input_prompt: inputPrompt,
          expected_answer: expectedAnswer,
          evaluation_prompt: evalPrompt || null,
          output_schema: outputSchema,
        });
        if (pendingFiles.length > 0) {
          setUploading(true);
          let uploaded = 0;
          for (const f of pendingFiles) {
            try {
              await uploadFile.mutateAsync({ entryId: created.id, file: f });
              uploaded++;
            } catch {
              toast({ title: t_i18n("Failed to upload \"{name}\"", { name: f.name }), variant: "error" });
            }
          }
          setUploading(false);
          toast({ title: t_i18n("Entry added with {count, plural, one {# file} other {# files}}", { count: uploaded }), variant: "success" });
        } else {
          toast({ title: t_i18n("Entry added"), variant: "success" });
        }
      } else {
        await updateEntry.mutateAsync({
          entryId: (entry as DatasetEntry).id,
          description: description || null,
          input_prompt: inputPrompt,
          expected_answer: expectedAnswer,
          evaluation_prompt: evalPrompt || null,
          output_schema: outputSchema,
        });
        if (pendingFiles.length > 0) {
          setUploading(true);
          let uploaded = 0;
          for (const f of pendingFiles) {
            try {
              await uploadFile.mutateAsync({ entryId: (entry as DatasetEntry).id, file: f });
              uploaded++;
            } catch {
              toast({ title: t_i18n("Failed to upload \"{name}\"", { name: f.name }), variant: "error" });
            }
          }
          setUploading(false);
          toast({ title: t_i18n("Entry updated with {count, plural, one {# new file} other {# new files}}", { count: uploaded }), variant: "success" });
        } else {
          toast({ title: t_i18n("Entry updated"), variant: "success" });
        }
      }
      onCancel();
    } catch {
      toast({ title: t_i18n("Failed to save entry"), variant: "error" });
    }
  };

  const handleFilePick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files?.length) return;
    setPendingFiles((prev) => [...prev, ...Array.from(files)]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const handleRemoveExisting = async (fileId: string) => {
    if (isNew) return;
    try {
      await removeFile.mutateAsync({ entryId: (entry as DatasetEntry).id, fileId });
      toast({ title: t_i18n("File removed"), variant: "success" });
    } catch {
      toast({ title: t_i18n("Failed to remove file"), variant: "error" });
    }
  };

  return (
    <Card className={cn("layer-1 bg-elevation-default backdrop-blur-sm", isNew ? "border-accent/20" : "border-elevation-subtle-soft")}>
      <CardContent className="p-4 space-y-3">
        <div className="flex items-center justify-between">
          <span className={cn("flex h-6 w-6 shrink-0 items-center justify-center rounded-sm content-caption font-mono-plex", "bg-accent/10 text-accent")}>
            {isNew ? "+" : idx + 1}
          </span>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" className="h-7 gap-1 text-default-secondary" onClick={onCancel}>
              <X className="h-3 w-3" /> {t_i18n("Cancel")}
            </Button>
            <AiDisabledTip>
              <Button size="sm" className="h-7 gap-1" onClick={handleSave} disabled={isPending || aiOff} aria-busy={isPending || undefined}>
                {isPending && <Spinner size="sm" tone="inherit" />}
                {!isPending && <Check className="h-3 w-3" />}
                {isNew ? t_i18n("Add") : t_i18n("Save")}
              </Button>
            </AiDisabledTip>
          </div>
        </div>
        <div>
          <Label htmlFor={`${fieldId}-description`} className="mb-1.5 block">
            {t_i18n("Description")} <span className="font-normal text-default-secondary">{t_i18n("(optional)")}</span>
          </Label>
          <Input
            id={`${fieldId}-description`}
            aria-label={t_i18n("Description")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t_i18n("Short label to identify this test case in results...")}
          />
        </div>
        <div>
          <div className="mb-1.5 flex items-center gap-1">
            <Label htmlFor={`${fieldId}-input`}>
              {t_i18n("Input Prompt")} {!hasFiles && <span className="text-default-secondary">*</span>}
            </Label>
            <InfoTip content={t_i18n(DATASET_TERMS.inputPrompt)} label={t_i18n(DATASET_TERMS.inputPromptLabel)} />
          </div>
          <textarea
            id={`${fieldId}-input`}
            aria-label={t_i18n("Input Prompt")}
            className={`w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base resize-y min-h-[60px] ${FIELD_FOCUS_RING}`}
            value={inputPrompt}
            onChange={(e) => setInputPrompt(e.target.value)}
            placeholder={t_i18n("The user message to send to the agent...")}
          />
          {(existingFiles.length > 0 || pendingFiles.length > 0) && (
            <div className="flex flex-wrap gap-2 mt-1">
              {existingFiles.map((f) => {
                const isPdf = f.content_type?.includes("pdf");
                const isImg = f.content_type?.startsWith("image/");
                return (
                  <div key={f.id} className="flex items-center gap-2 rounded-sm layer-1 bg-elevation-default px-3 py-1.5 content-compact">
                    {isPdf ? <FileText className="h-4 w-4 text-feedback-error-primary" /> : isImg ? <ImageIcon className="h-4 w-4 text-feedback-info-primary" /> : <FileText className="h-4 w-4 text-default-secondary" />}
                    <span className="max-w-[150px] truncate text-default-secondary">{f.filename}</span>
                    <button aria-label={t_i18n("Remove file")}
                      type="button"
                      onClick={() => handleRemoveExisting(f.id)}
                      disabled={removeFile.isPending}
                      className="ml-0.5 text-default-secondary hover:text-default-primary transition-colors"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                );
              })}
              {pendingFiles.map((f, i) => {
                const isPdf = f.type.includes("pdf");
                const isImg = f.type.startsWith("image/");
                return (
                  <div key={`pending-${i}`} className="flex items-center gap-2 rounded-sm layer-1 bg-elevation-default px-3 py-1.5 content-compact border border-accent/20">
                    {isPdf ? <FileText className="h-4 w-4 text-feedback-error-primary" /> : isImg ? <ImageIcon className="h-4 w-4 text-feedback-info-primary" /> : <FileText className="h-4 w-4 text-default-secondary" />}
                    <span className="max-w-[150px] truncate text-default-secondary">{f.name}</span>
                    <button aria-label={t_i18n("Remove file")}
                      type="button"
                      onClick={() => setPendingFiles((prev) => prev.filter((_, j) => j !== i))}
                      className="ml-0.5 text-default-secondary hover:text-default-primary transition-colors"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                );
              })}
            </div>
          )}
          <div className="mt-1">
            <input ref={fileInputRef} type="file" multiple className="hidden" onChange={handleFilePick} />
            <Button variant="outline" size="sm" className="h-7 gap-1 content-compact" onClick={() => fileInputRef.current?.click()} disabled={uploading} aria-busy={uploading || undefined}>
              {uploading && <Spinner size="sm" tone="inherit" />}
              {!uploading && <Paperclip className="h-3 w-3" />} {t_i18n("Attach File")}
            </Button>
          </div>
        </div>
        <div>
          {/* Required on every save, not only on a new entry (`handleSave`). */}
          <div className="mb-1.5 flex items-center gap-1">
            <Label htmlFor={`${fieldId}-expected`}>{t_i18n("Expected Answer")} *</Label>
            <InfoTip content={t_i18n(EVALUATION_TERMS.expectedAnswer)} label={t_i18n(DATASET_TERMS.expectedAnswerLabel)} />
          </div>
          <textarea
            id={`${fieldId}-expected`}
            aria-label={t_i18n("Expected Answer")}
            className={`w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base resize-y min-h-[60px] ${FIELD_FOCUS_RING}`}
            value={expectedAnswer}
            onChange={(e) => setExpectedAnswer(e.target.value)}
            placeholder={t_i18n("The answer the agent should give, in the output family's shape...")}
          />
        </div>
        <div>
          <div className="mb-1.5 flex items-center gap-1">
            <Label htmlFor={`${fieldId}-judge`}>
              {t_i18n("Judge Instructions")} <span className="font-normal text-default-secondary">{t_i18n("(optional)")}</span>
            </Label>
            <InfoTip
              content={t_i18n(EVALUATION_TERMS.judgeInstructionsEntry)}
              label={t_i18n(DATASET_TERMS.judgeInstructionsEntryLabel)}
            />
          </div>
          <textarea
            id={`${fieldId}-judge`}
            aria-label={t_i18n("Judge Instructions")}
            className={`w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base resize-y min-h-[40px] ${FIELD_FOCUS_RING}`}
            value={evalPrompt}
            onChange={(e) => setEvalPrompt(e.target.value)}
            placeholder={globalInstructions ? t_i18n("Dataset default: \"{text}\"", { text: `${globalInstructions.slice(0, 80)}${globalInstructions.length > 80 ? "…" : ""}` }) : t_i18n("Judge instructions for this entry only...")}
          />
        </div>
        {canHaveOutputSchema && (
          <div>
            <div className="flex items-center justify-between mb-1">
              <Label htmlFor={`${fieldId}-schema`}>
                {t_i18n("JSON Schema")} <span className="font-normal text-default-secondary">{t_i18n("(optional override: replaces the dataset's schema for this entry only)")}</span>
              </Label>
              <Switch id={`${fieldId}-schema`} aria-label={t_i18n("JSON Schema")} checked={hasSchema} onCheckedChange={setHasSchema} />
            </div>
            {hasSchema && (
              <textarea
                className={`w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base font-mono-plex resize-y min-h-[80px] ${FIELD_FOCUS_RING}`}
                value={schemaDef}
                onChange={(e) => setSchemaDef(e.target.value)}
                placeholder='{ "type": "object", "required": ["answer"] }'
              />
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Entries Tab
// ---------------------------------------------------------------------------

function EntriesTab({ datasetId, entries, canManage, globalInstructions, family }: {
  datasetId: string;
  entries: DatasetEntry[];
  canManage: boolean;
  globalInstructions?: string | null;
  family: EvaluationFamily | null;
}) {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const deleteEntry = useDeleteDatasetEntry();
  const [ConfirmEl, confirm] = useConfirm();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [page, setPage] = useState(0);
  const pageEntries = entries.slice(page * ENTRIES_PER_PAGE, (page + 1) * ENTRIES_PER_PAGE);

  const handleDelete = async (entryId: string) => {
    if (!(await confirm({ title: "Remove entry?", description: "This cannot be undone.", variant: "danger" }))) return;
    deleteEntry.mutate(entryId, {
      onSuccess: () => toast({ title: t_i18n("Entry removed"), variant: "success" }),
      onError: () => toast({ title: t_i18n("Failed to remove entry"), variant: "error" }),
    });
  };

  return (
    <div className="space-y-3">
      {ConfirmEl}
      {/* The total once, at the top, for every reader; the pager under the entries never repeats it (#11191). */}
      <div className="flex items-center justify-between">
        <p className="content-base text-default-secondary">{t_i18n("{count, plural, one {# entry} other {# entries}}", { count: entries.length })}</p>
        {canManage && (
          <Button size="sm" variant="outline" className="gap-1.5" onClick={() => { setAdding(true); setEditingId(null); }}>
            <Plus className="h-3.5 w-3.5" /> {t_i18n("Add Entry")}
          </Button>
        )}
      </div>

      {entries.length === 0 && !adding ? (
        <Card className="border-dashed border-elevation-subtle-soft">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <Database className="h-10 w-10 text-default-secondary mb-3" />
            <p className="content-base text-default-secondary">{t_i18n("No entries yet")}</p>
            {canManage && (
              <Button size="sm" variant="outline" className="gap-1.5 mt-3" onClick={() => setAdding(true)}>
                <Plus className="h-3.5 w-3.5" /> {t_i18n("Add First Entry")}
              </Button>
            )}
          </CardContent>
        </Card>
      ) : (
        <>
          {pageEntries.map((entry, idx) => (
            editingId === entry.id ? (
              <EntryForm
                key={entry.id}
                idx={page * ENTRIES_PER_PAGE + idx}
                entry={entry}
                datasetId={datasetId}
                onCancel={() => setEditingId(null)}
                isNew={false}
                globalInstructions={globalInstructions}
                family={family}
              />
            ) : (
              <Card key={entry.id} className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft">
                <CardContent className="p-4">
                  <div className="flex items-start gap-3">
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm bg-elevation-hover content-caption font-mono-plex text-default-secondary">
                      {page * ENTRIES_PER_PAGE + idx + 1}
                    </span>
                    <div className="flex-1 min-w-0 space-y-2">
                      {entry.description && (
                        <p className="content-base font-medium text-accent">{entry.description}</p>
                      )}
                      {entry.files && entry.files.length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {entry.files.map((f) => (
                            <span key={f.id} className="inline-flex items-center gap-1.5 rounded-sm border border-transparent bg-elevation-hover px-2 py-1 content-compact text-default-secondary">
                              <Paperclip className="h-3 w-3" />
                              <span className="max-w-[180px] truncate">{f.filename}</span>
                            </span>
                          ))}
                        </div>
                      )}
                      {entry.input_prompt && (
                        <div>
                          <p className="content-caption font-medium text-default-secondary uppercase tracking-wider mb-0.5">{t_i18n("Input")}</p>
                          <p className="content-base leading-relaxed">
                            {entry.input_prompt.length > 300 ? `${entry.input_prompt.slice(0, 300)}...` : entry.input_prompt}
                          </p>
                        </div>
                      )}
                      <div>
                        <p className="content-caption font-medium text-default-secondary uppercase tracking-wider mb-0.5">
                          <InfoLabel content={t_i18n(EVALUATION_TERMS.expectedAnswer)}>{t_i18n("Expected Answer")}</InfoLabel>
                        </p>
                        <p className="content-base leading-relaxed text-default-secondary">
                          {entry.expected_answer.length > 300 ? `${entry.expected_answer.slice(0, 300)}...` : entry.expected_answer}
                        </p>
                      </div>
                      {entry.evaluation_prompt && (
                        <div>
                          <p className="content-caption font-medium text-default-secondary uppercase tracking-wider mb-0.5">
                            <InfoLabel content={t_i18n(EVALUATION_TERMS.judgeInstructionsEntry)}>{t_i18n("Judge Instructions")}</InfoLabel>
                          </p>
                          <p className="content-compact text-default-secondary italic">
                            {entry.evaluation_prompt.length > 200 ? `${entry.evaluation_prompt.slice(0, 200)}...` : entry.evaluation_prompt}
                          </p>
                        </div>
                      )}
                    </div>
                    {canManage && (
                      <div className="flex items-center gap-1 shrink-0">
                        <Tip content={t_i18n("Edit entry")}>
                          <Button aria-label={t_i18n("Edit entry")}
                            variant="ghost"
                            size="sm"
                            className="h-7 w-7 p-0 text-default-secondary hover:text-default-primary"
                            onClick={() => { setEditingId(entry.id); setAdding(false); }}
                          >
                            <Pencil className="h-3 w-3" />
                          </Button>
                        </Tip>
                        {/* With AI off the button is disabled: the AI-off reason is its tooltip. */}
                        <AiDisabledTip>
                          <Tip content={t_i18n("Delete entry")}>
                            <Button aria-label={t_i18n("Delete entry")}
                              variant="ghost"
                              size="sm"
                              className="h-7 w-7 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                              onClick={() => handleDelete(entry.id)} disabled={aiOff}>
                              <Trash2 className="h-3 w-3" />
                            </Button>
                          </Tip>
                        </AiDisabledTip>
                      </div>
                    )}
                  </div>
                </CardContent>
              </Card>
            )
          ))}

          {entries.length > ENTRIES_PER_PAGE && (
            <Pagination page={page} pageSize={ENTRIES_PER_PAGE} total={entries.length} onChange={setPage} />
          )}
        </>
      )}

      {adding && (
        <EntryForm
          idx={entries.length}
          entry={{ description: null, input_prompt: "", expected_answer: "", evaluation_prompt: null, file_ids: null, files: null, output_schema: null }}
          datasetId={datasetId}
          onCancel={() => setAdding(false)}
          isNew={true}
          globalInstructions={globalInstructions}
          family={family}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Settings Tab
// ---------------------------------------------------------------------------

function SettingsTab({
  dataset,
  canEditContent,
  canToggleReadOnly,
}: {
  dataset: {
    id: string;
    name: string;
    description: string | null;
    evaluation_instructions: string | null;
    output_schema: OutputSchema | null;
    is_read_only: boolean;
    family: EvaluationFamily | null;
    label_vocabulary: string[] | null;
    needs_family: boolean;
    family_conflicts?: { evaluation_id: string; evaluation_name: string; message: string }[] | null;
  };
  canEditContent: boolean;
  canToggleReadOnly: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateDataset();
  const aiOff = useAiActionDisabled();
  const [name, setName] = useState(dataset.name);
  const [description, setDescription] = useState(dataset.description ?? "");
  const [evalInstructions, setEvalInstructions] = useState(dataset.evaluation_instructions ?? "");
  const [isReadOnly, setIsReadOnly] = useState(dataset.is_read_only);
  const dirty =
    name !== dataset.name ||
    description !== (dataset.description ?? "") ||
    evalInstructions !== (dataset.evaluation_instructions ?? "") ||
    isReadOnly !== dataset.is_read_only;

  const handleSave = () => {
    if (!name.trim()) {
      toast({ title: t_i18n("Name is required"), variant: "error" });
      return;
    }
    update.mutateAsync({
      id: dataset.id,
      name: name.trim(),
      description: description.trim() || null,
      evaluation_instructions: evalInstructions.trim() || null,
      is_read_only: isReadOnly,
    }).then(() => {
      toast({ title: t_i18n("Dataset updated"), variant: "success" });
    }).catch(() => {
      toast({ title: t_i18n("Failed to update dataset"), variant: "error" });
    });
  };

  return (
    <div className="space-y-4 max-w-2xl">
      <Card>
        <CardHeader><CardTitle className="text-base">{t_i18n("General")}</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <label className="content-base font-medium">{t_i18n("Read-only")}</label>
                <p className="text-[11px] text-default-secondary mt-0.5">
                  {t_i18n("When on, the entries, settings and output family cannot be changed and the dataset cannot be deleted, until read-only is turned off.")}
                </p>
              </div>
              <Switch aria-label={t_i18n("Read-only")}
                checked={isReadOnly}
                onCheckedChange={setIsReadOnly}
                disabled={!canToggleReadOnly}
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <label className="content-base font-medium">{t_i18n("Name *")}</label>
            <Input aria-label={t_i18n("Name *")} value={name} onChange={(e) => setName(e.target.value)} disabled={!canEditContent} />
          </div>
          <div className="space-y-1.5">
            <label className="content-base font-medium">{t_i18n("Description")}</label>
            <textarea
              className={`w-full min-h-[80px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y disabled:opacity-50 ${FIELD_FOCUS_RING}`}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={!canEditContent}
              placeholder={t_i18n("Optional description")}
            />
          </div>
          <div className="space-y-1.5">
            <label className="flex items-center gap-1 content-base font-medium">
              {t_i18n("Default Judge Instructions")}
              <InfoTip
                content={t_i18n(EVALUATION_TERMS.judgeInstructionsDataset)}
                label={t_i18n(DATASET_TERMS.judgeInstructionsDatasetLabel)}
              />
            </label>
            <textarea
              aria-label={t_i18n("Default Judge Instructions")}
              className={`w-full min-h-[80px] rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base resize-y disabled:opacity-50 ${FIELD_FOCUS_RING}`}
              value={evalInstructions}
              onChange={(e) => setEvalInstructions(e.target.value)}
              disabled={!canEditContent}
              placeholder={t_i18n("Instructions for the LLM judge on every entry of this dataset...")}
            />
          </div>
          {(canEditContent || canToggleReadOnly) && (
            <div className="flex justify-end">
              <AiDisabledTip>
                <Button onClick={handleSave} disabled={!dirty || update.isPending || aiOff} aria-busy={update.isPending || undefined} size="sm">
                  {update.isPending && <Spinner size="sm" tone="inherit" />}
                  {t_i18n("Save changes")}
                </Button>
              </AiDisabledTip>
            </div>
          )}
        </CardContent>
      </Card>
      <FamilyCard dataset={dataset} canEditContent={canEditContent} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dataset family + classification vocabulary
// ---------------------------------------------------------------------------

/**
 * The dataset's output family, its ground-truth material, and (for `CLASSIFICATION`)
 * its declared label vocabulary.
 *
 * A family-less dataset blocks every evaluation linked to it, so the family gets its
 * own card where an author will notice it. A family change that conflicts with a linked
 * evaluation leaves the link alone; the backend's `family_conflicts` are shown verbatim.
 *
 * The JSON schema editor appears only for `STRUCTURED_JSON`, the one family with
 * per-field scoring; the backend rejects a schema on any other. All three fields save in
 * one PUT so they are validated against each other in the same request.
 */
function FamilyCard({
  dataset,
  canEditContent,
}: {
  dataset: {
    id: string;
    family: EvaluationFamily | null;
    label_vocabulary: string[] | null;
    output_schema: OutputSchema | null;
    needs_family: boolean;
    family_conflicts?: { evaluation_id: string; evaluation_name: string; message: string }[] | null;
  };
  canEditContent: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: familyCatalog } = useEvaluationFamilies();
  const update = useUpdateDataset();
  const seedAgentIdRef = useRef<string | null>(null);
  const seed = useSeedGroundTruth(dataset.id, seedAgentIdRef);
  const { data: agents } = useAgents();
  const aiOff = useAiActionDisabled();
  const [family, setFamily] = useState<EvaluationFamily | "none">(dataset.family ?? "none");
  const [labelVocabulary, setLabelVocabulary] = useState<string[]>(dataset.label_vocabulary ?? []);
  const [hasSchema, setHasSchema] = useState(!!dataset.output_schema);
  const [schemaDef, setSchemaDef] = useState<string>(() =>
    JSON.stringify(dataset.output_schema?.definition ?? null, null, 2),
  );
  const [conflicts, setConflicts] = useState(dataset.family_conflicts ?? null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [seedPickerOpen, setSeedPickerOpen] = useState(false);
  const [seedPreview, setSeedPreview] = useState<{ found: boolean; output_schema?: OutputSchema; message?: string } | null>(null);
  const dirty = family !== (dataset.family ?? "none") ||
    JSON.stringify(labelVocabulary) !== JSON.stringify(dataset.label_vocabulary ?? []) ||
    hasSchema !== !!dataset.output_schema ||
    (hasSchema && schemaDef !== JSON.stringify(dataset.output_schema?.definition ?? null, null, 2));

  const familyTip =
    family === "none"
      ? t_i18n(EVALUATION_TERMS.needsFamily)
      : familyDescription(familyCatalog?.find((f) => f.id === family), t_i18n);

  // Only STRUCTURED_JSON has an agent-side declaration to seed from.
  const canSeed = family === "STRUCTURED_JSON";

  const handleSeedFromAgent = (agentId: string) => {
    setSeedPickerOpen(false);
    seedAgentIdRef.current = agentId;
    // `refetch()` resolves with `error` set and keeps the last `data`, so `error` is
    // checked first or a failed re-seed would redisplay the previous preview.
    seed.refetch().then((res) => {
      if (res.error) {
        toast({ title: res.error.message || t_i18n("Failed to seed from agent"), variant: "error" });
      } else if (res.data) {
        setSeedPreview(res.data);
      }
    });
  };

  const acceptSeedPreview = () => {
    if (!seedPreview?.output_schema) return;
    setHasSchema(true);
    setSchemaDef(JSON.stringify(seedPreview.output_schema.definition ?? null, null, 2));
    setSeedPreview(null);
  };

  const handleSave = () => {
    setConflicts(null);
    setSaveError(null);
    // `{}`, not `null`: on a partial update `null` means "leave the schema alone".
    let outputSchema: OutputSchema | Record<string, never> = {};
    if (family === "STRUCTURED_JSON" && hasSchema) {
      let definition: unknown;
      try {
        definition = JSON.parse(schemaDef);
      } catch {
        toast({ title: t_i18n("Schema definition must be valid JSON"), variant: "error" });
        return;
      }
      outputSchema = { type: "json_schema", definition };
    }
    update
      .mutateAsync({
        id: dataset.id,
        family: family === "none" ? null : family,
        label_vocabulary:
          family === "CLASSIFICATION" && labelVocabulary.length > 0 ? labelVocabulary : null,
        output_schema: outputSchema,
      })
      .then((updated) => {
        toast({ title: t_i18n("Family saved"), variant: "success" });
        setConflicts(updated.family_conflicts ?? null);
      })
      .catch((err: Error) => {
        setSaveError(err.message || t_i18n("Failed to save the family"));
      });
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base flex items-center gap-2">
          {t_i18n("Output Family")}
          {dataset.needs_family ? <NeedsFamilyBadge size="sm" /> : <FamilyBadge family={dataset.family} size="sm" />}
        </CardTitle>
        {canSeed && canEditContent && (
          <div className="relative">
            <AiDisabledTip>
              <Tip content={t_i18n(DATASET_TERMS.seedFromAgent)}>
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  aria-expanded={seedPickerOpen}
                  onClick={() => setSeedPickerOpen((v) => !v)}
                  disabled={seed.isFetching || aiOff}
                  aria-busy={seed.isFetching || undefined}
                >
                  {seed.isFetching && <Spinner size="sm" tone="inherit" />}
                  {t_i18n("Seed from Agent")}
                </Button>
              </Tip>
            </AiDisabledTip>
            {seedPickerOpen && (
              <div className="absolute right-0 z-50 mt-1 w-64 rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay max-h-52 overflow-y-auto">
                {(agents ?? []).length > 0 ? (
                  (agents ?? []).map((a) => (
                    <button
                      key={a.id}
                      type="button"
                      className="flex items-center gap-2 w-full px-3 py-2 text-left content-compact transition-colors hover:bg-elevation-hover"
                      onClick={() => handleSeedFromAgent(a.id)}
                    >
                      <span className="truncate">{a.name}</span>
                    </button>
                  ))
                ) : (
                  <p className="px-3 py-3 text-[11px] text-default-secondary text-center">
                    {t_i18n("No agents available.")}
                  </p>
                )}
              </div>
            )}
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-[11px] text-default-secondary">
          {t_i18n(
            "The output family every entry's expected answer is shaped for. An evaluation linked to a family-less dataset cannot be run, replayed, re-run, or rescored.",
          )}
        </p>
        <div className="flex items-center gap-1.5">
          <Select value={family} onValueChange={(v) => setFamily(v as EvaluationFamily | "none")} disabled={!canEditContent}>
            <SelectTrigger aria-label={t_i18n("Output Family")} className="w-[220px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">{t_i18n("None (needs family)")}</SelectItem>
              {(familyCatalog ?? []).map((f) => (
                <SelectItem key={f.id} value={f.id}>{t_i18n(f.label)}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {/* The picked family's catalog description, so each option explains itself once picked. */}
          {familyTip && <InfoTip content={familyTip} label={t_i18n(DATASET_TERMS.familyLabel)} />}
        </div>
        {family === "CLASSIFICATION" && (
          <div className="space-y-1.5">
            <label className="content-base font-medium">{t_i18n("Label Vocabulary")}</label>
            <p className="text-[11px] text-default-secondary">
              {t_i18n("The closed set of labels every entry's expected answer must be drawn from.")}
            </p>
            <TagInput
              value={labelVocabulary}
              onChange={setLabelVocabulary}
              placeholder={t_i18n("Add label…")}
              disabled={!canEditContent}
              // Case is kept so the author reads back what they declared (matching
              // folds it), and this closed set owes nothing to the platform's
              // tags.
              preserveCase
              suggestFromPlatformTags={false}
            />
          </div>
        )}
        {family === "STRUCTURED_JSON" && (
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label className="content-base font-medium">{t_i18n("JSON Schema")}</label>
              <Switch aria-label={t_i18n("JSON Schema")} checked={hasSchema} onCheckedChange={setHasSchema} disabled={!canEditContent} />
            </div>
            <p className="text-[11px] text-default-secondary">
              {t_i18n(
                "Declared once here and used by every evaluation that reuses this dataset. Per-field scoring compares each field as the schema declares it: without a JSON Schema, here or on the entry, a JSON entry gets no quality metric.",
              )}
            </p>
            {seedPreview && (
              <div className="rounded-sm border border-accent/30 bg-accent/[0.04] p-3 space-y-2">
                {seedPreview.found && seedPreview.output_schema ? (
                  <>
                    <p className="text-[11px] font-medium text-accent">
                      {t_i18n("Preview seeded from the agent's declaration: accept it, edit it below if needed and save, or discard it.")}
                    </p>
                    <pre className="text-[11px] font-mono-plex max-h-40 overflow-y-auto whitespace-pre-wrap break-words">
                      {JSON.stringify(seedPreview.output_schema.definition, null, 2)}
                    </pre>
                    <div className="flex justify-end gap-2">
                      <Button variant="ghost" size="sm" onClick={() => setSeedPreview(null)}>{t_i18n("Discard")}</Button>
                      <Button size="sm" onClick={acceptSeedPreview}>{t_i18n("Accept")}</Button>
                    </div>
                  </>
                ) : (
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-[11px] text-default-secondary">
                      {seedPreview.message ?? t_i18n("Nothing found to seed.")}
                    </p>
                    <Button variant="ghost" size="sm" onClick={() => setSeedPreview(null)}>{t_i18n("Dismiss")}</Button>
                  </div>
                )}
              </div>
            )}
            {hasSchema && (
              <textarea
                className={`w-full rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base font-mono-plex resize-y min-h-[120px] disabled:opacity-50 ${FIELD_FOCUS_RING}`}
                value={schemaDef}
                onChange={(e) => setSchemaDef(e.target.value)}
                disabled={!canEditContent}
                placeholder='{ "type": "object", "required": ["verdict"] }'
              />
            )}
          </div>
        )}
        {conflicts && conflicts.length > 0 && (
          <div className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/5 p-3 space-y-1">
            <p className="flex items-center gap-1 content-caption font-medium text-feedback-warning-primary">
              {t_i18n("This family now conflicts with linked evaluations (link kept, not removed):")}
              <InfoTip content={t_i18n(DATASET_TERMS.familyConflicts)} label={t_i18n(DATASET_TERMS.familyConflictsLabel)} />
            </p>
            {conflicts.map((c) => (
              <p key={c.evaluation_id} className="text-[11px] text-default-secondary">{c.message}</p>
            ))}
          </div>
        )}
        {saveError && (
          <div className="rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/[0.06] px-3 py-2">
            <p className="content-compact text-feedback-error-primary whitespace-pre-wrap break-words">{saveError}</p>
          </div>
        )}
        {canEditContent && (
          <div className="flex justify-end">
            <AiDisabledTip>
              <Button onClick={handleSave} disabled={!dirty || update.isPending || aiOff} aria-busy={update.isPending || undefined} size="sm">
                {update.isPending && <Spinner size="sm" tone="inherit" />}
                {t_i18n("Save changes")}
              </Button>
            </AiDisabledTip>
          </div>
        )}
      </CardContent>
    </Card>
  );
}


// ---------------------------------------------------------------------------
// Versions Tab (reproducibility snapshots)
// ---------------------------------------------------------------------------

function VersionsTab({ datasetId, currentVersion }: { datasetId: string; currentVersion: number }) {
  const { t_i18n, intl } = useFormatter();
  const { data: versions, isLoading } = useDatasetVersions(datasetId);
  const [selected, setSelected] = useState<number | null>(null);
  const { data: detail, isLoading: detailLoading } = useDatasetVersion(datasetId, selected ?? undefined);

  if (isLoading) return <div className="flex justify-center py-12"><Spinner size="md" label={t_i18n("Loading...")} /></div>;

  if (!versions || versions.length === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-center">
          <History className="h-8 w-8 mx-auto text-default-disabled mb-3" />
          <p className="content-base text-default-secondary">{t_i18n("No versions yet.")}</p>
          <p className="text-[11px] text-default-secondary mt-1">
            {t_i18n("A version is saved each time its entries, files, JSON schema or label vocabulary change, and when an evaluation first runs on this dataset.")}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-[280px_1fr]">
      <Card className="self-start">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-1.5">
            {t_i18n("Version History")}
            <InfoTip content={t_i18n(DATASET_TERMS.versionHistory)} label={t_i18n(DATASET_TERMS.versionHistoryLabel)} />
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-1.5">
          {versions.map((v) => (
            <button
              key={v.id}
              onClick={() => setSelected(v.version_number)}
              className={cn(
                "w-full flex items-center justify-between rounded-sm border px-3 py-2 text-left transition-colors",
                selected === v.version_number
                  ? "border-accent/50 bg-accent/5"
                  : "border-elevation-subtle-soft hover:bg-elevation-hover",
              )}
            >
              <div className="flex items-center gap-2">
                <span className="content-base font-medium">{t_i18n("v{revision}", { revision: v.version_number })}</span>
                {v.version_number === currentVersion && (
                  <Badge variant="outline" className="text-[9px] border-accent/40 text-accent">{t_i18n("Current")}</Badge>
                )}
              </div>
              <div className="text-right">
                <div className="content-caption text-default-secondary">{t_i18n("{count, plural, one {# entry} other {# entries}}", { count: v.entry_count })}</div>
                <div className="content-caption text-default-secondary">{formatTimeAgo(intl, v.created_at)}</div>
              </div>
            </button>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-1.5">
            {selected != null ? t_i18n("Version {version} snapshot", { version: selected }) : t_i18n("Select a version")}
            {selected != null && (
              <InfoTip content={t_i18n(DATASET_TERMS.versionSnapshot)} label={t_i18n(DATASET_TERMS.versionSnapshotLabel)} />
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {selected == null && (
            <p className="content-base text-default-secondary">{t_i18n("Choose a version on the left to inspect its immutable entry snapshot.")}</p>
          )}
          {selected != null && detailLoading && (
            <div className="flex justify-center py-8"><Spinner size="md" label={t_i18n("Loading...")} /></div>
          )}
          {selected != null && !detailLoading && detail && (
            <div className="space-y-3">
              <div className="text-[11px] text-default-secondary">
                {t_i18n("Captured {date} · {count, plural, one {# entry} other {# entries}}", {
                  date: formatShortDateTime(detail.created_at),
                  count: detail.entries.length,
                })}
              </div>
              {detail.entries.map((e, i) => (
                <div key={e.id} className="rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3 space-y-1.5">
                  <div className="content-caption font-medium text-default-secondary">{t_i18n("Entry {n}", { n: i + 1 })}</div>
                  <div>
                    <div className="content-caption uppercase tracking-wide text-default-secondary">{t_i18n("Input")}</div>
                    <p className="content-base whitespace-pre-wrap break-words">{e.input_prompt}</p>
                  </div>
                  <div>
                    <div className="content-caption uppercase tracking-wide text-default-secondary">{t_i18n("Expected")}</div>
                    <p className="content-base whitespace-pre-wrap break-words">{e.expected_answer}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function DatasetDetail() {
  const aiOff = useAiActionDisabled();
  const { t_i18n, intl } = useFormatter();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { data: dataset, isLoading } = useDataset(id!);
  const { isAdmin } = useAuth();
  const deleteDataset = useDeleteDataset();
  const [ConfirmEl, confirm] = useConfirm();
  const [tab] = useTabParam<Tab>("entries");
  const [exporting, setExporting] = useState(false);

  if (isLoading) return <DetailPageSkeleton backTo="/evaluations/datasets" backLabel="Back to Datasets" tabs={3} descriptionLines={1} actions={2} />;
  if (!dataset) return (
    <NotFoundState
      icon={Database}
      title="Dataset not found"
      description="This dataset does not exist or you do not have access."
      actionLabel="Back to Datasets"
      onAction={() => navigate("/evaluations/datasets")}
      backTo="/evaluations/datasets"
      backLabel="Back to Datasets"
    />
  );

  const canManageByVisibility = !dataset.is_company_managed || isAdmin;
  const canEditContent = canManageByVisibility && !dataset.is_read_only;
  const canToggleReadOnly = canManageByVisibility && isAdmin;

  const handleDelete = async () => {
    if (!(await confirm({
      title: "Delete dataset?",
      description: "This permanently deletes the dataset and all its entries, and unlinks it from every evaluation that uses it.",
      variant: "danger",
    }))) return;
    deleteDataset.mutateAsync(id!).then(() => {
      toast({ title: t_i18n("Dataset deleted"), variant: "success" });
      navigate("/evaluations/datasets");
    }).catch(() => {
      toast({ title: t_i18n("Failed to delete dataset"), variant: "error" });
    });
  };

  const handleExport = async () => {
    setExporting(true);
    try {
      await exportDataset(id!, dataset.name);
    } catch {
      toast({ title: t_i18n("Export failed"), variant: "error" });
    } finally {
      setExporting(false);
    }
  };

  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "entries", label: "Entries", icon: Database },
    { key: "versions", label: "Versions", icon: History },
    { key: "settings", label: "Settings", icon: Settings2 },
  ];

  return (
    <div className="space-y-6">
      {ConfirmEl}
      <div>
        <EntityPageHeader
          backTo="/evaluations/datasets"
          backLabel="Back to Datasets"
          icon={Database}
          title={dataset.name}
          description={dataset.description}
          badges={
            dataset.is_read_only && (
              <InfoLabel content={t_i18n(EVALUATION_TERMS.datasetReadOnly)}>
                <Badge variant="outline" className="content-compact">{t_i18n("Read-only")}</Badge>
              </InfoLabel>
            )
          }
          actions={
            <>
            <Tip content={t_i18n("Export as ZIP")}>
              <Button variant="outline" size="sm" className="gap-1.5" onClick={handleExport} disabled={exporting} aria-busy={exporting || undefined}>
                {exporting && <Spinner size="sm" tone="inherit" />}
                {!exporting && <Download className="h-3.5 w-3.5" />}
                {t_i18n("Export")}
              </Button>
            </Tip>
            {canEditContent && (
              <AiDisabledTip>
                <Button variant="ghost" size="sm" className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={handleDelete} disabled={aiOff}>
                  <Trash2 className="h-3.5 w-3.5" /> {t_i18n("Delete")}
                </Button>
              </AiDisabledTip>
            )}
            </>
          }
        />

        <div className="flex items-center gap-3 mt-3">
          <Badge variant="outline" className="content-caption">{t_i18n("{count, plural, one {# entry} other {# entries}}", { count: dataset.entry_count })}</Badge>
          {(dataset.current_version ?? 0) > 0 && (
            <InfoLabel content={t_i18n(EVALUATION_TERMS.datasetVersion)}>
              <Badge variant="outline" className="content-caption">
                <History className="h-2.5 w-2.5 mr-0.5" />
                {t_i18n("v{revision}", { revision: dataset.current_version })}
              </Badge>
            </InfoLabel>
          )}
          {dataset.evaluation_count > 0 && (
            <InfoLabel content={t_i18n(EVALUATION_TERMS.datasetEvaluationCount)}>
              <Badge variant="outline" className="content-caption">
                <FlaskConical className="h-2.5 w-2.5 mr-0.5" />
                {t_i18n("{count, plural, one {# evaluation} other {# evaluations}}", { count: dataset.evaluation_count })}
              </Badge>
            </InfoLabel>
          )}
          {dataset.needs_family ? (
            <NeedsFamilyBadge />
          ) : (
            <FamilyBadge family={dataset.family} />
          )}
          <span className="content-caption text-default-secondary ml-auto">{t_i18n("Updated {time}", { time: formatTimeAgo(intl, dataset.updated_at) })}</span>
        </div>
      </div>

      <DetailTabs tabs={tabs} active={tab} />

      <div>
        {tab === "entries" && (
          <EntriesTab
            datasetId={id!}
            entries={dataset.entries}
            canManage={canEditContent}
            globalInstructions={dataset.evaluation_instructions}
            family={dataset.family}
          />
        )}
        {tab === "versions" && (
          <VersionsTab datasetId={id!} currentVersion={dataset.current_version ?? 0} />
        )}
        {tab === "settings" && (
          <SettingsTab
            dataset={dataset}
            canEditContent={canEditContent}
            canToggleReadOnly={canToggleReadOnly}
          />
        )}
      </div>
    </div>
  );
}
