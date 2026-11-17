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

/** Custom tool repository source hooks (`/custom-tool-sources`). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type {
  CreateCustomToolSourceRequest,
  CustomToolSource,
  CustomToolSyncReport,
} from "@/types";

const KEY = ["custom-tool-sources"] as const;

export function useCustomToolSources(enabled = true) {
  return useQuery<CustomToolSource[]>({
    queryKey: KEY,
    queryFn: () => api.get("/custom-tool-sources"),
    enabled,
  });
}

export function useCreateCustomToolSource() {
  const qc = useQueryClient();
  return useMutation<CustomToolSource, Error, CreateCustomToolSourceRequest>({
    mutationFn: (data) => api.post("/custom-tool-sources", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

export function useUpdateCustomToolSource() {
  const qc = useQueryClient();
  return useMutation<
    CustomToolSource,
    Error,
    { id: string } & Partial<Omit<CreateCustomToolSourceRequest, "provider" | "is_company_managed">> & {
      detached_paths?: string[];
    }
  >({
    mutationFn: ({ id, ...data }) => api.put(`/custom-tool-sources/${id}`, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

export function useDeleteCustomToolSource() {
  const qc = useQueryClient();
  return useMutation<void, Error, { id: string; deleteTools: boolean }>({
    mutationFn: ({ id, deleteTools }) =>
      api.delete(`/custom-tool-sources/${id}${deleteTools ? "?delete_tools=true" : ""}`),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["custom-tools"] });
      return qc.invalidateQueries({ queryKey: KEY });
    },
  });
}

/** Sync now: the repository is read, new or changed tools are tested, and the
 *  versions that pass are activated. */
export function useSyncCustomToolSource() {
  const qc = useQueryClient();
  return useMutation<{ source: CustomToolSource; report: CustomToolSyncReport | null }, Error, string>({
    mutationFn: (id) => api.post(`/custom-tool-sources/${id}/sync`, {}),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      qc.invalidateQueries({ queryKey: ["custom-tools"] });
      return qc.invalidateQueries({ queryKey: KEY });
    },
  });
}
