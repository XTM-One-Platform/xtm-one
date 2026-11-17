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

/**
 * Catalog API hooks — all endpoints require authentication.
 *
 * All hooks use the standard `api` client which automatically attaches JWT.
 * The add-to-account mutation accepts an optional setup body for agents.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { saveBlobAs } from "@/lib/utils";
import type {
  AddToAccountResponse,
  CatalogEntityType,
  PaginatedCatalogResponse,
  PublicCatalogAgent,
  PublicCatalogAgentDetail,

  PublicCatalogCustomTool,
  PublicCatalogCustomToolDetail,
  PublicCatalogMCPServer,
  PublicCatalogMCPServerDetail,
  PublicCatalogPrompt,
  PublicCatalogPromptDetail,
  PublicCatalogSkill,
  PublicCatalogSkillDetail,
  PublicCatalogSummary,
  PublicCatalogTags,
} from "@/types";

const PREFIX = "/public/catalog";

/** Params accepted by all catalog list hooks. */
interface CatalogListParams {
  search?: string;
  tags?: string[];
  offset?: number;
  limit?: number;
}

function buildQuery(params?: CatalogListParams): string {
  if (!params) return "";
  const sp = new URLSearchParams();
  if (params.search) sp.set("search", params.search);
  if (params.tags && params.tags.length > 0) sp.set("tags", params.tags.join(","));
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}

// ---------------------------------------------------------------------------
// Summary (landing page)
// ---------------------------------------------------------------------------

export function usePublicCatalogSummary() {
  return useQuery<PublicCatalogSummary>({
    queryKey: ["catalog", "summary"],
    queryFn: () => api.get(`${PREFIX}`),
  });
}

// ---------------------------------------------------------------------------
// Tags (for filter pills)
// ---------------------------------------------------------------------------

export function usePublicCatalogTags() {
  return useQuery<PublicCatalogTags>({
    queryKey: ["catalog", "tags"],
    queryFn: () => api.get(`${PREFIX}/tags`),
  });
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

export function usePublicCatalogAgents(params?: CatalogListParams) {
  return useQuery<PaginatedCatalogResponse<PublicCatalogAgent>>({
    queryKey: ["catalog", "agents", params],
    queryFn: () => api.get(`${PREFIX}/agents${buildQuery(params)}`),
  });
}

export function usePublicCatalogAgent(id: string) {
  return useQuery<PublicCatalogAgentDetail>({
    queryKey: ["catalog", "agents", id],
    queryFn: () => api.get(`${PREFIX}/agents/${id}`),
    enabled: !!id,
  });
}



// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

export function usePublicCatalogPrompts(params?: CatalogListParams) {
  return useQuery<PaginatedCatalogResponse<PublicCatalogPrompt>>({
    queryKey: ["catalog", "prompts", params],
    queryFn: () => api.get(`${PREFIX}/prompts${buildQuery(params)}`),
  });
}

export function usePublicCatalogPrompt(id: string) {
  return useQuery<PublicCatalogPromptDetail>({
    queryKey: ["catalog", "prompts", id],
    queryFn: () => api.get(`${PREFIX}/prompts/${id}`),
    enabled: !!id,
  });
}

// ---------------------------------------------------------------------------
// Skills
// ---------------------------------------------------------------------------

export function usePublicCatalogSkills(params?: CatalogListParams) {
  return useQuery<PaginatedCatalogResponse<PublicCatalogSkill>>({
    queryKey: ["catalog", "skills", params],
    queryFn: () => api.get(`${PREFIX}/skills${buildQuery(params)}`),
  });
}

export function usePublicCatalogSkill(id: string) {
  return useQuery<PublicCatalogSkillDetail>({
    queryKey: ["catalog", "skills", id],
    queryFn: () => api.get(`${PREFIX}/skills/${id}`),
    enabled: !!id,
  });
}

// ---------------------------------------------------------------------------
// MCP Servers
// ---------------------------------------------------------------------------

export function usePublicCatalogMCPServers(params?: CatalogListParams) {
  return useQuery<PaginatedCatalogResponse<PublicCatalogMCPServer>>({
    queryKey: ["catalog", "mcp-servers", params],
    queryFn: () => api.get(`${PREFIX}/mcp-servers${buildQuery(params)}`),
  });
}

export function usePublicCatalogMCPServer(id: string) {
  return useQuery<PublicCatalogMCPServerDetail>({
    queryKey: ["catalog", "mcp-servers", id],
    queryFn: () => api.get(`${PREFIX}/mcp-servers/${id}`),
    enabled: !!id,
  });
}

// ---------------------------------------------------------------------------
// Custom Tools
// ---------------------------------------------------------------------------

export function usePublicCatalogCustomTools(params?: CatalogListParams) {
  return useQuery<PaginatedCatalogResponse<PublicCatalogCustomTool>>({
    queryKey: ["catalog", "custom-tools", params],
    queryFn: () => api.get(`${PREFIX}/custom-tools${buildQuery(params)}`),
  });
}

export function usePublicCatalogCustomTool(id: string) {
  return useQuery<PublicCatalogCustomToolDetail>({
    queryKey: ["catalog", "custom-tools", id],
    queryFn: () => api.get(`${PREFIX}/custom-tools/${id}`),
    enabled: !!id,
  });
}

// ---------------------------------------------------------------------------
// Download bundle URL helper
// ---------------------------------------------------------------------------

export async function downloadCatalogItem(type: CatalogEntityType, id: string) {
  const data = await api.get<Record<string, unknown>>(
    `/public/catalog/${type}/${id}/download`,
  );
  const blob = new Blob([JSON.stringify(data, null, 2)], {
    type: "application/json",
  });
  saveBlobAs(blob, `${type.replace(/s$/, "")}-export.json`);
}

// ---------------------------------------------------------------------------
// Add to account (with optional setup configuration)
// ---------------------------------------------------------------------------

export interface CatalogInstallConfig {
  name?: string;
  integration_bindings?: Record<string, string[]>;
}

export function useAddCatalogItemToAccount() {
  const qc = useQueryClient();
  return useMutation<
    AddToAccountResponse,
    Error,
    { type: CatalogEntityType; id: string; config?: CatalogInstallConfig }
  >({
    mutationFn: ({ type, id, config }) =>
      api.post<AddToAccountResponse>(
        `/public/catalog/${type}/${id}/add`,
        config ?? {},
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["prompts"] });
      qc.invalidateQueries({ queryKey: ["skills"] });
      qc.invalidateQueries({ queryKey: ["mcp-servers"] });
      qc.invalidateQueries({ queryKey: ["custom-tools"] });
      syncFlowNodeCatalog(qc);
    },
  });
}
