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

/** Nova prompt-safety rule API hooks. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  CreateNovaRuleRequest,
  NovaEngineStatus,
  NovaRule,
  NovaRuleSource,
  NovaRuleValidation,
  UpdateNovaRuleRequest,
} from "@/types";

export function useNovaRules() {
  return useQuery<NovaRule[]>({
    queryKey: ["nova-rules"],
    queryFn: () => api.get("/nova-rules"),
  });
}

export function useCreateNovaRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateNovaRuleRequest) => api.post("/nova-rules", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["nova-rules"] }),
  });
}

export function useUpdateNovaRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string } & UpdateNovaRuleRequest) =>
      api.put(`/nova-rules/${id}`, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["nova-rules"] }),
  });
}

export function useDeleteNovaRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/nova-rules/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["nova-rules"] }),
  });
}

/** Dry-parse a `.nov` rule for live editor feedback (no write). */
export function useValidateNovaRule() {
  return useMutation<NovaRuleValidation, Error, string>({
    mutationFn: (rule_text: string) =>
      api.post("/nova-rules/validate", { rule_text }),
  });
}

// ── Rule sync sources + engine status (admin) ────────────────────────────────

export function useNovaEngineStatus() {
  return useQuery<NovaEngineStatus>({
    queryKey: ["nova-engine-status"],
    queryFn: () => api.get("/nova-rules/engine-status"),
  });
}

export function useNovaRuleSources() {
  return useQuery<NovaRuleSource[]>({
    queryKey: ["nova-rule-sources"],
    queryFn: () => api.get("/nova-rules/sources"),
  });
}

export function useUpdateNovaRuleSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...data
    }: { id: string; sync_interval?: string; enabled?: boolean }) =>
      api.put(`/nova-rules/sources/${id}`, data),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["nova-rule-sources"] }),
  });
}

/** Trigger an immediate sync of a source; then refresh rules + sources. */
export function useSyncNovaRuleSource() {
  const qc = useQueryClient();
  return useMutation<{ synced_rules: number }, Error, string>({
    mutationFn: (id: string) => api.post(`/nova-rules/sources/${id}/sync`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["nova-rules"] });
      qc.invalidateQueries({ queryKey: ["nova-rule-sources"] });
    },
  });
}


/** Set the models the semantics:/llm: tiers run on (stored on the integration). */
export function useUpdateNovaModels() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      nova_llm_model?: string | null;
      nova_semantic_model?: string | null;
    }) => api.put("/nova-rules/models", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["nova-engine-status"] }),
  });
}
