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

/** Knowledge base, document, sync source, and search API hooks. */

import { keepPreviousData, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { API_BASE, api, getToken, notFoundIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_FAST, REFETCH_INTERVAL_SLOW, REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { saveBlobAs } from "@/lib/utils";
import type {
  EntityListOptions,
  GlobalKnowledgeDocumentsResponse,
  GlobalRAGSearchResponse,
  KnowledgeBase,
  KnowledgeBaseHtmlIngestion,
  KnowledgeDocument,
  KBExportPackage,
  KBExportResponse,
  KBSyncSource,
  KBActivityLog,
  RAGSearchResponse,
} from "@/types";
import type { KBEmbeddingDimensionStat } from "@/types";

// --- Knowledge Bases ---
// Creating, updating, deleting or importing one also syncs the flow node
// catalog, where the flow builder's and the agent wizard's pickers list them;
// a deleted one leaves the cached catalog at once.

/** The knowledge bases list; `{ scope: "all" }` is every knowledge base for an admin (detail pages). */
function knowledgeBasesQuery(scope: ReturnType<typeof useEntityListScope>) {
  return {
    queryKey: entityListKey(["knowledge-bases"], scope),
    queryFn: () => api.get<KnowledgeBase[]>(entityListPath("/knowledge-bases", scope)),
    // Poll fast while some KB is actively indexing; fall back to a slow poll
    // when idle so externally-initiated re-indexing (server-side
    // auto-remediation, other users' uploads) is still detected. The whole
    // platform's list is always somewhere indexing: it stays on the slow poll.
    refetchInterval: (query: { state: { data?: KnowledgeBase[] } }) =>
      !scope && query.state.data?.some((kb) => (kb.pending_count ?? 0) > 0)
        ? REFETCH_INTERVAL_FAST
        : REFETCH_INTERVAL_SLOW,
  };
}

export function useKnowledgeBases(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<KnowledgeBase[]>(knowledgeBasesQuery(scope));
}

const dropWorkspaceOwned = (bases: KnowledgeBase[]) => bases.filter((kb) => !kb.managed_by_workspace);

/**
 * The knowledge bases a picker may bind (to an agent, a flow, a person's
 * defaults, a workspace, a pack): never a workspace's own one, which
 * reaches its turns through the workspace and is refused by every binding
 * route. A list page shows them all, with its badge.
 */
export function useBindableKnowledgeBases(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  // The same cached list, filtered by `select` (memoised on the data).
  return useQuery<KnowledgeBase[]>({ ...knowledgeBasesQuery(scope), select: dropWorkspaceOwned });
}

export function useCreateKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation<KnowledgeBase, Error, { name: string; description?: string; is_company_managed?: boolean; is_read_only?: boolean; html_ingestion?: KnowledgeBaseHtmlIngestion; describe_figures?: boolean; tags?: string[] }>({
    mutationFn: (data) => api.post("/knowledge-bases", data),
    onSuccess: (kb) => {
      syncFlowNodeCatalog(qc, kb);
      return qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

/**
 * One knowledge base. A 404 is an ordinary answer (deleted, not shared with
 * the caller, or a workset's internal storage, whose `detail` names the
 * workset): the page shows its not-found state, never the error boundary.
 */
export function useKnowledgeBase(kbId: string) {
  return useQuery<KnowledgeBase>({
    queryKey: ["knowledge-base", kbId],
    queryFn: () => api.get(`/knowledge-bases/${kbId}`),
    enabled: !!kbId,
    ...notFoundIsOrdinary,
    refetchInterval: (query) =>
      (query.state.data?.pending_count ?? 0) > 0 ? REFETCH_INTERVAL_STANDARD : false,
  });
}

/** The knowledge bases synced from an integration; `{ scope: "all" }` is every one of them for an admin (detail pages). */
export function useKBsByIntegration(integrationId: string, options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<KnowledgeBase[]>({
    queryKey: entityListKey(["knowledge-bases", "by-integration", integrationId], scope),
    queryFn: () => api.get(entityListPath(`/knowledge-bases?integration_id=${integrationId}`, scope)),
    enabled: !!integrationId,
  });
}

export function useUpdateKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string; name?: string; description?: string; is_company_managed?: boolean; is_read_only?: boolean; html_ingestion?: KnowledgeBaseHtmlIngestion; describe_figures?: boolean; tags?: string[]; user_id?: string | null }) =>
      api.put(`/knowledge-bases/${id}`, data),
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
      qc.invalidateQueries({ queryKey: ["knowledge-base", id] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useDeleteKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/knowledge-bases/${id}`),
    onSuccess: (_d, id) => {
      dropFromFlowNodeCatalog(qc, "knowledge_base", id);
      return qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

export function useKBExportPackages(kbId: string) {
  return useQuery<KBExportPackage[]>({
    queryKey: ["kb-export-packages", kbId],
    queryFn: () => api.get(`/knowledge-bases/${kbId}/exports`),
    enabled: !!kbId,
  });
}

export function useExportKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation<KBExportResponse, Error, string>({
    mutationFn: (kbId) => api.post(`/knowledge-bases/${kbId}/export`),
    onSuccess: (_data, kbId) => {
      qc.invalidateQueries({ queryKey: ["kb-export-packages", kbId] });
    },
  });
}

export function useImportKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation<KnowledgeBase, Error, File[]>({
    mutationFn: async (files) => {
      if (!files.length) throw new Error("No ZIP file selected");
      const formData = new FormData();
      files.forEach((file) => formData.append("files", file));
      const token = getToken();
      const resp = await fetch(`${API_BASE}/knowledge-bases/import`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: formData,
      });
      if (!resp.ok) {
        const err = await resp
          .json()
          .catch(() => ({ detail: "Knowledge base import failed" }));
        throw new Error(err.detail || `HTTP ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: (kb) => {
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
      syncFlowNodeCatalog(qc, kb);
    },
  });
}

export function useKnowledgeDocuments(kbId: string, limit = 50, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return useQuery<KnowledgeDocument[]>({
    queryKey: ["kb-documents", kbId, limit, offset],
    queryFn: () => api.get(`/knowledge-bases/${kbId}/documents?${params}`),
    enabled: !!kbId,
    refetchInterval: (query) => {
      const docs = query.state.data ?? [];
      const hasActive = docs.some(
        (d) => d.status === "pending" || d.status === "processing"
      );
      return hasActive ? REFETCH_INTERVAL_STANDARD : false;
    },
  });
}

export function useKnowledgeDocumentCount(kbId: string) {
  return useQuery<{ count: number }>({
    queryKey: ["kb-documents-count", kbId],
    queryFn: () => api.get(`/knowledge-bases/${kbId}/documents/count`),
    enabled: !!kbId,
  });
}

export function useGlobalKnowledgeDocuments(params: {
  limit: number;
  offset: number;
  status?: string;
  kbName?: string;
  kbIds?: string;
  /** Words the file name or the knowledge base name holds. */
  q?: string;
  sort?: "created_at" | "filename" | "knowledge_base" | "status";
  direction?: "asc" | "desc";
}) {
  const qs = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  });
  if (params.status && params.status !== "all") qs.set("status", params.status);
  if (params.kbName) qs.set("kb_name", params.kbName);
  if (params.kbIds) qs.set("kb_ids", params.kbIds);
  if (params.q) qs.set("q", params.q);
  if (params.sort) qs.set("sort", params.sort);
  if (params.direction) qs.set("direction", params.direction);

  return useQuery<GlobalKnowledgeDocumentsResponse>({
    queryKey: [
      "kb-documents-global",
      params.limit,
      params.offset,
      params.status ?? "all",
      params.kbName ?? "",
      params.kbIds ?? "",
      params.q ?? "",
      params.sort ?? "created_at",
      params.direction ?? "desc",
    ],
    queryFn: () => api.get(`/knowledge-bases/global/documents?${qs.toString()}`),
    placeholderData: (previousData) => previousData,
    // The documents table draws its own error line with Retry, never the page boundary.
    throwOnError: false,
    refetchInterval: (query) => {
      const docs = query.state.data?.items ?? [];
      const hasActive = docs.some(
        (d) => d.status === "pending" || d.status === "processing"
      );
      return hasActive ? REFETCH_INTERVAL_STANDARD : false;
    },
  });
}

