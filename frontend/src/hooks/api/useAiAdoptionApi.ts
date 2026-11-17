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
import { invalidateApprovalUsage } from "@/hooks/api/useToolApprovals";
import type {
  AiAdoptionGaps,
  AiAdoptionOverview,
  PlatformSettings,
} from "@/types";
import { REFETCH_INTERVAL_SLOW } from "@/lib/constants";

// ---------------------------------------------------------------------------
// AI Adoption dashboard endpoints (agentic AI on the platform only, #11380)
// ---------------------------------------------------------------------------

export function useAiAdoptionOverview(days = 30, groupId?: string) {
  const params = new URLSearchParams({ days: String(days) });
  if (groupId) params.set("group_id", groupId);
  return useQuery<AiAdoptionOverview>({
    queryKey: ["ai-adoption", "overview", days, groupId],
    queryFn: () => api.get(`/ai-adoption/overview?${params}`),
    refetchInterval: REFETCH_INTERVAL_SLOW,
  });
}

// ---------------------------------------------------------------------------
// Platform settings
// ---------------------------------------------------------------------------

export function usePlatformSettings() {
  return useQuery<PlatformSettings>({
    queryKey: ["platform-settings"],
    queryFn: () => api.get("/settings/platform"),
  });
}

export function useUpdatePlatformSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: Partial<PlatformSettings>) =>
      api.put("/settings/platform", data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["platform-settings"] });
      // Also refresh the public platform config so branding meta tags,
      // page title, favicon, and sidebar logo update immediately.
      qc.invalidateQueries({ queryKey: ["platform-config"] });
      // The AI switch and the license decide whether the General Assistant asks.
      invalidateApprovalUsage(qc);
    },
  });
}

// ---------------------------------------------------------------------------
// Adoption gaps
// ---------------------------------------------------------------------------

export function useAiAdoptionGaps(days: number, groupId?: string) {
  const params = new URLSearchParams({ days: String(days) });
  if (groupId) params.set("group_id", groupId);
  return useQuery<AiAdoptionGaps>({
    queryKey: ["ai-adoption-gaps", days, groupId],
    queryFn: () => api.get(`/ai-adoption/gaps?${params}`),
  });
}
