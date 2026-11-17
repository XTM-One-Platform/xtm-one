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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { BACKEND_FETCH_LIMIT } from "@/lib/constants";
import type {
  LLMSpanListResponse,
  LLMTrace,
  LLMTraceListResponse,
  PayloadField,
  TracePayloadResponse,
  TraceAnalyticsCostByAgentResponse,
  TraceAnalyticsCostByGroupResponse,
  TraceAnalyticsCostByModelResponse,
  TraceAnalyticsCostByUserResponse,
  TraceAnalyticsCostOverTimeResponse,
  TraceAnalyticsErrorsResponse,
  TraceAnalyticsFilters,
  TraceAnalyticsLatencyResponse,
  TraceAnalyticsSummary,
  TraceAnalyticsRefreshResponse,
  TraceAnalyticsVolumeOverTimeResponse,
  TraceFilters,
  TraceSessionFilters,
  TraceSessionExplorerResponse,
  TraceSessionListResponse,
} from "@/types";

interface TraceQueryOptions {
  enabled?: boolean;
  refetchInterval?: number;
  /** false keeps a failure in the card that reads it (its own error line and Retry) instead of the page boundary. */
  throwOnError?: false;
}

/** Spread so an absent option never overwrites the app's default `throwOnError: true` with undefined. */
function tolerant(options: TraceQueryOptions) {
  return options.throwOnError === false ? { throwOnError: false as const } : {};
}

function buildTraceParams(filters: TraceFilters = {}): string {
  const params = new URLSearchParams();
  if (filters.search) params.set("search", filters.search);
  if (filters.conversation_id) params.set("conversation_id", filters.conversation_id);
  if (filters.assignment_id) params.set("assignment_id", filters.assignment_id);
  if (filters.assignment_run_id) params.set("assignment_run_id", filters.assignment_run_id);
  for (const source of filters.source ?? []) params.append("source", source);
  for (const agentId of filters.agent_id ?? []) params.append("agent_id", agentId);
  for (const model of filters.model ?? []) params.append("model", model);
  if (filters.status) params.set("status", filters.status);
  for (const cause of filters.failure_cause ?? []) params.append("failure_cause", cause);
  if (filters.level) params.set("level", filters.level);
  if (filters.date_from) params.set("date_from", filters.date_from);
  if (filters.date_to) params.set("date_to", filters.date_to);
  if (filters.cursor) params.set("cursor", filters.cursor);
  if (filters.cursor_id) params.set("cursor_id", filters.cursor_id);
  if (filters.sort_by) params.set("sort_by", filters.sort_by);
  if (filters.sort_dir) params.set("sort_dir", filters.sort_dir);
  if (filters.offset) params.set("offset", String(filters.offset));
  if (filters.reverse) params.set("reverse", "true");
  for (const groupId of filters.group_id ?? []) params.append("group_id", groupId);
  const normalizedLimit =
    Number.isFinite(filters.limit) && (filters.limit ?? 0) >= 1
      ? Math.min(filters.limit ?? 50, 200)
      : 50;
  params.set("limit", String(normalizedLimit));
  return params.toString();
}

function buildAnalyticsParams(filters: TraceAnalyticsFilters = {}): string {
  const params = new URLSearchParams();
  const effectivePeriod = (filters.date_from || filters.date_to) ? "custom" : (filters.period ?? "30d");
  params.set("period", effectivePeriod);
  if (filters.date_from) params.set("date_from", filters.date_from);
  if (filters.date_to) params.set("date_to", filters.date_to);
  if (filters.agent_ids && filters.agent_ids.length > 0) params.set("agent_ids", filters.agent_ids.join(","));
  if (filters.models && filters.models.length > 0) params.set("models", filters.models.join(","));
  if (filters.sources && filters.sources.length > 0) params.set("sources", filters.sources.join(","));
  if (filters.group_ids && filters.group_ids.length > 0) params.set("group_ids", filters.group_ids.join(","));
  return params.toString();
}

