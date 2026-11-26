// SPDX-License-Identifier: Apache-2.0

/** Integration API hooks. */

import { useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api, unavailableIsOrdinary } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import type {
  EntityListOptions,
  Integration,
  ProviderCatalogItem,
  ProviderTool,
  CreateIntegrationRequest,
  UpdateIntegrationRequest,
  IntegrationBundleImportResponse,
  IntegrationToolInfo,
  IntegrationElement,
  ManagedWebhook,
  PerUserReadinessResponse,
  OpenAPIImportOptions,
  OpenAPIPreview,
  OpenAPIRefreshResult,
  OpenAPISourceRequest,
  OpenAPISpec,
  OpenAPIToolOverrides,
  ClientCertificateInfo,
  DeviceAuthorization,
  DevicePollResult,
  IntegrationUserCredential,
  OpenCrqCompatibility,
  WhatsAppPairing,
} from "@/types";
// --- Integration Provider Catalog ---
/**
 * Provider catalog.
 *
 * `includeDisabled` asks the API for providers an admin has switched off too
 * (they come back with `is_disabled: true`). Only the browsing catalog wants them —
 * everywhere else a disabled provider is not selectable, so it stays off.
 */
export function useProviderCatalog(category?: "integration" | "channel", includeDisabled = false) {
  const query = [
    category ? `category=${category}` : "",
    includeDisabled ? "include_disabled=true" : "",
  ].filter(Boolean).join("&");
  const params = query ? `?${query}` : "";
  return useQuery<ProviderCatalogItem[]>({
    queryKey: ["provider-catalog", category ?? "all", includeDisabled],
    queryFn: () => api.get(`/integrations/catalog${params}`),
    staleTime: 60_000,
  });
}

// --- Integrations ---
/** The integrations list; `{ scope: "all" }` is every integration for an admin (detail pages). */
export function useIntegrations(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<Integration[]>({
    queryKey: entityListKey(["integrations"], scope),
    queryFn: () => api.get(entityListPath("/integrations", scope)),
  });
}

/**
 * Per-provider readiness for "resolved per user at runtime" bindings.
 *
 * Tells the agent config UI whether a provider actually has a company-wide
 * fallback, so it can distinguish "users without their own account still get
 * tools" from "every user must connect their own account first" instead of
 * implying the former.
 */
export function usePerUserReadiness() {
  return useQuery<PerUserReadinessResponse>({
    queryKey: ["integrations", "per-user-readiness"],
    queryFn: () => api.get("/integrations/per-user-readiness"),
    staleTime: 60_000,
  });
}

export function useCreateIntegration() {
  const qc = useQueryClient();
  return useMutation<Integration, Error, CreateIntegrationRequest>({
    mutationFn: (data) => api.post("/integrations", data),
    onSuccess: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["integrations"] });
    },
  });
}

/**
 * An integration write can also change the CALLER's user record: turning an
 * OAuth integration's sharing off (or deleting it) drops it from the owner's
 * closed OAuth-share reminders server-side. `["me"]` is cached for
 * `REFETCH_INTERVAL_SLOW`, so without this the dashboard would keep reading
 * the stale dismissal and hide a reminder the server has already un-muted.
 */
function invalidateIntegrationsAndUser(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ["integrations"] });
  qc.invalidateQueries({ queryKey: ["me"] });
}

