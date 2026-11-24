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

/** Flow builder API hooks. */

import { keepPreviousData, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api, notFoundIsOrdinary, unavailableIsOrdinary } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import type {
  EntityListOptions,
  FlowCallableTool,
  CreateFlowRequest,
  FlowActivateResult,
  FlowChatEntry,
  FlowEventType,
  FlowIntegrationOperation,
  FlowDetachResult,
  Flow,
  FlowGraph,
  FlowImportResult,
  FlowNodeCatalog,
  FlowOutputSchemas,
  FlowRunDetail,
  FlowRunList,
  FlowRunStatus,
  FlowRunSummary,
  FlowSummary,
  FlowVersionList,
  UpdateFlowRequest,
} from "@/types";
import {
  REFETCH_INTERVAL_FAST,
  REFETCH_INTERVAL_STANDARD,
} from "@/lib/constants";
import { catalogThrowOnError, dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type { ExecutionRunSort } from "@/hooks/api/useAssignmentApi";

// --- Event types (typed event declarations the event nodes bind to) ---

export function useFlowEventTypes(enabled = true) {
  return useQuery<FlowEventType[]>({
    queryKey: ["flow-event-types"],
    queryFn: () => api.get("/flow-event-types"),
    enabled,
  });
}

export function useCreateFlowEventType() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      name: string;
      description?: string;
      fields: { name: string; type: string; required: boolean }[];
    }) => api.post<FlowEventType>("/flow-event-types", data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["flow-event-types"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

/** @public */
export function useDeleteFlowEventType() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/flow-event-types/${id}`),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ["flow-event-types"] });
      dropFromFlowNodeCatalog(qc, "event_type", id);
    },
  });
}

// --- Integration operations (the Integration node's callable actions) ---

export function useFlowIntegrationOperations(integrationId: string | null) {
  return useQuery<FlowIntegrationOperation[]>({
    queryKey: ["flow-integration-operations", integrationId],
    queryFn: () => api.get(`/flows/integration-operations/${integrationId}`),
    enabled: !!integrationId,
    staleTime: 5 * 60 * 1000,
    // A 403 (the integration's owner has not shared their personal OAuth
    // credentials) is shown on the node, not thrown to the builder's
    // ErrorBoundary. The retry policy stays the app's.
    throwOnError: unavailableIsOrdinary.throwOnError,
  });
}

export function useFlowCallableTools() {
  return useQuery<FlowCallableTool[]>({
    queryKey: ["flow-callable-tools"],
    queryFn: () => api.get("/flows/callable-tools"),
    staleTime: 60_000,
  });
}

// --- Debugger ---

export function useFlowRunDebug() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, action }: { runId: string; action: "continue" | "step" }) =>
      api.post(`/flows/runs/${runId}/debug`, { action }),
    onSuccess: (_data, { runId }) => {
      qc.invalidateQueries({ queryKey: ["flow-run", runId] });
      qc.invalidateQueries({ queryKey: ["flow-runs"] });
    },
  });
}

export function useFlowRunRespond() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, response }: { runId: string; response: string }) =>
      api.post(`/flows/runs/${runId}/respond`, { response }),
    onSuccess: (_data, { runId }) => {
      qc.invalidateQueries({ queryKey: ["flow-run", runId] });
      qc.invalidateQueries({ queryKey: ["flow-runs"] });
    },
  });
}

// --- Flows ---

export function useFlows(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<FlowSummary[]>({
    queryKey: entityListKey(["flows"], scope),
    queryFn: () => api.get(entityListPath("/flows", scope)),
  });
}

export function useFlow(
  id: string,
  /** A 404 is an answer, not a fault: the builder renders Page not found (#797 S4). */
  options: { notFoundIsOrdinary?: boolean } = {},
) {
  return useQuery<Flow>({
    queryKey: ["flow", id],
    queryFn: () => api.get(`/flows/${id}`),
    enabled: !!id,
    ...(options.notFoundIsOrdinary ? notFoundIsOrdinary : {}),
  });
}

export function useCreateFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateFlowRequest) => api.post<Flow>("/flows", data),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["flows"] });
    },
  });
}

export function useUpdateFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string } & UpdateFlowRequest) =>
      api.put<Flow>(`/flows/${id}`, data),
    onSuccess: (_d, { id, ...changed }) => {
      qc.invalidateQueries({ queryKey: ["flows"] });
      // The builder saves every graph edit through this hook while the catalog
      // is on screen; the catalog only reads a flow's name and who may see it.
      const listed = ["name", "user_id", "is_company_managed", "shared_group_ids"] as const;
      if (listed.some((field) => changed[field] !== undefined)) syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["flow", id] });
      qc.invalidateQueries({ queryKey: ["flow-versions", id] });
    },
  });
}

export function useDeleteFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/flows/${id}`),
    onSuccess: (_d, id) => {
      dropFromFlowNodeCatalog(qc, "flow", id);
      return qc.invalidateQueries({ queryKey: ["flows"] });
    },
  });
}

