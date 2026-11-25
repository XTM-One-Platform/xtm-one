// SPDX-License-Identifier: Apache-2.0

/** LLM provider, general assistant, SSO, Langfuse, OAuth defaults, and tags hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { invalidateMyMemory } from "@/hooks/api/useMemoryConsolidationsApi";
import { invalidateApprovalUsage } from "@/hooks/api/useToolApprovals";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api } from "@/lib/api";
import { REFETCH_INTERVAL_SLOW, REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { SECURITY_AUDIT_QUERY_KEY } from "./useSecurityAuditApi";
import type {
  AgentFailurePoolStatus,
  BuiltinSubTool,
  BuiltinToolGroup,
  ContextWindowScan,
  EntityListScope,
  UserMemoryEntry,
  LLMProvider,
  LLMProviderAccess,
  LLMProviderDeletionImpact,
  AvailableModel,
  MediaModelsInUse,
  AvailableDecisionModels,
  GeneralAssistantConfig,
  SpeechVoicesResponse,
  SSOConfig,
  SSOOIDCTestResult,
  UpdateSSOConfigRequest,
  LangfuseConfig,
  SecurityOptionsResponse,
  ScannableToolsResponse,
  UpdateSecurityOptionsRequest,
  ObservabilityConfig,
  OAuthDefault,
  EmbeddingModelTestResult,
  ProviderModelsResponse,
  ProviderValidationResult,
  QuotaConfig,
  QuotaLegacyNotices,
  UserQuotaInfo,
  QuotaMonitoring,
  ModelPricingStatus,
  ModelPricingSyncResult,
  ModelPricingRepriceResult,
  ModelPricingRow,
  ModelPricingRowListResponse,
  ModelPricingUnpricedModelListResponse,
  ModelAnnouncementCatalogs,
  ModelAnnouncementPreview,
  ModelAnnouncementStatus,
  ModelAnnouncementSummary,
  RetiredModelChoice,
  SupportCodeRedeemResult,
} from "@/types";

function invalidateModelQueries(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["available-models"] });
  qc.invalidateQueries({ queryKey: ["available-embedding-models"] });
  qc.invalidateQueries({ queryKey: ["available-image-models"] });
  qc.invalidateQueries({ queryKey: ["media-models-in-use"] });
  qc.invalidateQueries({ queryKey: ["available-decision-models"] });
}

// --- LLM Providers ---
export function useLLMProviders() {
  return useQuery<LLMProvider[]>({ queryKey: ["llm-providers"], queryFn: () => api.get("/settings/llm-providers") });
}

export function useCreateLLMProvider() {
  const qc = useQueryClient();
  return useMutation<
    LLMProvider,
    Error,
    {
      provider: string;
      api_key: string;
      display_name?: string;
      default_model?: string;
      available_models?: string[];
      context_windows?: Record<string, number> | null;
    } & Partial<LLMProviderAccess>
  >({
    mutationFn: (data) => api.post("/settings/llm-providers", data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      invalidateModelQueries(qc);
      qc.invalidateQueries({ queryKey: ["general-assistant-config"] });
      qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY });
      syncFlowNodeCatalog(qc);
    },
  });
}

/** Lists an env-seeded provider's models again, as a restart would. */
export function useRefreshProviderModels(providerId: string) {
  const qc = useQueryClient();
  return useMutation<LLMProvider, Error, { discardEdits?: boolean } | void>({
    // `discardEdits`: an admin-edited catalog follows the listing again.
    mutationFn: (vars) =>
      vars?.discardEdits
        ? api.post(`/settings/llm-providers/${providerId}/refresh-models`, { discard_edits: true })
        : api.post(`/settings/llm-providers/${providerId}/refresh-models`),
    // Only this provider's catalog moves, and the announcement it records.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      invalidateModelQueries(qc);
      qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useUpdateLLMProvider() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      ...data
    }: {
      id: string;
      display_name?: string | null;
      default_model?: string;
      available_models?: string[];
      context_windows?: Record<string, number> | null;
    } & Partial<LLMProviderAccess>) => api.put<LLMProvider>(`/settings/llm-providers/${id}`, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      invalidateModelQueries(qc);
      qc.invalidateQueries({ queryKey: ["general-assistant-config"] });
      qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY });
      syncFlowNodeCatalog(qc);
    },
  });
}

function deletionQuery(replacementProviderId?: string | null, expectedImpact?: string | null) {
  const params = new URLSearchParams();
  if (replacementProviderId) params.set("replacement_provider_id", replacementProviderId);
  if (expectedImpact) params.set("expected_impact", expectedImpact);
  const query = params.toString();
  return query ? `?${query}` : "";
}

