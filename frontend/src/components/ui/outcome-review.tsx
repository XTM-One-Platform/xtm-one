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
 * OutcomeReview: the ROI review row of a run or a work item (#9365, design
 * `dev-docs/roi-outcomes.md` section 6.2).
 *
 * Distinct from `AnnotationButtons` ("Was this result useful?"), which is the
 * reader's feedback and moves no figure: this row marks the ROI record
 * accepted, modified or rejected, and the ROI views count it that way. It shows
 * no figure (no hours, no value), only the review itself, and only to a reader
 * the server says may review (`can_review`).
 *
 * Three toggle buttons with `aria-pressed`, none pressed while unreviewed;
 * clicking the pressed one returns the record to unreviewed. Modified is never
 * saved on the click: it opens the review time field (a help line names the
 * unit's default minutes) and only Save review writes it, once the minutes are
 * valid. A note, optional, goes with any outcome. Form controls are the
 * full-size ones (36 px, body text): the forms standard.
 */
import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/toast";
import { useReviewRoiOutcome, type RoiOutcomeReview } from "@/hooks/api/useRoiReviewApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { RoiOutcome, RoiReviewBlock } from "@/types";

const NOTE_MAX = 500;

/** The label of each outcome, read through `t_i18n`. @i18n-keys */
const OUTCOME_LABELS = {
  accepted: "Accepted",
  modified: "Modified",
  rejected: "Rejected",
} as const satisfies Record<RoiOutcome, string>;

/** The outcomes in their order. */
const OUTCOMES = (["accepted", "modified", "rejected"] as const).map((key) => ({ key, label: OUTCOME_LABELS[key] }));

interface OutcomeReviewProps {
  /** The `roi` block of the run or work item response; nothing renders without it. */
  block: RoiReviewBlock | null | undefined;
  className?: string;
}

export function OutcomeReview({ block, className }: OutcomeReviewProps) {
  if (!block || !block.can_review) return null;
  return <OutcomeReviewRow block={block} className={className} />;
}

function OutcomeReviewRow({ block, className }: { block: RoiReviewBlock; className?: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const review = useReviewRoiOutcome(block.record_id);
  const ids = useId();
  const minutesRef = useRef<HTMLInputElement | null>(null);
  const [minutes, setMinutes] = useState(block.review_min != null ? String(block.review_min) : "");
  const [note, setNote] = useState(block.review_note ?? "");
  const [minutesError, setMinutesError] = useState<string | null>(null);
  // Modified picked but not saved yet: it needs the review time first, so the
  // click opens the field and only Save review writes it (UX round 1, B3).
  const [pendingModified, setPendingModified] = useState(false);

  // A new record or a review saved elsewhere resets the fields.
  useEffect(() => {
    setMinutes(block.review_min != null ? String(block.review_min) : "");
    setNote(block.review_note ?? "");
    setMinutesError(null);
    setPendingModified(false);
  }, [block.record_id, block.outcome, block.review_min, block.review_note]);

  const saved = block.outcome;
  const active: RoiOutcome | null = pendingModified ? "modified" : saved;

  const send = (body: RoiOutcomeReview) =>
    review.mutate(body, {
      onSuccess: () => toast({ title: t_i18n("ROI outcome saved"), variant: "success" }),
      onError: (error) =>
        toast({
          title: t_i18n("Could not save the ROI outcome"),
          description: error instanceof Error ? error.message : undefined,
          variant: "error",
        }),
    });

  const parseMinutes = (input: string): number | "invalid" => {
    const raw = input.trim();
    if (raw === "") return "invalid";
    const value = Number(raw);
    return Number.isFinite(value) && value >= 0 ? value : "invalid";
  };
  const parsedMinutes = () => parseMinutes(minutes);

  // The error replaces the help line while it is set; a valid value brings the
  // help back at once (UX round 3, B1).
  const changeMinutes = (value: string) => {
    setMinutes(value);
    if (minutesError && parseMinutes(value) !== "invalid") setMinutesError(null);
  };

  const choose = (outcome: RoiOutcome) => {
    setMinutesError(null);
    if (pendingModified && outcome === "modified") {
      // A second click on the unsaved Modified cancels it.
      setPendingModified(false);
      return;
    }
    if (!pendingModified && saved === outcome) {
      send({ outcome: null });
      return;
    }
    if (outcome === "modified") {
      // Never saved on the click: the review time is asked first.
      setPendingModified(true);
      requestAnimationFrame(() => minutesRef.current?.focus());
      return;
    }
    setPendingModified(false);
    send({ outcome, note: note.trim() || null });
  };

  const save = () => {
    if (!active) return;
    const body: RoiOutcomeReview = { outcome: active, note: note.trim() || null };
    if (active === "modified") {
      const value = parsedMinutes();
      if (value === "invalid") {
        setMinutesError(t_i18n("Enter the review time in minutes, 0 or more."));
        minutesRef.current?.focus();
        return;
      }
      body.review_min = value;
    }
    setMinutesError(null);
    send(body);
  };

  const minutesId = `${ids}-minutes`;
  const minutesHelpId = `${ids}-minutes-help`;
  const minutesErrorId = `${ids}-minutes-error`;
  const noteId = `${ids}-note`;
  const noteCountId = `${ids}-note-count`;
  const busy = review.isPending;

  return (
    <div className={cn("space-y-3 border-t border-elevation-subtle-soft pt-3", className)} data-testid="outcome-review">
      <div className="space-y-1">
        <span className="content-base font-medium text-default-primary" id={`${ids}-label`}>
          {t_i18n("ROI outcome")}
        </span>
        <p className="content-compact text-default-secondary" data-testid="outcome-review-unit">
          {t_i18n("Unit: {unit}", { unit: block.unit_name })}
        </p>
      </div>
      <div role="group" aria-labelledby={`${ids}-label`} className="flex flex-wrap gap-2">
        {OUTCOMES.map((opt) => {
          const pressed = active === opt.key;
          return (
            <Button
              key={opt.key}
              type="button"
              variant={pressed ? "default" : "outline"}
              aria-pressed={pressed}
              aria-busy={busy || undefined}
              disabled={busy}
              data-outcome={opt.key}
              onClick={() => choose(opt.key)}
            >
              {t_i18n(opt.label)}
            </Button>
          );
        })}
      </div>

      {active === null && (
        <p className="content-compact text-default-secondary" data-testid="outcome-review-unreviewed">
          {t_i18n("Unreviewed: counted as delivered in ROI until someone reviews it.")}
        </p>
      )}

      {active !== null && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-start gap-4">
            {active === "modified" && (
              <div className="space-y-1.5">
                <Label htmlFor={minutesId} id={`${minutesId}-label`}>
                  {t_i18n("Review time")}
                </Label>
                <div className="flex items-center gap-2">
                  <Input
                    id={minutesId}
                    aria-labelledby={`${minutesId}-label`}
                    ref={minutesRef}
                    type="number"
                    inputMode="decimal"
                    min={0}
                    step="any"
                    className="w-28"
                    value={minutes}
                    aria-invalid={minutesError ? true : undefined}
                    aria-describedby={minutesError ? minutesErrorId : minutesHelpId}
                    onChange={(event) => changeMinutes(event.target.value)}
                  />
                  <span className="content-base text-default-secondary">{t_i18n("min")}</span>
                </div>
                {/* The error takes the help line's place, at the same size (Forms rule). */}
                {minutesError ? (
                  <p id={minutesErrorId} className="content-compact text-feedback-error-primary" data-testid="outcome-review-minutes-error">
                    {minutesError}
                  </p>
                ) : (
                  <p id={minutesHelpId} className="content-compact text-default-secondary" data-testid="outcome-review-minutes-help">
                    {block.default_review_min != null
                      ? t_i18n("Minutes a person spent checking or fixing this result. Default for this unit: {minutes}.", {
                          minutes: block.default_review_min,
                        })
                      : t_i18n("Minutes a person spent checking or fixing this result.")}
                  </p>
                )}
              </div>
            )}
            <div className="min-w-0 flex-1 space-y-1.5">
              <Label htmlFor={noteId} id={`${noteId}-label`}>
                {t_i18n("Note (optional)")}
              </Label>
              {/* No maxLength on the Input: the design-system Input draws its own
                  10 px counter when it gets one, which doubled this 12 px counter
                  (UX round 2, B1). The value is capped here instead. */}
              <Input
                id={noteId}
                aria-labelledby={`${noteId}-label`}
                value={note}
                aria-describedby={noteCountId}
                onChange={(event) => setNote(event.target.value.slice(0, NOTE_MAX))}
              />
              <p id={noteCountId} className="content-compact text-default-secondary tabular-nums" data-testid="outcome-review-note-count">
                {note.length} / {NOTE_MAX}
              </p>
            </div>
          </div>
          <Button type="button" onClick={save} disabled={busy} aria-busy={busy || undefined} data-testid="outcome-review-save">
            {t_i18n("Save review")}
          </Button>
        </div>
      )}
    </div>
  );
}
