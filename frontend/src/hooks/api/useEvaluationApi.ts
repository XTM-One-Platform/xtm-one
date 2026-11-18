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

import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient, useQueries } from "@tanstack/react-query";
import { API_BASE, api, getToken } from "@/lib/api";
import { REFETCH_INTERVAL_FAST, REFETCH_INTERVAL_STANDARD, DEFAULT_CHART_RANGE } from "@/lib/constants";
import { saveBlobAs } from "@/lib/utils";
import { evaluationRunPollTier } from "@/lib/held";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import type {
  AgentTagsResponse,
  AvailableModel,
  BundleReview,
  CreateEvaluationRequest,
  EntityListOptions,
  Evaluation,
  EvaluationChartResponse,
  EvaluationComparison,
  MetricCatalogEntry,
  EvaluationDetail,
  EvaluationRun,
  EvaluationRunDetail,
  PlaceboComparison,
  PlaceboEstimate,
  RescoreFromRunsResult,
  RunDiff,
  RunMetric,
  RunMetricsAggregate,
  RunParamsResponse,
  RunSearchFilters,
  RunSearchResponse,
  RunTagsResponse,
  FamilyInfo,
  ExtractorPreviewResult,
  ExtractorPreviewSample,
  ExtractorSampleResult,
  ExtractorSpec,
} from "@/types";

// An explicit VITE_API_URL (split dev setup) wins; otherwise the same-origin
// API under the URL path prefix the platform is served from.
const API_V1_BASE = import.meta.env.VITE_API_URL
  ? `${String(import.meta.env.VITE_API_URL).replace(/\/$/, "")}/api/v1`
  : API_BASE;

function sanitizeErrorMessage(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const sanitized = value.replace(/[<>]/g, "").trim();
  if (!sanitized) return fallback;
  return sanitized.slice(0, 300);
}

async function authorizedApiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  return fetch(`${API_V1_BASE}${path}`, {
    ...init,
    headers,
  });
}

// ---------------------------------------------------------------------------
// Evaluation CRUD
// ---------------------------------------------------------------------------

/** Search agents across the platform for the evaluation agent pickers (admin-only,
 *  no per-user filtering). The server filters and caps results at 10.
 *
 *  `throwOnError: false`: the picker says a failed search in its list
 *  rather than handing the page to its ErrorBoundary. It is the only reader. */
export function useEvaluationAgents(search?: string, excludeIds?: string[]) {
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (excludeIds?.length) params.set("exclude_ids", excludeIds.join(","));
  const qs = params.toString();
  return useQuery<
    { id: string; name: string; slug: string | null; icon: string; is_company_managed: boolean; enabled: boolean }[]
  >({
    queryKey: ["evaluation-agents", search ?? "", excludeIds ?? []],
    queryFn: () => api.get(`/evaluations/agents/all${qs ? `?${qs}` : ""}`),
    throwOnError: false,
  });
}

/** The evaluations list; `{ scope: "all" }` is every evaluation for an admin (detail pages). */
export function useEvaluations(agentId?: string, options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  const params = agentId ? `?agent_id=${agentId}` : "";
  return useQuery<Evaluation[]>({
    queryKey: entityListKey(["evaluations", agentId ?? "all"], scope),
    queryFn: () => api.get(entityListPath(`/evaluations${params}`, scope)),
    refetchInterval: (query) =>
      (query.state.data as Evaluation[] | undefined)?.some((e) => e.has_active_runs)
        ? REFETCH_INTERVAL_FAST
        : false,
  });
}

export function useEvaluation(id: string) {
  return useQuery<EvaluationDetail>({
    queryKey: ["evaluation", id],
    queryFn: () => api.get(`/evaluations/${id}`),
    enabled: !!id,
  });
}

export function useCreateEvaluation() {
  const qc = useQueryClient();
  return useMutation<Evaluation, Error, CreateEvaluationRequest>({
    mutationFn: (data) => api.post("/evaluations", data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export function useUpdateEvaluation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...data
    }: { id: string } & Partial<CreateEvaluationRequest>) =>
      api.put(`/evaluations/${id}`, data),
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["evaluations"] });
      qc.invalidateQueries({ queryKey: ["evaluation", id] });
      // A family change may make a linked dataset conflict, or stop.
      qc.invalidateQueries({ queryKey: ["evaluation-datasets", id] });
    },
  });
}

