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
 * AnnotationDialog — what a rating says beyond its thumb.
 *
 * One dialog for every rateable thing: after a thumbs down it asks what went
 * wrong, and when admins have defined feedback criteria (Settings > Agents and automations > Quality)
 * it asks for a score per criterion, after either thumb. It writes nothing
 * itself: the caller saves `{ comment, scores }` through its own endpoint.
 */
import { useMemo, useState } from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { draftFromScores, parseScoreDraft, scaleSteps, type ScoreDraft } from "@/lib/feedback-criteria";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { AnnotationScore, FeedbackCriterion, MessageFeedbackRating } from "@/types";

export interface AnnotationDialogValue {
  comment: string | null;
  /** Left out when no criterion is on offer, so the stored scores are kept. */
  scores?: AnnotationScore[];
}

interface AnnotationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  rating: MessageFeedbackRating;
  /** The enabled criteria, in display order. */
  criteria: readonly FeedbackCriterion[];
  initialComment?: string | null;
  initialScores?: readonly AnnotationScore[];
  saving?: boolean;
  onSave: (value: AnnotationDialogValue) => void;
}

export function AnnotationDialog({ open, onOpenChange, ...formProps }: AnnotationDialogProps) {
  const { t_i18n } = useFormatter();
  const scored = formProps.criteria.length > 0;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        {/* The title is written here, inside DialogContent's own JSX: the
            library finds it by walking that tree, and a title rendered by a
            child component is invisible to it. */}
        <DialogHeader>
          <DialogTitle>
            {scored ? t_i18n("Rate this result") : t_i18n("Tell us what went wrong (optional)")}
          </DialogTitle>
          {scored && (
            <DialogDescription>{t_i18n("Score what matters to you and skip the rest.")}</DialogDescription>
          )}
        </DialogHeader>
        {/* Mounted only while open, so each opening starts from the stored rating. */}
        <AnnotationForm {...formProps} onCancel={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}

function AnnotationForm({
  rating,
  criteria,
  initialComment,
  initialScores,
  saving,
  onSave,
  onCancel,
}: Omit<AnnotationDialogProps, "open" | "onOpenChange"> & { onCancel: () => void }) {
  const { t_i18n } = useFormatter();
  const [comment, setComment] = useState(initialComment ?? "");
  const [draft, setDraft] = useState<ScoreDraft>(() => draftFromScores(initialScores));
  const parsed = useMemo(() => parseScoreDraft(criteria, draft), [criteria, draft]);
  const scored = criteria.length > 0;
  const askComment = rating === "negative";
  const blocked = Object.keys(parsed.problems).length > 0;

  const setValue = (criterionId: string, value: string) =>
    setDraft((current) => ({ ...current, [criterionId]: value }));
  // Picking the chosen option or step again clears it.
  const toggleValue = (criterionId: string, value: string) =>
    setDraft((current) => ({ ...current, [criterionId]: current[criterionId] === value ? "" : value }));

  const save = () => {
    if (blocked) return;
    onSave({
      comment: askComment ? comment.trim() || null : null,
      ...(scored ? { scores: parsed.scores } : {}),
    });
  };

  return (
    <>
      <div className="space-y-4">
        {criteria.map((criterion) => (
          <div key={criterion.id} className="space-y-1.5">
            <div>
              <p className="content-base font-medium" id={`criterion-${criterion.id}`}>
                {criterion.name}
              </p>
              {criterion.description && (
                <p className="content-compact text-default-secondary">{criterion.description}</p>
              )}
            </div>
            {criterion.type === "numeric" ? (
              <NumericInput
                criterion={criterion}
                value={draft[criterion.id] ?? ""}
                problem={parsed.problems[criterion.id]}
                onChange={(value) => setValue(criterion.id, value)}
              />
            ) : (
              <div
                role="group"
                aria-labelledby={`criterion-${criterion.id}`}
                className="flex flex-wrap gap-1.5"
              >
                {(criterion.type === "categorical"
                  ? (criterion.options ?? []).map((option) => ({
                      value: option.value,
                      label: option.label || option.value,
                    }))
                  : scaleSteps(criterion).map((step) => ({ value: String(step), label: String(step) }))
                ).map((choice) => {
                  const selected = draft[criterion.id] === choice.value;
                  return (
                    <button
                      key={choice.value}
                      type="button"
                      aria-pressed={selected}
                      onClick={() => toggleValue(criterion.id, choice.value)}
                      className={cn(
                        "rounded-sm border px-2.5 py-1 content-compact transition-colors",
                        criterion.type === "scale" && "min-w-8",
                        selected
                          ? "border-accent bg-accent/15 text-accent"
                          : "border-elevation-subtle text-default-secondary hover:border-accent/50 hover:text-default-primary",
                      )}
                    >
                      {choice.label}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        ))}
        {askComment && (
          <textarea
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder={t_i18n("What could be improved?")}
            aria-label={t_i18n("What could be improved?")}
            maxLength={2000}
            className="min-h-24 w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base outline-none ring-offset-focus placeholder:text-default-secondary focus-visible:ring-2 focus-visible:ring-focus"
          />
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel}>
            {t_i18n("Skip")}
          </Button>
          <Button onClick={save} disabled={saving || blocked} className="bg-accent text-negative-primary hover:bg-accent/90">
            {t_i18n("Save")}
          </Button>
        </div>
      </div>
    </>
  );
}

function NumericInput({
  criterion,
  value,
  problem,
  onChange,
}: {
  criterion: FeedbackCriterion;
  value: string;
  problem: string | undefined;
  onChange: (value: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const hintId = `criterion-${criterion.id}-hint`;
  const range = t_i18n("From {min} to {max}", { min: criterion.min_value ?? 0, max: criterion.max_value ?? 0 });
  return (
    <div className="space-y-1">
      <Input
        type="number"
        inputMode="decimal"
        step="any"
        min={criterion.min_value ?? undefined}
        max={criterion.max_value ?? undefined}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-labelledby={`criterion-${criterion.id}`}
        aria-describedby={hintId}
        aria-invalid={problem ? true : undefined}
        className="h-8 w-32"
      />
      <p id={hintId} className={cn("text-[11px]", problem ? "text-feedback-error-primary" : "text-default-secondary")}>
        {problem === "not_a_number" ? t_i18n("Enter a number.") : range}
      </p>
    </div>
  );
}
