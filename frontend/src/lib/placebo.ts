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
 * The placebo-controlled evaluation in the UI
 * (`dev-docs/agent-placebo-evaluation.md`): the configuration while it is
 * edited and what stops it from being saved, a run's arm, and how a
 * comparison's effects, verdicts and warnings read.
 */
import type {
  EvaluationArm,
  EvaluationPlaceboConfig,
  EvaluationRun,
  PlaceboEffect,
  PlaceboTarget,
  PlaceboTargetKind,
  PlaceboVerdict,
} from "@/types";

/** The bounds `PlaceboConfig` enforces on the server (FR-1, FR-4, FR-4b). */
export const MAX_TRIALS_PER_ENTRY = 30;
const MAX_TARGETS = 20;
export const MAX_CUSTOM_TEXT = 20_000;

/** A placebo configuration while it is edited: one switch per whole-set target, single skills by id. */
export interface PlaceboDraft {
  persona: boolean;
  prompts: boolean;
  skills: boolean;
  skillIds: string[];
  trialsPerEntry: number;
  includeNoTarget: boolean;
  customText: string;
}

/** What a new configuration starts from: the persona, five trials, a no-target arm. */
export const NEW_PLACEBO_DRAFT: PlaceboDraft = {
  persona: true,
  prompts: false,
  skills: false,
  skillIds: [],
  trialsPerEntry: 5,
  includeNoTarget: true,
  customText: "",
};

export function draftFromConfig(config: EvaluationPlaceboConfig | null | undefined): PlaceboDraft {
  if (!config) return { ...NEW_PLACEBO_DRAFT };
  const kinds = new Set(config.targets.map((target) => target.kind));
  return {
    persona: kinds.has("persona"),
    prompts: kinds.has("prompts"),
    skills: kinds.has("skills"),
    skillIds: config.targets.filter((target) => target.kind === "skill" && target.skill_id).map((target) => target.skill_id!),
    trialsPerEntry: config.trials_per_entry,
    includeNoTarget: config.include_no_target,
    customText: config.custom_text ?? "",
  };
}

export function configFromDraft(draft: PlaceboDraft): EvaluationPlaceboConfig {
  const targets: PlaceboTarget[] = [];
  for (const kind of ["persona", "prompts", "skills"] as const) {
    if (draft[kind]) targets.push({ kind, skill_id: null });
  }
  for (const skillId of draft.skillIds) targets.push({ kind: "skill", skill_id: skillId });
  return {
    targets,
    include_no_target: draft.includeNoTarget,
    trials_per_entry: draft.trialsPerEntry,
    custom_text: draft.customText.trim() ? draft.customText : null,
  };
}

/** Why a draft cannot be saved, read by `t_i18n(PLACEBO_DRAFT_ERRORS[error])`. @i18n-keys */
export const PLACEBO_DRAFT_ERRORS = {
  no_target: "Choose at least one target.",
  skills_and_skill: "Choose all skills or single skills, not both.",
  too_many_targets: "A placebo control holds at most 20 targets.",
  trials: "Trials per entry must be a whole number between 1 and 30.",
  custom_text: "The custom text holds at most 20,000 characters.",
  needs_full_loop: "Prompts and skills targets need the Full Loop execution mode: in LLM Only the model sees the persona alone.",
} as const;

/** The first rule the draft breaks (the server's 422s, checked before sending), or `null`. */
export function placeboDraftError(draft: PlaceboDraft, executionMode: string): keyof typeof PLACEBO_DRAFT_ERRORS | null {
  const targets = configFromDraft(draft).targets;
  if (targets.length === 0) return "no_target";
  if (draft.skills && draft.skillIds.length > 0) return "skills_and_skill";
  if (targets.length > MAX_TARGETS) return "too_many_targets";
  if (!Number.isInteger(draft.trialsPerEntry) || draft.trialsPerEntry < 1 || draft.trialsPerEntry > MAX_TRIALS_PER_ENTRY) {
    return "trials";
  }
  if (draft.customText.length > MAX_CUSTOM_TEXT) return "custom_text";
  if (executionMode !== "full_loop" && targets.some((target) => target.kind !== "persona")) return "needs_full_loop";
  return null;
}

export function runArm(run: Pick<EvaluationRun, "arm">): EvaluationArm {
  return run.arm ?? "real";
}

/** A run of a placebo comparison, real arm included: it ranks no configuration (FR-28). */
export function isComparisonRun(run: Pick<EvaluationRun, "placebo_comparison_id">): boolean {
  return !!run.placebo_comparison_id;
}

/** A placebo or no-target arm: it only means something next to its comparison's other arms. */
export function isControlArm(run: Pick<EvaluationRun, "arm">): boolean {
  return runArm(run) !== "real";
}