/**
 * What deleting a provider config does to every route that uses it,
 * for the replacement the admin picked (or the rule's pick when none). Errors
 * are rendered by the confirmation dialog, not thrown to the page.
 */
export function useLLMProviderDeletionImpact(
  providerId: string | undefined,
  replacementProviderId: string | null,
) {
  return useQuery<LLMProviderDeletionImpact>({
    queryKey: ["llm-provider-deletion-impact", providerId, replacementProviderId],
    queryFn: () =>
      api.get(
        `/settings/llm-providers/${providerId}/deletion-impact${deletionQuery(replacementProviderId)}`,
      ),
    enabled: !!providerId,
    // Switching the replacement keeps the previous answer on screen while the
    // new one loads, instead of blanking the dialog.
    placeholderData: (prev) => prev,
    staleTime: 0,
    retry: false,
    throwOnError: false,
  });
}

export function useDeleteLLMProvider() {
  const qc = useQueryClient();
  return useMutation({
    // `expectedImpact` is the fingerprint of the preview the admin read: the
    // backend answers 409 instead of applying anything else.
    mutationFn: ({
      id,
      replacementProviderId,
      expectedImpact,
    }: {
      id: string;
      replacementProviderId?: string | null;
      expectedImpact?: string | null;
    }) => api.delete(`/settings/llm-providers/${id}${deletionQuery(replacementProviderId, expectedImpact)}`),
    // The confirmation dialog shows the error next to the choice that caused it.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
      invalidateModelQueries(qc);
      dropFromFlowNodeCatalog(qc, "llm_provider", id);
      // The backend resets agents and user preferences pinned to the config
      // and re-seeds the GA / platform defaults, so every cache holding them
      // is stale.
      qc.invalidateQueries({ queryKey: ["general-assistant-config"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["agent"] });
      qc.invalidateQueries({ queryKey: ["me"] });
      qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY });
      qc.removeQueries({ queryKey: ["llm-provider-deletion-impact"] });
    },
    // A refused delete may mean the provider is already gone (another admin):
    // the list says so, and the dialog of a provider no longer listed closes.
    onError: () => {
      qc.invalidateQueries({ queryKey: ["llm-providers"] });
    },
  });
}

export function useValidateProvider() {
  return useMutation<
    ProviderValidationResult,
    Error,
    { provider: string; api_key: string; capability: "reasoning" | "embedding" | "image" | "speech" | "video" }
  >({
    mutationFn: (data) => api.post("/settings/llm-providers/validate", data),
  });
}

export function useProviderModels(providerId: string | undefined) {
  return useQuery<ProviderModelsResponse>({
    queryKey: ["provider-models", providerId],
    queryFn: () => api.get(`/settings/llm-providers/${providerId}/models`),
    enabled: !!providerId,
    // The editor retires models the provider no longer lists: every opening reads the list
    // afresh, and nothing refetches it behind the admin's back (a refetch would rewrite the
    // selection, and calls the provider each time). The hook stays mounted and its key flips with
    // `providerId`, which `refetchOnMount` ignores: the entry is dropped on close instead.
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    /* Not thrown to the page ErrorBoundary: this reaches a provider, so it
       is refused with AI off (FR-007), and the global `throwOnError: true`
       turned that refusal into a blank page. The caller shows no results
       instead (/code-review, 2026-08-28). */
    throwOnError: false,
  });
}

/**
 * Looks up the context window of some of a provider's models (its own listing,
 * then the model catalog). Read-only: what it finds is saved with the models.
 */
export function useScanContextWindows(providerId: string) {
  return useMutation<ContextWindowScan, Error, string[]>({
    mutationFn: (models) =>
      api.post(`/settings/llm-providers/${providerId}/context-windows/scan`, { models }),
    // Writes nothing; the editor says itself when a scan it was asked for fails,
    // and stays quiet when the one run on enabling a model does.
    meta: { readOnly: true, suppressGlobalErrorToast: true },
  });
}

/**
 * The chat models the viewer may pick: those of the providers they may use.
 * `{ scope: "all" }` is every configured provider's for an admin
 * setting a model for others (platform defaults, quotas, someone else's agent).
 */
export function useAvailableModels(options: { enabled?: boolean; scope?: EntityListScope } = {}) {
  const scope = useEntityListScope(options);
  return useQuery<AvailableModel[]>({
    queryKey: entityListKey(["available-models"], scope),
    queryFn: () => api.get(entityListPath("/settings/available-models", scope)),
    enabled: options.enabled ?? true,
  });
}

