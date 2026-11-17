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

/** Channel Bot API hooks. */

import { useQuery, useQueries, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEntityListScope } from "@/hooks/useEntityListScope";
import { api } from "@/lib/api";
import { entityListKey, entityListPath } from "@/lib/entity-list-scope";
import type {
  AllowedChannel,
  ChannelBot,
  ChannelBotCatalogItem,
  CreateChannelBotRequest,
  EntityListOptions,
  UpdateChannelBotRequest,
  IntegrationToolInfo,
} from "@/types";

// --- Channel Bot Catalog ---
export function useChannelBotCatalog() {
  return useQuery<ChannelBotCatalogItem[]>({
    queryKey: ["channel-bot-catalog"],
    queryFn: () => api.get("/channel-bots/catalog"),
    staleTime: 60_000,
  });
}

// --- Channel Bots ---
/** The channel bots list; `{ scope: "all" }` is every bot for an admin (detail pages). */
export function useChannelBots(options?: EntityListOptions) {
  const scope = useEntityListScope(options);
  return useQuery<ChannelBot[]>({
    queryKey: entityListKey(["channel-bots"], scope),
    queryFn: () => api.get(entityListPath("/channel-bots", scope)),
  });
}

export function useCreateChannelBot() {
  const qc = useQueryClient();
  return useMutation<ChannelBot, Error, CreateChannelBotRequest>({
    mutationFn: (data) => api.post("/channel-bots", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["channel-bots"] }),
  });
}

export function useUpdateChannelBot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...data }: { id: string } & UpdateChannelBotRequest) =>
      api.put(`/channel-bots/${id}`, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["channel-bots"] });
      // The approval switch on a bot decides what the tools list shows as
      // gating; without this the two screens disagreed until a reload.
      qc.invalidateQueries({ queryKey: ["tool-gate-catalog"] });
    },
  });
}

export function useDeleteChannelBot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/channel-bots/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["channel-bots"] }),
  });
}

export function useChannelBotTools(botId: string) {
  return useQuery<{ tools: IntegrationToolInfo[] }>({
    queryKey: ["channel-bot-tools", botId],
    queryFn: () => api.get(`/channel-bots/${botId}/tools`),
    enabled: !!botId,
  });
}

/**
 * Fetch channels for all given bots in parallel.
 * Returns a map of botId → AllowedChannel[].
 * Works for every provider — providers that don't support channel listing
 * simply return an empty array from the backend.
 */
export function useChannelBotChannelsMap(
  botIds: string[],
): { map: Record<string, AllowedChannel[]>; loadingMap: Record<string, boolean> } {
  const results = useQueries({
    queries: botIds.map((id) => ({
      queryKey: ["channel-bot-channels", id],
      queryFn: () =>
        api.get(`/channel-bots/${id}/channels`) as Promise<{
          channels: AllowedChannel[];
        }>,
      staleTime: 60_000,
    })),
  });

  const map: Record<string, AllowedChannel[]> = {};
  const loadingMap: Record<string, boolean> = {};
  botIds.forEach((id, i) => {
    loadingMap[id] = results[i]?.isLoading ?? false;
    const channels = results[i]?.data?.channels;
    if (channels?.length) {
      map[id] = channels;
    }
  });
  return { map, loadingMap };
}

export function useTestChannelBotTool() {
  return useMutation<
    { status: string; output?: string; message?: string; duration_ms?: number },
    Error,
    { botId: string; tool_name: string; arguments?: Record<string, unknown> }
  >({
    mutationFn: ({ botId, ...body }) =>
      api.post(`/channel-bots/${botId}/test-tool`, body),
  });
}
