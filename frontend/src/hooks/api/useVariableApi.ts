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
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type { Variable } from "@/types";

export function useVariables(options?: { enabled?: boolean }) {
  // Accept an explicit ``enabled`` opt-out so callers gated on the
  // ``DISABLE_CUSTOM_VARIABLES`` security flag can suppress the network
  // request entirely (rather than fetching, caching, then hiding the
  // result client-side — which would still leak data through DevTools
  // and the React Query cache).
  return useQuery<Variable[]>({
    queryKey: ["variables"],
    queryFn: () => api.get("/variables"),
    enabled: options?.enabled ?? true,
  });
}

/** One variable by id - the detail route, which keeps the admin bypass. */
export function useVariable(id: string | undefined, options?: { enabled?: boolean }) {
  return useQuery<Variable>({
    queryKey: ["variables", id],
    queryFn: () => api.get(`/variables/${id}`),
    enabled: !!id && (options?.enabled ?? true),
  });
}

export function useCreateVariable() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      name: string;
      value?: string;
      is_secret?: boolean;
      description?: string;
      tags?: string[];
    }) => api.post<Variable>("/variables", data),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["variables"] });
    },
  });
}

export function useUpdateVariable() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...data
    }: {
      id: string;
      name?: string;
      value?: string;
      is_secret?: boolean;
      description?: string;
      tags?: string[];
      is_company_managed?: boolean;
      shared_group_ids?: string[];
    }) => api.put(`/variables/${id}`, data),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["variables"] });
    },
  });
}

export function useDeleteVariable() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/variables/${id}`),
    onSuccess: (_d, id) => {
      dropFromFlowNodeCatalog(qc, "variable", id);
      return qc.invalidateQueries({ queryKey: ["variables"] });
    },
  });
}
