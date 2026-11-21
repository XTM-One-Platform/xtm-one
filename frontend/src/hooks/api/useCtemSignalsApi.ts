// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM signals (dev-docs/ctem-signals.md): the changes in exposure that start
 * the loop. Read as the caller may see them; the activity stream frame
 * `ctem_signal_update` (dev-docs/ctem-loop-visibility.md section 5) invalidates
 * the `["ctem-signals"]` keys, so a recorded signal shows without a reload.
 */
import { useQuery } from "@tanstack/react-query";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { ctemCasesPollInterval, ctemCasesRetry } from "./useCtemCasesApi";

const CTEM_SIGNAL_STATES = ["new", "confirmed", "to_validate", "discarded", "watchlist", "case_opened"] as const;
export type CtemSignalState = (typeof CTEM_SIGNAL_STATES)[number];
type CtemLens = "inside" | "outside" | "world";

/** A decision record (dev-docs/ctem-loop-visibility.md section 3), written by the reconcile and scope gate. */
export interface CtemDecisionRecord {
  outcome?: string;
  reason_code?: string;
  reason_params?: Record<string, string | number>;
  inputs?: { name: string; value: string | number | boolean | null; source?: string }[];
  at?: string;
  actor?: string;
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

/** One signal; one the caller cannot read is a 404 the loop trace shows as "not available". */
export function useCtemSignal(id: string | undefined) {
  return useQuery<CtemSignal>({
    queryKey: ["ctem-signals", "signal", id],
    queryFn: () => api.get(`/ctem-signals/${id}`),
    enabled: Boolean(id),
    ...notFoundIsOrdinary,
  });
}
