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

/** Agent account API hooks: accounts, their approval queue, the rule simulator and pickers. */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import type {
  AgentAccount,
  AgentAccountActivity,
  AgentAccountActivityPeriod,
  AgentAccountCatalog,
  AgentAccountDecisionFilters,
  AgentAccountDecisionPage,
  AgentAccountPickerConversation,
  AgentAccountPickerIdentity,
  AgentAccountPickerPerson,
  AgentAccountProviderInfo,
  AgentAccountReply,
  AgentAccountReplyFilters,
  AgentAccountReplyPage,
  AgentAccountSimulateRequest,
  AgentAccountSimulateResponse,
  CreateAgentAccountRequest,
  EntityListOptions,
  UpdateAgentAccountRequest,
} from "@/types";

const ACCOUNTS = ["agent-accounts"];
const REPLIES = ["agent-account-replies"];
const DECISIONS = ["agent-account-decisions"];
const ACTIVITY = ["agent-account-activity"];

/** Read every agent-account view again: an agent's tool changed an account from a chat. */
export function invalidateAgentAccounts(qc: QueryClient) {
  for (const queryKey of [ACCOUNTS, REPLIES, DECISIONS, ACTIVITY]) {
    void qc.invalidateQueries({ queryKey });
  }
}

export function useAgentAccountCatalog() {
  return useQuery<AgentAccountCatalog>({
    queryKey: ["agent-account-catalog"],
    queryFn: () => api.get("/agent-accounts/catalog"),
    staleTime: 60_000,
  });
}

/** One provider of the catalog - its kind and capabilities - once the catalog is read. */
export function useAgentAccountProvider(key: string | null | undefined): AgentAccountProviderInfo | undefined {
  const { data } = useAgentAccountCatalog();
  return data?.providers.find((provider) => provider.key === key);
}

/** How often the list is read again while an account is being synced in the background. */
const SETTING_UP_REFRESH_MS = 3_000;

/** The caller's accounts; `{ scope: "all" }` is every account for an admin. */
export function useAgentAccounts(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<AgentAccount[]>({
    queryKey: entityListKey(ACCOUNTS, scope),
    queryFn: () => api.get(entityListPath("/agent-accounts", scope)),
    refetchInterval: (query) =>
      query.state.data?.some((account) => account.enabled && account.status === "setup") ? SETTING_UP_REFRESH_MS : false,
  });
}

export function useCreateAgentAccount() {
  const qc = useQueryClient();
  return useMutation<AgentAccount, Error, CreateAgentAccountRequest>({
    mutationFn: (data) => api.post("/agent-accounts", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ACCOUNTS }),
  });
}

export function useUpdateAgentAccount() {
  const qc = useQueryClient();
  return useMutation<AgentAccount, Error, { id: string } & UpdateAgentAccountRequest>({
    mutationFn: ({ id, ...data }) => api.put(`/agent-accounts/${id}`, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ACCOUNTS }),
  });
}

export function useDeleteAgentAccount() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/agent-accounts/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ACCOUNTS });
      qc.invalidateQueries({ queryKey: REPLIES });
    },
  });
}

export function useSyncAgentAccount() {
  const qc = useQueryClient();
  return useMutation<AgentAccount, Error, string>({
    mutationFn: (id) => api.post(`/agent-accounts/${id}/sync`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ACCOUNTS }),
  });
}

/** One account, kept fresh while it is being synced in the background. */
export function useAgentAccount(accountId: string | undefined) {
  return useQuery<AgentAccount>({
    queryKey: [...ACCOUNTS, "detail", accountId],
    queryFn: () => api.get(`/agent-accounts/${accountId}`),
    enabled: !!accountId,
    refetchInterval: (query) =>
      query.state.data?.enabled && query.state.data.status === "setup" ? SETTING_UP_REFRESH_MS : false,
  });
}

/** How often an account's activity is read again while its page is open. */
const ACTIVITY_REFRESH_MS = 20_000;

export function useAgentAccountDecisionLog(
  accountId: string | undefined,
  filters: AgentAccountDecisionFilters,
  page: number,
  pageSize: number,
) {
  const params = new URLSearchParams({ limit: String(pageSize), offset: String(page * pageSize) });
  for (const group of filters.groups ?? []) params.append("group", group);
  for (const type of filters.conversationTypes ?? []) params.append("conversation_type", type);
  if (filters.sort && (filters.sort !== "date" || filters.direction === "asc")) {
    params.set("sort", filters.sort);
    params.set("direction", filters.direction ?? "desc");
  }
  if (filters.conversationId) params.set("conversation_id", filters.conversationId);
  if (filters.sender) params.set("sender", filters.sender);
  if (filters.q) params.set("q", filters.q);
  return useQuery<AgentAccountDecisionPage>({
    queryKey: [...DECISIONS, accountId, params.toString()],
    queryFn: () => api.get(`/agent-accounts/${accountId}/decisions?${params.toString()}`),
    enabled: !!accountId,
    placeholderData: keepPreviousData,
    refetchInterval: ACTIVITY_REFRESH_MS,
    // The Decisions table draws a failed read as its own error row with Retry:
    // the app-wide throwOnError would hand it to the section boundary and hide the tabs.
    throwOnError: false,
  });
}