export function useUpdateIntegration() {
  const qc = useQueryClient();
  return useMutation<Integration, Error, { id: string } & UpdateIntegrationRequest>({
    mutationFn: ({ id, ...data }) => api.put(`/integrations/${id}`, data),
    onSuccess: () => {
      invalidateIntegrationsAndUser(qc);
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useDeleteIntegration() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/integrations/${id}`),
    onSuccess: (_d, id) => {
      invalidateIntegrationsAndUser(qc);
      dropFromFlowNodeCatalog(qc, "integration", id);
    },
  });
}

// --- Each person's own account on a per_user integration ---

/** Disconnect the caller's own account from a `per_user` integration. */
export function useDeleteMyCredential() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (integrationId) => api.delete(`/integrations/${integrationId}/my-credential`),
    onSuccess: (_data, integrationId) => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      qc.invalidateQueries({ queryKey: ["integration-credentials", integrationId] });
    },
  });
}

/** Who connected their own account to a `per_user` integration (its managers). */
export function useIntegrationCredentials(integrationId: string, enabled = true) {
  return useQuery<IntegrationUserCredential[]>({
    queryKey: ["integration-credentials", integrationId],
    queryFn: () => api.get(`/integrations/${integrationId}/credentials`),
    enabled: enabled && !!integrationId,
  });
}

/** Disconnect someone's own account from a `per_user` integration (its managers). */
export function useDeleteUserCredential() {
  const qc = useQueryClient();
  return useMutation<void, Error, { integrationId: string; userId: string }>({
    mutationFn: ({ integrationId, userId }) =>
      api.delete(`/integrations/${integrationId}/credentials/${userId}`),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["integrations"] });
      qc.invalidateQueries({ queryKey: ["integration-credentials", vars.integrationId] });
    },
  });
}

export function useTestIntegration() {
  const qc = useQueryClient();
  return useMutation<{ status: string; message: string }, Error, string>({
    mutationFn: (id: string) => api.post(`/integrations/${id}/test`, {}),
    // The test's result is stored (#11837): the status shown follows it.
    onSettled: () => {
      syncFlowNodeCatalog(qc);
      return qc.invalidateQueries({ queryKey: ["integrations"] });
    },
  });
}

/** Start an integration's OAuth flow: a URL to open, or a code to enter (an
 *  OpenAPI integration's device authorization). Refused with a 403 naming the
 *  rule for anyone whose token the callback would not store. */
export function useAuthorizeIntegration() {
  return useMutation<{ authorize_url?: string; device_authorization?: DeviceAuthorization }, Error, string>({
    mutationFn: (id: string) => api.get(`/integrations/${id}/authorize`),
    // The caller raises its own "OAuth failed" toast with the server's reason;
    // the global MutationCache handler would stack a second, vaguer one on top.
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useIntegrationTools(integrationId: string, enabled = true) {
  return useQuery<{ tools: IntegrationToolInfo[] }>({
    queryKey: ["integration-tools", integrationId],
    queryFn: () => api.get(`/integrations/${integrationId}/tools`),
    // `enabled` lets a caller skip the request for a provider that has no
    // agent-callable tools by definition (e.g. the A2A federation config),
    // where the answer is always an empty list.
    enabled: enabled && !!integrationId,
  });
}

/**
 * What the connected OpenCRQ lets the agents do: its version, the state
 * of the manifest it registered, the tools it cannot serve and the fields it
 * refuses. The server never calls OpenCRQ for it; the answer is kept a minute.
 */
export function useOpenCrqCompatibility(integrationId: string) {
  return useQuery<OpenCrqCompatibility>({
    queryKey: ["integration-opencrq-compatibility", integrationId],
    queryFn: () => api.get(`/integrations/${integrationId}/opencrq/compatibility`),
    enabled: !!integrationId,
    staleTime: 60_000,
  });
}

/**
 * The Pairing card of a WhatsApp integration on the linked-device gateway: the
 * live pairing state, the login QR while it is `qr` and the gateway webhook path, the last
 * two for a user who may manage the integration. Refreshed every 5 seconds while a QR is
 * shown or the channel is starting (the gateway rotates its QR), every minute otherwise.
 */
export function useWhatsAppPairing(integrationId: string) {
  return useQuery<WhatsAppPairing>({
    queryKey: ["integration-whatsapp-pairing", integrationId],
    queryFn: () => api.get(`/integrations/${integrationId}/whatsapp/pairing`),
    enabled: !!integrationId,
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state === "qr" || state === "starting" ? 5_000 : 60_000;
    },
  });
}

/**
 * The tools an integration offers, as the tool cards list them.
 *
 * Catalog providers declare them in `available_tools`; a provider whose tools
 * come from the integration's own imported spec (`spec_import`, OpenAPI)
 * reads them from the integration — the same runtime view an agent gets.
 */
export function useIntegrationToolList(
  integration: Pick<Integration, "id">,
  catalogEntry?: ProviderCatalogItem,
): ProviderTool[] {
  const fromIntegration = Boolean(catalogEntry?.spec_import);
  const { data } = useIntegrationTools(integration.id, fromIntegration);
  return useMemo(() => {
    if (!fromIntegration) return catalogEntry?.available_tools ?? [];
    return (data?.tools ?? []).map((t) => ({
      name: t.name,
      description: t.description,
      tool_type: t.tool_type ?? "read",
    }));
  }, [fromIntegration, catalogEntry?.available_tools, data?.tools]);
}

export function useTestIntegrationTool() {
  return useMutation<
    { status: string; output?: string; message?: string; duration_ms?: number },
    Error,
    { integrationId: string; tool_name: string; arguments?: Record<string, unknown> }
  >({
    mutationFn: ({ integrationId, ...body }) =>
      api.post(`/integrations/${integrationId}/test-tool`, body),
  });
}

export function useImportIntegrationBundle() {
  const qc = useQueryClient();
  return useMutation<IntegrationBundleImportResponse, Error, File>({
    mutationFn: async (file) => {
      const formData = new FormData();
      formData.append("bundle", file);
      return api.postForm("/integrations/admin/import", formData);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["provider-catalog"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useExportIntegrationBundle() {
  return useMutation<
    { blob: Blob; filename: string | null },
    Error,
    { provider: string; integrationId?: string }
  >({
    mutationFn: ({ provider, integrationId }) => {
      const query = integrationId ? `?integration_id=${encodeURIComponent(integrationId)}` : "";
      return api.download(`/integrations/admin/export/${encodeURIComponent(provider)}${query}`);
    },
  });
}

export function useUninstallCustomIntegrationProvider() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (provider: string) =>
      api.delete(`/integrations/admin/custom/${encodeURIComponent(provider)}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["provider-catalog"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useSetCustomIntegrationProviderEnabled() {
  const qc = useQueryClient();
  return useMutation<{ provider: string; enabled: boolean }, Error, { provider: string; enabled: boolean }>({
    mutationFn: ({ provider, enabled }) =>
      api.put(`/integrations/admin/custom/${encodeURIComponent(provider)}/enabled/${enabled}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["provider-catalog"] });
      qc.invalidateQueries({ queryKey: ["integrations"] });
      syncFlowNodeCatalog(qc);
    },
  });
}

export function useIntegrationElements(
  integrationId: string,
  elementType: string,
) {
  return useQuery<{
    elements: IntegrationElement[];
    element_type: string;
  }>({
    queryKey: ["integration-elements", integrationId, elementType],
    queryFn: () =>
      api.get(
        `/integrations/${integrationId}/elements?element_type=${elementType}`,
      ),
    enabled: !!integrationId && !!elementType,
    // A 403 is an answer, not a fault: the integration holds its owner's
    // unshared personal OAuth credentials. Pickers show the reason
    // instead of the page falling into the ErrorBoundary. The retry policy
    // stays the app's: a network blip is still retried before any error.
    throwOnError: unavailableIsOrdinary.throwOnError,
  });
}

// --- Notion webhook setup ---
/** The Notion webhook setup the integration's managers read: the URL to subscribe and the stored verification token. */
export interface NotionWebhookSetup {
  webhook_url: string;
  verification_token: string | null;
  events: string[];
  next_step: string;
}

export function useNotionWebhookSetup(integrationId: string, enabled = true) {
  return useQuery<NotionWebhookSetup>({
    queryKey: ["notion-webhook", integrationId],
    queryFn: () => api.get(`/integrations/${integrationId}/notion-webhook`),
    enabled: !!integrationId && enabled,
  });
}

// --- GitHub Webhook Management ---
export function useGitHubWebhooks(integrationId: string, enabled = true) {
  return useQuery<{ webhooks: ManagedWebhook[] }>({
    queryKey: ["github-webhooks", integrationId],
    queryFn: () => api.get(`/integrations/${integrationId}/github-webhooks`),
    enabled: !!integrationId && enabled,
  });
}

export function useCreateGitHubWebhook() {
  const qc = useQueryClient();
  return useMutation<
    ManagedWebhook,
    Error,
    {
      integrationId: string;
      repo: string;
      events: string[];
      target?: "integration" | "assignment";
      assignment_id?: string;
    }
  >({
    mutationFn: ({ integrationId, ...body }) =>
      api.post(`/integrations/${integrationId}/github-webhooks`, body),
    onSuccess: (_data, vars) =>
      qc.invalidateQueries({ queryKey: ["github-webhooks", vars.integrationId] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

export function useDeleteGitHubWebhook() {
  const qc = useQueryClient();
  return useMutation<void, Error, { integrationId: string; hookId: number }>({
    mutationFn: ({ integrationId, hookId }) =>
      api.delete(`/integrations/${integrationId}/github-webhooks/${hookId}`),
    onSuccess: (_data, vars) =>
      qc.invalidateQueries({ queryKey: ["github-webhooks", vars.integrationId] }),
    meta: { suppressGlobalErrorToast: true },
  });
}

// --- OpenAPI / Swagger integrations ---
/** Parse a spec (URL or file text) and list its operations; nothing is stored. */
export function usePreviewOpenAPISpec() {
  return useMutation<OpenAPIPreview, Error, OpenAPISourceRequest>({
    mutationFn: (body) => api.post("/integrations/openapi/preview", body),
  });
}

export function useIntegrationOpenAPISpec(integrationId: string, enabled = true) {
  return useQuery<OpenAPISpec>({
    queryKey: ["integration-openapi", integrationId],
    queryFn: () => api.get(`/integrations/${integrationId}/openapi`),
    enabled: enabled && !!integrationId,
  });
}

/** Everything that shows an OpenAPI integration's spec or tools. */
function invalidateOpenAPI(qc: ReturnType<typeof useQueryClient>, integrationId: string) {
  qc.invalidateQueries({ queryKey: ["integration-openapi", integrationId] });
  qc.invalidateQueries({ queryKey: ["integration-tools", integrationId] });
  qc.invalidateQueries({ queryKey: ["integrations"] });
}

export function useUpdateOpenAPIOperations() {
  const qc = useQueryClient();
  return useMutation<
    OpenAPISpec,
    Error,
    { integrationId: string; selected_operations: string[]; tool_overrides?: OpenAPIToolOverrides }
  >({
    mutationFn: ({ integrationId, selected_operations, tool_overrides }) =>
      api.put(`/integrations/${integrationId}/openapi/operations`, { selected_operations, tool_overrides }),
    onSuccess: (_data, vars) => invalidateOpenAPI(qc, vars.integrationId),
  });
}

export function useRefreshOpenAPISpec() {
  const qc = useQueryClient();
  return useMutation<OpenAPIRefreshResult, Error, string>({
    mutationFn: (integrationId) => api.post(`/integrations/${integrationId}/openapi/refresh`, {}),
    onSuccess: (_data, integrationId) => invalidateOpenAPI(qc, integrationId),
  });
}

export function useReplaceOpenAPISpec() {
  const qc = useQueryClient();
  return useMutation<OpenAPISpec, Error, { integrationId: string } & OpenAPIImportOptions>({
    mutationFn: ({ integrationId, ...body }) => api.put(`/integrations/${integrationId}/openapi`, body),
    onSuccess: (_data, vars) => invalidateOpenAPI(qc, vars.integrationId),
  });
}

/** Set or replace the client certificate an OpenAPI integration presents (mutual TLS). */
export function useSetClientCertificate() {
  const qc = useQueryClient();
  return useMutation<
    ClientCertificateInfo,
    Error,
    { integrationId: string; certificate: string; private_key: string; passphrase?: string }
  >({
    mutationFn: ({ integrationId, ...body }) => api.put(`/integrations/${integrationId}/openapi/client-certificate`, body),
    onSuccess: (_data, vars) => invalidateOpenAPI(qc, vars.integrationId),
  });
}

export function useDeleteClientCertificate() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (integrationId) => api.delete(`/integrations/${integrationId}/openapi/client-certificate`),
    onSuccess: (_data, integrationId) => invalidateOpenAPI(qc, integrationId),
  });
}

/** Whether a device-code sign-in completed, asked at the interval the API set
 *  until it is no longer pending (a finished sign-in refreshes the integrations). */
export function useDeviceAuthorizationPoll(integrationId: string, flow: DeviceAuthorization | null) {
  const qc = useQueryClient();
  return useQuery<DevicePollResult, Error>({
    queryKey: ["openapi-device-poll", integrationId, flow?.flow_id],
    queryFn: async () => {
      const result = await api.post<DevicePollResult>(`/integrations/${integrationId}/openapi/device/poll`, {
        flow_id: flow?.flow_id,
      });
      if (result.status === "connected") {
        void qc.invalidateQueries({ queryKey: ["integrations"] });
        syncFlowNodeCatalog(qc);
      }
      return result;
    },
    enabled: !!flow,
    refetchInterval: (query) =>
      !query.state.error && (!query.state.data || query.state.data.status === "pending")
        ? Math.max(query.state.data?.interval ?? flow?.interval ?? 5, 1) * 1000
        : false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
    gcTime: 0,
  });
}

export function cancelDeviceAuthorization(integrationId: string, flowId: string) {
  return api.delete<void>(`/integrations/${integrationId}/openapi/device/${encodeURIComponent(flowId)}`);
}

/** Hand the token of an OAuth2 implicit redirect (from the URL fragment) to the backend. */
export function completeImplicitAuthorization(fragment: Record<string, string>) {
  return api.post<{ integration_id: string; provider: string }>("/integrations/openapi/oauth/implicit", fragment);
}