export function useDuplicateFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Flow>(`/flows/${id}/duplicate`),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["flows"] });
    },
  });
}

// --- Node catalog ---

/** The node library + the caller's own agents/integrations/assignments.
 *
 * Long-lived: every hook that changes an entity it lists syncs it
 * (`lib/flow-node-catalog.ts`), so the builder does not re-fetch it per edit.
 * A failed refetch keeps the catalog already on screen instead of replacing
 * the page (a flow's edits, a wizard's draft) with the error boundary, and the
 * page says so with a retry (`CatalogRefreshNotice`). */
export function useFlowNodeCatalog(enabled = true) {
  return useQuery<FlowNodeCatalog>({
    queryKey: ["flow-node-catalog"],
    queryFn: () => api.get("/flows/node-catalog"),
    enabled,
    staleTime: 5 * 60 * 1000,
    throwOnError: catalogThrowOnError,
  });
}

// --- Validation ---

/**
 * What each node of a draft graph declares for its outputs. `graphKey`
 * is the graph's JSON without positions, so moving a node asks nothing.
 */
export function useFlowOutputSchemas(graphKey: string, enabled = true) {
  return useQuery<FlowOutputSchemas>({
    queryKey: ["flow-output-schemas", graphKey],
    queryFn: () =>
      api.post<FlowOutputSchemas>("/flows/output-schemas", { graph: JSON.parse(graphKey) }),
    enabled: enabled && !!graphKey,
    // The previous answer stays on screen while an edit is re-asked.
    placeholderData: keepPreviousData,
    staleTime: 30 * 1000,
  });
}

// --- Versions ---

export function useFlowVersions(flowId: string, enabled = true) {
  return useQuery<FlowVersionList>({
    queryKey: ["flow-versions", flowId],
    queryFn: () => api.get(`/flows/${flowId}/versions`),
    enabled: !!flowId && enabled,
  });
}

export function useRestoreFlowVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      flowId,
      versionNumber,
      commitMessage,
    }: {
      flowId: string;
      versionNumber: number;
      commitMessage?: string;
    }) =>
      api.post<Flow>(`/flows/${flowId}/versions/${versionNumber}/restore`, {
        commit_message: commitMessage,
      }),
    onSuccess: (_d, { flowId }) => {
      qc.invalidateQueries({ queryKey: ["flow", flowId] });
      qc.invalidateQueries({ queryKey: ["flow-versions", flowId] });
      qc.invalidateQueries({ queryKey: ["flows"] });
    },
  });
}

// --- Import / export ---

/** Fetches the export document. The caller turns it into a download. */
export function useExportFlow() {
  return useMutation({
    mutationFn: (id: string) => api.get<Record<string, unknown>>(`/flows/${id}/export`),
  });
}

/** Accepts a native XTM One, LangFlow or n8n export.
 *
 * The result's `report` says how many nodes came in unresolved or awaiting
 * review and how many edges could not be reconnected — always surface it, an
 * import is never silently partial. */
export function useImportFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      document,
      name,
    }: {
      document: Record<string, unknown>;
      name?: string;
    }) => api.post<FlowImportResult>("/flows/import", { document, name }),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["flows"] });
    },
  });
}

/** Move a selection out into its own flow.
 *
 * Sends the CURRENT canvas rather than relying on the last save: detaching
 * against a stale server-side graph would split the wrong thing whenever the
 * editor has unsaved changes. The call therefore also persists the parent. */
export function useDetachFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      flowId,
      graph,
      nodeIds,
      name,
      mode = "detach",
    }: {
      flowId: string;
      graph: FlowGraph;
      nodeIds: string[];
      name?: string;
      /** "collapse" leaves a wired flow.call behind (UE Collapse to Function). */
      mode?: "detach" | "collapse";
    }) =>
      api.post<FlowDetachResult>(`/flows/${flowId}/detach`, {
        graph,
        node_ids: nodeIds,
        name,
        mode,
      }),
    onSuccess: (_d, { flowId }) => {
      qc.invalidateQueries({ queryKey: ["flows"] });
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["flow", flowId] });
      qc.invalidateQueries({ queryKey: ["flow-versions", flowId] });
    },
  });
}

// --- Execution ---


const ACTIVE_RUN_STATUSES: FlowRunStatus[] = ["pending", "running", "awaiting"];

