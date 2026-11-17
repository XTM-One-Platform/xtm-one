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

/**
 * Favorite agents — the per-user starred set behind the `/agents` list.
 *
 * Stored server-side on the user (`User.favorite_agent_ids`) rather than in
 * localStorage like `useViewMode`: an operator's shortlist out of dozens of
 * agents is worth carrying across browsers and machines, and the platform
 * already owns per-user agent pointers (default chat agent, MCP exposure) on
 * the same record.
 *
 * The stored list is an insertion-ordered set, NOT a display order: inside the
 * pinned block items keep whatever order the surface already applies (name in
 * the grid, the sorted column in the list view). That is deliberate — honouring
 * starring order in the list view would override the column the user explicitly
 * sorted by, which is the very thing pinning is meant to leave alone.
 *
 * Writes go through `useUpdateProfile`, whose optimistic `["me"]` patch makes
 * the star flip instantly and rolls back if the round-trip fails.
 */

import { useCallback, useMemo } from "react";
import { useAuth } from "@/hooks/useAuth";
import { useUpdateProfile } from "@/hooks/api/useAuthApi";

interface FavoriteAgentsState {
  /** Starred agent ids (insertion-ordered; not a display order). */
  favoriteIds: string[];
  isFavorite: (agentId: string) => boolean;
  /** Star an unstarred agent, unstar a starred one. */
  toggleFavorite: (agentId: string) => void;
}

export function useFavoriteAgents(): FavoriteAgentsState {
  const { user } = useAuth();
  const updateProfile = useUpdateProfile();

  const favoriteIds = useMemo(() => user?.favorite_agent_ids ?? [], [user]);
  const favoriteSet = useMemo(() => new Set(favoriteIds), [favoriteIds]);

  const isFavorite = useCallback((agentId: string) => favoriteSet.has(agentId), [favoriteSet]);

  const toggleFavorite = useCallback(
    (agentId: string) => {
      // Derived from the cached list rather than from a local copy: the
      // optimistic patch rewrites `["me"]`, so consecutive clicks each read the
      // value the previous one produced instead of racing on a stale snapshot.
      const next = favoriteSet.has(agentId)
        ? favoriteIds.filter((id) => id !== agentId)
        : [...favoriteIds, agentId];
      updateProfile.mutate({ favorite_agent_ids: next });
    },
    [favoriteIds, favoriteSet, updateProfile],
  );

  // The list and the grid of /agents pin favourites through the table's `pinFirst`
  // (one partition after the sort, standard v2), so the two views never disagree.
  return { favoriteIds, isFavorite, toggleFavorite };
}
