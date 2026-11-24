// SPDX-License-Identifier: Apache-2.0

/** Group management API hooks. */

import { keepPreviousData, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Group, GroupDetail, GroupManager, PaginatedGroupMembers, UserGroupItem } from "@/types";

// --- Groups ---

export function useGroups() {
  return useQuery<Group[]>({
    queryKey: ["groups"],
    queryFn: () => api.get("/groups"),
  });
}

/** The Groups table's query (`GET /groups`, #11940): every filter is one value or several comma-separated. */
export interface GroupTableQuery {
  search?: string;
  kind?: string;
  members?: string;
  sort?: string;
  direction?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

function groupCountQuery(query: GroupTableQuery): string {
  const params = new URLSearchParams();
  for (const key of ["search", "kind", "members"] as const) {
    if (query[key]) params.set(key, query[key]);
  }
  return params.toString();
}

/** One page of the Groups table, searched, filtered and sorted on the server. */
export function useGroupsPage(query: GroupTableQuery) {
  const params = new URLSearchParams(groupCountQuery(query));
  if (query.sort) params.set("sort", query.sort);
  if (query.direction) params.set("direction", query.direction);
  if (query.limit) params.set("limit", String(query.limit));
  if (query.offset) params.set("offset", String(query.offset));
  const qs = params.toString();
  return useQuery<Group[]>({
    queryKey: ["groups", "page", qs],
    queryFn: () => api.get(`/groups?${qs}`),
    placeholderData: keepPreviousData,
  });
}

/** How many groups the Groups table's filters match: its total. */
export function useGroupsCount(query: GroupTableQuery) {
  const qs = groupCountQuery(query);
  return useQuery<{ count: number }>({
    queryKey: ["groups", "count", qs],
    queryFn: () => api.get(`/groups/count${qs ? `?${qs}` : ""}`),
    placeholderData: keepPreviousData,
  });
}

export function useGroup(groupId: string) {
  return useQuery<GroupDetail>({
    queryKey: ["group", groupId],
    queryFn: () => api.get(`/groups/${groupId}`),
    enabled: !!groupId,
  });
}

export function useGroupMembers(
  groupId: string,
  params: {
    search?: string;
    /** One role or several comma-separated. */
    role?: string;
    /** yes: the managers; no: the other members. */
    manager?: string;
    /** name | email | role | added; omitted = by name. */
    sort?: string;
    direction?: "asc" | "desc";
    limit?: number;
    offset?: number;
  },
) {
  return useQuery<PaginatedGroupMembers>({
    queryKey: ["group-members", groupId, params],
    placeholderData: keepPreviousData,
    queryFn: () => {
      const qs = new URLSearchParams();
      if (params.search) qs.set("search", params.search);
      if (params.role) qs.set("role", params.role);
      if (params.manager) qs.set("manager", params.manager);
      if (params.sort) qs.set("sort", params.sort);
      if (params.direction) qs.set("direction", params.direction);
      if (params.limit !== undefined) qs.set("limit", String(params.limit));
      if (params.offset !== undefined) qs.set("offset", String(params.offset));
      const suffix = qs.toString() ? `?${qs.toString()}` : "";
      return api.get(`/groups/${groupId}/members${suffix}`);
    },
    enabled: !!groupId,
  });
}

export function useCreateGroup() {
  const qc = useQueryClient();
  return useMutation<Group, Error, { name: string; description?: string | null }>({
    mutationFn: (data) => api.post("/groups", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["groups"] }),
  });
}