export function useAddTextDocument() {
  const qc = useQueryClient();
  return useMutation<KnowledgeDocument, Error, { kbId: string; filename: string; content: string; source_type?: string }>({
    mutationFn: ({ kbId, ...data }) => api.post(`/knowledge-bases/${kbId}/documents/text`, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

export function useUploadDocument() {
  const qc = useQueryClient();
  return useMutation<KnowledgeDocument, Error, { kbId: string; file: File }>({
    mutationFn: async ({ kbId, file }) => {
      const formData = new FormData();
      formData.append("file", file);
      const token = getToken();
      const resp = await fetch(`${API_BASE}/knowledge-bases/${kbId}/documents/upload`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: formData,
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "Upload failed" }));
        throw new Error(err.detail || `HTTP ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

export async function downloadDocument(kbId: string, docId: string, filename: string) {
  const token = getToken();
  const resp = await fetch(`${API_BASE}/knowledge-bases/${kbId}/documents/${docId}/download`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) throw new Error(`Download failed: HTTP ${resp.status}`);
  const blob = await resp.blob();
  saveBlobAs(blob, filename);
}

// --- KB Sync Sources (scheduled syncs) ---
export function useKBSyncSources(kbId: string) {
  return useQuery<KBSyncSource[]>({
    queryKey: ["kb-sync-sources", kbId],
    queryFn: () => api.get(`/knowledge-bases/${kbId}/sync-sources`),
    enabled: !!kbId,
  });
}

export function useCreateKBSyncSource() {
  const qc = useQueryClient();
  return useMutation<KBSyncSource, Error, { kbId: string; source_type: string; config: Record<string, string>; sync_interval: string; integration_id?: string }>({
    mutationFn: ({ kbId, ...data }) =>
      api.post(`/knowledge-bases/${kbId}/sync-sources`, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-sync-sources"] });
    },
  });
}

export function useUpdateKBSyncSource() {
  const qc = useQueryClient();
  return useMutation<KBSyncSource, Error, { kbId: string; sourceId: string; config?: Record<string, string>; sync_interval?: string; enabled?: boolean; integration_id?: string }>({
    mutationFn: ({ kbId, sourceId, ...data }) =>
      api.put(`/knowledge-bases/${kbId}/sync-sources/${sourceId}`, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-sync-sources"] });
    },
  });
}

export function useDeleteKBSyncSource() {
  const qc = useQueryClient();
  return useMutation<void, Error, { kbId: string; sourceId: string }>({
    mutationFn: ({ kbId, sourceId }) =>
      api.delete(`/knowledge-bases/${kbId}/sync-sources/${sourceId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-sync-sources"] });
    },
  });
}

export function useTriggerKBSyncSource() {
  const qc = useQueryClient();
  return useMutation<{ status: string; message: string }, Error, { kbId: string; sourceId: string }>({
    mutationFn: ({ kbId, sourceId }) =>
      api.post(`/knowledge-bases/${kbId}/sync-sources/${sourceId}/trigger`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-sync-sources"] });
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

export function useForceResyncKBSyncSource() {
  const qc = useQueryClient();
  return useMutation<
    { status: string; message: string; purged_documents: number },
    Error,
    { kbId: string; sourceId: string }
  >({
    mutationFn: ({ kbId, sourceId }) =>
      api.post(`/knowledge-bases/${kbId}/sync-sources/${sourceId}/force-resync`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-sync-sources"] });
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

export function useDeleteDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ kbId, docId }: { kbId: string; docId: string }) =>
      api.delete(`/knowledge-bases/${kbId}/documents/${docId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

export function useReprocessDocuments() {
  const qc = useQueryClient();
  return useMutation<
    { requeued: number; message: string },
    Error,
    { kbId: string; includeReady?: boolean }
  >({
    mutationFn: ({ kbId, includeReady }) => {
      const qs = includeReady ? "?include_ready=true" : "";
      return api.post(`/knowledge-bases/${kbId}/documents/reprocess${qs}`);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
    },
  });
}

export function useReprocessDocument() {
  const qc = useQueryClient();
  return useMutation<
    { requeued: number; message: string },
    Error,
    { kbId: string; docId: string }
  >({
    mutationFn: ({ kbId, docId }) =>
      api.post(`/knowledge-bases/${kbId}/documents/${docId}/reprocess`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["kb-documents-global"] });
    },
  });
}

export function useReprocessAllStale() {
  const qc = useQueryClient();
  return useMutation<
    // `kbs_skipped`: stale KBs a skill package installed, which the route
    // does not re-embed.
    { requeued: number; kbs_affected: number; kbs_skipped?: number; message: string },
    Error,
    void
  >({
    mutationFn: () => api.post("/knowledge-bases/global/reprocess-stale"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge-bases"] });
      qc.invalidateQueries({ queryKey: ["kb-documents"] });
      qc.invalidateQueries({ queryKey: ["kb-documents-global"] });
    },
  });
}

/** The event types of the KB activity filter (`event_type`, repeated; the API's ACTIVITY_EVENT_TYPES). */
export type KBActivityEventType = "sync" | "documents" | "embeddings";

export interface KBActivityFilters {
  search?: string;
  eventTypes?: readonly KBActivityEventType[];
  /** Time order, newest first by default. */
  direction?: "asc" | "desc";
}

function kbActivityParams({ search, eventTypes }: KBActivityFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  for (const type of eventTypes ?? []) params.append("event_type", type);
  return params;
}

export function useKBActivity(kbId: string, limit = 50, offset = 0, filters: KBActivityFilters = {}) {
  const params = kbActivityParams(filters);
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  if (filters.direction) params.set("direction", filters.direction);
  return useQuery<KBActivityLog[]>({
    queryKey: ["kb-activity", kbId, limit, offset, params.toString()],
    queryFn: () => api.get(`/knowledge-bases/${kbId}/activity?${params}`),
    enabled: !!kbId,
    // The previous page stays shown while a filter change refetches, so the toolbar never remounts.
    placeholderData: keepPreviousData,
  });
}

export function useKBActivityCount(kbId: string, filters: KBActivityFilters = {}) {
  const qs = kbActivityParams(filters).toString();
  return useQuery<{ count: number }>({
    queryKey: ["kb-activity-count", kbId, qs],
    queryFn: () => api.get(`/knowledge-bases/${kbId}/activity/count${qs ? `?${qs}` : ""}`),
    enabled: !!kbId,
    placeholderData: keepPreviousData,
  });
}

export function useTestKBSearch() {
  return useMutation<RAGSearchResponse, Error, { kbId: string; query: string; top_k?: number }>({
    mutationFn: ({ kbId, ...data }) =>
      api.post(`/knowledge-bases/${kbId}/test-search`, data),
  });
}

export function useGlobalKBSearch() {
  return useMutation<
    GlobalRAGSearchResponse,
    Error,
    { query: string; top_k?: number; kb_ids?: string[] }
  >({
    mutationFn: (data) => api.post("/knowledge-bases/global/test-search", data),
  });
}

export function useKBEmbeddingStats(kbId: string) {
  return useQuery<KBEmbeddingDimensionStat[]>({
    queryKey: ["kb-embedding-stats", kbId],
    queryFn: () => api.get(`/knowledge-bases/${kbId}/embedding-stats`),
    enabled: !!kbId,
  });
}

export function useDeleteEmbeddingsByDimension() {
  const qc = useQueryClient();
  return useMutation<
    void,
    Error,
    { kbId: string; dim: number; provider?: string | null; model?: string | null }
  >({
    mutationFn: ({ kbId, dim, provider, model }) => {
      const params = new URLSearchParams();
      if (provider) params.set("provider", provider);
      if (model) params.set("model", model);
      const qs = params.toString();
      return api.delete(
        `/knowledge-bases/${kbId}/embeddings/dimension/${dim}${qs ? `?${qs}` : ""}`
      );
    },
    onSuccess: (_d, { kbId }) => {
      qc.invalidateQueries({ queryKey: ["kb-embedding-stats", kbId] });
      qc.invalidateQueries({ queryKey: ["kb-documents", kbId] });
      qc.invalidateQueries({ queryKey: ["knowledge-base", kbId] });
      qc.invalidateQueries({ queryKey: ["kb-activity", kbId] });
    },
  });
}
