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
 * The reason line a run shows next to its status (the run card, its
 * lightweight row, a delegation step of the run page).
 *
 * A bulk cancel of runs not started writes `Cancelled by <actor>` in the run's
 * `error` (`backend/app/services/run_bulk_cancel.py`, `cancel_reason`). That is
 * a deliberate act, not a failure: it reads in the app language and in the
 * neutral tone, like the run the platform interrupted. Every other
 * error is the run's own words and keeps the error tone.
 */

const CANCELLED_BY = /^Cancelled by (.+)$/s;

interface RunReason {
  status?: string | null;
  outcome?: string | null;
  error: string | null;
}

type Translate = (id: string, values?: Record<string, string | number>) => string;

/** The reason in the app language: a bulk cancel is localised, anything else is shown as written. */
export function runErrorText(run: RunReason, t_i18n: Translate): string {
  const error = run.error ?? "";
  const cancelled = run.status === "cancelled" ? CANCELLED_BY.exec(error) : null;
  return cancelled ? t_i18n("Cancelled by {actor}", { actor: cancelled[1] }) : error;
}

/** True when the reason reads neutral: a cancelled run or one the platform interrupted. */
export function runErrorIsNeutral(run: RunReason): boolean {
  return run.status === "cancelled" || run.outcome === "interrupted";
}
