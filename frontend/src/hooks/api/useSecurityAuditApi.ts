// SPDX-License-Identifier: Apache-2.0

/** Security audit hook (admin) — the configuration review behind Settings > Access and security. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SecurityAuditReport } from "@/types";

export const SECURITY_AUDIT_QUERY_KEY = ["security-audit"] as const;

/**
 * Runs the audit: the endpoint computes a fresh report on every request, so
 * `refetch()` is the Re-run. `enabled` must be false for a non-admin, whom the
 * endpoint refuses.
 */
export function useSecurityAudit(enabled: boolean) {
  return useQuery<SecurityAuditReport>({
    queryKey: SECURITY_AUDIT_QUERY_KEY,
    queryFn: () => api.get("/security-audit"),
    enabled,
    // A re-run replaces what the admin is reading, so it happens when they ask.
    refetchOnWindowFocus: false,
    // The section renders its own error state with a retry, and keeps the last
    // report on screen when a re-run fails.
    throwOnError: false,
  });
}

const dismissalPath = (findingId: string) => `/security-audit/findings/${encodeURIComponent(findingId)}/dismissal`;

/**
 * Every dismiss and restore shares one mutation scope, so they run one after
 * the other and their reports reach the cache in the order they were written.
 */
const AUDIT_WRITE_SCOPE = { id: "security-audit-write" };

/**
 * A write answering the updated report. A Re-run in flight was computed
 * before the write and would land after it, so it is cancelled first.
 */
function useAuditWrite<TVariables>(write: (variables: TVariables) => Promise<SecurityAuditReport>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: write,
    scope: AUDIT_WRITE_SCOPE,
    onMutate: () => queryClient.cancelQueries({ queryKey: SECURITY_AUDIT_QUERY_KEY }),
    onSuccess: async (report) => {
      await queryClient.cancelQueries({ queryKey: SECURITY_AUDIT_QUERY_KEY });
      queryClient.setQueryData(SECURITY_AUDIT_QUERY_KEY, report);
    },
  });
}

/**
 * Accept a finding: it leaves the counts and the score and is never
 * notified. The endpoint answers the updated report, which replaces the one on
 * screen without running the audit a second time.
 */
export function useDismissSecurityAuditFinding() {
  return useAuditWrite(({ findingId, reason }: { findingId: string; reason: string | null }) =>
    api.put<SecurityAuditReport>(dismissalPath(findingId), { reason }),
  );
}

/** Undo a dismissal: the finding counts again. Answers the updated report. */
export function useRestoreSecurityAuditFinding() {
  return useAuditWrite((findingId: string) => api.delete<SecurityAuditReport>(dismissalPath(findingId)));
}
