// SPDX-License-Identifier: Apache-2.0

/**
 * Workspaces API.
 *
 * A workspace is a named environment that scopes what agents share. The list
 * endpoint is also what CREATES the caller's default workspace on first use —
 * the backend has no migration that backfilled one — so the first render of
 * any surface that lists workspaces is what materialises it. Nothing here
 * needs to know that, but it explains why the list can return a row the user
 * never created.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, notFoundIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_FAST } from "@/lib/constants";
import type {
  ConversationListItem,
  Workspace,
  WorkspaceKnowledgeBaseRef,
  WorkspaceKnowledgeConfig,
  WorkspaceKnowledgeSummary,
  WorkspaceSkillRef,
} from "@/types";

const KEY = ["workspaces"];

export function useWorkspaces(options?: {
  includeArchived?: boolean;
  /** Ask for what each workspace holds; only the management view needs it. */
  withContents?: boolean;
  enabled?: boolean;
  /**
   * A surface that only ADDS workspaces to something that works without
   * them (the chat sidebar) passes `false`: a refusal then leaves it as it
   * was instead of replacing the page with the error boundary.
   */
  throwOnError?: boolean;
}) {
  const includeArchived = options?.includeArchived ?? false;
  const withContents = options?.withContents ?? false;
  const params = new URLSearchParams();
  if (includeArchived) params.set("include_archived", "true");
  if (withContents) params.set("with_contents", "true");
  const query = params.toString();
  return useQuery<Workspace[]>({
    queryKey: [...KEY, { includeArchived, withContents }],
    queryFn: () => api.get(`/workspaces${query ? `?${query}` : ""}`),
    enabled: options?.enabled ?? true,
    ...(options?.throwOnError === false ? { throwOnError: false, retry: false } : {}),
  });
}

/** Invalidate every workspace list (not the per-workspace binding queries). */
function useInvalidate() {
  const qc = useQueryClient();
  return () =>
    qc.invalidateQueries({
      queryKey: KEY,
      predicate: (q) => typeof q.queryKey[1] === "object",
    });
}

// The workspace surfaces toast their own errors with the endpoint's reason.
const OWN_ERROR_TOAST = { suppressGlobalErrorToast: true };

export function useCreateWorkspace() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (data: {
      name: string;
      description?: string;
      icon?: string;
      color?: string;
    }) => api.post<Workspace>("/workspaces", data),
    meta: OWN_ERROR_TOAST,
    onSuccess: invalidate,
  });
}

export function useUpdateWorkspace() {
  const invalidate = useInvalidate();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...data
    }: {
      id: string;
      name?: string;
      description?: string;
      icon?: string;
      color?: string;
      is_archived?: boolean;
      // Sharing: the same fields every shared entity takes.
      is_company_managed?: boolean;
      shared_group_ids?: string[] | null;
      shared_group_permissions?: Record<string, string> | null;
      shared_user_ids?: string[] | null;
      shared_user_permissions?: Record<string, string> | null;
      access_restricted?: boolean;
      /** What every agent turn in a filed conversation receives. */
      instructions?: string;
    }) => api.put<Workspace>(`/workspaces/${id}`, data),
    meta: OWN_ERROR_TOAST,
    onSuccess: (_data, variables) => {
      invalidate();
      // The one workspace and its knowledge panel (instructions, a rename).
      qc.invalidateQueries({ queryKey: [...KEY, variables.id] });
    },
  });
}

