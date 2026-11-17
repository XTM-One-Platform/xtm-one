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

import { useEffect } from "react";

/**
 * How long after the notice says "resuming by itself" the conversation is read
 * again: the backend's same-build deferral
 * (`cut_turn_resume.SAME_BUILD_DEFER_SECONDS`, 90 s) plus one sweep interval
 * (`SWEEP_INTERVAL_SECONDS`, 10 s). By then the queued turn has resumed (the
 * notice gives way to the working turn) or has left the queue without a resume
 * (cap reached, record expired, AI turned off), and the refetched
 * `interrupted_turn.auto_resume` is false, so the notice shows Resume again
 * without a reload.
 */
export const AUTO_RESUME_RECHECK_MS = 100_000;

/**
 * While `autoResume` is true for `conversationId`, call `refetch` once after
 * {@link AUTO_RESUME_RECHECK_MS}. The wait restarts only when the conversation
 * or the value changes, never on each response: one read per queued episode,
 * no polling.
 */
export function useAutoResumeRecheck(
  conversationId: string | null | undefined,
  autoResume: boolean,
  refetch: () => unknown,
): void {
  useEffect(() => {
    if (!conversationId || !autoResume) return;
    const timer = window.setTimeout(() => {
      void refetch();
    }, AUTO_RESUME_RECHECK_MS);
    return () => window.clearTimeout(timer);
    // `refetch` is not a dependency: a new function identity on each render
    // must not restart the wait.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, autoResume]);
}
