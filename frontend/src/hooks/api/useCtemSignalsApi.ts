// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM signals (dev-docs/ctem-signals.md): the changes in exposure that start
 * the loop. Read as the caller may see them; the activity stream frame
 * `ctem_signal_update` (dev-docs/ctem-loop-visibility.md section 5) invalidates
 * the `["ctem-signals"]` keys, so a recorded signal shows without a reload.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { ctemCasesPollInterval, ctemCasesRetry } from "./useCtemCasesApi";

export const CTEM_SIGNAL_STATES = ["new", "confirmed", "to_validate", "discarded", "watchlist", "case_opened"] as const;
export type CtemSignalState = (typeof CTEM_SIGNAL_STATES)[number];
type CtemLens = "inside" | "outside" | "world";

/**
 * A decision record as the reconcile and scope gate writes it (version 1,
 * dev-docs/ctem-scope-gate.md section 6): `reason_code` and `reason_params`
 * are what the UI translates; `reason` is the English line agents read.
 */
export interface CtemDecisionRecord {
  version?: number;
  outcome?: string;
  state?: string;
  reason?: string;
  reason_code?: string;
  reason_params?: Record<string, string | number>;
  inputs?: { lens?: string; source_provider?: string; cves?: string[] } & Record<string, unknown>;
  reconcile?: {
    assets?: string[];
    crown_jewels_named?: string[];
    tech_stack?: string[];
    lenses?: string[];
    confidence?: string;
  } & Record<string, unknown>;
  scope?: {
    criticality?: string | null;
    threshold?: string;
    risk_scenarios?: { id: string; name: string }[];
  } | null;
  policy_checks?: { rule: string; value?: unknown; threshold?: unknown; passed: boolean }[];
  severity?: string | null;
  decided_at?: string;
}

/** One signal as the API answers it (`to_dict` in backend/app/services/ctem_signals.py). */
export interface CtemSignal {
  id: string;
  trigger_path: string;
  trigger_path_label: string;
  lens: CtemLens;
  source: { provider: string; external_id: string };
  subject: { kind: string; ref: string; label: string };
  observed_at: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  seen_count: number;
  evidence: { link?: string } & Record<string, unknown>;
  state: CtemSignalState;
  decision: CtemDecisionRecord | null;
  case_id: string | null;
  recorded_via: string;
}

export interface CtemSignalList {
  total: number;
  counts: Partial<Record<CtemSignalState, number>>;
  signals: CtemSignal[];
}

/** The newest signals the caller sees, for the Live activity card. */
export function useRecentCtemSignals(limit = 6) {
  return useQuery<CtemSignalList>({
    queryKey: ["ctem-signals", "recent", limit],
    queryFn: () => api.get(`/ctem-signals?limit=${limit}`),
    retry: ctemCasesRetry,
    // The stream frame is the accelerator; the slow poll stays the floor and
    // stops after a refused read (#11999), as the cases sections do.
    refetchInterval: (query) => ctemCasesPollInterval(query.state.status, query.state.error),
    throwOnError: false,
  });
}

/**
 * The signals in one state (or every state), a page of 50 at a time through
 * `offset`, for the signal list of the loop trace (CTEM-01-UI; #877 UX round 1
 * B2: every signal reachable). Same stream key family as the recent read, so a
 * recorded signal joins the open list without a reload.
 */
export function useCtemSignalList(state: CtemSignalState | null, limit = 50, enabled = true) {
  const query = state ? `&state=${state}` : "";
  return useInfiniteQuery({
    queryKey: ["ctem-signals", "list", state ?? "all", limit],
    queryFn: ({ pageParam }): Promise<CtemSignalList> => api.get(`/ctem-signals?limit=${limit}&offset=${pageParam}${query}`),
    initialPageParam: 0,
    getNextPageParam: (last: CtemSignalList, pages: CtemSignalList[]) => {
      const loaded = pages.reduce((sum, page) => sum + page.signals.length, 0);
      return last.signals.length > 0 && loaded < last.total ? loaded : undefined;
    },
    enabled,
    retry: ctemCasesRetry,
    refetchInterval: (q) => ctemCasesPollInterval(q.state.status, q.state.error),
    throwOnError: false,
  });
}

/**
 * The signals that opened or joined one case, for its "Opened by" card
 * (CTEM-03-UI, #892). Read as the caller may see them; the `ctem_case_update`
 * frame of the case refreshes it.
 */
export function useCtemCaseSignals(caseId: string | undefined) {
  return useQuery<CtemSignalList>({
    queryKey: ["ctem-signals", "case", caseId],
    queryFn: () => api.get(`/ctem-signals?case_id=${caseId}&limit=50`),
    enabled: Boolean(caseId),
    retry: ctemCasesRetry,
    throwOnError: false,
  });
}

/** One trigger path with the signals the caller sees on it (`GET /ctem-signals/paths`, CTEM-30). */
export interface CtemSignalPath {
  id: string;
  label: string;
  default_lens: CtemLens;
  /** An integration listener records signals on this path. */
  listener: boolean;
  counts: Partial<Record<CtemSignalState, number>>;
  total: number;
  /** A listener feeds it or it holds a signal; otherwise it reads "Not configured", never 0. */
  configured: boolean;
}

/** The 12 trigger paths with their counts, for the Discovery stage's signals card. */
export function useCtemSignalPaths() {
  return useQuery<{ paths: CtemSignalPath[] }>({
    queryKey: ["ctem-signals", "paths"],
    queryFn: () => api.get("/ctem-signals/paths"),
    retry: ctemCasesRetry,
    refetchInterval: (query) => ctemCasesPollInterval(query.state.status, query.state.error),
    throwOnError: false,
  });
}

/**
 * "Propose to raise the scope" on a watchlisted signal (CTEM-30): stores an
 * organization context proposal an administrator accepts in Settings >
 * Organization. Nothing changes in the profile until then; never a gate.
 */
export function useProposeSignalScope() {
  const qc = useQueryClient();
  return useMutation<{ proposal_id: string; status: string; field: string; value: string }, Error, string>({
    mutationFn: (id) => api.post(`/ctem-signals/${encodeURIComponent(id)}/propose-scope`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-signals"] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

/** One signal; one the caller cannot read is a 404 the loop trace shows as "not available". */
export function useCtemSignal(id: string | undefined) {
  return useQuery<CtemSignal>({
    queryKey: ["ctem-signals", "signal", id],
    queryFn: () => api.get(`/ctem-signals/${id}`),
    enabled: Boolean(id),
    ...notFoundIsOrdinary,
  });
}
