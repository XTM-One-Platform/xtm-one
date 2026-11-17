// SPDX-License-Identifier: Apache-2.0

/**
 * How every query of the app behaves while the backend is unreachable (a
 * restart, a deploy, a node drain): the `QueryClient` defaults of `main.tsx`.
 *
 * A single-replica restart makes the whole API unreachable for tens of seconds.
 * A read that fails then is read again for about two minutes, and a read that
 * already has data on screen never throws to an error boundary for an outage:
 * the page (a chat transcript and its sidebar included) stays as it was and
 * catches up once the backend answers, instead of being replaced by the
 * full-page "Unable to reach the server" state (#10910).
 */

import { BackendUnavailableError, isOutageOverDataOnScreen, type QueryOnScreen } from "@/lib/api";

/** How many times a read is tried again while the backend is unreachable (about two minutes with the default backoff). */
export const OUTAGE_QUERY_RETRIES = 8;

/** True when the error is a network/connection failure (backend unreachable). */
export function isNetworkError(error: unknown): boolean {
  if (error instanceof BackendUnavailableError) return true; // explicit backend-down signal
  if (error instanceof TypeError) return true; // Failed to fetch, NetworkError, etc.
  return error instanceof Error && /fetch|network|ECONNREFUSED/i.test(error.message);
}

/** Whether a read is tried again after *failureCount* failures: an outage for about two minutes, any other error once. */
export function retryQuery(failureCount: number, error: unknown): boolean {
  if (isNetworkError(error)) return failureCount < OUTAGE_QUERY_RETRIES;
  return failureCount < 1;
}

/**
 * Whether a failed read is thrown to the nearest error boundary: every error,
 * except an outage met by a read whose data is already on screen.
 */
export function throwQueryError(error: unknown, query: QueryOnScreen): boolean {
  return !isOutageOverDataOnScreen(error, query);
}