export function useAgentAccountReplyPage(
  accountId: string | undefined,
  filters: AgentAccountReplyFilters,
  page: number,
  pageSize: number,
) {
  const params = new URLSearchParams({ limit: String(pageSize), offset: String(page * pageSize) });
  for (const status of filters.status ?? []) params.append("status", status);
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.sort && (filters.sort !== "date" || filters.direction === "asc")) {
    params.set("sort", filters.sort);
    params.set("direction", filters.direction ?? "desc");
  }
  if (filters.q) params.set("q", filters.q);
  return useQuery<AgentAccountReplyPage>({
    queryKey: [...REPLIES, "page", accountId, params.toString()],
    queryFn: () => api.get(`/agent-accounts/${accountId}/replies?${params.toString()}`),
    enabled: !!accountId,
    placeholderData: keepPreviousData,
    refetchInterval: ACTIVITY_REFRESH_MS,
    // The Messages sent table draws a failed read as its own error row with Retry:
    // the app-wide throwOnError would hand it to the section boundary and hide the tabs.
    throwOnError: false,
  });
}

/** What the account did over *period*, its timeline in the browser's time zone. */
export function useAgentAccountActivity(accountId: string | undefined, period: AgentAccountActivityPeriod) {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return useQuery<AgentAccountActivity>({
    queryKey: [...ACTIVITY, accountId, period, tz],
    queryFn: () =>
      api.get(`/agent-accounts/${accountId}/activity?period=${period}&tz=${encodeURIComponent(tz)}`),
    enabled: !!accountId,
    placeholderData: keepPreviousData,
    refetchInterval: ACTIVITY_REFRESH_MS,
  });
}

export function useAgentAccountReplies(filters: { status?: string; accountId?: string } = {}) {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.accountId) params.set("account_id", filters.accountId);
  const query = params.toString();
  return useQuery<AgentAccountReply[]>({
    queryKey: [...REPLIES, filters.status ?? "", filters.accountId ?? ""],
    queryFn: () => api.get(`/agent-accounts/replies${query ? `?${query}` : ""}`),
    refetchInterval: 20_000,
  });
}

function useReplyDecision<TVariables>(request: (variables: TVariables) => Promise<AgentAccountReply>) {
  const qc = useQueryClient();
  return useMutation<AgentAccountReply, Error, TVariables>({
    mutationFn: request,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: REPLIES });
      qc.invalidateQueries({ queryKey: ACCOUNTS });
      qc.invalidateQueries({ queryKey: DECISIONS });
      qc.invalidateQueries({ queryKey: ACTIVITY });
    },
  });
}

export function useApproveAgentAccountReply() {
  return useReplyDecision<{ id: string; text?: string }>(({ id, text }) =>
    api.post(`/agent-accounts/replies/${id}/approve`, text === undefined ? {} : { text }),
  );
}

export function useRejectAgentAccountReply() {
  return useReplyDecision<string>((id) => api.post(`/agent-accounts/replies/${id}/reject`, {}));
}

export function useSimulateAgentAccount() {
  return useMutation<AgentAccountSimulateResponse, Error, AgentAccountSimulateRequest>({
    mutationFn: (data) => api.post("/agent-accounts/simulate", data),
  });
}

/** Who an integration is signed in as, and whether an agent account already operates that account. */
export function useAgentAccountIdentity(provider: string, integrationId: string | null, accountId: string | null) {
  return useQuery<AgentAccountPickerIdentity>({
    queryKey: ["agent-account-identity", provider, integrationId, accountId],
    queryFn: () =>
      api.get(
        `/agent-accounts/pickers/${provider}/identity?integration_id=${encodeURIComponent(integrationId ?? "")}` +
          (accountId ? `&account_id=${encodeURIComponent(accountId)}` : ""),
      ),
    enabled: !!integrationId,
    staleTime: 60_000,
    retry: false,
  });
}

/** The chats and channels of the account an integration is signed in as. */
export function useAgentAccountConversations(provider: string, integrationId: string | null, query: string) {
  return useQuery<AgentAccountPickerConversation[]>({
    queryKey: ["agent-account-conversations", provider, integrationId, query],
    queryFn: () =>
      api.get(
        `/agent-accounts/pickers/${provider}/conversations?integration_id=${encodeURIComponent(integrationId ?? "")}` +
          (query ? `&q=${encodeURIComponent(query)}` : ""),
      ),
    enabled: !!integrationId,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
}

/** People of the account's directory, searched by name or email. */
export function useAgentAccountPeople(provider: string, integrationId: string | null, query: string) {
  return useQuery<AgentAccountPickerPerson[]>({
    queryKey: ["agent-account-people", provider, integrationId, query],
    queryFn: () =>
      api.get(
        `/agent-accounts/pickers/${provider}/people?integration_id=${encodeURIComponent(integrationId ?? "")}` +
          `&q=${encodeURIComponent(query)}`,
      ),
    enabled: !!integrationId && query.trim().length >= 2,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
}
