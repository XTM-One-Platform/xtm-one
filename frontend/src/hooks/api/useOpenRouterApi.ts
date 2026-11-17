// SPDX-License-Identifier: Apache-2.0

/** OpenRouter console hooks (`/integrations/:id/openrouter/...`). */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { MODEL_ANNOUNCEMENTS_QUERY_KEY } from "@/hooks/api/useSettingsApi";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type {
  CreateOpenRouterKeyRequest,
  LLMProvider,
  OpenRouterActivityRow,
  OpenRouterApiKey,
  OpenRouterCatalogKind,
  OpenRouterEndpoint,
  OpenRouterModel,
  OpenRouterOverview,
  UpdateOpenRouterKeyRequest,
} from "@/types";

const base = (integrationId: string) => `/integrations/${integrationId}/openrouter`;
const key = (integrationId: string, ...rest: unknown[]) => ["openrouter", integrationId, ...rest];

/** OpenRouter's refusals are deliberate answers (424 / 409 / 404): retrying them only delays the message. */
const noRetryOnAnswer = (failureCount: number, error: unknown) => {
  const status = (error as { status?: number } | null)?.status;
  if (status !== undefined && status < 500) return false;
  return failureCount < 2;
};

export function useOpenRouterOverview(integrationId: string, enabled = true) {
  return useQuery<OpenRouterOverview>({
    queryKey: key(integrationId, "overview"),
    queryFn: () => api.get(`${base(integrationId)}/overview`),
    enabled,
    retry: noRetryOnAnswer,
  });
}

export function useOpenRouterActivity(integrationId: string, enabled: boolean) {
  return useQuery<{ rows: OpenRouterActivityRow[] }>({
    queryKey: key(integrationId, "activity"),
    queryFn: () => api.get(`${base(integrationId)}/activity`),
    enabled,
    retry: noRetryOnAnswer,
    staleTime: 5 * 60_000,
  });
}

export function useOpenRouterModels(integrationId: string, kind: OpenRouterCatalogKind, enabled = true) {
  return useQuery<{ kind: OpenRouterCatalogKind; models: OpenRouterModel[] }>({
    queryKey: key(integrationId, "models", kind),
    queryFn: () => api.get(`${base(integrationId)}/models?kind=${kind}`),
    enabled,
    retry: noRetryOnAnswer,
    staleTime: 5 * 60_000,
  });
}

export function useOpenRouterModelEndpoints(integrationId: string, modelId: string | null) {
  return useQuery<{ model_id: string; endpoints: OpenRouterEndpoint[] }>({
    queryKey: key(integrationId, "endpoints", modelId),
    queryFn: () =>
      api.get(`${base(integrationId)}/model-endpoints?model_id=${encodeURIComponent(modelId ?? "")}`),
    enabled: !!modelId,
    retry: noRetryOnAnswer,
    staleTime: 5 * 60_000,
  });
}

export function useOpenRouterKeys(integrationId: string, enabled: boolean) {
  return useQuery<{ keys: OpenRouterApiKey[]; truncated: boolean }>({
    queryKey: key(integrationId, "keys"),
    queryFn: () => api.get(`${base(integrationId)}/keys`),
    enabled,
    retry: noRetryOnAnswer,
  });
}

function invalidateKeys(qc: QueryClient, integrationId: string) {
  qc.invalidateQueries({ queryKey: key(integrationId, "keys") });
  qc.invalidateQueries({ queryKey: key(integrationId, "overview") });
}

export function useCreateOpenRouterKey(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateOpenRouterKeyRequest) =>
      api.post<{ key: OpenRouterApiKey; secret: string }>(`${base(integrationId)}/keys`, data),
    onSuccess: () => invalidateKeys(qc, integrationId),
  });
}

export function useUpdateOpenRouterKey(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ hash, ...data }: { hash: string } & UpdateOpenRouterKeyRequest) =>
      api.patch<OpenRouterApiKey>(`${base(integrationId)}/keys/${hash}`, data),
    onSuccess: () => invalidateKeys(qc, integrationId),
  });
}

export function useDeleteOpenRouterKey(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (hash: string) => api.delete(`${base(integrationId)}/keys/${hash}`),
    onSuccess: () => invalidateKeys(qc, integrationId),
  });
}

/** A provider edit changes what every model picker offers: the same caches as Settings' provider edits. */
function invalidateProviders(qc: QueryClient, integrationId: string) {
  qc.invalidateQueries({ queryKey: key(integrationId, "overview") });
  qc.invalidateQueries({ queryKey: ["llm-providers"] });
  qc.invalidateQueries({ queryKey: ["available-models"] });
  qc.invalidateQueries({ queryKey: ["available-embedding-models"] });
  qc.invalidateQueries({ queryKey: ["available-image-models"] });
  qc.invalidateQueries({ queryKey: ["general-assistant-config"] });
  qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY });
}

export function useLinkOpenRouterProvider(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (
      data:
        | { provider_config_id: string }
        | { display_name?: string; available_models: string[]; default_model?: string | null },
    ) => api.post<LLMProvider>(`${base(integrationId)}/llm-provider`, data),
    onSuccess: () => {
      invalidateProviders(qc, integrationId);
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useUpdateOpenRouterProviderModels(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ configId, ...data }: { configId: string; available_models: string[]; default_model?: string | null }) =>
      api.put<LLMProvider>(`${base(integrationId)}/llm-provider/${configId}/models`, data),
    onSuccess: () => {
      invalidateProviders(qc, integrationId);
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useUnlinkOpenRouterProvider(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (configId: string) => api.delete(`${base(integrationId)}/llm-provider/${configId}`),
    onSuccess: () => {
      invalidateProviders(qc, integrationId);
      syncFlowNodeCatalog(qc);
    },
  });
}
