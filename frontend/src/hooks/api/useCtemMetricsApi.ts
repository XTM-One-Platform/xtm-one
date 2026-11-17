// SPDX-License-Identifier: Apache-2.0

import { useQueries, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW, REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import type {
  CtemBriefingResponse,
  CtemPosture,
  CtemStageAgentsResponse,
  CtemStageAssessmentResponse,
  CtemVertical,
} from "@/types";

/** Cross-product Exposure Posture for the active vertical: composite score,
 *  per-stage scorecards, products, blind spots. XTM One mode only. */
export function useCtemPosture(vertical?: CtemVertical) {
  const q = vertical ? `?vertical=${vertical}` : "";
  return useQuery<CtemPosture>({
    queryKey: ["ctem-metrics", "posture", vertical ?? "default"],
    queryFn: () => api.get(`/ctem-metrics/posture${q}`),
    // While the server rebuilds a stale copy (`refreshing`), poll at the
    // standard pace so the fresh figures replace it as soon as they land (#12183).
    refetchInterval: (query) => (query.state.data?.refreshing ? REFETCH_INTERVAL_STANDARD : REFETCH_INTERVAL_SLOW),
    // The server serves a cached, stale-while-revalidate payload (rebuilt at
    // most once a day - see _SOFT_TTL), so a window-focus refetch only adds load
    // without surfacing newer data - the interval already keeps it current.
    refetchOnWindowFocus: false,
  });
}

/** A recorded day of posture history: composite score + per-stage metrics. */
interface CtemPostureHistoryPoint {
  date: string;
  posture_score: number | null;
  stage_metrics: Record<string, Record<string, number | null>>;
}
interface CtemPostureHistoryResponse {
  vertical: string;
  history: CtemPostureHistoryPoint[];
}

/** Historical per-stage posture metrics for the active vertical (oldest first),
 *  powering the trend charts. XTM One mode only. */
export function useCtemPostureHistory(vertical?: CtemVertical) {
  const q = vertical ? `?vertical=${vertical}` : "";
  return useQuery<CtemPostureHistoryResponse>({
    queryKey: ["ctem-metrics", "posture-history", vertical ?? "default"],
    queryFn: () => api.get(`/ctem-metrics/posture/history${q}`),
    refetchInterval: REFETCH_INTERVAL_SLOW,
    // History changes at most once/day; no need to refetch on every tab focus.
    refetchOnWindowFocus: false,
  });
}

/** The current user's AI exposure briefing for the active vertical (per-user,
 *  per-vertical, EE-gated). Polls faster while a generation is in flight. */
export function useCtemBriefing(vertical?: CtemVertical) {
  const q = vertical ? `?vertical=${vertical}` : "";
  return useQuery<CtemBriefingResponse>({
    queryKey: ["ctem-metrics", "briefing", vertical ?? "default"],
    queryFn: () => api.get(`/ctem-metrics/briefing${q}`),
    refetchInterval: (query) =>
      query.state.data?.status === "generating"
        ? REFETCH_INTERVAL_STANDARD
        : REFETCH_INTERVAL_SLOW,
  });
}

/** The query string of a refresh call: the vertical, and `auto` for a
 *  regeneration the page starts by itself on landing (#12183). */
function refreshQuery(vertical?: CtemVertical, auto?: boolean): string {
  const params = new URLSearchParams();
  if (vertical) params.set("vertical", vertical);
  if (auto) params.set("auto", "true");
  const s = params.toString();
  return s ? `?${s}` : "";
}

/** Generate the current user's briefing for the active vertical: on demand
 *  (a vertical), or on landing when it is missing or stale ({ vertical, auto }). */
export function useRefreshMyBriefing() {
  const qc = useQueryClient();
  const read = (arg?: CtemVertical | { vertical?: CtemVertical; auto?: boolean }) =>
    typeof arg === "object" ? arg : { vertical: arg, auto: false };
  return useMutation({
    mutationFn: (arg?: CtemVertical | { vertical?: CtemVertical; auto?: boolean }) => {
      const { vertical, auto } = read(arg);
      return api.post(`/ctem-metrics/briefing/refresh${refreshQuery(vertical, auto)}`);
    },
    onSuccess: (_data, arg) =>
      qc.invalidateQueries({
        queryKey: ["ctem-metrics", "briefing", read(arg).vertical ?? "default"],
      }),
  });
}

/** The current user's AI assessment for a single CTEM stage (per-user, EE-gated,
 *  scoped to the active vertical). Polls faster while generating. */
export function useCtemStageAssessment(stage: string, vertical?: CtemVertical) {
  const q = vertical ? `?vertical=${vertical}` : "";
  return useQuery<CtemStageAssessmentResponse>({
    queryKey: ["ctem-metrics", "stage-assessment", stage, vertical ?? "default"],
    queryFn: () => api.get(`/ctem-metrics/stages/${stage}/assessment${q}`),
    enabled: Boolean(stage),
    refetchInterval: (query) =>
      query.state.data?.status === "generating"
        ? REFETCH_INTERVAL_STANDARD
        : REFETCH_INTERVAL_SLOW,
    // The command center renders one assessment strip per stage (5x), each with
    // this query. The interval already keeps a generating assessment fresh, so a
    // window-focus refetch would only fire a redundant burst of 5 requests.
    refetchOnWindowFocus: false,
  });
}

/** The stored assessments of several stages, read only: the same cache entries
 *  as {@link useCtemStageAssessment}, so the Act now queue of the command center
 *  shares the stage cards' reads. Never generates one (no run at page load). */
export function useCtemStageAssessments(stages: readonly string[], vertical?: CtemVertical, enabled = true) {
  const q = vertical ? `?vertical=${vertical}` : "";
  return useQueries({
    queries: stages.map((stage) => ({
      queryKey: ["ctem-metrics", "stage-assessment", stage, vertical ?? "default"],
      queryFn: () => api.get(`/ctem-metrics/stages/${stage}/assessment${q}`) as Promise<CtemStageAssessmentResponse>,
      enabled,
      refetchInterval: (query: { state: { data?: CtemStageAssessmentResponse } }) =>
        query.state.data?.generating || query.state.data?.status === "generating"
          ? REFETCH_INTERVAL_STANDARD
          : REFETCH_INTERVAL_SLOW,
      refetchOnWindowFocus: false,
    })),
  });
}

/** Generate the current user's assessment for a stage on demand (as the user). */
export function useRefreshStageAssessment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ stage, vertical, auto }: { stage: string; vertical?: CtemVertical; auto?: boolean }) =>
      api.post(`/ctem-metrics/stages/${stage}/assessment/refresh${refreshQuery(vertical, auto)}`),
    onSuccess: (_data, { stage, vertical }) =>
      qc.invalidateQueries({
        queryKey: ["ctem-metrics", "stage-assessment", stage, vertical ?? "default"],
      }),
  });
}

