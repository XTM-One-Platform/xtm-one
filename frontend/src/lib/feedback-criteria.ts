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
 * Feedback criteria in the UI, without React.
 *
 * What each criterion offers the person rating, and what their inputs become:
 * the `scores` of an annotation. The server checks every score and every
 * definition again (`services/feedback_criteria.py`); this keeps the dialog and
 * the Settings form from sending what it would refuse, and says why in place.
 */
import type {
  AnnotationScore,
  CriterionDay,
  FeedbackCriterion,
  FeedbackCriterionCreate,
  FeedbackCriterionType,
  FeedbackCriterionUpdate,
} from "@/types";

/** Criterion id -> what the person entered: an option value, a scale step, or the text of a number. */
export type ScoreDraft = Record<string, string>;

/** Why a typed number cannot be sent. */
type ScoreProblem = "not_a_number" | "out_of_range";

interface ParsedScores {
  scores: AnnotationScore[];
  /** Criterion id -> why its input cannot be sent. */
  problems: Record<string, ScoreProblem>;
}

/** Each whole step of a scale criterion, lowest first. */
export function scaleSteps(criterion: Pick<FeedbackCriterion, "min_value" | "max_value">): number[] {
  const lo = criterion.min_value;
  const hi = criterion.max_value;
  if (lo === null || hi === null || !Number.isInteger(lo) || !Number.isInteger(hi) || hi < lo) return [];
  return Array.from({ length: hi - lo + 1 }, (_, index) => lo + index);
}

/** A draft holding the scores of a stored rating. */
export function draftFromScores(scores: readonly AnnotationScore[] | undefined): ScoreDraft {
  const draft: ScoreDraft = {};
  for (const score of scores ?? []) draft[score.criterion_id] = String(score.value);
  return draft;
}

/**
 * The scores a draft sends, criterion by criterion; an empty input is no score.
 *
 * Only the given (enabled) criteria are read. An option or a scale step is
 * picked, never typed, so one that is not valid can only be a stored score
 * from before the criterion changed: it is dropped, and saving removes it. A
 * typed number out of bounds is a problem to show next to its input.
 */
export function parseScoreDraft(criteria: readonly FeedbackCriterion[], draft: ScoreDraft): ParsedScores {
  const scores: AnnotationScore[] = [];
  const problems: Record<string, ScoreProblem> = {};
  for (const criterion of criteria) {
    const raw = (draft[criterion.id] ?? "").trim();
    if (!raw) continue;
    if (criterion.type === "categorical") {
      if ((criterion.options ?? []).some((option) => option.value === raw)) {
        scores.push({ criterion_id: criterion.id, value: raw });
      }
      continue;
    }
    const value = Number(raw);
    if (criterion.type === "scale") {
      if (scaleSteps(criterion).includes(value)) scores.push({ criterion_id: criterion.id, value });
      continue;
    }
    if (!Number.isFinite(value)) {
      problems[criterion.id] = "not_a_number";
    } else if (
      criterion.min_value === null ||
      criterion.max_value === null ||
      value < criterion.min_value ||
      value > criterion.max_value
    ) {
      problems[criterion.id] = "out_of_range";
    } else {
      scores.push({ criterion_id: criterion.id, value });
    }
  }
  return { scores, problems };
}

/** A 0-1 score as a whole percentage, or null. */
export function scorePercent(score: number | null | undefined): number | null {
  if (score === null || score === undefined || !Number.isFinite(score)) return null;
  return Math.round(Math.min(1, Math.max(0, score)) * 100);
}

interface TimelineSlot {
  /** `YYYY-MM-DD`, in UTC like the days the server groups by. */
  date: string;
  day: CriterionDay | null;
}

/** The last *days* days up to *today*, oldest first, each with its scores or none. */
export function criterionTimeline(daily: readonly CriterionDay[], days: number, today: Date): TimelineSlot[] {
  const byDate = new Map(daily.map((day) => [day.date, day]));
  const slots: TimelineSlot[] = [];
  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const date = new Date(
      Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() - offset),
    )
      .toISOString()
      .slice(0, 10);
    slots.push({ date, day: byDate.get(date) ?? null });
  }
  return slots;
}

// ---------------------------------------------------------------------------
// Settings > Agents and automations > Quality: the definition form
// ---------------------------------------------------------------------------

interface CriterionOptionDraft {
  value: string;
  label: string;
  score: string;
}

export interface CriterionFormDraft {
  name: string;
  description: string;
  type: FeedbackCriterionType;
  weight: string;
  options: CriterionOptionDraft[];
  minValue: string;
  maxValue: string;
  enabled: boolean;
}

/** Why the form cannot be saved yet; the server words everything else. */
export type CriterionFormProblem = "name" | "weight" | "options" | "bounds";

/** The form for *criterion*, or for a new one: a 1-5 scale with two blank options ready. */
export function criterionFormDraft(criterion?: FeedbackCriterion | null): CriterionFormDraft {
  if (!criterion) {
    return {
      name: "",
      description: "",
      type: "scale",
      weight: "1",
      options: [
        { value: "", label: "", score: "0" },
        { value: "", label: "", score: "1" },
      ],
      minValue: "1",
      maxValue: "5",
      enabled: true,
    };
  }
  return {
    name: criterion.name,
    description: criterion.description ?? "",
    type: criterion.type,
    weight: String(criterion.weight),
    options: (criterion.options ?? []).map((option) => ({
      value: option.value,
      label: option.label ?? "",
      score: String(option.score),
    })),
    minValue: criterion.min_value === null ? "" : String(criterion.min_value),
    maxValue: criterion.max_value === null ? "" : String(criterion.max_value),
    enabled: criterion.enabled,
  };
}

function numberIn(raw: string): number | null {
  if (!raw.trim()) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

export function criterionFormProblem(form: CriterionFormDraft): CriterionFormProblem | null {
  if (!form.name.trim()) return "name";
  const weight = numberIn(form.weight);
  if (weight === null || weight < 0) return "weight";
  if (form.type === "categorical") {
    const valid = form.options.every((option) => {
      const score = numberIn(option.score);
      return option.value.trim() !== "" && score !== null && score >= 0 && score <= 1;
    });
    return form.options.length >= 2 && valid ? null : "options";
  }
  const lo = numberIn(form.minValue);
  const hi = numberIn(form.maxValue);
  if (lo === null || hi === null || hi <= lo) return "bounds";
  if (form.type === "scale" && (!Number.isInteger(lo) || !Number.isInteger(hi))) return "bounds";
  return null;
}

/** The request body of a form without a problem (`criterionFormProblem`). */
export function criterionPayload(form: CriterionFormDraft): FeedbackCriterionCreate {
  const categorical = form.type === "categorical";
  return {
    name: form.name.trim(),
    description: form.description.trim() || null,
    type: form.type,
    weight: numberIn(form.weight) ?? 1,
    options: categorical
      ? form.options.map((option) => ({
          value: option.value.trim(),
          label: option.label.trim() || null,
          score: numberIn(option.score) ?? 0,
        }))
      : null,
    min_value: categorical ? null : numberIn(form.minValue),
    max_value: categorical ? null : numberIn(form.maxValue),
    enabled: form.enabled,
  };
}

/** The same, for an update: the type of a criterion never changes. */
export function criterionUpdatePayload(form: CriterionFormDraft): FeedbackCriterionUpdate {
  const payload = criterionPayload(form);
  return {
    name: payload.name,
    description: payload.description,
    weight: payload.weight,
    options: payload.options,
    min_value: payload.min_value,
    max_value: payload.max_value,
    enabled: payload.enabled,
  };
}
