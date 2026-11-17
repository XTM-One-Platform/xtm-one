// SPDX-License-Identifier: Apache-2.0
/**
 * The home dashboard's dismissible context notices (#11586): no permanent,
 * non-dismissible block on the command center. What a person closed is
 * stored on the user (`users.organization_context_notice_dismissed`), never
 * in localStorage, so it holds across reloads, browsers and devices:
 *
 * - the organization context fields that were missing when they closed the
 *   "organization context is incomplete" notice (contract paths): the notice
 *   comes back only when a field outside that list becomes missing;
 * - `generic:<notice id>` for a widget's "this is generic" qualifier closed.
 *
 * Briefs and stage pages never read this: their notice stays (it explains
 * the quality of the analysis to every reader).
 */
import { useCallback, useMemo } from "react";

import { useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useAuth } from "@/hooks/useAuth";

export const GENERIC_NOTICE_PREFIX = "generic:";

/** Whether the incomplete-context notice shows: some missing field was not missing when it was closed. */
export function contextNoticeVisible(missing: readonly string[], dismissed: readonly string[] | null | undefined): boolean {
  if (dismissed == null) return true;
  const closed = new Set(dismissed);
  return missing.some((field) => !closed.has(field));
}

/** The next stored list once *keys* are closed: what was closed before, plus *keys*, deduplicated. */
export function withDismissed(dismissed: readonly string[] | null | undefined, keys: readonly string[]): string[] {
  return [...new Set([...(dismissed ?? []), ...keys])];
}

export function useNoticeDismissals() {
  const { user } = useAuth();
  const updateProfile = useUpdateProfile();
  const dismissed = user?.organization_context_notice_dismissed ?? null;
  const dismiss = useCallback(
    (keys: readonly string[]) =>
      // Whole-field write; `useUpdateProfile` patches `["me"]` optimistically,
      // so the notice goes at once and comes back if the round-trip fails.
      updateProfile.mutate({ organization_context_notice_dismissed: withDismissed(dismissed, keys) }),
    [updateProfile, dismissed],
  );
  return useMemo(() => ({ dismissed, dismiss }), [dismissed, dismiss]);
}
