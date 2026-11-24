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

/** Agent, template, sub-agent task, delegation, and channel chat API hooks. */

import { keepPreviousData, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW, REFETCH_INTERVAL_FAST } from "@/lib/constants";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { saveBlobAs } from "@/lib/utils";
import { invalidateApprovalUsage } from "@/hooks/api/useToolApprovals";
import type {
  Agent,
  VariableNeedingValue,
  AgentTemplate,
  AgentWizardAssistResponse,
  CreateAgentRequest,
  EntityListOptions,
  AgentExportFormat,
  AgentExportPortability,
  AgentImportFileCheckResult,
  AgentImportFileResult,
  AgentConfigVersionField,
  VersionDiffResponse,
  VersionListResponse,
  SubAgentTask,
  AgentDelegation,
  AgentChatListResponse,
  AgentChatDetail,
  AgentFeedbackCommentListResponse,
  ChannelRoutingSummary,
  BudgetPolicy,
  DuplicatedAgent,
} from "@/types";

// --- Agent Templates ---

/** One round with the agent wizard's assistant (AGENT_WIZARD_ASSIST): a reply
 *  and the fields to change, every id one the wizard offers. Nothing is stored. */
export function useAssistAgentWizard() {
  return useMutation({
    mutationFn: ({ signal, ...body }: {
      instruction: string;
      draft: Record<string, unknown>;
      started: boolean;
      history: { role: "user" | "assistant"; content: string }[];
      icon_names: string[];
      /** Aborts the round; the server then cancels its model call. */
      signal?: AbortSignal;
    }) => api.post<AgentWizardAssistResponse>("/ai/agent-wizard/assist", body, { signal }),
    // The refusal is said in the assistant's own transcript.
    meta: { suppressGlobalErrorToast: true, readOnly: true },
  });
}
export function useAgentTemplates() {
  return useQuery<AgentTemplate[]>({
    queryKey: ["agent-templates"],
    queryFn: () => api.get("/agents/templates"),
    staleTime: 60_000,
  });
}

// --- Agents ---
/** The agents list; `{ scope: "all" }` is every agent for an admin (detail pages). */
export function useAgents(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<Agent[]>({
    queryKey: entityListKey(["agents"], scope),
    queryFn: () => api.get(entityListPath("/agents", scope)),
  });
}

export function useAgent(
  agentId: string,
  /** A 404 is an answer (no data, no retry), not a fault thrown to the page boundary: the detail page renders Page not found (#797 S4). */
  options: { notFoundIsOrdinary?: boolean } = {},
) {
  return useQuery<Agent>({
    queryKey: ["agent", agentId],
    queryFn: () => api.get(`/agents/${agentId}`),
    enabled: !!agentId,
    ...(options.notFoundIsOrdinary ? notFoundIsOrdinary : {}),
  });
}

/**
 * The variables an agent references that have no value yet. Keyed
 * under ``variables`` so every variable mutation, which invalidates that key,
 * refreshes the notice: it goes away once they are filled.
 */
export function useAgentMissingVariables(agentId: string) {
  return useQuery<VariableNeedingValue[]>({
    queryKey: ["variables", "missing-for-agent", agentId],
    queryFn: () => api.get(`/agents/${agentId}/missing-variables`),
    enabled: !!agentId,
  });
}

/**
 * The platform run-budget configuration, and whether this user may exceed it.
 *
 * Long-lived: it only changes when an operator edits the environment and
 * restarts, so the Budget tab reads it once per session rather than on every
 * keystroke. Not admin-gated — a non-admin who cannot see the ceiling cannot
 * understand why their value was refused.
 */
export function useBudgetPolicy() {
  return useQuery<BudgetPolicy>({
    queryKey: ["budget-policy"],
    queryFn: () => api.get("/agents/budget-policy"),
    staleTime: 5 * 60_000,
  });
}

