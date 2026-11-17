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
 * How the cached flow node catalog follows a change to what it lists.
 *
 * Every picker of the flow builder and the agent wizard - tools, knowledge
 * bases, skills, agents, integrations, assignments, flows, variables, event
 * types, runners, models, LLM providers - lists its options from one cached
 * `GET /flows/node-catalog`, kept for five minutes. So:
 *
 * - creating, changing or deleting one of those entities brings the change
 *   into the cached catalog at once rather than five minutes later (every such
 *   hook calls `syncFlowNodeCatalog` or `dropFromFlowNodeCatalog`, pinned by
 *   `flow-node-catalog-sync.test.ts`); a created knowledge base is written in
 *   from the create response, and a deleted entity is taken out, so the
 *   catalog is right before the refetch that follows and stays right if that
 *   refetch fails;
 * - a failed background refetch keeps the catalog already on screen, and the
 *   builder and the wizard say it may be out of date, with a retry. The app
 *   throws every query error to the page's error boundary (`main.tsx`), and
 *   TanStack Query reports a failed refetch as an error even with the previous
 *   data in hand, so one failed catalog refetch replaced a whole page - an
 *   unsaved wizard draft included - with "Failed to load this section".
 *
 * No React and no `@/` import, so `flow-node-catalog.test.ts` runs under
 * `node --test`.
 */
import type { QueryClient } from "@tanstack/react-query";
import type { FlowFieldOption, FlowNodeCatalog, KnowledgeBase } from "../types/index.ts";

/** The catalog source listing knowledge bases (`SOURCE_KNOWLEDGE_BASES` server-side). */
const KNOWLEDGE_BASES = "knowledge_bases";

type CatalogKnowledgeBase = Pick<KnowledgeBase, "id" | "name" | "description">;

/** A knowledge base as the catalog lists it (`_catalog_sources` in `api/v1/flows.py`). */
export function knowledgeBaseOption(kb: CatalogKnowledgeBase): FlowFieldOption {
  const description = (kb.description ?? "").trim().slice(0, 200);
  return { value: kb.id, label: kb.name, description: description || null, icon: "BookOpen" };
}

/**
 * `catalog` with `kb` listed first among its knowledge bases - the server
 * orders them newest first - replacing an entry it already had. An absent
 * catalog stays absent: one made of a single knowledge base would pass for a
 * loaded catalog that has no tools, skills or agents.
 */
export function withKnowledgeBaseOption(
  catalog: FlowNodeCatalog | undefined,
  kb: CatalogKnowledgeBase,
): FlowNodeCatalog | undefined {
  if (!catalog) return undefined;
  const others = (catalog.sources?.[KNOWLEDGE_BASES] ?? []).filter((o) => o.value !== kb.id);
  return {
    ...catalog,
    sources: { ...catalog.sources, [KNOWLEDGE_BASES]: [knowledgeBaseOption(kb), ...others] },
  };
}

/** `throwOnError` for the catalog query: only a catalog that never loaded is thrown. */
export function catalogThrowOnError(_error: unknown, query: { state: { data: unknown } }): boolean {
  return query.state.data === undefined;
}

const CATALOG_KEY = ["flow-node-catalog"];

/**
 * Bring the cached catalog in step with a change to an entity it lists: a
 * created knowledge base is written in from the response, then the catalog is
 * refetched. A first load still in flight may have read the knowledge bases
 * before the change, and a refetch would join it rather than start over, so
 * it is cancelled and restarted. Not awaited by the caller: no success handler
 * waits on a catalog round trip.
 */
export function syncFlowNodeCatalog(qc: QueryClient, created?: CatalogKnowledgeBase): void {
  if (created) {
    qc.setQueryData<FlowNodeCatalog>(CATALOG_KEY, (catalog) => withKnowledgeBaseOption(catalog, created));
  }
  const state = qc.getQueryState(CATALOG_KEY);
  if (state?.fetchStatus === "fetching" && state.data === undefined) {
    void qc.cancelQueries({ queryKey: CATALOG_KEY }).then(() => qc.invalidateQueries({ queryKey: CATALOG_KEY }));
    return;
  }
  void qc.invalidateQueries({ queryKey: CATALOG_KEY });
}

/** The sources each kind of entity is listed in (`_catalog_sources`). A custom
 *  tool is listed twice: among the tools and among the custom tools. */
const SOURCES_BY_KIND = {
  agent: ["agents"],
  assignment: ["assignments"],
  custom_tool: ["tools", "custom_tools"],
  event_type: ["event_types"],
  flow: ["flows"],
  integration: ["integrations"],
  knowledge_base: [KNOWLEDGE_BASES],
  llm_provider: ["llm_providers"],
  relay_group: ["relay_groups"],
  runner: ["runners"],
  skill: ["skills"],
  variable: ["variables"],
} as const satisfies Record<string, readonly string[]>;

export type CatalogEntityKind = keyof typeof SOURCES_BY_KIND;

/**
 * `catalog` without the entity `id` of `kind` - a deleted LLM provider takes
 * the models it published with it - or `undefined` when there is nothing to
 * remove (no catalog, an id it does not list), which leaves the cache as it is.
 */
export function withoutCatalogEntity(
  catalog: FlowNodeCatalog | undefined,
  kind: CatalogEntityKind,
  id: string,
): FlowNodeCatalog | undefined {
  if (!catalog?.sources) return undefined;
  const sources = { ...catalog.sources };
  let removed = false;
  const drop = (source: string, keep: (option: FlowFieldOption) => boolean) => {
    const options = sources[source];
    if (!options) return;
    const kept = options.filter(keep);
    if (kept.length === options.length) return;
    sources[source] = kept;
    removed = true;
  };
  for (const source of SOURCES_BY_KIND[kind]) drop(source, (option) => option.value !== id);
  if (kind === "llm_provider") drop("models", (option) => option.provider_id !== id);
  return removed ? { ...catalog, sources } : undefined;
}

/**
 * Bring the cached catalog in step with a successful delete: the entity leaves
 * it at once, so a refetch that fails keeps the last good catalog without it,
 * then the catalog is synced like any other change.
 */
export function dropFromFlowNodeCatalog(qc: QueryClient, kind: CatalogEntityKind, id: string): void {
  qc.setQueryData<FlowNodeCatalog>(CATALOG_KEY, (catalog) => withoutCatalogEntity(catalog, kind, id));
  syncFlowNodeCatalog(qc);
}
