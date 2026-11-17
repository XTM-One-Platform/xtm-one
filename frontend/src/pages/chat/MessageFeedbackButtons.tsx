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
import { useQueryClient } from "@tanstack/react-query";
import { Icon, IconButton } from "@filigran/design-system";
import { Tip } from "@/components/ui/tooltip";
import { AnnotationDialog, type AnnotationDialogValue } from "@/components/ui/annotation-dialog";
import { useToast } from "@/components/ui/toast";
import { useUpsertAnnotation } from "@/hooks/api/useAnnotationApi";
import { useRatingCriteria } from "@/hooks/api/useFeedbackCriteriaApi";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { AnnotationScore, MessageFeedback, MessageFeedbackRating } from "@/types";

interface MessageFeedbackButtonsProps {
  conversationId: string;
  messageId: string;
  /** The rating stored on the server: shown on its thumb after a reload,
   *  and final until the user rates again in this session. */
  initialFeedback?: MessageFeedback | null;
}

/**
 * The panel's answer is the named group `group/msg` (`@filigran/chatbot`
 * ChatMessages): the thumbs show with the copy and read aloud buttons beside
 * them, on hover or focus of the answer, and always where nothing hovers. An
 * unnamed `group-hover` never matches there, which left the thumbs invisible.
 */
export const REVEALED_WITH_ANSWER =
  "opacity-0 transition-opacity group-hover/msg:opacity-100 group-focus-within/msg:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100";

export function MessageFeedbackButtons({
  conversationId,
  messageId,
  initialFeedback,
}: MessageFeedbackButtonsProps) {
  const { t_i18n } = useFormatter();
  const qc = useQueryClient();
  const { toast } = useToast();

  // What the user did in this session; before that, the stored rating shows.
  const [sessionFeedback, setSessionFeedback] = useState<MessageFeedback | null>(null);
  // Whether the user rated in this session (a rating given then can change).
  const [hasInteracted, setHasInteracted] = useState(false);
  const [dialogRating, setDialogRating] = useState<MessageFeedbackRating | null>(null);
  // The criterion scores given this session: like the rating, a stored
  // score is never shown back after a reload.
  const [sessionScores, setSessionScores] = useState<AnnotationScore[]>([]);
  const { criteria, resolveCriteria } = useRatingCriteria();
  // Scores go through the annotations endpoint, which records a message rating
  // with the same chat write as the thumbs.
  const upsertScored = useUpsertAnnotation("message", messageId);

  // A rating stored before this session is shown on its thumb and final, as
  // the panel's own thumbs do (`shownFeedback`): the other thumb is not drawn.
  const isLocked = !hasInteracted && !!initialFeedback?.rating;
  const activeRating = isLocked ? initialFeedback!.rating : (sessionFeedback?.rating ?? null);

  /** Record the thumb; whether it was recorded. */
  const submitFeedback = async (rating: MessageFeedbackRating, nextComment?: string | null) => {
    const previous = sessionFeedback;
    setSessionFeedback({ rating, comment: nextComment ?? null });
    try {
      await api.post(
        `/chat/conversations/${conversationId}/messages/${messageId}/feedback`,
        { rating, comment: nextComment ?? null },
      );
      qc.invalidateQueries({ queryKey: ["agent-chats"] });
      return true;
    } catch (error) {
      setSessionFeedback(previous);
      toast({
        title: t_i18n("Feedback failed"),
        description: error instanceof Error ? error.message : t_i18n("Could not submit feedback."),
        variant: "error",
      });
      return false;
    }
  };

  const retractFeedback = async () => {
    const previous = sessionFeedback;
    setSessionFeedback(null);
    try {
      await api.delete(
        `/chat/conversations/${conversationId}/messages/${messageId}/feedback`,
      );
      qc.invalidateQueries({ queryKey: ["agent-chats"] });
    } catch (error) {
      setSessionFeedback(previous);
      toast({
        title: t_i18n("Feedback failed"),
        description: error instanceof Error ? error.message : t_i18n("Could not retract feedback."),
        variant: "error",
      });
    }
  };

  const handleVote = async (rating: MessageFeedbackRating) => {
    if (isLocked) return;
    setHasInteracted(true);
    if (activeRating === rating) {
      setDialogRating(null);
      await retractFeedback();
      setSessionScores([]);
      return;
    }
    const nextComment = rating === "negative" ? sessionFeedback?.comment ?? null : null;
    if (!(await submitFeedback(rating, nextComment))) {
      setDialogRating(null);
      return;
    }
    const asks = rating === "negative" || (await resolveCriteria()).length > 0;
    setDialogRating(asks ? rating : null);
  };

  const saveDialog = async ({ comment, scores }: AnnotationDialogValue) => {
    const rating = dialogRating;
    if (!rating) return;
    if (scores === undefined) {
      if (await submitFeedback(rating, comment)) setDialogRating(null);
      return;
    }
    const previous = sessionFeedback;
    setSessionFeedback({ rating, comment });
    try {
      await upsertScored.mutateAsync({ rating, comment, scores });
      setSessionScores(scores);
      qc.invalidateQueries({ queryKey: ["agent-chats"] });
      setDialogRating(null);
    } catch (error) {
      setSessionFeedback(previous);
      toast({
        title: t_i18n("Feedback failed"),
        description: error instanceof Error ? error.message : t_i18n("Could not submit feedback."),
        variant: "error",
      });
    }
  };

  return (
    <>
      {(!isLocked || activeRating === "positive") && (
        <Tip content={isLocked ? t_i18n("Feedback already submitted") : t_i18n("Helpful response")}>
          <IconButton
            type="button"
            variant="default"
            priority="tertiary"
            size="sm"
            aria-label={t_i18n("Thumbs up")}
            aria-pressed={activeRating === "positive"}
            // Not `disabled`: a locked thumb keeps its focus and its tooltip.
            aria-disabled={isLocked || undefined}
            onClick={() => void handleVote("positive")}
            className={cn(activeRating ? "opacity-100" : REVEALED_WITH_ANSWER, isLocked && "cursor-default")}
            icon={<Icon name="thumbs-up" size={16} aria-hidden className={cn(activeRating === "positive" && "fill-current")} />}
          />
        </Tip>
      )}

      {(!isLocked || activeRating === "negative") && (
        <Tip content={isLocked ? t_i18n("Feedback already submitted") : t_i18n("Not helpful")}>
          <IconButton
            type="button"
            variant="default"
            priority="tertiary"
            size="sm"
            aria-label={t_i18n("Thumbs down")}
            aria-pressed={activeRating === "negative"}
            aria-disabled={isLocked || undefined}
            onClick={() => void handleVote("negative")}
            className={cn(activeRating ? "opacity-100" : REVEALED_WITH_ANSWER, isLocked && "cursor-default")}
            icon={<Icon name="thumbs-down" size={16} aria-hidden className={cn(activeRating === "negative" && "fill-current")} />}
          />
        </Tip>
      )}

      <AnnotationDialog
        open={dialogRating !== null}
        onOpenChange={(open) => {
          if (!open) setDialogRating(null);
        }}
        rating={dialogRating ?? "negative"}
        criteria={criteria}
        initialComment={sessionFeedback?.comment}
        initialScores={sessionScores}
        saving={upsertScored.isPending}
        onSave={(value) => void saveDialog(value)}
      />
    </>
  );
}
