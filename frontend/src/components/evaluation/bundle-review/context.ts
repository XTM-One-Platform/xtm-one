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

import type { Derived, LocatedText, ReviewFilters, ReviewMode, ReviewModel, ReviewPair } from "@/lib/evaluation-bundle-review";
import type { BundleReview } from "@/types";

/** The selected object (`e`, a group id) or relationship (`r`, a row id). */
export interface ReviewSelection {
  kind: "e" | "r";
  id: string;
}

/** What every view of the review reads: the data, the pair on screen, the filters and the selection. */
export interface ReviewContext {
  review: BundleReview;
  model: ReviewModel;
  pair: ReviewPair;
  mode: ReviewMode;
  whatIf: boolean;
  derived: Derived;
  filters: ReviewFilters;
  located: LocatedText;
  selection: ReviewSelection | null;
  select: (selection: ReviewSelection) => void;
  /** One letter per source: E for the expected bundle, A to D for the answers. */
  letter: (source: string) => string;
  /** A source's name: "Expected", or the answer's evaluation (its agent within one) and model. */
  sourceName: (source: string) => string;
  /** The sources on screen: the expected bundle, then the pair's answers. */
  shown: string[];
  /** The answers Coverage lays side by side: every loaded answer, or the pair of a run review. */
  columns: string[];
  /** Every loaded source's groups (the run's other answers on this entry included), with
   *  the same group and relationship ids as `model`: what the history and the comparator read. */
  fullModel: ReviewModel;
  /** The answer the reviewed one is measured against on this entry (the reference run, or
   *  the previous run of its agent and model), its name; null without one. */
  comparator: { key: string; name: string } | null;
  /** The reviewed answer's agent and model on every run of theirs that answered this entry,
   *  oldest first (a run review only): what the inspector's history line reads. */
  history: { label: string; runs: { key: string; code: string }[] } | null;
}

export function isSelected(ctx: ReviewContext, kind: ReviewSelection["kind"], id: string): boolean {
  return ctx.selection?.kind === kind && ctx.selection.id === id;
}

/** Enter and Space activate a focusable row, node or edge, as a click. */
export function onActivate(action: () => void) {
  return (event: { key: string; preventDefault: () => void }) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      action();
    }
  };
}