export function useAvailableEmbeddingModels() {
  return useQuery<AvailableModel[]>({
    queryKey: ["available-embedding-models"],
    queryFn: () => api.get("/settings/available-embedding-models"),
  });
}

export function useAvailableDecisionModels() {
  return useQuery<AvailableDecisionModels>({
    queryKey: ["available-decision-models"],
    queryFn: () => api.get("/settings/available-decision-models"),
  });
}

export function useAvailableImageModels() {
  return useQuery<AvailableModel[]>({
    queryKey: ["available-image-models"],
    queryFn: () => api.get("/settings/available-image-models"),
  });
}

/** Video generation models across the configured providers: Sora, Veo, and what a gateway serves. */
export function useAvailableVideoModels() {
  return useQuery<AvailableModel[]>({
    queryKey: ["available-video-models"],
    queryFn: () => api.get("/settings/available-video-models"),
  });
}

/** Why the video model chosen in Settings can no longer make a video (a retired model), if it cannot. */
export function useVideoModelStatus(enabled: boolean) {
  return useQuery<{ unavailable: string | null }>({
    queryKey: ["video-model-status"],
    queryFn: () => api.get("/settings/video-model-status"),
    enabled,
  });
}

/** The image and speech models generation runs on: asked only while a card has none saved, since resolving may ask the gateway. */
export function useMediaModelsInUse(enabled: boolean) {
  return useQuery<MediaModelsInUse>({
    queryKey: ["media-models-in-use"],
    queryFn: () => api.get("/settings/media-models-in-use"),
    enabled,
  });
}

/** The speech models of the configured providers. */
export function useAvailableSpeechModels() {
  return useQuery<AvailableModel[]>({
    queryKey: ["available-speech-models"],
    queryFn: () => api.get("/settings/available-speech-models"),
  });
}

/** The music generation models of the configured providers: Eleven Music, Lyria. */
export function useAvailableMusicModels(enabled = true) {
  return useQuery<AvailableModel[]>({
    queryKey: ["available-music-models"],
    queryFn: () => api.get("/settings/available-music-models"),
    enabled,
  });
}

/** The voices of a speech model; the platform's speech model when none is named. */
export function useSpeechVoices(providerId: string | null, model: string | null, enabled = true) {
  const params = new URLSearchParams();
  if (providerId) params.set("provider_id", providerId);
  if (model) params.set("model", model);
  const query = params.toString();
  return useQuery<SpeechVoicesResponse>({
    queryKey: ["speech-voices", providerId, model],
    queryFn: () => api.get(`/settings/speech-voices${query ? `?${query}` : ""}`),
    enabled,
    staleTime: 5 * 60_000,
  });
}

export function useTestEmbeddingModel() {
  return useMutation<
    EmbeddingModelTestResult,
    Error,
    { provider: string; model: string; text?: string; provider_id?: string }
  >({
    mutationFn: (data) => api.post("/settings/test-embedding-model", data),
  });
}

export function useGeneralAssistantConfig(options: { enabled?: boolean } = {}) {
  return useQuery<GeneralAssistantConfig>({
    queryKey: ["general-assistant-config"],
    queryFn: () => api.get("/settings/general-assistant"),
    enabled: options.enabled ?? true,
  });
}

export function useUpdateGeneralAssistantConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      linked_agent_id?: string | null;
      llm_provider?: string | null;
      llm_provider_id?: string | null;
      llm_model?: string | null;
      agent_default_provider?: string | null;
      agent_default_provider_id?: string | null;
      agent_default_model?: string | null;
      default_reasoning_effort?: string | null;
      embedding_provider?: string | null;
      embedding_provider_id?: string | null;
      embedding_model?: string | null;
      embedding_dimensions?: number | null;
      image_provider?: string | null;
      image_provider_id?: string | null;
      image_model?: string | null;
      video_provider?: string | null;
      video_provider_id?: string | null;
      video_model?: string | null;
      speech_provider?: string | null;
      speech_provider_id?: string | null;
      speech_model?: string | null;
      speech_voice?: string | null;
      music_provider?: string | null;
      music_provider_id?: string | null;
      music_model?: string | null;
      routing_model?: string | null;
      routing_provider_id?: string | null;
      fallback_model?: string | null;
      fallback_provider_id?: string | null;
      decision_model?: string | null;
      decision_provider_id?: string | null;
      max_iterations?: number;
      system_prompt?: string;
      knowledge_base_ids?: string[];
      mcp_server_ids?: string[];
      custom_tool_ids?: string[];
      builtin_tool_ids?: string[];
      /** Company-managed skills; `null` = back to the built-in ones. */
      skill_ids?: string[] | null;
      integration_bindings?: Record<string, string[]>;
      handover_agent_ids?: string[];
      handover_types?: Record<string, string>;
      available_channel_types?: string[] | null;
      channel_config?: Record<string, unknown> | null;
      mcp_exposed_agent_ids?: string[];
      platform_mcp_enabled?: boolean;
      xtm_mcp_enabled?: boolean;
      reply_suggestions_enabled?: boolean;
      /** Also email every user that `agent_default_model` is the new default. */
      announce_default_model?: boolean;
    }) =>
      api.put<GeneralAssistantConfig>("/settings/general-assistant", data),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: ["general-assistant-config"] });
      // An image or speech change drops it, so a model just cleared never shows the cached answer that named it.
      if (Object.entries(variables).some(([key, value]) => value !== undefined && /^(image|speech)_/.test(key))) {
        qc.removeQueries({ queryKey: ["media-models-in-use"] });
      }
      if (variables.agent_default_model !== undefined) {
        qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY });
      }
    },
  });
}