export function useDeleteWorkspace() {
  const invalidate = useInvalidate();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/workspaces/${id}`),
    meta: OWN_ERROR_TOAST,
    onSuccess: () => {
      invalidate();
      // Its conversations are now in no workspace.
      qc.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

export function useSetDefaultWorkspace() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: string) => api.post<Workspace>(`/workspaces/${id}/default`),
    meta: OWN_ERROR_TOAST,
    onSuccess: invalidate,
  });
}

export function useWorkspaceKnowledgeBases(id: string | null) {
  return useQuery<WorkspaceKnowledgeBaseRef[]>({
    queryKey: [...KEY, id, "knowledge-bases"],
    queryFn: () => api.get(`/workspaces/${id}/knowledge-bases`),
    enabled: !!id,
  });
}

export function useSetWorkspaceKnowledgeBases() {
  const qc = useQueryClient();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({
      id,
      knowledgeBaseIds,
    }: {
      id: string;
      knowledgeBaseIds: string[];
    }) =>
      api.put<WorkspaceKnowledgeBaseRef[]>(
        `/workspaces/${id}/knowledge-bases`,
        // The endpoint REPLACES the set rather than adding to it, so the
        // caller always sends the whole list it means to end up with.
        { knowledge_base_ids: knowledgeBaseIds },
      ),
    meta: OWN_ERROR_TOAST,
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: [...KEY, variables.id, "knowledge-bases"] });
      qc.invalidateQueries({ queryKey: [...KEY, variables.id, "knowledge"] });
      // The list's counts include the bindings.
      invalidate();
    },
  });
}

// ── Workspace knowledge ────────────────────────────────────────────

/** One workspace, for its project page. A 404 (deleted, unknown or not
 *  shared) is an answer the page shows as NotFoundState, not a crash. */
export function useWorkspace(id: string | null) {
  return useQuery<Workspace>({
    queryKey: [...KEY, id],
    queryFn: () => api.get(`/workspaces/${id}`),
    enabled: !!id,
    ...notFoundIsOrdinary,
  });
}

/** How many of the person's conversations the project page lists. */
export const WORKSPACE_CONVERSATIONS_SHOWN = 100;

/** The conversations filed in a workspace that the caller sees (own ones), the
 *  most recent first. */
export function useWorkspaceConversations(id: string | null) {
  return useQuery<ConversationListItem[]>({
    // Under "conversations": filing, creating or deleting one refreshes it.
    queryKey: ["conversations", "workspace", id],
    queryFn: () => api.get(`/chat/conversations?workspace_id=${encodeURIComponent(id ?? "")}&limit=${WORKSPACE_CONVERSATIONS_SHOWN}`),
    enabled: !!id,
  });
}

/** What the workspace gives its agents. Polls while a source is processing
 *  or a conversation waits to be indexed, so the counts settle on their own. */
export function useWorkspaceKnowledge(id: string | null) {
  return useQuery<WorkspaceKnowledgeSummary>({
    queryKey: [...KEY, id, "knowledge"],
    queryFn: () => api.get(`/workspaces/${id}/knowledge`),
    enabled: !!id,
    refetchInterval: (query) =>
      (query.state.data?.sources.pending ?? 0) > 0 ? REFETCH_INTERVAL_FAST : false,
  });
}

/** Create the workspace's Sources knowledge base on first use (idempotent):
 *  its documents and sync sources then go through the knowledge-base routes. */
export function useEnsureWorkspaceSources() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      api.post<{ knowledge_base_id: string }>(`/workspaces/${id}/sources`),
    meta: OWN_ERROR_TOAST,
    onSuccess: (_data, id) => qc.invalidateQueries({ queryKey: [...KEY, id, "knowledge"] }),
  });
}

/** Add a file attached in one of the workspace's conversations to its Sources. */
export function useAddWorkspaceSourceFromAttachment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, fileAttachmentId }: { id: string; fileAttachmentId: string }) =>
      api.post<{ knowledge_base_id: string; document_id: string }>(
        `/workspaces/${id}/sources/from-attachment`,
        { file_attachment_id: fileAttachmentId },
      ),
    meta: OWN_ERROR_TOAST,
    onSuccess: (data, variables) => {
      qc.invalidateQueries({ queryKey: [...KEY, variables.id, "knowledge"] });
      qc.invalidateQueries({ queryKey: ["knowledge-base", data.knowledge_base_id] });
      qc.invalidateQueries({ queryKey: ["kb-documents", data.knowledge_base_id] });
    },
  });
}

export function useWorkspaceSkills(id: string | null) {
  return useQuery<WorkspaceSkillRef[]>({
    queryKey: [...KEY, id, "skills"],
    queryFn: () => api.get(`/workspaces/${id}/skills`),
    enabled: !!id,
  });
}

/** Replace the linked skills (the whole set, like the knowledge bases). */
export function useSetWorkspaceSkills() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, skillIds }: { id: string; skillIds: string[] }) =>
      api.put<WorkspaceSkillRef[]>(`/workspaces/${id}/skills`, { skill_ids: skillIds }),
    meta: OWN_ERROR_TOAST,
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: [...KEY, variables.id, "skills"] });
      qc.invalidateQueries({ queryKey: [...KEY, variables.id, "knowledge"] });
    },
  });
}

const SETTINGS_KEY = ["workspace-knowledge-config"];

/** The platform-wide workspace knowledge settings (admin). */
export function useWorkspaceKnowledgeSettings(options: { enabled?: boolean } = {}) {
  return useQuery<WorkspaceKnowledgeConfig>({
    queryKey: SETTINGS_KEY,
    queryFn: () => api.get("/settings/workspace-knowledge"),
    enabled: options.enabled ?? true,
  });
}

export function useUpdateWorkspaceKnowledgeSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: WorkspaceKnowledgeConfig) =>
      api.put<WorkspaceKnowledgeConfig>("/settings/workspace-knowledge", body),
    onSuccess: (data) => qc.setQueryData(SETTINGS_KEY, data),
  });
}
