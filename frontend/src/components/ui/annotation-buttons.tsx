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

import { Icon } from "@filigran/design-system";
/**
 * AnnotationButtons — rate a piece of agent work with a thumbs up or down.
 *
 * The chat bubble's `MessageFeedbackButtons`, for every other thing an agent
 * produces: an assignment run's result, a background task, a delegation's
 * answer. The rating is the caller's own and is shown as they left it; clicking
 * the active thumb retracts it, and a thumbs down asks, optionally, what went
 * wrong. When admins have defined feedback criteria, either thumb also
 * opens the dialog to score them. Clicks never reach an enclosing clickable card.
 */
import { useState } from "react";
import { Tip } from "@/components/ui/tooltip";
import { AnnotationDialog, type AnnotationDialogValue } from "@/components/ui/annotation-dialog";
import { useToast } from "@/components/ui/toast";
import { useDeleteAnnotation, useMyAnnotation, useUpsertAnnotation } from "@/hooks/api/useAnnotationApi";
import { useRatingCriteria } from "@/hooks/api/useFeedbackCriteriaApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { AnnotationTargetType, MessageFeedbackRating } from "@/types";

interface AnnotationButtonsProps {
  targetType: AnnotationTargetType;
  targetId: string;
  /** Short prompt shown before the thumbs (an i18n key). */
  label?: string;
  className?: string;
  /** The page loads the caller's ratings of all its rows at once
   *  (`useMyAnnotations`), so this row does not fetch its own. */
  batched?: boolean;
}

export function AnnotationButtons({ targetType, targetId, label, className, batched = false }: AnnotationButtonsProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // Pending, not loading: a batched row's own query never runs, and its rating
  // is unknown until the page's batch has seeded it.
  const { data: annotation, isPending } = useMyAnnotation(targetType, targetId, { enabled: !batched });
  const { criteria, resolveCriteria } = useRatingCriteria();
  const upsert = useUpsertAnnotation(targetType, targetId);
  const retract = useDeleteAnnotation(targetType, targetId);
  const [dialogRating, setDialogRating] = useState<MessageFeedbackRating | null>(null);

  const active = annotation?.rating ?? null;
  const busy = isPending || upsert.isPending || retract.isPending;

  const reportFailure = (error: unknown, fallback: string) => {
    toast({
      title: t_i18n("Feedback failed"),
      description: error instanceof Error ? error.message : t_i18n(fallback),
      variant: "error",
    });
  };

  const vote = (rating: MessageFeedbackRating) => {
    if (active === rating) {
      setDialogRating(null);
      retract.mutate(undefined, {
        onError: (error) => reportFailure(error, "Could not retract feedback."),
      });
      return;
    }
    // A thumbs down keeps the comment it already carries; a thumbs up drops it.
    // The scores are not sent, so the thumb alone never loses them.
    const keptComment = rating === "negative" ? annotation?.comment ?? null : null;
    upsert.mutate(
      { rating, comment: keptComment },
      {
        onSuccess: async () => {
          if (rating === "negative" || (await resolveCriteria()).length > 0) setDialogRating(rating);
        },
        onError: (error) => reportFailure(error, "Could not submit feedback."),
      },
    );
  };

  const saveDialog = (value: AnnotationDialogValue) => {
    if (!dialogRating) return;
    upsert.mutate(
      { rating: dialogRating, ...value },
      {
        onSuccess: () => setDialogRating(null),
        onError: (error) => reportFailure(error, "Could not submit feedback."),
      },
    );
  };

  const thumbClass = (rating: MessageFeedbackRating) =>
    cn(
      "inline-flex items-center justify-center rounded-sm p-1 transition-colors disabled:cursor-not-allowed disabled:opacity-50",
      active === rating ? "text-accent" : "text-default-secondary hover:text-accent",
    );

  return (
    <div
      className={cn("flex items-center gap-1", className)}
      // The thumbs sit on clickable rows (a background task opens its run):
      // a vote, or a click in the dialog portaled from here, must not also
      // open the row.
      onClick={(event) => event.stopPropagation()}
    >
      {label && <span className="mr-1 content-caption text-default-secondary">{t_i18n(label)}</span>}
      <Tip content={t_i18n("Helpful response")}>
        <button
          type="button"
          aria-label={t_i18n("Thumbs up")}
          aria-pressed={active === "positive"}
          disabled={busy}
          onClick={() => vote("positive")}
          className={thumbClass("positive")}
        >
          <Icon name="thumbs-up" size={16} className={cn(active === "positive" && "fill-current")} />
        </button>
      </Tip>
      <Tip content={t_i18n("Not helpful")}>
        <button
          type="button"
          aria-label={t_i18n("Thumbs down")}
          aria-pressed={active === "negative"}
          disabled={busy}
          onClick={() => vote("negative")}
          className={thumbClass("negative")}
        >
          <Icon name="thumbs-down" size={16} className={cn(active === "negative" && "fill-current")} />
        </button>
      </Tip>

      <AnnotationDialog
        open={dialogRating !== null}
        onOpenChange={(open) => {
          if (!open) setDialogRating(null);
        }}
        rating={dialogRating ?? "negative"}
        criteria={criteria}
        initialComment={annotation?.comment}
        initialScores={annotation?.scores}
        saving={upsert.isPending}
        onSave={saveDialog}
      />
    </div>
  );
}