// --- Model announcements ---
export const MODEL_ANNOUNCEMENTS_QUERY_KEY = ["model-announcements"] as const;

/** What an announcement of the default model would reach, and the latest one sent. */
export function useModelAnnouncementStatus(enabled = true) {
  return useQuery<ModelAnnouncementStatus>({
    queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY,
    queryFn: () => api.get("/settings/model-announcements"),
    enabled,
    // An open announcement goes out one batch a minute, so its progress moves while the panel is open.
    refetchInterval: (query) =>
      query.state.data?.latest && ["pending", "sending"].includes(query.state.data.latest.status)
        ? REFETCH_INTERVAL_STANDARD
        : false,
  });
}

/**
 * What an announcement email is about: `model` the default being saved,
 * `added_models` / `removed_models` the model changes as the admin edited them,
 * none of them the current default.
 */
export interface ModelAnnouncementEmailBody {
  model?: string | null;
  provider?: string | null;
  added_models?: string[];
  removed_models?: (string | RetiredModelChoice)[];
}

/** Email the calling admin the announcement the users would get. */
export function useSendTestModelAnnouncement() {
  return useMutation<{ sent_to: string }, Error, ModelAnnouncementEmailBody>({
    meta: { suppressGlobalErrorToast: true },
    mutationFn: (body) => api.post("/settings/model-announcements/test", body),
  });
}

/**
 * The announcement email rendered as the users would receive it, sent to
 * nobody. Rendering is a pure read, so it is a query keyed on the body:
 * the previous rendering stays on screen while the next one loads.
 */
export function useModelAnnouncementPreview(body: ModelAnnouncementEmailBody, enabled: boolean) {
  return useQuery<ModelAnnouncementPreview>({
    queryKey: ["model-announcement-preview", body],
    queryFn: () => api.post("/settings/model-announcements/preview", body),
    enabled,
    placeholderData: (previous) => previous,
    staleTime: 60_000,
    retry: false,
    // A refused selection is shown in the preview pane, never as a blank page.
    throwOnError: false,
  });
}

/**
 * The chat models the configured providers serve that the users cannot pick,
 * with how each provider answered. Asks every provider, so it runs when
 * the editor opens and is kept: `refetch()` asks again.
 */
export function useModelAnnouncementCatalogs(enabled: boolean) {
  return useQuery<ModelAnnouncementCatalogs>({
    queryKey: ["model-announcement-catalogs"],
    queryFn: () => api.get("/settings/model-announcements/catalogs"),
    enabled,
    staleTime: 10 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
    // Refused with AI off, like any model listing: the editor works from the platform's own lists instead.
    throwOnError: false,
  });
}

/** Email every user that the current default model is the default. */
export function useAnnounceDefaultModel() {
  const qc = useQueryClient();
  return useMutation<ModelAnnouncementSummary, Error, void>({
    meta: { suppressGlobalErrorToast: true },
    mutationFn: () => api.post("/settings/model-announcements", {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY }),
  });
}

/**
 * Email every user the models the admin chose as new and as retired:
 * `draft_id` the saved changes they reviewed (`unannounced_change.id`, whose
 * saves it consumes), omitted for an announcement of their own.
 */
