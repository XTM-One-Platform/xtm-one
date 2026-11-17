// SPDX-License-Identifier: Apache-2.0

/** LM Studio console hooks (`/integrations/:id/lmstudio/...`). */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { MODEL_ANNOUNCEMENTS_QUERY_KEY } from "@/hooks/api/useSettingsApi";
import { syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type {
  LLMProvider,
  LMStudioDownloadJob,
  LMStudioLoadRequest,
  LMStudioModel,
  LMStudioOverview,
} from "@/types";

const base = (integrationId: string) => `/integrations/${integrationId}/lmstudio`;
const key = (integrationId: string, ...rest: unknown[]) => ["lmstudio", integrationId, ...rest];

/** The server's and the relay's refusals are deliberate answers (424 / 409 / 404): retrying them only delays the message. */
const noRetryOnAnswer = (failureCount: number, error: unknown) => {
  const status = (error as { status?: number } | null)?.status;
  if (status !== undefined && status < 500) return false;
  return failureCount < 2;
};

/** A download still moving is asked again this often. */
const DOWNLOAD_POLL_MS = 2_000;

export function useLMStudioOverview(integrationId: string, enabled = true) {
  return useQuery<LMStudioOverview>({
    queryKey: key(integrationId, "overview"),
    queryFn: () => api.get(`${base(integrationId)}/overview`),
    enabled,
    retry: noRetryOnAnswer,
  });
}

export function useLMStudioModels(integrationId: string, enabled = true) {
  return useQuery<{ models: LMStudioModel[] }>({
    queryKey: key(integrationId, "models"),
    queryFn: () => api.get(`${base(integrationId)}/models`),
    enabled,
    retry: noRetryOnAnswer,
  });
}

function invalidateModels(qc: QueryClient, integrationId: string) {
  qc.invalidateQueries({ queryKey: key(integrationId, "models") });
}

export function useLoadLMStudioModel(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: LMStudioLoadRequest) =>
      api.post<{ instance_id: string; type: string; load_time_seconds: number | null }>(`${base(integrationId)}/models/load`, data),
    onSuccess: () => invalidateModels(qc, integrationId),
  });
}

export function useUnloadLMStudioModel(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (instanceId: string) =>
      api.post<{ instance_id: string }>(`${base(integrationId)}/models/unload`, { instance_id: instanceId }),
    onSuccess: () => invalidateModels(qc, integrationId),
  });
}

export function useStartLMStudioDownload(integrationId: string) {
  return useMutation({
    mutationFn: (data: { model: string; quantization?: string }) =>
      api.post<LMStudioDownloadJob>(`${base(integrationId)}/downloads`, data),
  });
}

/** A download's progress, asked again while it moves; the listing is refreshed once it completes. */
export function useLMStudioDownload(integrationId: string, jobId: string | null) {
  const qc = useQueryClient();
  return useQuery<LMStudioDownloadJob>({
    queryKey: key(integrationId, "download", jobId),
    queryFn: async () => {
      const job = await api.get<LMStudioDownloadJob>(`${base(integrationId)}/downloads/${encodeURIComponent(jobId ?? "")}`);
      if (job.status === "completed") invalidateModels(qc, integrationId);
      return job;
    },
    enabled: !!jobId,
    retry: noRetryOnAnswer,
    refetchInterval: (query) => (query.state.data?.status === "downloading" ? DOWNLOAD_POLL_MS : false),
  });
}

/** A provider edit changes what every model picker offers: the same caches as Settings' provider edits. */
function invalidateProviders(qc: QueryClient, integrationId: string) {
  qc.invalidateQueries({ queryKey: key(integrationId, "overview") });
  invalidateModels(qc, integrationId);
  qc.invalidateQueries({ queryKey: ["llm-providers"] });
  qc.invalidateQueries({ queryKey: ["available-models"] });
  qc.invalidateQueries({ queryKey: ["available-embedding-models"] });
  qc.invalidateQueries({ queryKey: ["available-image-models"] });
  qc.invalidateQueries({ queryKey: ["general-assistant-config"] });
  qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY });
}

export function useLinkLMStudioProvider(integrationId: string) {
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

export function useUpdateLMStudioProviderModels(integrationId: string) {
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

export function useUnlinkLMStudioProvider(integrationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (configId: string) => api.delete(`${base(integrationId)}/llm-provider/${configId}`),
    onSuccess: () => {
      invalidateProviders(qc, integrationId);
      syncFlowNodeCatalog(qc);
    },
  });
}
