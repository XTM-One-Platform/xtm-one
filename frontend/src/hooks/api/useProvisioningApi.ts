// SPDX-License-Identifier: Apache-2.0

/**
 * Provisioning hooks (#122): the SCIM endpoint and its tokens
 * (`/api/v1/scim/tokens`), the SCIM activity, the deactivation episodes with
 * their review list (`/api/v1/users/deactivations`) and the preview of a
 * deactivation before the administrator confirms it.
 * Contract: dev-docs/scim.md and dev-docs/user-deactivation.md.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface ScimToken {
  id: string;
  name: string;
  token_prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ScimTokens {
  endpoint: string;
  tokens: ScimToken[];
}

/** A generated token: `token` is the raw value, shown once and never again. */
export interface ScimTokenCreated extends ScimToken {
  token: string;
  endpoint: string;
}

export interface ScimActivity {
  id: string;
  token: string;
  action: string;
  entity_type: string;
  entity_name: string | null;
  summary: string;
  created_at: string | null;
}

export interface DeactivationReviewItem {
  key: string;
  kind: string;
  id: string;
  name: string;
  action: string;
  detail: string;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  resolution?: string | null;
}

export interface DeactivationEpisode {
  id: string;
  user_id: string;
  user_email: string | null;
  user_name: string | null;
  deprovisioned: boolean;
  source: string;
  actor_label: string;
  successor_user_id: string | null;
  counts: Record<string, number>;
  review_items: DeactivationReviewItem[];
  open_review_count: number;
  deactivated_at: string | null;
  reactivated_at: string | null;
}

export interface DeactivationPreview {
  user_id: string;
  refusal: string | null;
  successor: { id: string; name: string; email: string } | null;
  counts: Record<string, number>;
  kept_without_successor: number;
}

export type ReviewResolution = "keep" | "reassign" | "delete";

const SCIM_TOKENS_KEY = ["scim-tokens"] as const;
const SCIM_ACTIVITY_KEY = ["scim-activity"] as const;
const DEACTIVATIONS_KEY = ["user-deactivations"] as const;

export function useScimTokens() {
  return useQuery<ScimTokens>({ queryKey: SCIM_TOKENS_KEY, queryFn: () => api.get("/scim/tokens") });
}

export function useScimActivity() {
  return useQuery<ScimActivity[]>({ queryKey: SCIM_ACTIVITY_KEY, queryFn: () => api.get("/scim/tokens/activity?limit=50") });
}

export function useCreateScimToken() {
  const qc = useQueryClient();
  return useMutation<ScimTokenCreated, Error, string>({
    mutationFn: (name) => api.post("/scim/tokens", { name }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SCIM_TOKENS_KEY }),
  });
}

export function useRevokeScimToken() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/scim/tokens/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SCIM_TOKENS_KEY }),
  });
}

export function useDeactivations() {
  return useQuery<DeactivationEpisode[]>({ queryKey: DEACTIVATIONS_KEY, queryFn: () => api.get("/users/deactivations?open=true") });
}

export function useResolveReviewItem() {
  const qc = useQueryClient();
  return useMutation<DeactivationEpisode, Error, { episodeId: string; key: string; resolution: ReviewResolution }>({
    mutationFn: ({ episodeId, key, resolution }) => api.post(`/users/deactivations/${episodeId}/review`, { key, resolution }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: DEACTIVATIONS_KEY }),
  });
}

/** Read only when the confirmation dialog opens (`enabled`). */
export function useDeactivationPreview(userId: string, enabled: boolean) {
  return useQuery<DeactivationPreview>({
    queryKey: ["deactivation-preview", userId],
    queryFn: () => api.get(`/users/${userId}/deactivation-preview`),
    enabled: enabled && !!userId,
    staleTime: 0,
  });
}