export function useAnnounceModelChanges() {
  const qc = useQueryClient();
  return useMutation<
    ModelAnnouncementSummary,
    Error,
    { draft_id?: string | null; added_models: string[]; removed_models: (string | RetiredModelChoice)[] }
  >({
    meta: { suppressGlobalErrorToast: true },
    mutationFn: (body) => api.post("/settings/model-announcements/model-changes", body),
    // A refusal (the changes were emailed meanwhile) leaves a panel to refresh too.
    onSettled: () => qc.invalidateQueries({ queryKey: MODEL_ANNOUNCEMENTS_QUERY_KEY }),
  });
}

/** Save (activate/update) the XTM enterprise license PEM. */
export function useSaveEnterpriseLicense() {
  const qc = useQueryClient();
  return useMutation<unknown, Error, string>({
    // Invalid PEMs render inline in the calling form; suppress the global
    // MutationCache toast in ``main.tsx`` to avoid duplicating the message.
    meta: { suppressGlobalErrorToast: true },
    mutationFn: (license) =>
      api.put("/settings/general-assistant", { enterprise_license: license }),
    onSuccess: () => {
      qc.refetchQueries({ queryKey: ["platform-config"] });
      invalidateApprovalUsage(qc);
    },
  });
}

// --- SSO Configuration (admin) ---
export function useSSOConfig() {
  return useQuery<SSOConfig>({
    queryKey: ["sso-config"],
    queryFn: () => api.get("/settings/sso"),
  });
}

export function useUpdateSSOConfig() {
  const qc = useQueryClient();
  return useMutation<SSOConfig, Error, UpdateSSOConfigRequest>({
    mutationFn: (body) => api.put("/settings/sso", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sso-config"] });
      qc.invalidateQueries({ queryKey: ["auth-config"] });
    },
  });
}

// Tries the URL typed in the form, saved or not.
export function useTestOidcDiscovery() {
  return useMutation<SSOOIDCTestResult, Error, string>({
    mutationFn: (oidc_discovery_url) =>
      api.post("/settings/sso/test-oidc", { oidc_discovery_url }),
  });
}

// --- Langfuse Configuration (admin) ---
// `GET /settings/langfuse` is gated by `require_admin` on the backend, so this
// query 403s for non-admin users. Callers that render on non-admin surfaces
// (e.g. the run detail page) must pass `{ enabled: isAdmin }` to avoid a 403
// bubbling up through the global `throwOnError` and crashing the page.
export function useLangfuseConfig(options?: { enabled?: boolean }) {
  return useQuery<LangfuseConfig>({
    queryKey: ["langfuse-config"],
    queryFn: () => api.get("/settings/langfuse"),
    enabled: options?.enabled ?? true,
  });
}

export function useUpdateLangfuseConfig() {
  const qc = useQueryClient();
  return useMutation<LangfuseConfig, Error, { enabled: boolean; public_key?: string; secret_key?: string; host?: string }>({
    mutationFn: (body) => api.put("/settings/langfuse", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["langfuse-config"] });
    },
  });
}

// --- Security options (admin, Settings > Access and security > Security options) ---
const SECURITY_OPTIONS_QUERY_KEY = ["security-options"] as const;

/**
 * `enabled` must be false for a non-admin, whom the endpoint refuses. The
 * section renders its own error state with a retry, and the Security audit
 * reads the options only to link a finding to its field: a failure costs the
 * link, never the page.
 */
export function useSecurityOptions(enabled = true) {
  return useQuery<SecurityOptionsResponse>({
    queryKey: SECURITY_OPTIONS_QUERY_KEY,
    queryFn: () => api.get("/settings/security-options"),
    enabled,
    throwOnError: false,
  });
}

/** The tools "Tools screened by the prompt guard" can list, for its editor. Admin only. */
export function useScannableTools(enabled = true) {
  return useQuery<ScannableToolsResponse>({
    queryKey: ["scannable-tools"],
    queryFn: () => api.get("/settings/security-options/scannable-tools"),
    enabled,
    throwOnError: false,
    staleTime: 60_000,
  });
}

export function useUpdateSecurityOptions() {
  const qc = useQueryClient();
  return useMutation<SecurityOptionsResponse, Error, UpdateSecurityOptionsRequest>({
    mutationFn: (body) => api.put("/settings/security-options", body),
    // The section says why a save failed in a toast of its own.
    meta: { suppressGlobalErrorToast: true },
    onSuccess: (data) => {
      qc.setQueryData(SECURITY_OPTIONS_QUERY_KEY, data);
      // The password length is served by /auth/config, the feature switches
      // by /platform/config.
      qc.invalidateQueries({ queryKey: ["auth-config"] });
      qc.invalidateQueries({ queryKey: ["platform-config"] });
      // The API key maximum lifetime, read by the key dialog and lists.
      qc.invalidateQueries({ queryKey: ["api-key-policy"] });
      // The audit reads the live configuration: the next visit re-runs it.
      qc.invalidateQueries({ queryKey: SECURITY_AUDIT_QUERY_KEY });
    },
  });
}

