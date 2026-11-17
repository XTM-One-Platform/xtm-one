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
 * Settings > Agents and automations > Quality: the feedback criteria.
 *
 * The dimensions people score when they rate an agent's work, on top of the
 * thumbs: each has a kind (scale, options, number), a weight in the weighted
 * score, and can be disabled to stop asking for it while keeping its history.
 * Admin-only, like the page tab; the server enforces it.
 */
import { useState } from "react";
import { ClipboardCheck, Pencil, Plus, Trash2, X } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import {
  useCreateFeedbackCriterion,
  useDeleteFeedbackCriterion,
  useFeedbackCriteria,
  useUpdateFeedbackCriterion,
} from "@/hooks/api/useFeedbackCriteriaApi";
import { FEEDBACK_CRITERION_TYPE_CONFIG, FEEDBACK_CRITERION_TYPES } from "@/lib/constants";
import {
  criterionFormDraft,
  criterionFormProblem,
  criterionPayload,
  criterionUpdatePayload,
  type CriterionFormDraft,
  type CriterionFormProblem,
} from "@/lib/feedback-criteria";
import { useFormatter } from "@/i18n";
import type { FeedbackCriterion, FeedbackCriterionType } from "@/types";
import { Spinner } from "@filigran/design-system";

const PROBLEM_MESSAGES: Record<CriterionFormProblem, string> = {
  name: "Give the criterion a name.",
  weight: "The weight is a number of 0 or more.",
  options: "Give at least two options, each with a value and a score from 0 to 1.",
  bounds: "Give a minimum below the maximum (whole numbers for a scale).",
};

/**
 * Widths in px under the fixed layout (slice 9 of #9378): the Criterion column takes what
 * is left and cuts a long name or description with the shared `TruncatedText` tooltip, so
 * the card never scrolls sideways at 1024.
 */
export const CRITERIA_COLUMN_WIDTHS = {
  type: 128,
  values: 200,
  weight: 88,
  enabled: 96,
} as const;

/** The criterion's state, as the Status filter reads it. */
const stateOf = (criterion: FeedbackCriterion) => (criterion.enabled ? "enabled" : "disabled");

function describeRange(criterion: FeedbackCriterion, t_i18n: ReturnType<typeof useFormatter>["t_i18n"]) {
  if (criterion.type === "categorical") {
    return (criterion.options ?? []).map((option) => option.label || option.value).join(", ");
  }
  return t_i18n("From {min} to {max}", { min: criterion.min_value ?? 0, max: criterion.max_value ?? 0 });
}