/** The target a server key names (a warning's or a skipped target's `target`). */
export function targetFromKey(key: string): PlaceboTarget {
  return key.startsWith("skill:")
    ? { kind: "skill", skill_id: key.slice("skill:".length) }
    : { kind: key as PlaceboTargetKind, skill_id: null };
}

/**
 * A corpus version short enough to show: each hash without its `sha256:` prefix, first 12
 * characters. A comparison drawn from two corpora (English and French) lists both, comma-joined.
 */
export function shortCorpusVersion(version: string): string {
  return version
    .split(",")
    .map((part) => part.trim().replace(/^sha256:/, "").slice(0, 12))
    .join(", ");
}

/** A target kind's name, read by `t_i18n(PLACEBO_TARGET_LABELS[kind])`. @i18n-keys */
export const PLACEBO_TARGET_LABELS = {
  persona: "Persona",
  prompts: "Prompts",
  skills: "Skills",
  skill: "Skill",
} as const satisfies Record<PlaceboTargetKind, string>;

/** An arm's name, read by `t_i18n(PLACEBO_ARM_LABELS[arm])`. @i18n-keys */
export const PLACEBO_ARM_LABELS = {
  real: "Real",
  placebo: "Placebo",
  no_target: "No target",
} as const satisfies Record<EvaluationArm, string>;

/** A verdict as a person reads it (FR-26), by `t_i18n(PLACEBO_VERDICT_LABELS[verdict])`. @i18n-keys */
export const PLACEBO_VERDICT_LABELS = {
  beats_placebo: "Beats placebo",
  trade_off: "Trade-off",
  worse_than_placebo: "Worse than placebo",
  no_better_than_placebo: "No better than placebo",
  not_computable: "Not computable",
} as const satisfies Record<PlaceboVerdict, string>;

/** The badge a verdict wears. */
export const PLACEBO_VERDICT_BADGES = {
  beats_placebo: "success",
  trade_off: "warning",
  worse_than_placebo: "destructive",
  no_better_than_placebo: "secondary",
  not_computable: "outline",
} as const satisfies Record<PlaceboVerdict, string>;

/**
 * The warnings a comparison, a row or a placebo snapshot carries, by code
 * (`evaluation_placebo.py`, `placebo_launch.py`, `placebo_runs.py`,
 * `placebo_stats.py`, `placebo_comparisons.py`, the run deletion), read through
 * `placeboWarningLabel`. @i18n-keys
 */
const PLACEBO_WARNING_LABELS = {
  few_entries: "Few entries: read with care",
  run_incomplete: "A run did not complete: no verdict",
  quality_unmeasured: "Overall Quality was not measured on both arms",
  tokens_unmeasured: "Tokens were not measured",
  tokens_partially_measured: "Tokens were measured on some trials only",
  corpus_repeated: "The placebo is longer than the corpus: passages repeat",
  length_out_of_tolerance: "A placebo part is outside ±5 % of its original's length",
  language_undetermined: "Language undetermined: the placebo is in English",
  token_probe_failed: "Lengths matched in characters: the model could not be probed",
  target_skipped: "Target left out",
  sequential_run_mode: "The arms ran one after the other, not interleaved",
  statistics_failed: "The statistics could not be computed",
  run_deleted: "One of its runs was deleted: no results",
} as const;

/** A warning's label: an i18n key for a known code, the code itself for one this UI does not know yet. */
export function placeboWarningLabel(code: string): { key: string; known: boolean } {
  const label = (PLACEBO_WARNING_LABELS as Record<string, string>)[code];
  return label ? { key: label, known: true } : { key: code, known: false };
}

/** A quality difference in points with its interval: `+12.3 [+8.1, +16.0]`. */
export function formatDifference(effect: PlaceboEffect): string {
  const signed = (value: number) => `${value > 0 ? "+" : ""}${value.toFixed(1)}`;
  return `${signed(effect.estimate)} [${signed(effect.ci_low)}, ${signed(effect.ci_high)}]`;
}

/** A token ratio with its interval: `0.82× [0.75, 0.90]`; below 1 the real agent is cheaper. */
export function formatRatio(effect: PlaceboEffect): string {
  return `${effect.estimate.toFixed(2)}× [${effect.ci_low.toFixed(2)}, ${effect.ci_high.toFixed(2)}]`;
}

/** A placebo part's deviation from its original's length: `+3.2 %`, or `-` without an original. */
export function formatDeviation(deviation: number | null): string {
  if (deviation === null) return "-";
  const percent = deviation * 100;
  return `${percent > 0 ? "+" : ""}${percent.toFixed(1)} %`;
}

/** A share (0-1) as a percentage, `-` when unmeasured. */
export function formatShare(share: number | null): string {
  return share === null ? "-" : `${Math.round(share * 100)} %`;
}
