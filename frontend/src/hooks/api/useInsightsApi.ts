// SPDX-License-Identifier: Apache-2.0

/** Department-grade LLM analytics: the department mapping and its preview. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  DepartmentMapping,
  DepartmentMappingResponse,
  DepartmentPreview,
  DirectoryImportResult,
} from "@/types";

const MAPPING_KEY = ["insights", "departments"] as const;
const PREVIEW_KEY = ["insights", "departments", "preview"] as const;

export function useDepartmentMapping() {
  return useQuery<DepartmentMappingResponse>({
    queryKey: MAPPING_KEY,
    queryFn: () => api.get("/insights/departments"),
  });
}

export function useDepartmentPreview(enabled: boolean) {
  return useQuery<DepartmentPreview>({
    queryKey: PREVIEW_KEY,
    queryFn: () => api.get("/insights/departments/preview"),
    enabled,
  });
}

export function useSaveDepartmentMapping() {
  const qc = useQueryClient();
  return useMutation<DepartmentMappingResponse, Error, DepartmentMapping>({
    mutationFn: (mapping) => api.put("/insights/departments", mapping),
    onSuccess: (saved) => {
      qc.setQueryData(MAPPING_KEY, saved);
      qc.invalidateQueries({ queryKey: PREVIEW_KEY });
    },
  });
}

export function useImportDirectoryDepartments() {
  const qc = useQueryClient();
  return useMutation<DirectoryImportResult, Error, { email: string; department: string | null }[]>({
    mutationFn: (entries) => api.post("/insights/departments/directory", { entries }),
    onSuccess: () => qc.invalidateQueries({ queryKey: PREVIEW_KEY }),
  });
}

export function useApplyDepartmentsToHistory() {
  const qc = useQueryClient();
  return useMutation<DepartmentMappingResponse["backfill"], Error, void>({
    mutationFn: () => api.post("/insights/departments/history", { rewrite: true }),
    onSuccess: () => qc.invalidateQueries({ queryKey: MAPPING_KEY }),
  });
}
