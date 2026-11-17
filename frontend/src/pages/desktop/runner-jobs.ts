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

/** What the Machines page shows of a machine's background shell jobs. */

import type { RunnerJob } from "@/types";

/** Running jobs first, then the most recent ended ones. */
export function orderJobs(jobs: RunnerJob[]): RunnerJob[] {
  return [...jobs].sort((a, b) => {
    const ra = a.status === "running" ? 0 : 1;
    const rb = b.status === "running" ? 0 : 1;
    if (ra !== rb) return ra - rb;
    return (b.started_at ?? "").localeCompare(a.started_at ?? "");
  });
}

/**
 * The sentence for a list the machine could not give, by the reason the server answered.
 * @i18n-keys
 */
const JOBS_UNAVAILABLE_MESSAGES = {
  offline: "The machine is offline: its background jobs show once it is back.",
  not_allowed: "Shell commands are not enabled on this machine, so it runs no background jobs.",
  busy: "The machine is busy right now: its background jobs show at the next refresh.",
  no_answer: "The machine did not answer in time: its background jobs show at the next refresh.",
} as const;

/** A reason the server answers, mapped to its sentence. */
type JobsUnavailableReason = keyof typeof JOBS_UNAVAILABLE_MESSAGES;

function isKnownReason(reason: string): reason is JobsUnavailableReason {
  return Object.prototype.hasOwnProperty.call(JOBS_UNAVAILABLE_MESSAGES, reason);
}

/**
 * The catalog key to show (pass it through `t_i18n`).
 *
 * The fallback is deliberate: no reason, or one this frontend does
 * not know yet (a newer server), reads as `no_answer`, "did not answer in
 * time ... at the next refresh". It is the only sentence true for any reason:
 * the list was not given now and a refresh may give it. The four reasons the
 * server sends today (`backend/app/api/v1/runners.py`, the jobs route) are
 * each mapped explicitly above.
 */
export function jobsUnavailableMessage(reason: string | null): string {
  if (reason && isKnownReason(reason)) return JOBS_UNAVAILABLE_MESSAGES[reason];
  return JOBS_UNAVAILABLE_MESSAGES.no_answer;
}