export function useCreateAgent() {
  const qc = useQueryClient();
  return useMutation<Agent, Error, CreateAgentRequest>({
    mutationFn: (data) => api.post("/agents", data),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      invalidateApprovalUsage(qc);
      return qc.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

/** Dismiss built-in functions from an agent's release notice (#12245); an empty list only marks the
 *  release notices read once nothing they name is left to act on. Read state is the bell's. */
export function useDismissBuiltinTools() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, dismiss }: { id: string; dismiss: string[] }) =>
      api.post<{ dismissed: string[]; resolved_notifications: string[] }>(`/agents/${id}/builtin-tools/notice`, { dismiss }),
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
}

export function useUpdateAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string; commit_message?: string } & Partial<Agent>) =>
      api.put(`/agents/${id}`, data),
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["agent-persona-versions", id] });
      qc.invalidateQueries({ queryKey: ["agent-config-versions", id, "guardrails"] });
      qc.invalidateQueries({ queryKey: ["agent-config-versions", id, "temperature"] });
      qc.invalidateQueries({ queryKey: ["agent-config-versions", id, "reasoning_effort"] });
      qc.invalidateQueries({ queryKey: ["agent-config-versions", id, "max_iterations"] });
      // An agent edit can revoke its A2A exposure server-side (sharing, tier,
      // owner and enabled changes all do), so the exposure status is stale the
      // moment this succeeds. Without this the section keeps showing "Exposed"
      // and a card URL that now 404s — which a user may copy to a partner.
      qc.invalidateQueries({ queryKey: ["a2a-exposure", id] });
      // Enabling, disabling or setting it to ask changes who asks.
      invalidateApprovalUsage(qc);
    },
  });
}

export function useAgentPersonaVersions(
  agentId: string,
  params: { limit?: number; offset?: number } = {},
) {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return useQuery<VersionListResponse>({
    queryKey: ["agent-persona-versions", agentId, params],
    queryFn: () => api.get(`/agents/${agentId}/persona/versions${qs ? `?${qs}` : ""}`),
    enabled: !!agentId,
    placeholderData: (prev) => prev,
  });
}

export function useAgentPersonaVersionDiff(
  agentId: string,
  fromVersion: number | null,
  toVersion: number | null,
  enabled = true,
) {
  return useQuery<VersionDiffResponse>({
    queryKey: ["agent-persona-version-diff", agentId, fromVersion, toVersion],
    queryFn: () => api.get(`/agents/${agentId}/persona/versions/diff?from=${fromVersion}&to=${toVersion}`),
    enabled: !!agentId && fromVersion !== null && toVersion !== null && enabled,
  });
}

export function useRollbackAgentPersonaVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      version,
      commit_message,
    }: {
      agentId: string;
      version: number;
      commit_message?: string;
    }) => api.post(`/agents/${agentId}/persona/versions/${version}/rollback`, { commit_message }),
    onSuccess: (_d, { agentId }) => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["agent", agentId] });
      qc.invalidateQueries({ queryKey: ["agent-persona-versions", agentId] });
      qc.invalidateQueries({ queryKey: ["agent-persona-version-diff", agentId] });
    },
  });
}

export function useDeleteAgentPersonaVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, version }: { agentId: string; version: number }) =>
      api.delete(`/agents/${agentId}/persona/versions/${version}`),
    onSuccess: (_d, { agentId }) => {
      qc.invalidateQueries({ queryKey: ["agent-persona-versions", agentId] });
      qc.invalidateQueries({ queryKey: ["agent-persona-version-diff", agentId] });
    },
  });
}

export function useLockAgentPersonaVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, version }: { agentId: string; version: number }) =>
      api.post(`/agents/${agentId}/persona/versions/${version}/lock`, {}),
    onSuccess: (_d, { agentId }) => {
      qc.invalidateQueries({ queryKey: ["agent-persona-versions", agentId] });
    },
  });
}

export function useUnlockAgentPersonaVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, version }: { agentId: string; version: number }) =>
      api.post(`/agents/${agentId}/persona/versions/${version}/unlock`, {}),
    onSuccess: (_d, { agentId }) => {
      qc.invalidateQueries({ queryKey: ["agent-persona-versions", agentId] });
    },
  });
}