export function FeedbackCriteriaSection() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const {
    data: criteria = [],
    isLoading,
    isError,
    refetch,
  } = useFeedbackCriteria({ includeDisabled: true, throwOnError: false });
  const update = useUpdateFeedbackCriterion();
  const remove = useDeleteFeedbackCriterion();
  const [editing, setEditing] = useState<FeedbackCriterion | "new" | null>(null);

  const reportFailure = (error: unknown) =>
    toast({
      title: t_i18n("Could not save the criterion"),
      description: error instanceof Error ? error.message : undefined,
      variant: "error",
    });

  const toggleEnabled = (criterion: FeedbackCriterion, enabled: boolean) =>
    update.mutate({ id: criterion.id, enabled }, { onError: reportFailure });

  const deleteCriterion = async (criterion: FeedbackCriterion) => {
    const confirmed = await confirm({
      title: "Delete this criterion?",
      description:
        "Every score given on this criterion is deleted with it. Disable it instead to stop asking for it and keep its history.",
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!confirmed) return;
    remove.mutate(criterion.id, {
      onSuccess: () => toast({ title: t_i18n("Criterion deleted"), variant: "success" }),
      onError: reportFailure,
    });
  };

  const columns: DataTableColumn<FeedbackCriterion>[] = [
    {
      key: "name",
      header: "Criterion",
      cell: (criterion) => (
        <div className="min-w-0">
          <TruncatedText text={criterion.name} className="font-medium" />
          {criterion.description && (
            <TruncatedText text={criterion.description} className="content-compact text-default-secondary" />
          )}
        </div>
      ),
      sortValue: (criterion) => criterion.name.toLowerCase(),
      firstSort: "asc",
      csv: (criterion) => criterion.name,
    },
    {
      key: "type",
      header: "Type",
      width: CRITERIA_COLUMN_WIDTHS.type,
      cell: (criterion) => (
        <Badge variant="outline" className="whitespace-nowrap">
          {t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[criterion.type].label)}
        </Badge>
      ),
      sortValue: (criterion) => t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[criterion.type].label),
      csv: (criterion) => criterion.type,
    },
    {
      key: "values",
      header: "Values",
      width: CRITERIA_COLUMN_WIDTHS.values,
      hideable: true,
      cell: (criterion) => (
        <TruncatedText text={describeRange(criterion, t_i18n)} className="content-compact text-default-secondary" />
      ),
      csv: (criterion) => describeRange(criterion, t_i18n),
    },
    {
      key: "weight",
      header: "Weight",
      align: "right",
      width: CRITERIA_COLUMN_WIDTHS.weight,
      cell: (criterion) => criterion.weight,
      sortValue: (criterion) => criterion.weight,
      csv: (criterion) => criterion.weight,
    },
    {
      key: "enabled",
      header: "Enabled",
      width: CRITERIA_COLUMN_WIDTHS.enabled,
      interactive: true,
      cell: (criterion) => (
        <Switch
          checked={criterion.enabled}
          onCheckedChange={(enabled) => toggleEnabled(criterion, enabled)}
          disabled={update.isPending}
          aria-label={t_i18n("Enabled")}
        />
      ),
      sortValue: (criterion) => criterion.enabled,
      csv: (criterion) => criterion.enabled,
    },
  ];

  const filters: DataTableFilter<FeedbackCriterion>[] = [
    {
      key: "type",
      label: "Type",
      values: (criterion) => [criterion.type],
      options: FEEDBACK_CRITERION_TYPES.map((type) => ({ value: type, label: t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[type].label) })),
    },
    {
      key: "status",
      label: "Status",
      values: (criterion) => [stateOf(criterion)],
      options: [
        { value: "enabled", label: t_i18n("Enabled") },
        { value: "disabled", label: t_i18n("Disabled") },
      ],
    },
  ];

  const criterionActions = (criterion: FeedbackCriterion) => (
    <span className="inline-flex gap-1">
      <Tip content={t_i18n("Edit")}>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Edit")} onClick={() => setEditing(criterion)}>
          <Pencil className="h-4 w-4" />
        </Button>
      </Tip>
      <Tip content={t_i18n("Delete")}>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t_i18n("Delete")}
          onClick={() => void deleteCriterion(criterion)}
          disabled={remove.isPending}
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      </Tip>
    </span>
  );

  return (
    <>
      {ConfirmEl}
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-4">
            <div>
              <CardTitle className="flex items-center gap-2">
                <ClipboardCheck className="h-4 w-4 text-accent" />
                {t_i18n("Feedback criteria")}
              </CardTitle>
              <CardDescription className="mt-1">
                {t_i18n(
                  "What people score when they rate an agent's work, on top of the thumbs. Each criterion counts in the weighted score by its weight; with no criterion, a rating is worth its thumb.",
                )}
              </CardDescription>
            </div>
            <Button onClick={() => setEditing("new")} className="shrink-0 gap-2">
              <Plus className="h-4 w-4" />
              {t_i18n("Add criterion")}
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <DataTable
            rows={criteria}
            columns={columns}
            layout="fixed"
            rowKey={(criterion) => criterion.id}
            defaultSort={{ key: "name", direction: "asc" }}
            searchFields={(criterion) => [criterion.name, criterion.key, criterion.description]}
            searchPlaceholder={t_i18n("Search...")}
            filters={filters}
            exportName="feedback-criteria"
            urlKey="sfc_"
            storageKey="settings-feedback-criteria"
            isLoading={isLoading}
            isError={isError}
            onRetry={() => void refetch()}
            emptyLabel={t_i18n("No feedback criteria yet: ratings are scored by their thumbs alone.")}
            rowTestId="feedback-criterion-row"
            rowActions={criterionActions}
          />
        </CardContent>
      </Card>

      <CriterionDialog
        criterion={editing === "new" ? null : editing}
        open={editing !== null}
        nextSortOrder={criteria.reduce((highest, criterion) => Math.max(highest, criterion.sort_order + 1), 0)}
        onClose={() => setEditing(null)}
      />
    </>
  );
}

