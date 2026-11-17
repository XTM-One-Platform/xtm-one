// SPDX-License-Identifier: Apache-2.0

/** Activity, dashboard, audit logs, API keys, and AI utility hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW, REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import type {
  DashboardStats,
  AdoptionTimeSeries,
  AuditLogListResponse,
  UserApiKey,
  UserApiKeyCreated,
  ApiKeyPolicy,
  CreateApiKeyRequest,
  UpdateApiKeyRequest,
  AdminApiKey,
  AdminApiKeyPage,
  AdminApiKeyStatus,
  AdminApiKeyIssue,
  PlatformObjectListResponse,
} from "@/types";

// --- Activity ---
export function useDashboardStats(activityRange = "all", scope: "user" | "platform" = "user") {
  return useQuery<DashboardStats>({
    queryKey: ["dashboard-stats", activityRange, scope],
    queryFn: () => api.get(`/activity/stats?activity_range=${activityRange}&scope=${scope}`),
    staleTime: REFETCH_INTERVAL_STANDARD,
    refetchInterval: REFETCH_INTERVAL_SLOW,
    placeholderData: (prev) => prev, // keep previous data visible during range transitions
  });
}

export function useAdoptionTimeSeries(interval = "1w") {
  return useQuery<AdoptionTimeSeries>({
    queryKey: ["adoption-timeseries", interval],
    queryFn: () => api.get(`/activity/adoption?interval=${interval}`),
    staleTime: REFETCH_INTERVAL_STANDARD,
    refetchInterval: REFETCH_INTERVAL_SLOW,
    placeholderData: (prev) => prev,
  });
}

// --- Personal API keys ---
export function useApiKeys() {
  return useQuery<UserApiKey[]>({
    queryKey: ["api-keys"],
    queryFn: () => api.get("/api-keys"),
  });
}

/**
 * What a new key may be given on this deployment: surfaces, maximum lifetime, bounds.
 * `fresh` reads it from the server again when the caller mounts (the key dialog builds its form from it).
 */
export function useApiKeyPolicy({ fresh = false }: { fresh?: boolean } = {}) {
  return useQuery<ApiKeyPolicy>({
    queryKey: ["api-key-policy"],
    queryFn: () => api.get("/api-keys/policy"),
    staleTime: REFETCH_INTERVAL_STANDARD,
    refetchOnMount: fresh ? "always" : true,
  });
}

export function useCreateApiKey() {
  const qc = useQueryClient();
  return useMutation<UserApiKeyCreated, Error, CreateApiKeyRequest>({
    mutationFn: (body) => api.post("/api-keys", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
    },
  });
}

