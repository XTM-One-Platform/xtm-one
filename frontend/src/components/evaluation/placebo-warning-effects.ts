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

// What each placebo warning changes in how a comparison reads: the comparison
// card and the launch dialog show the same sentence for a code.

/**
 * What a placebo warning changes in how the comparison reads, by code (the codes of
 * `PLACEBO_WARNING_LABELS`, `placebo_stats.py`, `placebo_launch.py`,
 * `evaluation_placebo.py`), read by `t_i18n`. @i18n-keys
 */
const PLACEBO_WARNING_EFFECTS = {
  few_entries: "Fewer than 10 entries were counted, so the intervals are wide and the verdict is fragile.",
  run_incomplete: "A run of this agent, model and target did not complete, so the row has no verdict.",
  quality_unmeasured: "No selected quality metric has a value on both arms, so there is no D and no verdict.",
  tokens_unmeasured: "No trial recorded its tokens, so there is no R.",
  tokens_partially_measured: "R is computed from the trials that recorded their tokens only.",
  corpus_repeated: "The original is longer than the neutral corpus, so the placebo repeats passages.",
  length_out_of_tolerance: "A placebo part could not be brought within ±5 % of its original's length, so length may explain part of the difference.",
  language_undetermined: "The original's language could not be detected, so the placebo is in English and may differ from it in language.",
  token_probe_failed: "The model reported no token counts, so the placebo matches its original in characters, not tokens.",
  target_skipped: "This agent has no arm for this target, for the reason given.",
  sequential_run_mode: "The evaluation runs one run at a time, so a provider slowdown can hit one arm more than another.",
  statistics_failed: "The statistics could not be computed, so the comparison has no results.",
  run_deleted: "A run of the comparison was deleted, so it has no results.",
} as const;

/** A warning's effect, or `null` for a code this UI does not know yet. */
export function placeboWarningEffect(code: string): string | null {
  return Object.prototype.hasOwnProperty.call(PLACEBO_WARNING_EFFECTS, code)
    ? PLACEBO_WARNING_EFFECTS[code as keyof typeof PLACEBO_WARNING_EFFECTS]
    : null;
}