export function useTestLangfuse() {
  return useMutation<{ status: string; message: string }, Error, void>({
    mutationFn: () => api.post("/settings/langfuse/test", {}),
  });
}

export function useObservabilityConfig() {
  return useQuery<ObservabilityConfig>({
    queryKey: ["observability-config"],
    queryFn: () => api.get("/settings/observability"),
  });
}

export function useUpdateObservabilityConfig() {
  const qc = useQueryClient();
  return useMutation<
    ObservabilityConfig,
    Error,
    {
      trace_storage_enabled: boolean;
      trace_retention_days: number;
      trace_store_payloads: boolean;
      audit_log_retention_days?: number;
      coordination_retention?: NonNullable<ObservabilityConfig["coordination_retention"]>;
    }
  >({
    mutationFn: (body) => api.put("/settings/observability", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["observability-config"] });
    },
  });
}

export function useModelPricingStatus() {
  // Pricing data is effectively static — it only changes through the sync /
  // import / CRUD mutations below, each of which invalidates this key. No
  // background polling needed (it just re-fetched static data every 15s).
  return useQuery<ModelPricingStatus>({
    queryKey: ["model-pricing-status"],
    queryFn: () => api.get("/settings/model-pricing"),
  });
}

export function useSyncModelPricingFromSeed() {
  const qc = useQueryClient();
  return useMutation<ModelPricingSyncResult, Error, { source_path?: string } | undefined>({
    mutationFn: (body) => api.post("/settings/model-pricing/sync", body ?? {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["model-pricing-status"] });
      qc.invalidateQueries({ queryKey: ["observability-config"] });
    },
  });
}

export function useImportModelPricingPayload() {
  const qc = useQueryClient();
  return useMutation<ModelPricingSyncResult, Error, { payload: Record<string, unknown> }>({
    mutationFn: (body) => api.post("/settings/model-pricing/import", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["model-pricing-status"] });
      qc.invalidateQueries({ queryKey: ["observability-config"] });
    },
  });
}

export function useRepriceNullCostTraces() {
  const qc = useQueryClient();
  return useMutation<
    ModelPricingRepriceResult,
    Error,
    { model?: string; enforce_effective_from?: boolean; dry_run?: boolean } | undefined
  >({
    mutationFn: (body) => api.post("/settings/model-pricing/reprice", body ?? {}),
    onSuccess: (result) => {
      if (result.dry_run) return;
      qc.invalidateQueries({ queryKey: ["model-pricing-unpriced-models"] });
      qc.invalidateQueries({ queryKey: ["traces"] });
      qc.invalidateQueries({ queryKey: ["traces", "analytics"] });
    },
  });
}

export function useModelPricingRows(search?: string) {
  return useQuery<ModelPricingRowListResponse>({
    queryKey: ["model-pricing-rows", search ?? ""],
    queryFn: () => api.get(`/settings/model-pricing/rows${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  });
}

export function useModelsWithoutPricingRows() {
  return useQuery<ModelPricingUnpricedModelListResponse>({
    queryKey: ["model-pricing-unpriced-models"],
    queryFn: () => api.get("/settings/model-pricing/unpriced-models"),
  });
}

type ModelPricingRowPayload = {
  provider: string;
  model_id: string;
  model_pattern?: string | null;
  input_price_per_1m: number;
  output_price_per_1m: number;
  cache_read_price_per_1m?: number | null;
  cache_write_price_per_1m?: number | null;
  effective_from?: string | null;
  is_custom?: boolean;
};

function invalidateModelPricingQueries(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["model-pricing-status"] });
  qc.invalidateQueries({ queryKey: ["model-pricing-rows"] });
  qc.invalidateQueries({ queryKey: ["model-pricing-unpriced-models"] });
  qc.invalidateQueries({ queryKey: ["observability-config"] });
  qc.invalidateQueries({ queryKey: ["traces", "analytics"] });
}

export function useCreateModelPricingRow() {
  const qc = useQueryClient();
  return useMutation<ModelPricingRow, Error, ModelPricingRowPayload>({
    mutationFn: (body) => api.post("/settings/model-pricing/rows", body),
    onSuccess: () => {
      invalidateModelPricingQueries(qc);
    },
  });
}

export function useUpdateModelPricingRow() {
  const qc = useQueryClient();
  return useMutation<ModelPricingRow, Error, { id: string; body: ModelPricingRowPayload }>({
    mutationFn: ({ id, body }) => api.put(`/settings/model-pricing/rows/${id}`, body),
    onSuccess: () => {
      invalidateModelPricingQueries(qc);
    },
  });
}

// --- OAuth Defaults (admin) ---
export function useOAuthDefaults() {
  return useQuery<OAuthDefault[]>({
    queryKey: ["oauth-defaults"],
    queryFn: () => api.get("/settings/oauth-defaults"),
  });
}

export function useUpsertOAuthDefault() {
  const qc = useQueryClient();
  return useMutation<OAuthDefault, Error, { provider: string; client_id: string; client_secret: string; extra_config?: Record<string, string> }>({
    mutationFn: (body) => api.post("/settings/oauth-defaults", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["oauth-defaults"] });
      qc.invalidateQueries({ queryKey: ["provider-catalog"] });
    },
  });
}

export function useDeleteOAuthDefault() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (provider) => api.delete(`/settings/oauth-defaults/${provider}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["oauth-defaults"] });
      qc.invalidateQueries({ queryKey: ["provider-catalog"] });
    },
  });
}