export function useAgentConfigVersions(
  agentId: string,
  fieldName: AgentConfigVersionField,
  params: { limit?: number; offset?: number } = {},
) {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  const qs = query.toString();
  return useQuery<VersionListResponse>({
    queryKey: ["agent-config-versions", agentId, fieldName, params],
    queryFn: () => api.get(`/agents/${agentId}/config-versions/${fieldName}${qs ? `?${qs}` : ""}`),
    enabled: !!agentId,
    placeholderData: (prev) => prev,
  });
}

export function useAgentConfigVersionDiff(
  agentId: string,
  fieldName: AgentConfigVersionField,
  fromVersion: number | null,
  toVersion: number | null,
  enabled = true,
) {
  return useQuery<VersionDiffResponse>({
    queryKey: [
      "agent-config-version-diff",
      agentId,
      fieldName,
      fromVersion,
      toVersion,
    ],
    queryFn: () =>
      api.get(
        `/agents/${agentId}/config-versions/${fieldName}/diff?from=${fromVersion}&to=${toVersion}`,
      ),
    enabled: !!agentId && fromVersion !== null && toVersion !== null && enabled,
  });
}

export function useRollbackAgentConfigVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      fieldName,
      version,
      commit_message,
    }: {
      agentId: string;
      fieldName: AgentConfigVersionField;
      version: number;
      commit_message?: string;
    }) =>
      api.post(`/agents/${agentId}/config-versions/${fieldName}/${version}/rollback`, {
        commit_message,
      }),
    onSuccess: (_d, { agentId, fieldName }) => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["agent", agentId] });
      qc.invalidateQueries({ queryKey: ["agent-config-versions", agentId, fieldName] });
      qc.invalidateQueries({ queryKey: ["agent-config-version-diff", agentId, fieldName] });
    },
  });
}

export function useDeleteAgentConfigVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      fieldName,
      version,
    }: {
      agentId: string;
      fieldName: AgentConfigVersionField;
      version: number;
    }) => api.delete(`/agents/${agentId}/config-versions/${fieldName}/${version}`),
    onSuccess: (_d, { agentId, fieldName }) => {
      qc.invalidateQueries({ queryKey: ["agent-config-versions", agentId, fieldName] });
      qc.invalidateQueries({ queryKey: ["agent-config-version-diff", agentId, fieldName] });
    },
  });
}

export function useLockAgentConfigVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      fieldName,
      version,
    }: {
      agentId: string;
      fieldName: AgentConfigVersionField;
      version: number;
    }) => api.post(`/agents/${agentId}/config-versions/${fieldName}/${version}/lock`, {}),
    onSuccess: (_d, { agentId, fieldName }) => {
      qc.invalidateQueries({ queryKey: ["agent-config-versions", agentId, fieldName] });
    },
  });
}

export function useUnlockAgentConfigVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      fieldName,
      version,
    }: {
      agentId: string;
      fieldName: AgentConfigVersionField;
      version: number;
    }) => api.post(`/agents/${agentId}/config-versions/${fieldName}/${version}/unlock`, {}),
    onSuccess: (_d, { agentId, fieldName }) => {
      qc.invalidateQueries({ queryKey: ["agent-config-versions", agentId, fieldName] });
    },
  });
}

/** An agent to delete, and the agent its conversations continue with. */
export interface DeleteAgentInput {
  id: string;
  successorAgentId?: string | null;
}