export function useFlowRuns(flowId: string, enabled = true, limit = 20) {
  return useQuery<FlowRunList>({
    queryKey: ["flow-runs", flowId],
    queryFn: () => api.get(`/flows/${flowId}/runs?limit=${limit}`),
    enabled: !!flowId && enabled,
    // Poll fast only while something is actually moving; the global activity
    // stream invalidates this key too, so polling is the floor, not the wire.
    refetchInterval: (query) =>
      query.state.data?.runs.some((r) => ACTIVE_RUN_STATUSES.includes(r.status))
        ? REFETCH_INTERVAL_FAST
        : false,
  });
}

/** Flow runs across every visible flow — the Operations → Executions feed.
 * Own query-key root ("flow-runs-global") so the SSE handler can invalidate
 * it alongside the per-flow "flow-runs" keys. */
export function useAllFlowRuns(
  limit: number,
  offset: number,
  status?: string,
  search?: string,
  /** The visibility segment (`lib/visibility.ts`): mine | shared | company | all. */
  vis?: string,
  enabled = true,
  /** The order (`flow_runs.py` FLOW_RUN_SORT_PATTERN); absent = newest first. */
  sort?: ExecutionRunSort,
) {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (status) params.set("status", status);
  if (search) params.set("search", search);
  if (vis) params.set("vis", vis);
  if (sort) params.set("sort", sort);
  return useQuery<FlowRunList>({
    queryKey: [
      "flow-runs-global",
      limit,
      offset,
      status ?? "all",
      search ?? "",
      vis ?? "all",
      sort ?? "newest",
    ],
    queryFn: () => api.get(`/flows/runs/all?${params}`),
    enabled,
    // Triggers (schedule/webhook/events) create runs server-side, so keep a
    // slow floor even when everything visible is terminal.
    refetchInterval: (query) =>
      query.state.data?.runs.some((r) => ACTIVE_RUN_STATUSES.includes(r.status))
        ? REFETCH_INTERVAL_FAST
        : REFETCH_INTERVAL_STANDARD,
  });
}

export function useFlowRunDetail(runId: string | null) {
  return useQuery<FlowRunDetail>({
    queryKey: ["flow-run", runId],
    queryFn: () => api.get(`/flows/runs/${runId}`),
    enabled: !!runId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "running" || status === "pending") {
        return REFETCH_INTERVAL_FAST;
      }
      if (status === "awaiting") return REFETCH_INTERVAL_STANDARD;
      return false;
    },
  });
}

export function useRunFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      parameters,
      prompt,
      fileIds,
    }: {
      id: string;
      parameters?: Record<string, unknown>;
      prompt?: string;
      fileIds?: string[];
    }) => api.post<FlowRunSummary>(`/flows/${id}/run`, {
      parameters: parameters ?? {},
      prompt: prompt ?? null,
      file_ids: fileIds ?? [],
    }),
    onSuccess: (_d, { id }) =>
      qc.invalidateQueries({ queryKey: ["flow-runs", id] }),
  });
}

/** Conversational entries for ACTIVE chat-flows — the chat picker's Flows section.
 *
 *  `enabled` so the caller can skip the request when the `FLOW_BUILDER` feature
 *  flag hides the builder: the endpoint answers fine either way (the flag is
 *  UI-only), but a picker section naming flows is the feature showing through. */
export function useFlowChatEntries(enabled = true) {
  return useQuery<FlowChatEntry[]>({
    queryKey: ["flow-chat-entries"],
    queryFn: () => api.get<FlowChatEntry[]>("/flows/chat-entries"),
    staleTime: 30_000,
    enabled,
  });
}

/** Upload one run attachment; the returned file_id feeds runFlow's fileIds. */
export function uploadFlowRunFile(
  flowId: string,
  file: File,
): Promise<{ file_id: string; filename: string }> {
  const fd = new FormData();
  fd.append("file", file);
  return api.postForm<{ file_id: string; filename: string }>(
    `/flows/${flowId}/files`,
    fd,
  );
}

export function useActivateFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.post<FlowActivateResult>(`/flows/${id}/activate`),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ["flows"] });
      qc.invalidateQueries({ queryKey: ["flow", id] });
    },
  });
}

export function useDeactivateFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.post<FlowActivateResult>(`/flows/${id}/deactivate`),
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ["flows"] });
      qc.invalidateQueries({ queryKey: ["flow", id] });
    },
  });
}

export function useLintFlowCode() {
  return useMutation({
    mutationFn: (data: { code: string }) => api.post("/flows/lint-code", data),
  });
}

export function useCancelFlowRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ runId }: { runId: string; flowId: string }) =>
      api.post<FlowRunSummary>(`/flows/runs/${runId}/cancel`),
    onSuccess: (_d, { runId, flowId }) => {
      qc.invalidateQueries({ queryKey: ["flow-run", runId] });
      qc.invalidateQueries({ queryKey: ["flow-runs", flowId] });
    },
  });
}