// --- Disabled Integration Providers (admin) ---
/**
 * Disable or re-enable ONE provider.
 *
 * Per provider rather than the whole list: the array PUT is a
 * read-modify-write over a snapshot, so two toggles issued before the first
 * response lands both compute from the same pre-image and the second drops
 * the first — silently re-enabling a provider whose integrations were just
 * deleted. Deltas commute, and the server takes the settings row lock.
 */
export function useSetProviderDisabled() {
  const qc = useQueryClient();
  return useMutation<string[], Error, { provider: string; disabled: boolean }>({
    mutationFn: ({ provider, disabled }) => {
      const path = `/settings/disabled-providers/${encodeURIComponent(provider)}`;
      return disabled ? api.put(path, {}) : api.delete(path);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["disabled-providers"] });
      qc.invalidateQueries({ queryKey: ["provider-catalog"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

/** @public */
export function useUpdateDisabledProviders() {
  const qc = useQueryClient();
  return useMutation<string[], Error, { providers: string[] }>({
    mutationFn: (body) => api.put("/settings/disabled-providers", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["disabled-providers"] });
      qc.invalidateQueries({ queryKey: ["provider-catalog"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

// --- Tags (autocomplete) ---
export function useTags(options?: { enabled?: boolean }) {
  return useQuery<string[]>({
    queryKey: ["tags"],
    queryFn: () => api.get("/settings/tags"),
    staleTime: REFETCH_INTERVAL_SLOW,
    // A field that offers no platform suggestions must not fetch them either.
    enabled: options?.enabled ?? true,
  });
}

// --- Emergency memory purge (admin) ---
export function usePurgeAllMemories() {
  return useMutation<
    { status: string; persistent_deleted: number; style_embeddings_deleted: number; redis_deleted: number; total_deleted: number },
    Error,
    void
  >({
    mutationFn: () => api.post("/settings/purge-all-memories", {}),
  });
}

// --- User memory management ---
export interface UserMemoryAgent {
  agent_id: string;
  agent_name: string;
  agent_slug: string;
  agent_icon: string | null;
  redis_memory: {
    short_term: Array<{ source?: string; agent?: string; summary: string; timestamp: string; source_type?: string; interaction_type?: string }>;
    medium_term: string;
  };
  persistent_count: number;
}

interface UserMemoryResponse {
  user_profile: {
    short_term: Array<{ agent: string; agent_id?: string | null; summary: string; timestamp: string; interaction_type?: string }>;
    /** The profile alone, rendered: never a note. */
    medium_term: string;
    entries?: UserMemoryEntry[];
    /** A free-text profile from before the split, read by no agent until it is reorganized. */
    legacy_profile?: string;
  };
  agents: UserMemoryAgent[];
}

export function useMyMemory() {
  return useQuery<UserMemoryResponse>({
    queryKey: ["my-memory"],
    queryFn: () => api.get("/settings/me/memory"),
  });
}

export function usePurgeMyMemory() {
  const qc = useQueryClient();
  return useMutation<{ status: string; redis_deleted: number; persistent_deleted: number }, Error, void>({
    mutationFn: () => api.delete("/settings/me/memory"),
    onSuccess: () => invalidateMyMemory(qc),
  });
}

export function usePurgeMyUserProfile() {
  const qc = useQueryClient();
  return useMutation<{ status: string; redis_deleted: number }, Error, void>({
    mutationFn: () => api.delete("/settings/me/memory/user-profile"),
    onSuccess: () => invalidateMyMemory(qc),
  });
}

export function usePurgeMyAgentMemory() {
  const qc = useQueryClient();
  return useMutation<{ status: string; redis_deleted: number; persistent_deleted: number }, Error, string>({
    mutationFn: (agentId: string) => api.delete(`/settings/me/memory/agent/${agentId}`),
    onSuccess: () => invalidateMyMemory(qc),
  });
}

// --- MCP Endpoint key management (admin) ---
export function useGenerateMCPKey() {
  const qc = useQueryClient();
  return useMutation<{ key: string; prefix: string }, Error, void>({
    mutationFn: () => api.post("/settings/mcp-endpoint/generate-key", {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["general-assistant"] });
    },
  });
}

export function useRevokeMCPKey() {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () => api.delete("/settings/mcp-endpoint/revoke-key"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["general-assistant"] });
    },
  });
}

// --- Quotas ---

export function usePlatformQuotas() {
  return useQuery<{ quota_config: QuotaConfig | null; notices?: QuotaLegacyNotices }>({
    queryKey: ["platform-quotas"],
    queryFn: () => api.get("/settings/quotas"),
  });
}

export function useUpdatePlatformQuotas() {
  const qc = useQueryClient();
  return useMutation<{ quota_config: QuotaConfig | null }, Error, QuotaConfig>({
    mutationFn: (data) => api.put("/settings/quotas", data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["platform-quotas"] });
      qc.invalidateQueries({ queryKey: ["my-quotas"] });
    },
  });
}

export function useMyQuotas(options: { enabled?: boolean } = {}) {
  return useQuery<UserQuotaInfo>({
    queryKey: ["my-quotas"],
    queryFn: () => api.get("/settings/my-quotas"),
    enabled: options.enabled ?? true,
  });
}

export function useQuotaMonitoring() {
  return useQuery<QuotaMonitoring>({
    queryKey: ["quota-monitoring"],
    queryFn: () => api.get("/settings/quotas/monitoring"),
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

// --- Agent failure pool (env-sized, support-code recharge) ---
export function useAgentFailurePool(options: { enabled?: boolean } = {}) {
  return useQuery<AgentFailurePoolStatus>({
    queryKey: ["agent-failure-pool"],
    queryFn: () => api.get("/settings/quotas/agent-failure-pool"),
    enabled: options.enabled !== false,
  });
}

// One route for every kind of support code: a failure pool recharge, or a reset
// of the platform-wide usage counters. The answer says which it was.
export function useRedeemSupportCode() {
  const qc = useQueryClient();
  return useMutation<
    SupportCodeRedeemResult,
    Error,
    string
  >({
    // A refused code (malformed, unsigned, expired, already used) comes back as
    // a 400 whose detail is written for the admin. It is rendered inline in the
    // dialog, so suppress the global mutation toast rather than saying the same
    // thing twice in two places — the same contract the license activation
    // dialog relies on.
    meta: { suppressGlobalErrorToast: true },
    mutationFn: (code) =>
      api.post("/settings/quotas/support-codes/redeem", { code }),
    onSuccess: () => {
      // The redemption ledger lives on the pool card, whatever the kind.
      qc.invalidateQueries({ queryKey: ["agent-failure-pool"] });
      // Either kind changes what is billable next: refresh the usage views.
      qc.invalidateQueries({ queryKey: ["quota-monitoring"] });
      qc.invalidateQueries({ queryKey: ["my-quotas"] });
    },
  });
}

// --- Built-in Tool Groups ---
/** The registry every built-in picker reads, including the chat composer: a
 *  failed read must never hand the page to its error boundary, so it opts out
 *  of the global `throwOnError` and each picker shows its own error line. */
export function useBuiltinToolGroups() {
  return useQuery<BuiltinToolGroup[]>({
    queryKey: ["builtin-tool-groups"],
    queryFn: () => api.get("/settings/builtin-tools/groups"),
    staleTime: 5 * 60 * 1000,
    retry: 1,
    throwOnError: false,
  });
}

// --- Built-in Tool Catalog ---
export function useBuiltinToolCatalog() {
  return useQuery<Record<string, BuiltinSubTool[]>>({
    queryKey: ["builtin-tool-catalog"],
    queryFn: () => api.get("/settings/builtin-tools/catalog"),
    staleTime: 5 * 60 * 1000,
  });
}
