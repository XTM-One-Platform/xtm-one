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
 * Which machine activity cards the reader opened.
 *
 * Every card of a machine command (`run_shell`, a file read or write, a
 * listing, a capture, a computer or browser action) arrives collapsed on one
 * line, live and saved alike, failed or not: the line carries the status. A
 * card opens on a click only, and stays open for the session: through the rest
 * of the stream, the switch from the live card to the saved one (both carry the
 * same `command_id`), a remount of the transcript and a conversation switch and
 * back. Nothing opens by itself, so a turn of many commands no longer fills the
 * screen with bodies nobody asked for, and a card that finishes never changes
 * height under the reader.
 *
 * The state lives in the page (a module-level set), not in a card: a card is
 * unmounted and mounted again when the live block hands over to the persisted
 * message. Not persisted across reloads, on purpose: a reload starts collapsed.
 *
 * Pure (no React, no DOM, no `@/` import) so `runner-card-open.test.ts` runs
 * under `node --test`.
 */

/** Command ids the reader opened, oldest first. */
const opened = new Set<string>();

/**
 * How many opened cards the session remembers. A long session of hundreds of
 * commands forgets the oldest openings first; their cards fall back to
 * collapsed, which is the default anyway.
 */
export const OPENED_CARDS_LIMIT = 500;

/** Whether the card of `commandId` is open: only when the reader opened it. */
export function isRunnerCardOpen(commandId: string | null | undefined): boolean {
  return !!commandId && opened.has(commandId);
}

/** Record the reader opening (`true`) or closing (`false`) the card of `commandId`. */
export function setRunnerCardOpen(commandId: string | null | undefined, open: boolean): void {
  if (!commandId) return;
  opened.delete(commandId);
  if (!open) return;
  opened.add(commandId);
  while (opened.size > OPENED_CARDS_LIMIT) {
    const oldest = opened.values().next().value;
    if (oldest === undefined) break;
    opened.delete(oldest);
  }
}

/**
 * Background jobs the reader stopped from the chat this session.
 * The saved card keeps `job_status: running` (the record was
 * written when the job started), so the card reads this set to settle as
 * "Stopped by you" and drop Stop, through remounts and the live-to-saved
 * handover. Bounded like the openings.
 */
const stoppedJobs = new Set<string>();

/** Whether the reader stopped the background job of `commandId` this session. */
export function isRunnerJobStopped(commandId: string | null | undefined): boolean {
  return !!commandId && stoppedJobs.has(commandId);
}

/** Record that the reader stopped the background job of `commandId`. */
export function markRunnerJobStopped(commandId: string | null | undefined): void {
  if (!commandId) return;
  stoppedJobs.delete(commandId);
  stoppedJobs.add(commandId);
  while (stoppedJobs.size > OPENED_CARDS_LIMIT) {
    const oldest = stoppedJobs.values().next().value;
    if (oldest === undefined) break;
    stoppedJobs.delete(oldest);
  }
}

/** Forget every opening and every stop (tests). */
export function resetRunnerCardOpen(): void {
  opened.clear();
  stoppedJobs.clear();
}