export function useUpdateApiKey() {
  const qc = useQueryClient();
  return useMutation<UserApiKey, Error, { id: string; body: UpdateApiKeyRequest }>({
    mutationFn: ({ id, body }) => api.patch(`/api-keys/${id}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      qc.invalidateQueries({ queryKey: ["admin-api-keys"] });
    },
  });
}

export function useDeleteApiKey() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/api-keys/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["api-keys"] });
      qc.invalidateQueries({ queryKey: ["admin-api-keys"] });
    },
  });
}

interface AdminApiKeyFilters {
  status?: AdminApiKeyStatus;
  issue?: AdminApiKeyIssue;
  search?: string;
  ownerIds?: string[];
  /** Totals over every matching key, not only the page. */
  withSummary?: boolean;
  limit?: number;
  offset?: number;
}

/** Every personal API key of the platform with its owner (admin). */
export function useAdminApiKeys(filters: AdminApiKeyFilters, enabled = true) {
  const params = new URLSearchParams();
  if (filters.status && filters.status !== "all") params.set("status", filters.status);
  if (filters.issue) params.set("issue", filters.issue);
  if (filters.search) params.set("search", filters.search);
  for (const ownerId of filters.ownerIds ?? []) params.append("owner_id", ownerId);
  if (filters.withSummary) params.set("with_summary", "true");
  if (filters.limit !== undefined) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));
  const query = params.toString();
  return useQuery<AdminApiKeyPage>({
    queryKey: ["admin-api-keys", query],
    queryFn: () => api.get(`/admin/api-keys${query ? `?${query}` : ""}`),
    placeholderData: (prev) => prev,
    enabled,
  });
}

export function useRevokeApiKey() {
  const qc = useQueryClient();
  return useMutation<AdminApiKey, Error, string>({
    mutationFn: (id) => api.post(`/admin/api-keys/${id}/revoke`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-api-keys"] });
      qc.invalidateQueries({ queryKey: ["api-keys"] });
    },
  });
}

// --- AI Utilities ---
export function useGeneratePrompt() {
  return useMutation<
    { generated_prompt: string },
    Error,
    { description: string; context_type: string; current_text?: string; agent_name?: string; assignment_name?: string }
  >({
    mutationFn: (body) => api.post("/ai/generate-prompt", body),
  });
}

// --- Audit Logs (admin) ---
export function useAuditLogs(params: {
  search?: string;
  user_id?: string;
  action?: string;
  entity_type?: string;
  date_from?: string;
  date_to?: string;
  /** The table column the API orders by (time, action, entity, user); newest first by default. */
  sort?: string;
  direction?: "asc" | "desc";
  limit?: number;
  offset?: number;
} = {}) {
  const query = new URLSearchParams();
  if (params.search) query.set("search", params.search);
  if (params.sort) query.set("sort", params.sort);
  if (params.direction) query.set("direction", params.direction);
  if (params.user_id) query.set("user_id", params.user_id);
  if (params.action) query.set("action", params.action);
  if (params.entity_type) query.set("entity_type", params.entity_type);
  if (params.date_from) query.set("date_from", params.date_from);
  if (params.date_to) query.set("date_to", params.date_to);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return useQuery<AuditLogListResponse>({
    queryKey: ["audit-logs", params],
    queryFn: () => api.get(`/audit-logs${qs ? `?${qs}` : ""}`),
    placeholderData: (prev) => prev, // keep previous data visible during page/filter transitions
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

export function useAuditLogActions() {
  return useQuery<string[]>({
    queryKey: ["audit-log-actions"],
    queryFn: () => api.get("/audit-logs/actions"),
  });
}

export function useAuditLogEntityTypes() {
  return useQuery<string[]>({
    queryKey: ["audit-log-entity-types"],
    queryFn: () => api.get("/audit-logs/entity-types"),
  });
}

// --- Admin Objects (platform-wide entity browser) ---
export function useAdminObjects(params: {
  search?: string;
  /** One type or several, comma-separated. */
  object_type?: string;
  /** One owner id or several, comma-separated. */
  user_id?: string;
  /** One status or several, comma-separated. */
  status?: string;
  is_company_managed?: boolean;
  sort?: string;
  direction?: "asc" | "desc";
  limit?: number;
  offset?: number;
} = {}, options: { enabled?: boolean } = {}) {
  const query = new URLSearchParams();
  if (params.search) query.set("search", params.search);
  if (params.object_type) query.set("object_type", params.object_type);
  if (params.user_id) query.set("user_id", params.user_id);
  if (params.status) query.set("status", params.status);
  if (params.is_company_managed !== undefined) query.set("is_company_managed", String(params.is_company_managed));
  if (params.sort) query.set("sort", params.sort);
  if (params.direction) query.set("direction", params.direction);
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return useQuery<PlatformObjectListResponse>({
    queryKey: ["admin-objects", params],
    queryFn: () => api.get(`/admin/objects${qs ? `?${qs}` : ""}`),
    enabled: options.enabled ?? true,
    placeholderData: (prev) => prev,
    // A machine's status is the browser's one LIVE field: the backend
    // reads it off `last_seen_at` recency at request time, so a row that was
    // online when the page loaded goes stale on the heartbeat cadence. Poll
    // like the Desktop fleet list does - but only while a machine is on the
    // page; a page of agents and prompts has nothing that moves.
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => item.object_type === "runner") ? REFETCH_INTERVAL_STANDARD : false,
  });
}