export function useUpdateGroup() {
  const qc = useQueryClient();
  return useMutation<Group, Error, { groupId: string; data: { name?: string; description?: string | null; quota_config?: Record<string, unknown> | null; ai_enabled?: boolean; kind?: Group["kind"]; auto_join?: boolean } }>({
    mutationFn: ({ groupId, data }) => api.put(`/groups/${groupId}`, data),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ["groups"] });
      qc.invalidateQueries({ queryKey: ["group", vars.groupId] });
      // The administrator may be a member of the group they just switched.
      if ("ai_enabled" in vars.data) qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useDeleteGroup() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (groupId) => api.delete(`/groups/${groupId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["groups"] });
      // A deleted group's AI switch and quotas no longer apply to its former members.
      refreshGroupMembers(qc);
    },
  });
}

/**
 * A group's settings (AI switch, quotas) reach its members, so what applies to
 * a person changes with their membership: refresh the user pages that show it
 * (the members named, or every one) and the admin's own `["me"]`, in case they
 * belong to the group.
 */
function refreshGroupMembers(qc: ReturnType<typeof useQueryClient>, userIds?: string[]) {
  if (userIds) {
    for (const userId of userIds) qc.invalidateQueries({ queryKey: ["user-detail", userId] });
  } else {
    qc.invalidateQueries({ queryKey: ["user-detail"] });
  }
  qc.invalidateQueries({ queryKey: ["me"] });
}

// --- Group Members ---

export function useAddGroupMembers() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ groupId, userIds }: { groupId: string; userIds: string[] }) =>
      api.post(`/groups/${groupId}/members`, { user_ids: userIds }),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ["group", vars.groupId] });
      qc.invalidateQueries({ queryKey: ["group-members", vars.groupId] });
      qc.invalidateQueries({ queryKey: ["groups"] });
      qc.invalidateQueries({ queryKey: ["user-groups"] });
      refreshGroupMembers(qc, vars.userIds);
    },
  });
}

export function useRemoveGroupMember() {
  const qc = useQueryClient();
  return useMutation<void, Error, { groupId: string; userId: string }>({
    mutationFn: ({ groupId, userId }) => api.delete(`/groups/${groupId}/members/${userId}`),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ["group", vars.groupId] });
      qc.invalidateQueries({ queryKey: ["group-members", vars.groupId] });
      qc.invalidateQueries({ queryKey: ["groups"] });
      qc.invalidateQueries({ queryKey: ["user-groups"] });
      refreshGroupMembers(qc, [vars.userId]);
    },
  });
}

// --- Group Managers ---

export function useGroupManagers(groupId: string) {
  return useQuery<GroupManager[]>({
    queryKey: ["group-managers", groupId],
    queryFn: () => api.get(`/groups/${groupId}/managers`),
    enabled: !!groupId,
  });
}


function refreshManagers(qc: ReturnType<typeof useQueryClient>, groupId: string) {
  qc.invalidateQueries({ queryKey: ["group-managers", groupId] });
  qc.invalidateQueries({ queryKey: ["group-members", groupId] });
  qc.invalidateQueries({ queryKey: ["groups", "managed"] });
  // The admin may have elected themselves: `managed_groups` rides on `/me`.
  qc.invalidateQueries({ queryKey: ["me"] });
}

export function useAddGroupManagers() {
  const qc = useQueryClient();
  return useMutation<GroupManager[], Error, { groupId: string; userIds: string[] }>({
    mutationFn: ({ groupId, userIds }) => api.post(`/groups/${groupId}/managers`, { user_ids: userIds }),
    onSuccess: (_, vars) => refreshManagers(qc, vars.groupId),
  });
}

export function useRemoveGroupManager() {
  const qc = useQueryClient();
  return useMutation<void, Error, { groupId: string; userId: string }>({
    mutationFn: ({ groupId, userId }) => api.delete(`/groups/${groupId}/managers/${userId}`),
    onSuccess: (_, vars) => refreshManagers(qc, vars.groupId),
  });
}

// --- User Groups ---

export function useUserGroups(userId: string) {
  return useQuery<UserGroupItem[]>({
    queryKey: ["user-groups", userId],
    queryFn: () => api.get(`/users/${userId}/groups`),
    enabled: !!userId,
  });
}
