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

/** Relay hooks - on-premises egress relays and the groups they form.
 *
 * A relay group names a customer network; integrations, remote MCP servers
 * and flow HTTP nodes routed through it are reached from that network by its
 * relays, which connect out to the platform (no inbound flow). Groups are a
 * three-tier entity; relays and enrollment keys belong to their group. The
 * relay daemon's own transport endpoints have no hooks here.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api, notFoundIsOrdinary } from "@/lib/api";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import { dropFromFlowNodeCatalog, syncFlowNodeCatalog } from "@/lib/flow-node-catalog";
import { relaysEnabledFrom } from "@/lib/relays";
import type {
  EntityListOptions,
  Relay,
  RelayEnrollmentKey,
  RelayEnrollmentKeyCreate,
  RelayEnrollmentKeyCreated,
  RelayGroup,
  RelayGroupCreate,
  RelayGroupStats,
  RelayGroupUpdate,
  RelayMeta,
  RelayRequestFilters,
  RelayRequestPage,
  RelayRoutedResources,
  RelayTestResult,
} from "@/types";

/**
 * Whether relays are on for this deployment (`RELAYS_ENABLED`). `undefined`
 * until `/auth/config` answers (`relaysEnabledFrom`): an entry point renders
 * nothing until then, and the Relays page waits instead of saying relays are
 * off.
 */
export function useRelaysEnabled(): boolean | undefined {
  const { data, isPending } = useAuthConfig();
  return relaysEnabledFrom(data, isPending);
}

export function useRelayMeta(options?: { enabled?: boolean }) {
  return useQuery<RelayMeta>({
    queryKey: ["relay-meta"],
    queryFn: () => api.get("/relays/meta"),
    staleTime: 60_000,
    enabled: options?.enabled ?? true,
  });
}

// --- Groups ---

/** The relay groups; `{ scope: "all" }` is every group for an admin (detail pages). */
export function useRelayGroups(options?: EntityListOptions & { enabled?: boolean }) {
  const scope = useEntityListScope(options);
  return useQuery<RelayGroup[]>({
    queryKey: entityListKey(["relay-groups"], scope),
    queryFn: () => api.get(entityListPath("/relays/groups", scope)),
    // Online counts follow the relays' heartbeats.
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    enabled: options?.enabled ?? true,
  });
}

export function useRelayGroup(id: string | undefined) {
  return useQuery<RelayGroup>({
    queryKey: ["relay-group", id],
    queryFn: () => api.get(`/relays/groups/${id}`),
    enabled: !!id,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    ...notFoundIsOrdinary,
  });
}

function invalidateGroups(qc: ReturnType<typeof useQueryClient>, id?: string) {
  void qc.invalidateQueries({ queryKey: ["relay-groups"] });
  if (id) void qc.invalidateQueries({ queryKey: ["relay-group", id] });
  syncFlowNodeCatalog(qc);
}

export function useCreateRelayGroup() {
  const qc = useQueryClient();
  return useMutation<RelayGroup, Error, RelayGroupCreate>({
    mutationFn: (body) => api.post("/relays/groups", body),
    onSuccess: (group) => invalidateGroups(qc, group.id),
  });
}

export function useUpdateRelayGroup() {
  const qc = useQueryClient();
  return useMutation<RelayGroup, Error, { id: string; body: RelayGroupUpdate }>({
    mutationFn: ({ id, body }) => api.patch(`/relays/groups/${id}`, body),
    onSuccess: (group) => invalidateGroups(qc, group.id),
  });
}