export function useDeleteEvaluation() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/evaluations/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export function useCopyEvaluation() {
  const qc = useQueryClient();
  return useMutation<Evaluation, Error, string>({
    mutationFn: (id) => api.post(`/evaluations/${id}/copy`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export function useImportEvaluation() {
  const qc = useQueryClient();
  return useMutation<Evaluation, Error, File>({
    mutationFn: async (file) => {
      const formData = new FormData();
      formData.append("file", file);

      const res = await authorizedApiFetch("/evaluations/import", {
        method: "POST",
        body: formData,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Import failed" }));
        throw new Error(sanitizeErrorMessage(err.detail, "Import failed"));
      }
      return res.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export async function exportEvaluation(id: string, name: string) {
  const res = await authorizedApiFetch(`/evaluations/${id}/export`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Export failed" }));
    throw new Error(sanitizeErrorMessage(err.detail, "Export failed"));
  }

  const blob = await res.blob();
  const safeName = name.replace(/[^a-zA-Z0-9-_]/g, "_").slice(0, 60);
  const date = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  saveBlobAs(blob, `evaluation_${safeName}_${date}.zip`);
}

// ---------------------------------------------------------------------------
// Agent Binding
// ---------------------------------------------------------------------------

export function useAddEvaluationAgents() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      evaluationId,
      agentIds,
    }: {
      evaluationId: string;
      agentIds: string[];
    }) => api.post(`/evaluations/${evaluationId}/agents`, { agent_ids: agentIds }),
    onSuccess: (_d, { evaluationId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export function useRemoveEvaluationAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      evaluationId,
      agentId,
    }: {
      evaluationId: string;
      agentId: string;
    }) => api.delete(`/evaluations/${evaluationId}/agents/${agentId}`),
    onSuccess: (_d, { evaluationId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

// ---------------------------------------------------------------------------
// Evaluation Runs
// ---------------------------------------------------------------------------

export function useTriggerEvaluation() {
  const qc = useQueryClient();
  return useMutation<
    EvaluationRun[],
    Error,
    {
      evaluationId: string;
      modelIds?: string[];
      includeDefault?: boolean;
      /** Run the evaluation's placebo comparison instead of a plain run. */
      placebo?: boolean;
      confirmWriteTools?: boolean;
      /** The estimate's seed: the launch builds the placebos it checked. */
      placeboSeed?: string;
    }
  >({
    // Every caller shows `useTriggerFailedToast`, with the server's reason.
    meta: { suppressGlobalErrorToast: true },
    // The API also takes run `params`, `tags` and a `reuse` override at launch
    // (dev-docs/reference/evaluations.md §3, "Request bodies"). No
    // screen sets them: add them here only with the screen that does.
    mutationFn: ({ evaluationId, modelIds, includeDefault, placebo, confirmWriteTools, placeboSeed }) => {
      const body: Record<string, unknown> = {};
      if (modelIds !== undefined) body.model_ids = modelIds;
      if (includeDefault) body.include_default = true;
      if (placebo) body.placebo = true;
      if (confirmWriteTools) body.confirm_write_tools = true;
      if (placeboSeed) body.placebo_seed = placeboSeed;
      const hasBody = Object.keys(body).length > 0;
      return api.post(`/evaluations/${evaluationId}/trigger`, hasBody ? body : undefined);
    },
    onSuccess: (_d, { evaluationId, placebo }) => {
      qc.invalidateQueries({ queryKey: ["evaluation-runs", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
      if (placebo) qc.invalidateQueries({ queryKey: ["placebo-comparisons", evaluationId] });
    },
  });
}

/** What a placebo trigger would run on these models now, and what would refuse it. Writes nothing. */
export function usePlaceboEstimate(
  evaluationId: string,
  scope: { modelIds?: string[]; includeDefault?: boolean },
  enabled: boolean,
  /** Which opening of the dialog asks: a new one never reads an earlier one's estimate. */
  opening: number,
) {
  return useQuery<PlaceboEstimate>({
    queryKey: ["placebo-estimate", evaluationId, scope.modelIds ?? null, !!scope.includeDefault, opening],
    queryFn: () =>
      api.post(`/evaluations/${evaluationId}/placebo/estimate`, {
        ...(scope.modelIds !== undefined ? { model_ids: scope.modelIds } : {}),
        include_default: !!scope.includeDefault,
      }),
    enabled: enabled && !!evaluationId,
    // The estimate reads the agents and their last runs: fresh at each opening (``opening`` is in the key), and only then.
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
}

/** An evaluation's latest placebo comparisons, polled while one is still running. */
export function usePlaceboComparisons(evaluationId: string) {
  return useQuery<PlaceboComparison[]>({
    queryKey: ["placebo-comparisons", evaluationId],
    queryFn: () => api.get(`/evaluations/${evaluationId}/placebo-comparisons?limit=10`),
    enabled: !!evaluationId,
    // A comparison computes its statistics once its last run settles.
    refetchInterval: (query) =>
      query.state.data?.some((comparison) => comparison.status === "running" || comparison.status === "pending")
        ? REFETCH_INTERVAL_FAST
        : false,
  });
}

export function useTriggerEvaluationFromRuns() {
  const qc = useQueryClient();
  return useMutation<EvaluationRun[], Error, { evaluationId: string; runIds: string[] }>({
    meta: { suppressGlobalErrorToast: true },
    mutationFn: ({ evaluationId, runIds }) =>
      api.post(`/evaluations/${evaluationId}/trigger-from-runs`, {
        run_ids: runIds,
      }),
    onSuccess: (_d, { evaluationId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation-runs", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

/**
 * Bulk re-score selected runs from their stored predictions.
 * Per-run failures (no stored predictions, unknown run ID) come back in the
 * body, so a mixed batch resolves with HTTP 200; request-level failures (empty
 * `run_ids`, invalid `scorer_selection`, the admin/AI gates) reject as usual.
 */
export function useRescoreRunsFromRuns() {
  const qc = useQueryClient();
  return useMutation<
    { results: RescoreFromRunsResult[] },
    Error,
    { evaluationId: string; runIds: string[] }
  >({
    meta: { suppressGlobalErrorToast: true },
    mutationFn: ({ evaluationId, runIds }) =>
      api.post(`/evaluations/${evaluationId}/rescore-from-runs`, {
        run_ids: runIds,
      }),
    onSuccess: (_d, { evaluationId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation-runs", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export function useEvaluationRuns(
  evaluationId: string,
  limit = 20,
  offset = 0,
  options?: {
    refetchInterval?: number | false;
    /** Leave a placebo comparison's runs out, on the server, so they never fill the page. */
    excludeComparisons?: boolean;
  },
) {
  const exclude = !!options?.excludeComparisons;
  return useQuery<EvaluationRun[]>({
    queryKey: ["evaluation-runs", evaluationId, limit, offset, exclude],
    queryFn: () =>
      api.get(
        `/evaluations/${evaluationId}/runs?limit=${limit}&offset=${offset}${exclude ? "&exclude_comparisons=true" : ""}`,
      ),
    enabled: !!evaluationId,
    refetchInterval: options?.refetchInterval,
  });
}

/**
 * Total number of runs for an evaluation, for the Activity pagination. Keyed
 * under `["evaluation-runs", evaluationId, ...]` so every run mutation's
 * invalidation (trigger, delete, cancel, replay) refreshes it too.
 */
export function useEvaluationRunsCount(
  evaluationId: string,
  options?: { refetchInterval?: number | false },
) {
  return useQuery<number>({
    queryKey: ["evaluation-runs", evaluationId, "count"],
    queryFn: () =>
      api
        .get<{ count: number }>(`/evaluations/${evaluationId}/runs/count`)
        .then((r) => r.count),
    enabled: !!evaluationId,
    refetchInterval: options?.refetchInterval,
  });
}

/**
 * Polls while the run is in flight (agent execution or a background re-score),
 * so the run page picks up the terminal status without a refresh.
 *
 * Also polls while `rescore_pending` is set: a rescore batch
 * makes the run terminal BEFORE the batch's coalesced Overall Performance pass
 * commits, so a "completed" run may still be about to get its `performance_score`.
 *
 * And while `callback_pending` is set: a run waiting on an outside system
 * is terminal with an episode still parked, and its headlines are recomputed when
 * the callback lands. It is a separate flag because a callback can arrive hours
 * later, while a rescore's pass is short; it is cleared by the sweep that expires
 * a parked episode or by the resume once a callback is accepted.
 */
export function useEvaluationRun(runId: string) {
  return useQuery<EvaluationRunDetail>({
    queryKey: ["evaluation-run", runId],
    queryFn: () => api.get(`/evaluations/runs/${runId}`),
    enabled: !!runId,
    // A held run resumes by itself, so the page refreshes until the
    // run ends (see `evaluationRunPollTier`).
    refetchInterval: (query) => {
      const data = query.state.data as EvaluationRunDetail | undefined;
      if (data?.rescore_pending || data?.callback_pending) return REFETCH_INTERVAL_FAST;
      const tier = evaluationRunPollTier(data);
      if (tier === "fast") return REFETCH_INTERVAL_FAST;
      if (tier === "standard") return REFETCH_INTERVAL_STANDARD;
      return false;
    },
  });
}

// ---------------------------------------------------------------------------
// Run parameters & diff
// ---------------------------------------------------------------------------

export function useRunParams(runId: string) {
  return useQuery<RunParamsResponse>({
    queryKey: ["evaluation-run-params", runId],
    queryFn: () => api.get(`/evaluations/runs/${runId}/params`),
    enabled: !!runId,
  });
}

export function useRunDiff(runA?: string, runB?: string) {
  return useQuery<RunDiff>({
    queryKey: ["evaluation-run-diff", runA, runB],
    queryFn: () =>
      api.get(`/evaluations/runs/diff?run_a=${runA}&run_b=${runB}`),
    enabled: !!runA && !!runB,
  });
}

/** Evaluations of one family compared on their shared entries, or one evaluation's runs (#12282). `runIds`
 *  picks the runs compared; an evaluation none of whose runs is named keeps its newest. */
export function useEvaluationComparison(evaluationIds: string[], runIds: string[] = []) {
  const ids = evaluationIds.join(",");
  const runs = runIds.join(",");
  return useQuery<EvaluationComparison>({
    queryKey: ["evaluation-comparison", ids, runs],
    queryFn: () =>
      api.get(`/evaluations/compare?ids=${encodeURIComponent(ids)}${runs ? `&run_ids=${encodeURIComponent(runs)}` : ""}`),
    enabled: evaluationIds.length >= 1,
    // A refusal (another family, an unknown id) is the page's own message, not the boundary's.
    retry: false,
    throwOnError: false,
  });
}

/** One STIX entry's expected bundle against one to four answers. */
export function useBundleReview(resultIds: string[]) {
  const ids = resultIds.join(",");
  return useQuery<BundleReview>({
    queryKey: ["evaluation-bundle-review", ids],
    queryFn: () => api.get(`/evaluations/bundle-review?result_ids=${encodeURIComponent(ids)}`),
    enabled: resultIds.length > 0,
    retry: false,
    throwOnError: false,
  });
}

// ---------------------------------------------------------------------------
// Per-result metrics, run search & mutable tags
// ---------------------------------------------------------------------------

/** Scalar metrics logged for a single result. */
export function useResultMetrics(runId: string, resultId?: string) {
  return useQuery<RunMetric[]>({
    queryKey: ["evaluation-result-metrics", runId, resultId],
    queryFn: () =>
      api.get(`/evaluations/runs/${runId}/results/${resultId}/metrics`),
    enabled: !!runId && !!resultId,
  });
}

/** Server-side per-metric aggregates for a run. */
export function useRunMetricsAggregate(runId: string) {
  return useQuery<RunMetricsAggregate>({
    queryKey: ["evaluation-run-metrics-aggregate", runId],
    queryFn: () => api.get(`/evaluations/runs/${runId}/metrics/aggregate`),
    enabled: !!runId,
  });
}

/** Server-side run search over params, metrics, scores, tags and dates. */
export function useSearchRuns(filters: RunSearchFilters, enabled = true) {
  return useQuery<RunSearchResponse>({
    queryKey: ["evaluation-run-search", filters],
    queryFn: () => api.post("/evaluations/runs/search", filters),
    enabled,
  });
}

/** Read a run's mutable tags. */
export function useRunTags(runId: string) {
  return useQuery<RunTagsResponse>({
    queryKey: ["evaluation-run-tags", runId],
    queryFn: () => api.get(`/evaluations/runs/${runId}/tags`),
    enabled: !!runId,
  });
}

function invalidateRunTagQueries(qc: ReturnType<typeof useQueryClient>, runId: string) {
  qc.invalidateQueries({ queryKey: ["evaluation-run-tags", runId] });
  qc.invalidateQueries({ queryKey: ["evaluation-run", runId] });
  qc.invalidateQueries({ queryKey: ["evaluation-run-search"] });
}

/** Merge tags into a run; keys mapped to null are removed. */
export function usePatchRunTags() {
  const qc = useQueryClient();
  return useMutation<
    RunTagsResponse,
    Error,
    { runId: string; tags: Record<string, string | null> }
  >({
    mutationFn: ({ runId, tags }) =>
      api.patch(`/evaluations/runs/${runId}/tags`, { tags }),
    onSuccess: (_data, { runId }) => invalidateRunTagQueries(qc, runId),
  });
}

/** Designate the evaluation's baseline agent; `agentId=null` clears it. */
export function useSetAgentBaseline() {
  const qc = useQueryClient();
  return useMutation<
    { evaluation_id: string; baseline_agent_id: string | null },
    Error,
    { evaluationId: string; agentId: string | null }
  >({
    mutationFn: ({ evaluationId, agentId }) =>
      api.put(`/evaluations/${evaluationId}/baseline`, { agent_id: agentId }),
    onSuccess: (_data, { evaluationId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluation-run-search"] });
    },
  });
}

/** Replace an agent's evaluation-scoped tag set. */
export function useSetAgentTags() {
  const qc = useQueryClient();
  return useMutation<
    AgentTagsResponse,
    Error,
    { evaluationId: string; agentId: string; tags: Record<string, string> }
  >({
    mutationFn: ({ evaluationId, agentId, tags }) =>
      api.put(`/evaluations/${evaluationId}/agents/${agentId}/tags`, { tags }),
    onSuccess: (_data, { evaluationId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation", evaluationId] });
      qc.invalidateQueries({ queryKey: ["evaluation-run-search"] });
    },
  });
}

/** The selectable output families. */
export function useEvaluationFamilies() {
  return useQuery<FamilyInfo[]>({
    queryKey: ["evaluation-families"],
    queryFn: () => api.get("/evaluations/families"),
    staleTime: Infinity,
  });
}

/** Whether a prompt guard is connected on the platform, for the line under an evaluation's setting. */
export function useEvaluationPromptGuardState(enabled = true) {
  return useQuery<{ connected: boolean }>({
    queryKey: ["evaluation-prompt-guard"],
    queryFn: () => api.get("/evaluations/prompt-guard"),
    enabled,
    // The connection rarely changes, and the settings card mounts on every tab switch.
    staleTime: 30_000,
    // Advisory: on a failure the card shows no line, as when no guard is
    // connected, and the app-wide `throwOnError` must not reach the ErrorBoundary.
    retry: false,
    throwOnError: false,
  });
}

/** The selected agents' tools an administrator marked "always ask", for the warning under the tool approval setting. */
export function useEvaluationAlwaysAskTools(agentIds: string[], enabled = true) {
  const ids = useMemo(() => [...agentIds].sort(), [agentIds]);
  return useQuery<{ agents: { agent_id: string; agent_name: string; tools: string[] }[] }>({
    queryKey: ["evaluation-always-ask-tools", ids],
    queryFn: () => {
      const params = new URLSearchParams();
      ids.forEach((id) => params.append("agent_ids", id));
      return api.get(`/evaluations/always-ask-tools?${params.toString()}`);
    },
    enabled: enabled && ids.length > 0,
    staleTime: 30_000,
    // Advisory: on a failure the card says it could not check. The
    // app-wide `throwOnError` must not hand a 503 to the page's ErrorBoundary,
    // and a retry would read every agent's tools again first.
    retry: false,
    throwOnError: false,
  });
}

/** Re-run the evaluation's scorers on a run's stored predictions. */
export function useRescoreRun() {
  const qc = useQueryClient();
  return useMutation<
    { run_id: string; status: "running" },
    Error,
    { runId: string }
  >({
    mutationFn: ({ runId }) => api.post(`/evaluations/runs/${runId}/rescore`),
    onSuccess: (_data, { runId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation-run", runId] });
      qc.invalidateQueries({ queryKey: ["evaluation-runs"] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
    },
  });
}

export function useMultipleEvaluationRuns(runIds: string[]) {
  return useQueries({
    queries: runIds.map((id) => ({
      queryKey: ["evaluation-run", id] as const,
      queryFn: (): Promise<EvaluationRunDetail> => api.get(`/evaluations/runs/${id}`),
      enabled: !!id,
    })),
  });
}

export function useDeleteEvaluationRun() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (runId) => api.delete(`/evaluations/runs/${runId}`),
    // The batch-delete caller reports per-run outcomes in one summary toast;
    // otherwise a failed 20-run batch fires 21 error toasts.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evaluation-runs"] });
      // A placebo comparison's run takes its comparison with it.
      qc.invalidateQueries({ queryKey: ["placebo-comparisons"] });
    },
  });
}

export function useCancelEvaluationRun() {
  const qc = useQueryClient();
  return useMutation<EvaluationRun, Error, string>({
    mutationFn: (runId) => api.post(`/evaluations/runs/${runId}/cancel`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["evaluation-runs"] });
      qc.invalidateQueries({ queryKey: ["evaluation-run"] });
    },
  });
}

export function useReplayEvaluationRun(options?: { onSuccess?: (newRun: EvaluationRun) => void }) {
  const qc = useQueryClient();
  return useMutation<EvaluationRun, Error, { runId: string; reuse?: "inherit" | "on" | "off" }>({
    mutationFn: ({ runId, reuse }) =>
      api.post(`/evaluations/runs/${runId}/replay`, reuse ? { reuse } : undefined),
    onSuccess: (newRun, { runId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation-run", runId] });
      qc.invalidateQueries({ queryKey: ["evaluation-runs"] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
      options?.onSuccess?.(newRun);
    },
  });
}

export function useReplayJudgeResult() {
  const qc = useQueryClient();
  return useMutation<
    { status: string; run_id: string; result_id: string; score: number; judge_max_tokens: number; judge_model?: string; judge_stop_reason?: string | null },
    Error,
    { runId: string; resultId: string; judgeMaxTokens: number }
  >({
    mutationFn: ({ runId, resultId, judgeMaxTokens }) =>
      api.post(`/evaluations/runs/${runId}/results/${resultId}/replay-judge`, {
        judge_max_tokens: judgeMaxTokens,
      }),
    onSuccess: (_d, { runId }) => {
      qc.invalidateQueries({ queryKey: ["evaluation-run", runId] });
      qc.invalidateQueries({ queryKey: ["evaluation-runs"] });
      qc.invalidateQueries({ queryKey: ["evaluation-chart"] });
    },
  });
}

// ---------------------------------------------------------------------------
// Bulk run deletion
// ---------------------------------------------------------------------------

export function useDeleteAllEvaluationRuns() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (evaluationId) =>
      api.delete(`/evaluations/${evaluationId}/runs`),
    onSuccess: (_d, evaluationId) => {
      qc.invalidateQueries({ queryKey: ["evaluation-runs"] });
      qc.invalidateQueries({ queryKey: ["evaluation-chart"] });
      qc.invalidateQueries({ queryKey: ["evaluations"] });
      qc.invalidateQueries({ queryKey: ["placebo-comparisons", evaluationId] });
    },
  });
}

// ---------------------------------------------------------------------------
// Chart Data
// ---------------------------------------------------------------------------

/**
 * Six chart series for one evaluation.
 *
 * `range` is part of the query key so each window caches separately rather than
 * evicting the previous one on every switch.
 */
export function useEvaluationChart(evaluationId: string, range: string = DEFAULT_CHART_RANGE) {
  return useQuery<EvaluationChartResponse>({
    queryKey: ["evaluation-chart", evaluationId, range],
    queryFn: () => api.get(`/evaluations/${evaluationId}/chart?range=${encodeURIComponent(range)}`),
    enabled: !!evaluationId,
  });
}

/**
 * The metric keys selectable for this evaluation's family.
 *
 * Read from the package catalogue, not the stored metrics, so a metric is
 * selectable before it has ever been computed.
 */
export function useEvaluationMetricCatalog(evaluationId: string) {
  return useQuery<MetricCatalogEntry[]>({
    queryKey: ["evaluation-metric-catalog", evaluationId],
    queryFn: () => api.get(`/evaluations/${evaluationId}/metric-catalog`),
    enabled: !!evaluationId,
  });
}

/**
 * The chat models the evaluation's owner may use: what its save and its launches
 * accept, unlike the caller's own list. Keyed under "available-models", so a provider
 * change refreshes it with every model picker.
 */
export function useEvaluationAvailableModels(evaluationId: string | undefined, options: { enabled?: boolean } = {}) {
  return useQuery<AvailableModel[]>({
    queryKey: ["available-models", "evaluation", evaluationId],
    queryFn: () => api.get(`/evaluations/${evaluationId}/available-models`),
    enabled: !!evaluationId && (options.enabled ?? true),
  });
}

/** The same catalog for a family, before an evaluation exists: the creation screen. */
export function useFamilyMetricCatalog(family: string | null | undefined) {
  return useQuery<MetricCatalogEntry[]>({
    queryKey: ["evaluation-family-metric-catalog", family],
    queryFn: () => api.get(`/evaluations/metric-catalog?family=${encodeURIComponent(family ?? "")}`),
    enabled: !!family,
  });
}

export async function downloadEvaluationRunResultTrace(
  runId: string,
  resultId: string,
  traceIndex: number,
) {
  const res = await authorizedApiFetch(
    `/evaluations/runs/${runId}/results/${resultId}/traces/${traceIndex}/download`,
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Trace download failed" }));
    throw new Error(sanitizeErrorMessage(err.detail, "Trace download failed"));
  }

  const blob = await res.blob();
  saveBlobAs(blob, `evaluation_trace_${runId}_${resultId}_${traceIndex}.json`);
}

/** Stored results an author may preview an extractor against. */
export function useExtractorSampleResults(
  evaluationId: string,
  agentId: string,
  enabled: boolean,
) {
  return useQuery<ExtractorSampleResult[]>({
    queryKey: ["evaluation-extractor-samples", evaluationId, agentId],
    queryFn: () =>
      api.get(
        `/evaluations/${evaluationId}/extractors/sample-results?agent_id=${agentId}`,
      ),
    enabled: enabled && !!evaluationId && !!agentId,
  });
}

/** Preview an extractor against a stored result or a pasted sample.
 *
 * Read-only on the server: it creates no run, result or history entry, so this is
 * a mutation only because it carries a body.
 */
export function usePreviewExtractor() {
  return useMutation<
    ExtractorPreviewResult,
    Error,
    {
      evaluationId: string;
      agentId: string;
      extractor: ExtractorSpec;
      resultId?: string;
      sample?: ExtractorPreviewSample;
    }
  >({
    mutationFn: ({ evaluationId, agentId, extractor, resultId, sample }) =>
      api.post(`/evaluations/${evaluationId}/extractors/preview`, {
        agent_id: agentId,
        extractor,
        result_id: resultId ?? null,
        sample: sample ?? null,
      }),
  });
}

/** A pasted-sample preview before the evaluation exists, read from its family. */
export function usePreviewExtractorForFamily() {
  return useMutation<
    ExtractorPreviewResult,
    Error,
    { family: string; extractor: ExtractorSpec; sample: ExtractorPreviewSample }
  >({
    mutationFn: ({ family, extractor, sample }) =>
      api.post("/evaluations/extractors/preview", { family, extractor, sample }),
  });
}