function CriterionDialog({
  criterion,
  open,
  nextSortOrder,
  onClose,
}: {
  criterion: FeedbackCriterion | null;
  open: boolean;
  nextSortOrder: number;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{criterion ? t_i18n("Edit criterion") : t_i18n("New criterion")}</DialogTitle>
          <DialogDescription>
            {t_i18n("People score it in the rating dialog, after the thumbs.")}
          </DialogDescription>
        </DialogHeader>
        {/* Mounted only while open, so each opening starts from the stored definition. */}
        <CriterionForm criterion={criterion} nextSortOrder={nextSortOrder} onClose={onClose} />
      </DialogContent>
    </Dialog>
  );
}

function CriterionForm({
  criterion,
  nextSortOrder,
  onClose,
}: {
  criterion: FeedbackCriterion | null;
  nextSortOrder: number;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const create = useCreateFeedbackCriterion();
  const update = useUpdateFeedbackCriterion();
  const [form, setForm] = useState<CriterionFormDraft>(() => criterionFormDraft(criterion));
  const problem = criterionFormProblem(form);
  const saving = create.isPending || update.isPending;

  const patch = (changes: Partial<CriterionFormDraft>) => setForm((current) => ({ ...current, ...changes }));
  const patchOption = (index: number, changes: Partial<CriterionFormDraft["options"][number]>) =>
    setForm((current) => ({
      ...current,
      options: current.options.map((option, position) => (position === index ? { ...option, ...changes } : option)),
    }));

  const save = () => {
    if (problem) return;
    const callbacks = {
      onSuccess: () => {
        toast({ title: t_i18n("Criterion saved"), variant: "success" });
        onClose();
      },
      onError: (error: unknown) =>
        toast({
          title: t_i18n("Could not save the criterion"),
          description: error instanceof Error ? error.message : undefined,
          variant: "error",
        }),
    };
    if (criterion) {
      update.mutate({ id: criterion.id, ...criterionUpdatePayload(form) }, callbacks);
    } else {
      create.mutate({ ...criterionPayload(form), sort_order: nextSortOrder }, callbacks);
    }
  };

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="criterion-name">{t_i18n("Name")}</Label>
        <Input aria-label={t_i18n("Name")}
          id="criterion-name"
          value={form.name}
          maxLength={100}
          onChange={(event) => patch({ name: event.target.value })}
          placeholder={t_i18n("Accuracy")}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="criterion-description">{t_i18n("Description")}</Label>
        <Input aria-label={t_i18n("Description")}
          id="criterion-description"
          value={form.description}
          maxLength={2000}
          onChange={(event) => patch({ description: event.target.value })}
          placeholder={t_i18n("Shown under the criterion in the rating dialog")}
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label>{t_i18n("Type")}</Label>
          <Select
            value={form.type}
            onValueChange={(value) => patch({ type: value as FeedbackCriterionType })}
            disabled={criterion !== null}
          >
            <SelectTrigger className="w-full" aria-label={t_i18n("Type")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {FEEDBACK_CRITERION_TYPES.map((type) => (
                <SelectItem key={type} value={type}>
                  {t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[type].label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="criterion-weight">{t_i18n("Weight")}</Label>
          <Input aria-label={t_i18n("Weight")}
            id="criterion-weight"
            type="number"
            min={0}
            step="any"
            value={form.weight}
            onChange={(event) => patch({ weight: event.target.value })}
          />
        </div>
      </div>
      <p className="content-compact text-default-secondary">
        {t_i18n(FEEDBACK_CRITERION_TYPE_CONFIG[form.type].description)}
        {criterion && ` ${t_i18n("The type cannot change once the criterion exists.")}`}
      </p>

      {form.type === "categorical" ? (
        <div className="space-y-2">
          <div className="grid grid-cols-[1fr_1fr_5rem_2rem] gap-2 content-compact text-default-secondary">
            <span>{t_i18n("Value")}</span>
            <span>{t_i18n("Label")}</span>
            <span>{t_i18n("Score (0-1)")}</span>
            <span />
          </div>
          {form.options.map((option, index) => (
            <div key={index} className="grid grid-cols-[1fr_1fr_5rem_2rem] gap-2">
              <Input
                value={option.value}
                maxLength={100}
                aria-label={t_i18n("Value")}
                onChange={(event) => patchOption(index, { value: event.target.value })}
                className="h-8"
              />
              <Input
                value={option.label}
                maxLength={100}
                aria-label={t_i18n("Label")}
                onChange={(event) => patchOption(index, { label: event.target.value })}
                className="h-8"
              />
              <Input
                type="number"
                min={0}
                max={1}
                step="any"
                value={option.score}
                aria-label={t_i18n("Score (0-1)")}
                onChange={(event) => patchOption(index, { score: event.target.value })}
                className="h-8"
              />
              <Tip content={t_i18n("Remove option")}>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  aria-label={t_i18n("Remove option")}
                  disabled={form.options.length <= 2}
                  onClick={() => patch({ options: form.options.filter((_, position) => position !== index) })}
                >
                  <X className="h-4 w-4" />
                </Button>
              </Tip>
            </div>
          ))}
          <Button
            variant="ghost"
            size="sm"
            className="gap-1"
            disabled={form.options.length >= 20}
            onClick={() => patch({ options: [...form.options, { value: "", label: "", score: "" }] })}
          >
            <Plus className="h-3.5 w-3.5" />
            {t_i18n("Add option")}
          </Button>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="criterion-min">{t_i18n("Minimum")}</Label>
            <Input aria-label={t_i18n("Minimum")}
              id="criterion-min"
              type="number"
              step={form.type === "scale" ? 1 : "any"}
              value={form.minValue}
              onChange={(event) => patch({ minValue: event.target.value })}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="criterion-max">{t_i18n("Maximum")}</Label>
            <Input aria-label={t_i18n("Maximum")}
              id="criterion-max"
              type="number"
              step={form.type === "scale" ? 1 : "any"}
              value={form.maxValue}
              onChange={(event) => patch({ maxValue: event.target.value })}
            />
          </div>
        </div>
      )}

      <div className="flex items-center gap-2">
        <Switch
          id="criterion-enabled"
          checked={form.enabled}
          onCheckedChange={(enabled) => patch({ enabled })}
        />
        <Label htmlFor="criterion-enabled">{t_i18n("Ask for it in the rating dialog")}</Label>
      </div>

      {problem && <p className="content-compact text-feedback-warning-primary">{t_i18n(PROBLEM_MESSAGES[problem])}</p>}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          {t_i18n("Cancel")}
        </Button>
        <Button onClick={save} disabled={saving || problem !== null} className="bg-accent text-negative-primary hover:bg-accent/90" aria-busy={saving || undefined}>
          {saving && <Spinner size="sm" tone="inherit" />}
          {t_i18n("Save")}
        </Button>
      </div>
    </div>
  );
}
