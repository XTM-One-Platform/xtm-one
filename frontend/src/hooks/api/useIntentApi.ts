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

/** Intent and vertical routing API hooks. */

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { IntentItem, VerticalItem } from "@/types";

// --- Intents ---
/**
 * A page that draws its own error state (the Intents data table, with Retry) passes
 * `throwOnError: false`: a failed read then stays in the table instead of replacing
 * the whole page with the error boundary (the app default is `throwOnError: true`).
 */
interface IntentQueryOptions {
  throwOnError?: boolean;
  /** False: no request (the Agents hub reads intents only for an administrator). */
  enabled?: boolean;
}

export function useIntents(platform?: string, status?: string, options?: IntentQueryOptions) {
  const params = new URLSearchParams();
  if (platform) params.set("platform", platform);
  if (status) params.set("status", status);
  const qs = params.toString();
  return useQuery<IntentItem[]>({
    queryKey: ["intents", platform ?? null, status ?? null],
    queryFn: () => api.get(`/intents${qs ? `?${qs}` : ""}`),
    ...(options?.throwOnError === false ? { throwOnError: false } : {}),
    ...(options?.enabled === false ? { enabled: false } : {}),
  });
}

export function useCreateIntent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { name: string; description: string }) =>
      api.post("/intents", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["intents"] }),
  });
}

export function useUpdateIntent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ intentId, ...data }: { intentId: string; name: string; description: string }) =>
      api.put(`/intents/${intentId}`, data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["intents"] }),
  });
}

// --- Intent bindings ---

/** Create or update a single intent -> agent binding (optionally vertical-scoped). */
export function useBindIntent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      intentId,
      ...body
    }: {
      intentId: string;
      agent_id: string;
      vertical: string | null;
      priority: number;
      priority_override_vertical?: string;
    }) => api.put(`/intents/${intentId}/bind`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["intents"] }),
  });
}

/** Bind an agent to an intent for several verticals at once (null = universal). */
export function useAddIntentBindings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      intentId,
      agentId,
      verticals,
    }: {
      intentId: string;
      agentId: string;
      verticals: (string | null)[];
    }) => {
      for (const vertical of verticals) {
        await api.put(`/intents/${intentId}/bind`, {
          agent_id: agentId,
          vertical,
          priority: 0,
        });
      }
    },
    // onSettled (not onSuccess): the sequential PUT loop can fail midway
    // after some bindings were already created server-side — the cache must
    // be refreshed even on error so the UI doesn't show pre-mutation data.
    onSettled: (_d, _e, { agentId }) => {
      qc.invalidateQueries({ queryKey: ["intents"] });
      qc.invalidateQueries({ queryKey: ["agent", agentId] });
    },
  });
}

/** Move a binding to a different vertical (delete + recreate). */
export function useChangeIntentBindingVertical() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({
      intentId,
      bindingId,
      agentId,
      vertical,
      priority,
    }: {
      intentId: string;
      bindingId: string;
      agentId: string;
      vertical: string | null;
      priority: number;
    }) => {
      await api.delete(`/intents/${intentId}/bind/${bindingId}`);
      await api.put(`/intents/${intentId}/bind`, {
        agent_id: agentId,
        vertical,
        priority,
      });
    },
    // onSettled: the delete can succeed while the recreate fails — the cache
    // must be refreshed even on error to reflect the server-side change.
    onSettled: () => qc.invalidateQueries({ queryKey: ["intents"] }),
  });
}

export function useDeleteIntentBinding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ intentId, bindingId }: { intentId: string; bindingId: string; agentId?: string }) =>
      api.delete(`/intents/${intentId}/bind/${bindingId}`),
    onSuccess: (_d, { agentId }) => {
      qc.invalidateQueries({ queryKey: ["intents"] });
      if (agentId) qc.invalidateQueries({ queryKey: ["agent", agentId] });
    },
  });
}

// --- Verticals ---
export function useVerticals(options?: IntentQueryOptions) {
  return useQuery<VerticalItem[]>({
    queryKey: ["verticals"],
    queryFn: () => api.get("/verticals"),
    ...(options?.throwOnError === false ? { throwOnError: false } : {}),
  });
}

export function useCreateVertical() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.post("/verticals", { name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["verticals"] }),
  });
}

export function useRenameVertical() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      api.put(`/verticals/${id}`, { name }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["verticals"] });
      qc.invalidateQueries({ queryKey: ["intents"] });
    },
  });
}

export function useDeleteVertical() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/verticals/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["verticals"] }),
  });
}