export function useDeleteRelayGroup() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/relays/groups/${id}`),
    onSuccess: (_d, id) => {
      dropFromFlowNodeCatalog(qc, "relay_group", id);
      qc.removeQueries({ queryKey: ["relay-group", id] });
      return qc.invalidateQueries({ queryKey: ["relay-groups"] });
    },
  });
}

export function useRelayGroupRouted(id: string | undefined) {
  return useQuery<RelayRoutedResources>({
    queryKey: ["relay-group-routed", id],
    queryFn: () => api.get(`/relays/groups/${id}/routed`),
    enabled: !!id,
  });
}

export function useRelayGroupStats(id: string | undefined, hours = 24) {
  return useQuery<RelayGroupStats>({
    queryKey: ["relay-group-stats", id, hours],
    queryFn: () => api.get(`/relays/groups/${id}/stats?hours=${hours}`),
    enabled: !!id,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

export function useRelayGroupRequests(
  id: string | undefined,
  { limit = 50, offset = 0, ...filters }: RelayRequestFilters & { limit?: number; offset?: number } = {},
  { throwOnError }: { throwOnError?: boolean } = {},
) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  for (const [key, value] of Object.entries(filters)) {
    if (Array.isArray(value)) for (const item of value) params.append(key, String(item));
    else if (value) params.set(key, String(value));
  }
  return useQuery<RelayRequestPage>({
    queryKey: ["relay-group-requests", id, params.toString()],
    queryFn: () => api.get(`/relays/groups/${id}/requests?${params.toString()}`),
    enabled: !!id,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    // The app throws failed queries to the page boundary by default; a table
    // that draws its own error line with Retry passes false.
    ...(throwOnError === undefined ? {} : { throwOnError }),
  });
}

export function useTestRelayDestination(groupId: string) {
  const qc = useQueryClient();
  return useMutation<RelayTestResult, Error, { url: string; relay_id?: string }>({
    mutationFn: (body) => api.post(`/relays/groups/${groupId}/test`, body),
    onSettled: () => qc.invalidateQueries({ queryKey: ["relay-group-requests", groupId] }),
  });
}

// --- Relays of a group ---

export function useGroupRelays(groupId: string | undefined, includeRevoked = false) {
  return useQuery<Relay[]>({
    queryKey: ["relay-group-relays", groupId, includeRevoked],
    queryFn: () =>
      api.get(`/relays/groups/${groupId}/relays${includeRevoked ? "?include_revoked=true" : ""}`),
    enabled: !!groupId,
    refetchInterval: REFETCH_INTERVAL_STANDARD,
  });
}

function invalidateRelays(qc: ReturnType<typeof useQueryClient>, groupId: string) {
  void qc.invalidateQueries({ queryKey: ["relay-group-relays", groupId] });
  invalidateGroups(qc, groupId);
}

export function useUpdateRelay() {
  const qc = useQueryClient();
  return useMutation<Relay, Error, { id: string; name: string }>({
    mutationFn: ({ id, name }) => api.patch(`/relays/${id}`, { name }),
    onSuccess: (relay) => invalidateRelays(qc, relay.group_id),
  });
}

export function useRevokeRelay() {
  const qc = useQueryClient();
  return useMutation<Relay, Error, string>({
    mutationFn: (id) => api.post(`/relays/${id}/revoke`),
    onSuccess: (relay) => invalidateRelays(qc, relay.group_id),
  });
}

export function useDeleteRelay(groupId: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/relays/${id}`),
    onSuccess: () => invalidateRelays(qc, groupId),
  });
}

// --- Enrollment keys ---

export function useRelayEnrollmentKeys(groupId: string | undefined, options?: { enabled?: boolean }) {
  return useQuery<RelayEnrollmentKey[]>({
    queryKey: ["relay-enrollment-keys", groupId],
    queryFn: () => api.get(`/relays/groups/${groupId}/enrollment-keys`),
    enabled: !!groupId && (options?.enabled ?? true),
  });
}

export function useCreateRelayEnrollmentKey(groupId: string) {
  const qc = useQueryClient();
  return useMutation<RelayEnrollmentKeyCreated, Error, RelayEnrollmentKeyCreate>({
    mutationFn: (body) => api.post(`/relays/groups/${groupId}/enrollment-keys`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["relay-enrollment-keys", groupId] }),
  });
}

export function useDeleteRelayEnrollmentKey(groupId: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/relays/enrollment-keys/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["relay-enrollment-keys", groupId] }),
  });
}
