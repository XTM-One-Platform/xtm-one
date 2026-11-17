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

import { invalidateDashboards } from "@/hooks/api/useCustomDashboardApi";
import { api } from "@/lib/api";
import type { CustomDashboard, PackIntegration, WidgetPack } from "@/types";

const PACKS_KEY = ["dashboard-widget-packs"] as const;

export type WidgetPackScope = "all" | "mine" | "shared" | "company" | "builtin";

/**
 * The packs the caller may use: built-ins first, then the most recent (Dashboard Studio).
 * *tolerant* keeps a failed read in the caller (`isError`), for a page that draws its own
 * error line with Retry (`/dashboards/packs`); without it the app policy hands it to the boundary.
 */
export function useWidgetPacks(scope: WidgetPackScope = "all", enabled = true, { tolerant = false }: { tolerant?: boolean } = {}) {
  return useQuery<WidgetPack[]>({
    queryKey: [...PACKS_KEY, "list", scope],
    queryFn: () => api.get(`/dashboard-widget-packs?scope=${scope}`),
    enabled,
    ...(tolerant ? { throwOnError: false } : {}),
  });
}

/** The integrations a pack's live widgets can be bound to. */
export function usePackIntegrations(enabled = true) {
  return useQuery<PackIntegration[]>({
    queryKey: [...PACKS_KEY, "integrations"],
    queryFn: () => api.get("/dashboard-widget-packs/integrations"),
    enabled,
  });
}

/** Import a pack from a file (an XTM One pack, an OpenCTI or OpenAEV dashboard export). */
export function useImportWidgetPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.postForm<WidgetPack>("/dashboard-widget-packs/import", form);
    },
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void qc.invalidateQueries({ queryKey: PACKS_KEY }),
  });
}

/** A private copy of a pack (how a built-in is changed). */
export function useDuplicateWidgetPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<WidgetPack>(`/dashboard-widget-packs/${id}/duplicate`),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void qc.invalidateQueries({ queryKey: PACKS_KEY }),
  });
}

export function useDeleteWidgetPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/dashboard-widget-packs/${id}`),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void qc.invalidateQueries({ queryKey: PACKS_KEY }),
  });
}

/** Insert a pack's widgets into a dashboard, at the revision the page read. */
export function useInsertWidgetPack(dashboardId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      packId,
      ...body
    }: {
      packId: string;
      revision: number;
      widget_ids?: string[];
      bindings?: Record<string, string>;
      after?: string;
    }) =>
      api.post<{ inserted: string[]; dashboard: CustomDashboard }>(`/dashboard-widget-packs/${packId}/insert`, {
        dashboard_id: dashboardId,
        ...body,
      }),
    meta: { suppressGlobalErrorToast: true },
    onSuccess: () => void invalidateDashboards(qc),
  });
}