export function useDeleteAgent() {
  const qc = useQueryClient();
  return useMutation<unknown, Error, DeleteAgentInput>({
    mutationFn: ({ id, successorAgentId }) =>
      api.delete(
        successorAgentId
          ? `/agents/${id}?successor_agent_id=${encodeURIComponent(successorAgentId)}`
          : `/agents/${id}`,
      ),
    // `["me"]` too: the server purges the deleted id from the user's
    // agent-id columns (`purge_agent_references` — favorites, default chat
    // agent, MCP exposure), so a cached user still holding it is stale. Left
    // stale, the next write derived from it (starring an agent, say) would
    // send the purged id straight back. `["conversations"]`: the agent's
    // conversations now name it as deleted, or continue with the successor.
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      dropFromFlowNodeCatalog(qc, "agent", id);
      qc.invalidateQueries({ queryKey: ["me"] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      qc.invalidateQueries({ queryKey: ["conversation"] });
      invalidateApprovalUsage(qc);
    },
  });
}

export function useDuplicateAgent() {
  const qc = useQueryClient();
  return useMutation<DuplicatedAgent, Error, string>({
    mutationFn: (id) => api.post(`/agents/${id}/duplicate`),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      invalidateApprovalUsage(qc);
      return qc.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useGenerateSuggestedPrompts() {
  return useMutation<{ suggested_prompts: string[] }, Error, string>({
    mutationFn: (agentId) => api.post(`/agents/${agentId}/generate-suggested-prompts`),
  });
}

/**
 * Download an agent export in the chosen format, saving it under the filename
 * the server picked.
 *
 * Goes through `api.download` rather than `api.get` because the AgentSchema
 * format is YAML, not JSON — the response body is text the browser saves
 * as-is, and only the backend knows the right extension
 * (`.json` vs `.agent.yaml`).
 */
export function useDownloadAgentExport() {
  return useMutation<
    { filename: string },
    Error,
    { id: string; name: string; format: AgentExportFormat }
  >({
    mutationFn: async ({ id, name, format }) => {
      const { blob, filename } = await api.download(
        `/agents/${id}/export?format=${format}`,
      );
      const fallbackExt = format === "agentschema" ? "agent.yaml" : "json";
      const resolved =
        filename ??
        `agent-${name.toLowerCase().replace(/\s+/g, "-")}.${fallbackExt}`;
      saveBlobAs(blob, resolved);
      return { filename: resolved };
    },
  });
}

/**
 * What exporting this agent to `format` would not carry.
 *
 * A separate read from the export itself so the user can be shown the list of
 * non-portable fields *before* the file is generated. Only fetched when
 * `enabled` — there is nothing to report for the native format.
 */
export function useAgentExportPortability(
  agentId: string | undefined,
  format: AgentExportFormat,
  enabled: boolean,
) {
  return useQuery<AgentExportPortability>({
    queryKey: ["agent-export-portability", agentId, format],
    queryFn: () =>
      api.get(`/agents/${agentId}/export/portability?format=${format}`),
    enabled: !!agentId && enabled,
  });
}

/**
 * Dry-run the import verification loop on an uploaded agent file.
 *
 * The format is not declared: the backend detects whether the upload is an
 * XTM One bundle or an AgentSchema document, validates it against that
 * format's schema, and reports each stage's verdict — without creating
 * anything. Uploading the raw file (rather than parsing it here) is what lets
 * YAML documents work at all. A failing document is a normal response with
 * `ok: false`, not an error.
 */
export function useCheckAgentImportFile() {
  return useMutation<AgentImportFileCheckResult, Error, File>({
    mutationFn: (file) => {
      const formData = new FormData();
      formData.append("file", file);
      return api.postForm("/agents/import-file/check", formData);
    },
  });
}

/**
 * Import the native-shape bundle a previous dry-run check returned, so the
 * platform creates exactly what the user reviewed on the import page.
 * `corrections` are ids from the check report, applied server-side.
 */
export function useConfirmAgentImport() {
  const qc = useQueryClient();
  return useMutation<
    AgentImportFileResult,
    Error,
    { bundle: Record<string, unknown>; detected_format: AgentExportFormat; corrections: string[] }
  >({
    mutationFn: (body) => api.post("/agents/import/confirm", body),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      invalidateApprovalUsage(qc);
      return qc.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

/**
 * Ask the platform LLM how to finish setting up an imported agent, from the
 * check report's warnings. Advisory text — nothing is applied automatically.
 */
export function useSuggestImportFixes() {
  return useMutation<
    { suggestions: string },
    Error,
    { agent_name?: string; persona?: string; warnings: string[] }
  >({
    mutationFn: (body) => api.post("/agents/import-file/suggest", body),
  });
}

// --- Platform-wide agent list (admin-only, lightweight) ---
export function usePlatformAgents() {
  return useQuery<{ id: string; name: string; slug: string; icon: string; enabled: boolean; disable_chat: boolean; is_company_managed: boolean; user_id: string | null }[]>({
    queryKey: ["platform-agents"],
    queryFn: () => api.get("/agents/platform"),
  });
}

// --- Channel Routing Summary (admin-only) ---
export function useChannelRoutingSummary(enabled = true) {
  return useQuery<ChannelRoutingSummary>({
    queryKey: ["channel-routing-summary"],
    queryFn: () => api.get("/agents/channel-routing"),
    enabled,
    staleTime: REFETCH_INTERVAL_SLOW,
  });
}

// --- Pipeline-eligible tools (schema-filtered) ---
interface PipelineToolEntry {
  id: string;
  name: string;
  group: string;
  source: string;
  ref: { source: string; tool_id?: string; provider?: string; tool_name: string };
}

export function usePipelineTools() {
  return useQuery<{ prepare: PipelineToolEntry[]; finalize: PipelineToolEntry[] }>({
    queryKey: ["pipeline-tools"],
    queryFn: () => api.get("/agents/pipeline-tools"),
    staleTime: 60_000,
  });
}

// --- Unroute agent from a channel (admin-only) ---
export function useUnrouteAgent() {
  const qc = useQueryClient();
  return useMutation<void, Error, { agentId: string; channelType: string }>({
    mutationFn: ({ agentId, channelType }) =>
      api.delete(`/agents/${agentId}/channel-config/${channelType}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["channel-routing-summary"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

// --- Sub-Agent Tasks (legacy) ---
export function useSubAgentTasks(params: {
  agent_id?: string;
  status?: string;
  limit?: number;
} = {}) {
  const query = new URLSearchParams();
  if (params.agent_id) query.set("agent_id", params.agent_id);
  if (params.status) query.set("status", params.status);
  if (params.limit) query.set("limit", String(params.limit));
  const qs = query.toString();
  return useQuery<SubAgentTask[]>({
    queryKey: ["sub-agent-tasks", params],
    queryFn: () => api.get(`/chat/sub-agent-tasks${qs ? `?${qs}` : ""}`),
    // Poll fast while a task is in flight; fall back to a slow poll when idle
    // so externally-spawned delegations still surface even if the SSE
    // activity stream (which invalidates this key) is disconnected.
    refetchInterval: (query) =>
      query.state.data?.some((t) => t.status === "pending" || t.status === "running")
        ? REFETCH_INTERVAL_FAST
        : REFETCH_INTERVAL_SLOW,
  });
}

// --- Background Tasks control screen (scope-aware list + count + delete) ---

/** Invalidate every cache that reflects background-task state. */
function invalidateBackgroundTaskCaches(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["background-tasks"] });
  qc.invalidateQueries({ queryKey: ["background-task-count"] });
  qc.invalidateQueries({ queryKey: ["sub-agent-tasks"] });
}

/** The server orders of `/chat/sub-agent-tasks` (`BACKGROUND_TASK_SORTS` of the backend). */
export type BackgroundTaskSort =
  | "created"
  | "created_asc"
  | "last_progress"
  | "last_progress_asc"
  | "status"
  | "status_desc"
  | "from"
  | "from_desc"
  | "to"
  | "to_desc";

export function useBackgroundTasks(
  params: {
    /** [DEPRECATED] use `vis`. */
    scope?: "mine" | "all";
    /** The visibility segment (`lib/visibility.ts`); wins over `scope`. */
    vis?: "mine" | "shared" | "company" | "all";
    status?: string;
    agent_id?: string;
    search?: string;
    /** A fleet: parent or target agent is one of its home members. */
    fleet?: string;
    /** `no_live_execution`: the open tasks whose execution is gone. */
    liveness?: "no_live_execution";
    /** The server order (#9378 slice 8e); `created` (newest first) is the default. */
    sort?: BackgroundTaskSort;
    limit?: number;
    offset?: number;
  } = {},
  enabled = true,
) {
  const query = new URLSearchParams();
  if (params.vis) query.set("vis", params.vis);
  else if (params.scope) query.set("scope", params.scope);
  if (params.status) query.set("status", params.status);
  if (params.agent_id) query.set("agent_id", params.agent_id);
  if (params.search) query.set("search", params.search);
  if (params.fleet) query.set("fleet", params.fleet);
  if (params.liveness) query.set("liveness", params.liveness);
  if (params.sort && params.sort !== "created") query.set("sort", params.sort);
  query.set("limit", String(params.limit ?? 200));
  if (params.offset) query.set("offset", String(params.offset));
  const qs = query.toString();
  return useQuery<SubAgentTask[]>({
    // Key on the resolved query string (the actual request) rather than the raw
    // params: ``limit`` defaults to 200 here, so keying on ``params`` would give
    // a caller that omits ``limit`` a different cache entry than one passing
    // ``limit: 200`` for the identical request. Prefix-based invalidation
    // (``["background-tasks"]``) still matches.
    queryKey: ["background-tasks", qs],
    queryFn: () => api.get(`/chat/sub-agent-tasks?${qs}`),
    enabled,
    // A sort, filter or page change keeps the previous page on screen while the
    // new one loads: the list bar keeps its range and pager, so the sort the user
    // just pressed does not move under the pointer.
    placeholderData: keepPreviousData,
    // Poll fast only while a task is in flight; fall back to a slow poll when
    // idle (mirrors useSubAgentTasks) so the control screen doesn't fire two
    // requests every few seconds when nothing is running.
    refetchInterval: (query) =>
      query.state.data?.some((t) => t.status === "pending" || t.status === "running")
        ? REFETCH_INTERVAL_FAST
        : REFETCH_INTERVAL_SLOW,
  });
}

/** Total tasks matching the filters — powers pagination + "select all N". */
export function useBackgroundTaskCount(
  params: {
    /** [DEPRECATED] use `vis`. */
    scope?: "mine" | "all";
    /** The visibility segment (`lib/visibility.ts`); wins over `scope`. */
    vis?: "mine" | "shared" | "company" | "all";
    status?: string;
    agent_id?: string;
    search?: string;
    /** A fleet: parent or target agent is one of its home members. */
    fleet?: string;
    /** `no_live_execution`: the open tasks whose execution is gone. */
    liveness?: "no_live_execution";
  } = {},
  enabled = true,
) {
  const query = new URLSearchParams();
  if (params.vis) query.set("vis", params.vis);
  else if (params.scope) query.set("scope", params.scope);
  if (params.status) query.set("status", params.status);
  if (params.agent_id) query.set("agent_id", params.agent_id);
  if (params.search) query.set("search", params.search);
  if (params.fleet) query.set("fleet", params.fleet);
  if (params.liveness) query.set("liveness", params.liveness);
  const qs = query.toString();
  return useQuery<{ count: number }>({
    queryKey: ["background-task-count", qs],
    queryFn: () => api.get(`/chat/sub-agent-tasks/count?${qs}`),
    enabled,
    // Same as the list: the total stays while a filter change refetches.
    placeholderData: keepPreviousData,
    // The count only needs to stay roughly fresh for pagination / "select all";
    // the fast in-flight polling lives on the list query, and both are
    // invalidated together by the SSE activity stream.
    refetchInterval: REFETCH_INTERVAL_SLOW,
  });
}

export function useCancelBackgroundTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) =>
      api.post(`/chat/sub-agent-tasks/${taskId}/cancel`, {}),
    onSuccess: () => invalidateBackgroundTaskCaches(qc),
  });
}

export function useDeleteBackgroundTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) =>
      api.delete(`/chat/sub-agent-tasks/${taskId}`),
    onSuccess: () => invalidateBackgroundTaskCaches(qc),
  });
}

export function useBulkDeleteBackgroundTasks() {
  const qc = useQueryClient();
  return useMutation<
    { deleted: number },
    Error,
    {
      task_ids?: string[];
      all_matching_filters?: boolean;
      scope?: string;
      vis?: string;
      status?: string;
      agent_id?: string;
    }
  >({
    mutationFn: (body) => api.post("/chat/sub-agent-tasks/bulk-delete", body),
    onSuccess: () => invalidateBackgroundTaskCaches(qc),
  });
}

// --- Agent Delegations (unified) ---
export function useAgentDelegations(params: {
  agent_id?: string;
  parent_agent_id?: string;
  target_agent_id?: string;
  type?: string;
  status?: string;
  conversation_id?: string;
  limit?: number;
} = {}) {
  const query = new URLSearchParams();
  if (params.agent_id) query.set("agent_id", params.agent_id);
  if (params.parent_agent_id) query.set("parent_agent_id", params.parent_agent_id);
  if (params.target_agent_id) query.set("target_agent_id", params.target_agent_id);
  if (params.type) query.set("type", params.type);
  if (params.status) query.set("status", params.status);
  if (params.conversation_id) query.set("conversation_id", params.conversation_id);
  if (params.limit) query.set("limit", String(params.limit));
  const qs = query.toString();
  return useQuery<AgentDelegation[]>({
    queryKey: ["agent-delegations", params],
    queryFn: () => api.get(`/chat/delegations${qs ? `?${qs}` : ""}`),
  });
}

// --- Agent Channel Chats ---
export function useAgentChats(
  agentId: string | undefined,
  params: { limit?: number; offset?: number; search?: string; channel?: string } = {},
) {
  const query = new URLSearchParams();
  if (params.limit) query.set("limit", String(params.limit));
  if (params.offset) query.set("offset", String(params.offset));
  if (params.search) query.set("search", params.search);
  if (params.channel) query.set("channel", params.channel);
  const qs = query.toString();
  return useQuery<AgentChatListResponse>({
    queryKey: ["agent-chats", agentId, params],
    queryFn: () => api.get(`/agents/${agentId}/chats${qs ? `?${qs}` : ""}`),
    enabled: !!agentId,
  });
}

/**
 * One of the agent's conversations, for the header of its read-only view:
 * the transcript itself is the chat panel's (`agentChatPanelEndpoints`).
 */
export function useAgentChatSummary(agentId: string | undefined, conversationId: string | undefined) {
  return useQuery<AgentChatDetail>({
    queryKey: ["agent-chat-detail", agentId, conversationId],
    queryFn: () => api.get(`/agents/${agentId}/chats/${conversationId}?limit=1`),
    enabled: !!agentId && !!conversationId,
  });
}

export function useClearAgentChats(agentId: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.delete(`/agents/${agentId}/chats`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-chats", agentId] });
      qc.invalidateQueries({ queryKey: ["agent-chat-detail", agentId] });
    },
  });
}

export function useAgentFeedbackComments(
  agentId: string | undefined,
  params: { limit?: number; offset?: number; search?: string; rating?: "positive" | "negative" } = {},
  /** throwOnError false keeps a failure in the table that reads it (its own error line and Retry) instead of the page boundary. */
  options: { enabled?: boolean; refetchInterval?: number; throwOnError?: false } = {},
) {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  if (params.search) query.set("search", params.search);
  if (params.rating) query.set("rating", params.rating);
  const qs = query.toString();
  return useQuery<AgentFeedbackCommentListResponse>({
    queryKey: ["agent-feedback-comments", agentId, params],
    queryFn: () => api.get(`/agents/${agentId}/feedback-comments${qs ? `?${qs}` : ""}`),
    enabled: (options.enabled ?? true) && !!agentId,
    refetchInterval: options.refetchInterval,
    // Spread so an absent option keeps the app's default `throwOnError: true`.
    ...(options.throwOnError === false ? { throwOnError: false as const } : {}),
  });
}
