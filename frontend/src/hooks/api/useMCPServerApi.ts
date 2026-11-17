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

/** MCP server API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import type {
  EntityListOptions,
  MCPServer,
  CreateMCPServerRequest,
  MCPOAuthDiscovery,
  MCPConfigCandidate,
  MCPConfigPreviewResponse,
  MCPConfigImportResponse,
} from "@/types";

// --- MCP Servers (Tool Servers) ---
/** The MCP servers list; `{ scope: "all" }` is every server for an admin (detail pages). */
export function useMCPServers(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<MCPServer[]>({
    queryKey: entityListKey(["mcp-servers"], scope),
    queryFn: () => api.get(entityListPath("/mcp-servers", scope)),
  });
}

export function useCreateMCPServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateMCPServerRequest) => api.post("/mcp-servers", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });
}

export function useUpdateMCPServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string; user_id?: string | null } & Partial<CreateMCPServerRequest>) =>
      api.put(`/mcp-servers/${id}`, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });
}

export function useDeleteMCPServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/mcp-servers/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });
}

/** Connectivity test result.
 *
 * `auth_required` means the server is reachable and listed its tools, but will
 * refuse every `tools/call` until the connection is authorized — the state that
 * used to be reported as a plain success.
 */
interface MCPTestResult {
  status: "ok" | "auth_required" | "error";
  tools?: { name: string; description: string }[];
  tool_count?: number | null;
  message?: string;
  /** Only on `auth_required`: whether the server is already set to OAuth. */
  oauth_configured?: boolean;
}

export function useTestMCPServer() {
  const qc = useQueryClient();
  return useMutation<MCPTestResult, Error, string>({
    mutationFn: (id: string) => api.post(`/mcp-servers/${id}/test`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });
}

export function useRefreshMCPTools() {
  const qc = useQueryClient();
  return useMutation<MCPServer, Error, string>({
    mutationFn: (id: string) => api.post(`/mcp-servers/${id}/refresh-tools`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });
}

export function useTestMCPTool() {
  return useMutation<
    { status: string; output?: string; message?: string; duration_ms?: number },
    Error,
    { serverId: string; tool_name: string; arguments?: Record<string, unknown> }
  >({
    mutationFn: ({ serverId, ...body }) =>
      api.post(`/mcp-servers/${serverId}/test-tool`, body),
  });
}

export function useAuthorizeMCPServer() {
  return useMutation<{ authorize_url: string }, Error, string>({
    mutationFn: (id: string) => api.get(`/mcp-servers/${id}/authorize`),
    // The caller raises its own "OAuth failed" toast naming what to fix; the
    // global MutationCache handler would stack a second, vaguer one on top.
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Probe a remote MCP server for its OAuth metadata before saving it. */
export function useDiscoverMCPOAuth() {
  return useMutation<
    MCPOAuthDiscovery,
    Error,
    { url: string; authorize_url?: string; token_url?: string; relay_group_id?: string }
  >({
    mutationFn: (body) => api.post("/mcp-servers/oauth/discover", body),
    // Same reason: the form shows its own "Detection failed" toast.
    meta: { suppressGlobalErrorToast: true },
  });
}

/**
 * Read a configuration document without writing anything.
 *
 * Split from the import itself so the user sees what was found — and what was
 * skipped, and why — before any of it reaches their list.
 */
export function usePreviewMCPConfig() {
  return useMutation<MCPConfigPreviewResponse, Error, { content?: string; url?: string }>({
    mutationFn: (body) => api.post("/mcp-servers/import/config/preview", body),
    // The import dialog raises its own "Could not read that configuration"
    // toast with the reason (a 429 included); the global one would stack a
    // second, vaguer copy on top.
    meta: { suppressGlobalErrorToast: true },
  });
}

/** Create the servers the user picked out of a preview. */
export function useImportMCPConfig() {
  const qc = useQueryClient();
  return useMutation<MCPConfigImportResponse, Error, { servers: MCPConfigCandidate[]; tags?: string[] }>({
    mutationFn: (body) => api.post("/mcp-servers/import/config", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mcp-servers"] }),
    // Same reason: the dialog shows its own "Import failed" toast.
    meta: { suppressGlobalErrorToast: true },
  });
}
