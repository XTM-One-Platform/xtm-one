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

/** Custom tool API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { API_BASE, api, getToken } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type {
  CustomToolSkillLink,
  CustomTool,
  CustomToolTestReport,
  CreateCustomToolRequest,
  EntityListOptions,
  GenerateCustomToolCodeRequest,
  GenerateCustomToolCodeResponse,
} from "@/types";

// --- Custom Tools (User-defined JavaScript tools) ---
/** The custom tools list; `{ scope: "all" }` is every tool for an admin (detail pages). */
export function useCustomTools(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<CustomTool[]>({
    queryKey: entityListKey(["custom-tools"], scope),
    queryFn: () => api.get(entityListPath("/custom-tools", scope)),
  });
}

export function useCustomToolSkills(toolId: string | undefined) {
  return useQuery<CustomToolSkillLink[]>({
    queryKey: ["custom-tool-skills", toolId],
    queryFn: () => api.get(`/custom-tools/${toolId}/skills`),
    enabled: !!toolId,
  });
}

export function useCreateCustomTool() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateCustomToolRequest) => api.post("/custom-tools", data),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["custom-tools"] });
    },
  });
}

export function useLintCustomTool() {
  return useMutation({
    mutationFn: (data: { language: string; code: string }) =>
      api.post("/custom-tools/lint", data),
  });
}

export function useGenerateCustomToolCode() {
  return useMutation<GenerateCustomToolCodeResponse, Error, GenerateCustomToolCodeRequest>({
    mutationFn: (data) => api.post("/custom-tools/generate-code", data),
    meta: { readOnly: true },
  });
}

export function useUpdateCustomTool() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string; user_id?: string | null } & Partial<CreateCustomToolRequest>) =>
      api.put(`/custom-tools/${id}`, data),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["custom-tools"] });
    },
  });
}

export function useDeleteCustomTool() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/custom-tools/${id}`),
    onSuccess: (_d, id) => {
      dropFromFlowNodeCatalog(qc, "custom_tool", id);
      return qc.invalidateQueries({ queryKey: ["custom-tools"] });
    },
  });
}

export function useTestCustomTool() {
  return useMutation<{ status: string; output?: string; message?: string }, Error, { id: string; inputs?: Record<string, unknown> }>({
    mutationFn: ({ id, inputs }) => api.post(`/custom-tools/${id}/test`, inputs || {}),
  });
}

export function useUploadToolTestFile() {
  return useMutation<
    { file_id: string; filename: string; content_type: string; size: number },
    Error,
    { toolId: string; file: File }
  >({
    mutationFn: async ({ toolId, file }) => {
      const form = new FormData();
      form.append("file", file);
      const token = getToken();
      const res = await fetch(`${API_BASE}/custom-tools/${toolId}/test/upload`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: form,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || "Upload failed");
      }
      return res.json();
    },
  });
}

export function useCleanupToolTestFiles() {
  return useMutation<{ purged: number }, Error, { toolId: string; fileIds: string[] }>({
    mutationFn: ({ toolId, fileIds }) =>
      api.post(`/custom-tools/${toolId}/test/cleanup`, { file_ids: fileIds }),
  });
}

/** Run the tool's declared tests; a full run (no `case_names`) becomes its test status. */
export function useRunCustomToolTests() {
  const qc = useQueryClient();
  return useMutation<
    CustomToolTestReport,
    Error,
    { id: string; case_names?: string[]; include_code_tests?: boolean; dry_run_only?: boolean }
  >({
    mutationFn: ({ id, ...body }) => api.post(`/custom-tools/${id}/run-tests`, body),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["custom-tools"] });
    },
  });
}

/** Detach a synced tool from its repository source: it becomes editable. */
export function useDetachCustomTool() {
  const qc = useQueryClient();
  return useMutation<CustomTool, Error, string>({
    mutationFn: (id) => api.post(`/custom-tools/${id}/detach`, {}),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["custom-tool-sources"] });
      return qc.invalidateQueries({ queryKey: ["custom-tools"] });
    },
  });
}