/** Where CTEM analyst overrides are stored: platform-wide defaults (admin) or
 *  the current user's personal overrides. */
export type CtemAgentScope = "platform" | "user";

function stageAgentsPath(scope: CtemAgentScope): string {
  return scope === "user"
    ? "/ctem-metrics/my-stage-agents"
    : "/ctem-metrics/stage-agents";
}

/** CTEM analyst config for a vertical: per-stage rows + the Exposure Briefing
 *  Analyst row, each with the inherited default, the active override and the
 *  effective agent. `scope` selects platform (admin) vs per-user overrides. */
export function useStageAgents(scope: CtemAgentScope, vertical: CtemVertical) {
  return useQuery<CtemStageAgentsResponse>({
    queryKey: ["ctem-metrics", "stage-agents", scope, vertical],
    queryFn: () => api.get(`${stageAgentsPath(scope)}?vertical=${vertical}`),
  });
}

/** Assign / reset CTEM analyst overrides for a vertical. The overrides map is
 *  keyed by stage (or the reserved "briefing" key); null resets to the
 *  inherited default. */
export function useSetStageAgents(scope: CtemAgentScope) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      vertical,
      overrides,
    }: {
      vertical: CtemVertical;
      overrides: Record<string, string | null>;
    }) => api.put(stageAgentsPath(scope), { vertical, overrides }),
    // Overrides change the resolved stage owner and the briefing/assessment
    // agents shown across the command center, so refresh the whole CTEM cache.
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-metrics"] }),
  });
}

/** Force a live rebuild of the current user's posture for the active vertical.
 *  The request blocks until the server finishes the rebuild (cache writes are
 *  best-effort, so a warmed cache is not guaranteed); the refresh button spins
 *  for the whole rebuild and the existing data stays on screen until the fresh
 *  values land. A failed rebuild rejects (503), leaving the current data up. */
export function useRefreshCtemPosture() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vertical?: CtemVertical) =>
      api.post(`/ctem-metrics/sync${vertical ? `?vertical=${vertical}` : ""}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ctem-metrics"] }),
  });
}
