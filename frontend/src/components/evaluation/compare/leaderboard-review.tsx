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
 * Opening a run's review from the Leaderboard (#2): a click reviews the run against the
 * expected answers; a shift+click first sets a run as the reference A, then a click opens
 * the run against it (`/evaluations/review?run=<B>&against=<A>`). The last review opened
 * from this evaluation's board is remembered for this tab (session storage), so the board
 * says which review is current and reopens it.
 */

import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";

interface BoardReview {
  run: string;
  against: string | null;
}

const storageKey = (evaluationId: string) => `evaluation-board-review:${evaluationId}`;

function readReview(evaluationId: string): BoardReview | null {
  try {
    const raw = sessionStorage.getItem(storageKey(evaluationId));
    const parsed = raw ? (JSON.parse(raw) as BoardReview) : null;
    return parsed && typeof parsed.run === "string" ? parsed : null;
  } catch {
    return null;
  }
}

function writeReview(evaluationId: string, review: BoardReview) {
  try {
    sessionStorage.setItem(storageKey(evaluationId), JSON.stringify(review));
  } catch {
    // A browser without session storage only forgets the current review.
  }
}

const reviewPath = (review: BoardReview) =>
  `/evaluations/review?run=${encodeURIComponent(review.run)}${review.against ? `&against=${encodeURIComponent(review.against)}` : ""}`;

/** The board's review state: the pending reference, the current review, and the click. */
export function useBoardReview(evaluationId: string, nameOf: (runId: string) => string) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [reference, setReference] = useState<string | null>(null);
  const [current] = useState<BoardReview | null>(() => readReview(evaluationId));

  const open = useCallback(
    (runId: string, asReference: boolean) => {
      if (asReference) {
        setReference(runId);
        toast({
          title: t_i18n("{run} is the reference A. Now click the run to compare with it.", { run: nameOf(runId) }),
          tag: "board-reference",
        });
        return;
      }
      const review = { run: runId, against: reference && reference !== runId ? reference : null };
      writeReview(evaluationId, review);
      navigate(reviewPath(review));
    },
    [evaluationId, nameOf, navigate, reference, t_i18n, toast],
  );

  // The roles the board marks: the pending reference, else the current review's two runs.
  const roles = reference ? { a: reference, b: null } : { a: current?.against ?? null, b: current?.run ?? null };
  return { reference, clearReference: () => setReference(null), current, open, roles };
}

/** The head of the Leaderboard: how to open a review, and the current one. */
export function ReviewHead({
  reference,
  current,
  nameOf,
  onClear,
}: {
  reference: string | null;
  current: BoardReview | null;
  nameOf: (runId: string) => string;
  onClear: () => void;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 content-compact">
      <p className="text-default-secondary max-w-prose">
        {t_i18n("Click a run to review it against the dataset. Shift+click a first run to set it as the reference A, then click the run to compare with it.")}
      </p>
      {reference ? (
        <span className="inline-flex flex-wrap items-center gap-2">
          {t_i18n("Reference A: {run}. Now click the run to compare with it.", { run: nameOf(reference) })}
          <Button variant="ghost" size="sm" onClick={onClear}>
            {t_i18n("Clear")}
          </Button>
        </span>
      ) : current ? (
        <span className="inline-flex flex-wrap items-center gap-2">
          {current.against
            ? t_i18n("Current review: {run} vs {reference}", { run: nameOf(current.run), reference: nameOf(current.against) })
            : t_i18n("Current review: {run} vs the dataset", { run: nameOf(current.run) })}
          <Button variant="outline" size="sm" className="gap-1.5" onClick={() => navigate(reviewPath(current))}>
            {t_i18n("Open the review")} <Icon name="arrow-right" size={16} />
          </Button>
        </span>
      ) : null}
    </div>
  );
}
