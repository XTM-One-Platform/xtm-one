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
 * A run's mode badges, read from what the run recorded, never from the
 * evaluation's current setting, which can change after the run. Also how an
 * evaluation's stored mode runs.
 *
 * Self-contained so `node --test` runs it (`run-mode.test.ts`).
 */

export interface RecordedRunMode {
  execution_mode?: string | null;
  fallback_entries?: number;
}

export interface RunModeBadges {
  /** The run ran in LLM Only: only the persona and the entry reached the model. */
  llmOnly: boolean;
  /** Entries answered in LLM Only after their Full Loop run failed; only runs recorded while that fallback existed have any. */
  fallbackEntries: number;
}

export function runModeBadges(run: RecordedRunMode): RunModeBadges {
  const fallbackEntries = run.fallback_entries ?? 0;
  return {
    llmOnly: run.execution_mode === "llm_only",
    fallbackEntries: Number.isFinite(fallbackEntries) && fallbackEntries > 0 ? fallbackEntries : 0,
  };
}

/**
 * The mode a stored `execution_mode` runs in: Full Loop only for exactly
 * `full_loop`. Any other string, which older rows may hold, runs as LLM
 * Only, so the settings show and edit it as LLM Only.
 */
export function evaluationExecutionMode(stored: string | null | undefined): "llm_only" | "full_loop" {
  return stored === "full_loop" ? "full_loop" : "llm_only";
}