export function useTraces(filters: TraceFilters, options: TraceQueryOptions = {}) {
  const qs = buildTraceParams(filters);
  return useQuery<LLMTraceListResponse>({
    queryKey: ["traces", filters],
    queryFn: () => api.get(`/traces?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
    ...tolerant(options),
  });
}

export function useTraceSessions(filters: TraceSessionFilters, options: TraceQueryOptions = {}) {
  const qs = buildTraceParams(filters);
  return useQuery<TraceSessionListResponse>({
    queryKey: ["trace-sessions", filters],
    queryFn: () => api.get(`/traces/sessions?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
  });
}

export function useTraceSessionExplorer(sessionId: string, options: TraceQueryOptions & { limit?: number } = {}) {
  const normalizedLimit =
    Number.isFinite(options.limit) && (options.limit ?? 0) >= 1
      ? Math.min(options.limit ?? 200, 500)
      : 200;
  return useQuery<TraceSessionExplorerResponse>({
    queryKey: ["trace-session-explorer", sessionId, normalizedLimit],
    queryFn: () => api.get(`/traces/sessions/${sessionId}/explorer?limit=${normalizedLimit}`),
    enabled: (options.enabled ?? true) && !!sessionId,
    refetchInterval: options.refetchInterval,
  });
}

export function useTrace(id: string) {
  return useQuery<LLMTrace>({
    queryKey: ["trace", id],
    queryFn: () => api.get(`/traces/${id}`),
    enabled: !!id,
  });
}

export function useTraceSpans(traceId: string, options: TraceQueryOptions = {}) {
  return useQuery<LLMSpanListResponse>({
    queryKey: ["trace-spans", traceId],
    queryFn: () => api.get(`/traces/${traceId}/spans`),
    enabled: (options.enabled ?? true) && !!traceId,
    refetchInterval: options.refetchInterval,
  });
}

/**
 * Fetch one payload field of a span in full.
 *
 * Span responses carry bounded previews so the Explorer stays responsive; this
 * is how the inspector loads the complete payload on demand, whether it was
 * stored inline or offloaded to object storage. Disabled until `enabled` flips,
 * so the fetch only happens when the user asks for it.
 *
 * The synthetic trace-root span (`t-<trace_id>`) resolves against the trace's
 * own payloads, which the backend endpoint handles transparently.
 */
export function useSpanPayload(
  traceId: string,
  spanId: string,
  field: PayloadField,
  options: TraceQueryOptions = {},
) {
  return useQuery<TracePayloadResponse>({
    queryKey: ["trace-span-payload", traceId, spanId, field],
    queryFn: () => api.get(`/traces/${traceId}/spans/${spanId}/payload?field=${field}`),
    enabled: (options.enabled ?? true) && !!traceId && !!spanId,
    // A payload is immutable once written: never refetch it.
    staleTime: Infinity,
    gcTime: 5 * 60 * 1000,
  });
}

/**
 * Fetch one media blob referenced by a span payload.
 *
 * Images and PDFs in a trace payload are stored in object storage and the
 * payload keeps a reference (content type, size, digest); this loads the actual
 * bytes so the inspector can show the image an agent really saw. Disabled until
 * `enabled` flips, because a span can reference several megabytes of media and
 * none of it should load just because a pane was opened.
 *
 * The blob is addressed by digest through the payload it belongs to — there is
 * no endpoint that takes an object name, by design.
 *
 * Returns the `Blob`, deliberately, not an object URL. An object URL is owned by
 * whoever must revoke it, and this value outlives its consumer: it is cached for
 * `gcTime`, so a component that created the URL here and revoked it on unmount
 * would leave a revoked — permanently broken — URL in the cache, which
 * `staleTime: Infinity` then serves to the next mount without refetching. The
 * Blob is immutable and safe to share; each consumer creates and revokes its own
 * URL from it.
 */
export function useSpanPayloadMedia(
  traceId: string,
  spanId: string,
  field: PayloadField,
  sha256: string,
  options: TraceQueryOptions = {},
) {
  return useQuery<Blob>({
    queryKey: ["trace-span-media", traceId, spanId, field, sha256],
    queryFn: async () => {
      const { blob } = await api.download(
        `/traces/${traceId}/spans/${spanId}/payload/media?field=${field}&sha256=${sha256}`,
      );
      return blob;
    },
    enabled: (options.enabled ?? true) && !!traceId && !!spanId && !!sha256,
    // Content-addressed by digest: the bytes behind it can never change.
    staleTime: Infinity,
    gcTime: 5 * 60 * 1000,
  });
}

export function useTraceAnalyticsSummary(filters: TraceAnalyticsFilters, options: TraceQueryOptions = {}) {
  const qs = buildAnalyticsParams(filters);
  return useQuery<TraceAnalyticsSummary>({
    queryKey: ["traces", "analytics", "summary", filters],
    queryFn: () => api.get(`/traces/analytics/summary?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
  });
}

export function useTraceAnalyticsCostByModel(filters: TraceAnalyticsFilters, options: TraceQueryOptions = {}) {
  const qs = buildAnalyticsParams(filters);
  return useQuery<TraceAnalyticsCostByModelResponse>({
    queryKey: ["traces", "analytics", "cost-by-model", filters],
    queryFn: () => api.get(`/traces/analytics/cost-by-model?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
    ...tolerant(options),
  });
}

export function useTraceAnalyticsCostOverTime(filters: TraceAnalyticsFilters, options: TraceQueryOptions = {}) {
  const qs = buildAnalyticsParams(filters);
  return useQuery<TraceAnalyticsCostOverTimeResponse>({
    queryKey: ["traces", "analytics", "cost-over-time", filters],
    queryFn: () => api.get(`/traces/analytics/cost-over-time?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
  });
}

export function useTraceAnalyticsVolumeOverTime(filters: TraceAnalyticsFilters, options: TraceQueryOptions = {}) {
  const qs = buildAnalyticsParams(filters);
  return useQuery<TraceAnalyticsVolumeOverTimeResponse>({
    queryKey: ["traces", "analytics", "volume-over-time", filters],
    queryFn: () => api.get(`/traces/analytics/volume-over-time?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
  });
}

export function useTraceAnalyticsLatency(filters: TraceAnalyticsFilters, options: TraceQueryOptions = {}) {
  const qs = buildAnalyticsParams(filters);
  return useQuery<TraceAnalyticsLatencyResponse>({
    queryKey: ["traces", "analytics", "latency", filters],
    queryFn: () => api.get(`/traces/analytics/latency?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
  });
}

export function useTraceAnalyticsCostByAgent(filters: TraceAnalyticsFilters, options: TraceQueryOptions = {}) {
  const qs = buildAnalyticsParams(filters);
  return useQuery<TraceAnalyticsCostByAgentResponse>({
    queryKey: ["traces", "analytics", "cost-by-agent", filters],
    queryFn: () => api.get(`/traces/analytics/cost-by-agent?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
    ...tolerant(options),
  });
}

export function useTraceAnalyticsCostByUser(filters: TraceAnalyticsFilters, options: TraceQueryOptions = {}) {
  const qs = buildAnalyticsParams(filters);
  return useQuery<TraceAnalyticsCostByUserResponse>({
    queryKey: ["traces", "analytics", "cost-by-user", filters],
    queryFn: () => api.get(`/traces/analytics/cost-by-user?${qs}&limit=${BACKEND_FETCH_LIMIT}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
    ...tolerant(options),
  });
}

export function useTraceAnalyticsCostByGroup(filters: TraceAnalyticsFilters, options: TraceQueryOptions = {}) {
  const qs = buildAnalyticsParams(filters);
  return useQuery<TraceAnalyticsCostByGroupResponse>({
    queryKey: ["traces", "analytics", "cost-by-group", filters],
    queryFn: () => api.get(`/traces/analytics/cost-by-group?${qs}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
    ...tolerant(options),
  });
}

export function useTraceAnalyticsErrors(
  filters: TraceAnalyticsFilters,
  options: TraceQueryOptions & { limit?: number } = {},
) {
  const qs = buildAnalyticsParams(filters);
  const limit = options.limit ? `&limit=${options.limit}` : "";
  return useQuery<TraceAnalyticsErrorsResponse>({
    queryKey: ["traces", "analytics", "errors", filters, options.limit ?? null],
    queryFn: () => api.get(`/traces/analytics/errors?${qs}${limit}`),
    enabled: options.enabled ?? true,
    refetchInterval: options.refetchInterval,
    ...tolerant(options),
  });
}

export function useRefreshTraceAnalytics() {
  const qc = useQueryClient();
  return useMutation<TraceAnalyticsRefreshResponse, Error>({
    mutationFn: () => api.post("/traces/analytics/refresh"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["traces", "analytics"] });
    },
  });
}
