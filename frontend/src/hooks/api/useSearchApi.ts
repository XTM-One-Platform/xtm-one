// SPDX-License-Identifier: Apache-2.0

/**
 * Global search hooks (top bar field and `/search`) over `GET /search/counts`
 * and `GET /search` (`backend/app/api/v1/search.py`): one ranked, RBAC-scoped
 * query across every entity type, never one call per type.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/** The entity types the endpoint searches, in the order the results page lists them. */
export const SEARCH_TYPES = [
  "agent", "fleet", "assignment", "flow", "intent", "conversation", "work_item",
  "knowledge_base", "knowledge_document", "prompt", "skill", "template",
  "presentation", "video", "report", "file", "integration", "mcp_server",
  "custom_tool", "user", "group",
] as const;

export type SearchType = (typeof SEARCH_TYPES)[number];

export interface SearchHit {
  type: SearchType;
  id: string;
  name: string;
  excerpt?: string | null;
  updated_at?: string | null;
  parent_id?: string | null;
}

export interface SearchPage {
  query: string;
  type: SearchType | null;
  total: number;
  limit: number;
  offset: number;
  items: SearchHit[];
}

export interface SearchCounts {
  query: string;
  total: number;
  counts: Partial<Record<SearchType, number>>;
}

/** Shortest term sent to the server: one letter matches half the platform. */
export const MIN_SEARCH_LENGTH = 2;

const SEARCH_KEY = ["search"] as const;

const searchable = (q: string) => q.trim().length >= MIN_SEARCH_LENGTH;

/** Matches per entity type for `q`. */
export function useSearchCounts(q: string) {
  const query = q.trim();
  return useQuery<SearchCounts>({
    queryKey: [...SEARCH_KEY, "counts", query],
    queryFn: () => api.get(`/search/counts?${new URLSearchParams({ q: query })}`),
    enabled: searchable(query),
    placeholderData: keepPreviousData,
  });
}

/** The orders `GET /search` takes besides relevance (its `sort_by`). */
export type SearchSort = { key: "name" | "updated"; direction: "asc" | "desc" };

/** One page of hits for `q`, of one type or of every type merged; by rank unless `sort` is given. */
export function useSearch(
  q: string,
  options: { type?: SearchType | null; limit?: number; offset?: number; sort?: SearchSort | null } = {},
) {
  const query = q.trim();
  const params = new URLSearchParams({ q: query, limit: String(options.limit ?? 25), offset: String(options.offset ?? 0) });
  if (options.type) params.set("type", options.type);
  if (options.sort) {
    params.set("sort_by", options.sort.key);
    params.set("sort_dir", options.sort.direction);
  }
  return useQuery<SearchPage>({
    queryKey: [
      ...SEARCH_KEY, "page", query, options.type ?? null, options.limit ?? 25, options.offset ?? 0,
      options.sort?.key ?? "rank", options.sort?.direction ?? null,
    ],
    queryFn: () => api.get(`/search?${params}`),
    enabled: searchable(query),
    placeholderData: keepPreviousData,
  });
}
