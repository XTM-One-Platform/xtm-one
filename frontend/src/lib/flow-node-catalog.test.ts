/**
 * Unit tests for how the cached flow node catalog follows a knowledge-base
 * change - `yarn test:unit`.
 *
 * A knowledge base created from the agent wizard's dialog was selected at once
 * but missing from the catalog every picker reads until the refetch landed:
 * the canvas showed its raw id and the step's own list could not show it. And
 * when that refetch failed, the app-wide `throwOnError` replaced the whole
 * wizard - unsaved draft included - with the page's error boundary.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { QueryClient, QueryObserver } from "@tanstack/react-query";

import type { FlowFieldOption, FlowNodeCatalog } from "../types/index.ts";
import {
  type CatalogEntityKind,
  catalogThrowOnError,
  dropFromFlowNodeCatalog,
  knowledgeBaseOption,
  syncFlowNodeCatalog,
  withKnowledgeBaseOption,
  withoutCatalogEntity,
} from "./flow-node-catalog.ts";

const catalog: FlowNodeCatalog = {
  categories: [],
  node_kinds: [],
  edge_kinds: [],
  sources: {
    knowledge_bases: [{ value: "kb-old", label: "Runbooks", description: null, icon: "BookOpen" }],
    tools: [{ value: "builtin:web_fetch", label: "Web fetch" }],
  },
  limits: { max_nodes: 200, max_edges: 400 },
};

test("a created knowledge base is listed first, as the server orders them, in the catalog's option shape", () => {
  const next = withKnowledgeBaseOption(catalog, { id: "kb-new", name: "Incident notes", description: "  Postmortems  " });
  assert.deepEqual(next?.sources.knowledge_bases, [
    { value: "kb-new", label: "Incident notes", description: "Postmortems", icon: "BookOpen" },
    { value: "kb-old", label: "Runbooks", description: null, icon: "BookOpen" },
  ]);
  assert.deepEqual(next?.sources.tools, catalog.sources.tools);
});

test("a knowledge base the catalog already lists is replaced, not duplicated", () => {
  const next = withKnowledgeBaseOption(catalog, { id: "kb-old", name: "Runbooks v2", description: null });
  assert.deepEqual(next?.sources.knowledge_bases.map((o) => [o.value, o.label]), [["kb-old", "Runbooks v2"]]);
});

test("the cached catalog is never mutated", () => {
  const before = structuredClone(catalog);
  withKnowledgeBaseOption(catalog, { id: "kb-new", name: "Incident notes", description: null });
  assert.deepEqual(catalog, before);
});

test("a catalog without a knowledge-base source gains one", () => {
  const next = withKnowledgeBaseOption({ ...catalog, sources: {} }, { id: "kb-new", name: "Incident notes", description: null });
  assert.deepEqual(next?.sources.knowledge_bases.map((o) => o.value), ["kb-new"]);
});

test("a catalog without sources never throws: a throw would turn the successful create into an error", () => {
  const bare = { ...catalog, sources: undefined } as unknown as FlowNodeCatalog;
  const next = withKnowledgeBaseOption(bare, { id: "kb-new", name: "Incident notes", description: null });
  assert.deepEqual(next?.sources.knowledge_bases.map((o) => o.value), ["kb-new"]);
});

test("an absent catalog stays absent: one knowledge base must not pass for a loaded catalog", () => {
  assert.equal(withKnowledgeBaseOption(undefined, { id: "kb-new", name: "Incident notes", description: null }), undefined);
});

test("the description is cut like the server's catalog: trimmed, 200 characters, empty as null", () => {
  assert.equal(knowledgeBaseOption({ id: "a", name: "A", description: "x".repeat(250) }).description, "x".repeat(200));
  assert.equal(knowledgeBaseOption({ id: "a", name: "A", description: "   " }).description, null);
  assert.equal(knowledgeBaseOption({ id: "a", name: "A", description: null }).description, null);
});

test("only a catalog that never loaded reaches the error boundary; a failed refetch keeps the one on screen", () => {
  const error = new Error("HTTP 500");
  assert.equal(catalogThrowOnError(error, { state: { data: undefined } }), true);
  assert.equal(catalogThrowOnError(error, { state: { data: catalog } }), false);
});

// syncFlowNodeCatalog against a real query cache, the catalog watched the way
// a mounted picker watches it.

const KEY = ["flow-node-catalog"];
const created = { id: "kb-new", name: "Incident notes", description: null };
const listsCreated = (qc: QueryClient) =>
  !!qc.getQueryData<FlowNodeCatalog>(KEY)?.sources.knowledge_bases?.some((o) => o.value === created.id);

function watchedCatalog(queryFn: () => Promise<FlowNodeCatalog>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const unsubscribe = new QueryObserver(qc, { queryKey: KEY, queryFn }).subscribe(() => {});
  return { qc, stop: () => { unsubscribe(); qc.clear(); } };
}

async function until(condition: () => boolean) {
  for (let i = 0; i < 200 && !condition(); i++) await new Promise((resolve) => setTimeout(resolve, 1));
}

test("a base created while the catalog's first load is in flight is in the catalog that load leaves behind", async () => {
  // That load read the knowledge bases before the base existed, and a refetch
  // would join it rather than start over.
  let reads = 0;
  let answerFirstRead = () => {};
  const firstRead = new Promise<void>((resolve) => { answerFirstRead = resolve; });
  const { qc, stop } = watchedCatalog(async () => {
    reads += 1;
    if (reads === 1) {
      await firstRead;
      return catalog;
    }
    return withKnowledgeBaseOption(catalog, created)!;
  });
  try {
    syncFlowNodeCatalog(qc, created);
    answerFirstRead();
    await until(() => listsCreated(qc));
    assert.ok(listsCreated(qc));
  } finally {
    stop();
  }
});

test("a created base is listed at once, and still listed when the refetch fails", async () => {
  let reads = 0;
  const { qc, stop } = watchedCatalog(async () => {
    reads += 1;
    if (reads === 1) return catalog;
    throw new Error("HTTP 500");
  });
  try {
    await until(() => qc.getQueryData(KEY) !== undefined);
    syncFlowNodeCatalog(qc, created);
    assert.ok(listsCreated(qc), "listed before any refetch");
    await until(() => reads === 2 && qc.getQueryState(KEY)?.fetchStatus === "idle");
    assert.equal(qc.getQueryState(KEY)?.status, "error");
    assert.ok(listsCreated(qc), "kept through the failed refetch");
  } finally {
    stop();
  }
});

test("a rename or a deletion refetches the catalog", async () => {
  let reads = 0;
  const { qc, stop } = watchedCatalog(async () => {
    reads += 1;
    return catalog;
  });
  try {
    await until(() => qc.getQueryState(KEY)?.status === "success");
    syncFlowNodeCatalog(qc);
    await until(() => reads === 2);
    assert.equal(reads, 2);
  } finally {
    stop();
  }
});

// Deletions: every list the catalog serves, each holding one entity
// to delete and one to keep.

const option = (value: string, extra: Partial<FlowFieldOption> = {}): FlowFieldOption => ({ value, label: value, ...extra });

const everyList: FlowNodeCatalog = {
  ...catalog,
  sources: {
    agents: [option("agent-gone"), option("agent-kept")],
    assignments: [option("assignment-gone"), option("assignment-kept")],
    tools: [option("builtin:web_fetch"), option("tool-gone"), option("tool-kept")],
    custom_tools: [option("tool-gone"), option("tool-kept")],
    event_types: [option("event-type-gone"), option("event-type-kept")],
    flows: [option("flow-gone"), option("flow-kept")],
    integrations: [option("integration-gone"), option("integration-kept")],
    knowledge_bases: [option("kb-gone"), option("kb-kept")],
    llm_providers: [option("provider-gone"), option("provider-kept")],
    models: [option("gpt", { provider_id: "provider-gone" }), option("claude", { provider_id: "provider-kept" })],
    relay_groups: [option("relay-group-gone"), option("relay-group-kept")],
    runners: [option("runner-gone"), option("runner-kept")],
    skills: [option("skill-gone"), option("skill-kept")],
    variables: [option("variable-gone"), option("variable-kept")],
  },
};

/** What deleting each kind must take out, read off `_catalog_sources` in
 *  `api/v1/flows.py` rather than off the module under test. */
const DELETED: Record<CatalogEntityKind, { id: string; sources: string[] }> = {
  agent: { id: "agent-gone", sources: ["agents"] },
  assignment: { id: "assignment-gone", sources: ["assignments"] },
  custom_tool: { id: "tool-gone", sources: ["tools", "custom_tools"] },
  event_type: { id: "event-type-gone", sources: ["event_types"] },
  flow: { id: "flow-gone", sources: ["flows"] },
  integration: { id: "integration-gone", sources: ["integrations"] },
  knowledge_base: { id: "kb-gone", sources: ["knowledge_bases"] },
  llm_provider: { id: "provider-gone", sources: ["llm_providers", "models"] },
  relay_group: { id: "relay-group-gone", sources: ["relay_groups"] },
  runner: { id: "runner-gone", sources: ["runners"] },
  skill: { id: "skill-gone", sources: ["skills"] },
  variable: { id: "variable-gone", sources: ["variables"] },
};
const deletions = Object.entries(DELETED) as [CatalogEntityKind, { id: string; sources: string[] }][];

const lists = (c: FlowNodeCatalog | undefined, id: string) =>
  Object.values(c?.sources ?? {}).some((options) => options.some((o) => o.value === id || o.provider_id === id));

for (const [kind, { id, sources }] of deletions) {
  test(`a deleted ${kind} leaves every source it is listed in, and nothing else`, () => {
    const next = withoutCatalogEntity(everyList, kind, id);
    assert.ok(next, "something was removed");
    for (const [source, options] of Object.entries(everyList.sources)) {
      if (!sources.includes(source)) {
        assert.equal(next.sources[source], options, `${source} is left as it was`);
        continue;
      }
      assert.equal(next.sources[source].length, options.length - 1, `${source} lost exactly one option`);
    }
    assert.ok(!lists(next, id));
  });
}

test("a removal never mutates the cached catalog", () => {
  const before = structuredClone(everyList);
  for (const [kind, { id }] of deletions) withoutCatalogEntity(everyList, kind, id);
  assert.deepEqual(everyList, before);
});

test("nothing to remove is no change: no catalog, an id it does not list, a source it does not carry", () => {
  assert.equal(withoutCatalogEntity(undefined, "flow", "flow-gone"), undefined);
  assert.equal(withoutCatalogEntity(everyList, "flow", "flow-unknown"), undefined);
  assert.equal(withoutCatalogEntity({ ...catalog, sources: {} }, "runner", "runner-gone"), undefined);
  const bare = { ...catalog, sources: undefined } as unknown as FlowNodeCatalog;
  assert.equal(withoutCatalogEntity(bare, "variable", "variable-gone"), undefined);
});

test("every deleted entity leaves the cached catalog at once, and a failed refetch keeps the rest of it", async () => {
  for (const [kind, { id }] of deletions) {
    let reads = 0;
    const { qc, stop } = watchedCatalog(async () => {
      reads += 1;
      if (reads === 1) return everyList;
      throw new Error("HTTP 500");
    });
    try {
      await until(() => qc.getQueryData(KEY) !== undefined);
      dropFromFlowNodeCatalog(qc, kind, id);
      assert.ok(!lists(qc.getQueryData<FlowNodeCatalog>(KEY), id), `${kind} gone before any refetch`);
      await until(() => reads === 2 && qc.getQueryState(KEY)?.fetchStatus === "idle");
      assert.equal(reads, 2, `${kind} delete refetched the catalog`);
      assert.equal(qc.getQueryState(KEY)?.status, "error");
      const kept = qc.getQueryData<FlowNodeCatalog>(KEY);
      assert.ok(!lists(kept, id), `${kind} still gone after the failed refetch`);
      assert.equal(kept?.sources.flows.length, kind === "flow" ? 1 : 2, "the rest of the catalog is kept");
    } finally {
      stop();
    }
  }
});

test("a change while no picker is mounted makes no request: the next one to mount refetches", async () => {
  let reads = 0;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  qc.setQueryDefaults(KEY, {
    queryFn: async () => {
      reads += 1;
      return everyList;
    },
  });
  qc.setQueryData(KEY, everyList);
  try {
    syncFlowNodeCatalog(qc);
    dropFromFlowNodeCatalog(qc, "integration", "integration-gone");
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(reads, 0);
    assert.ok(!lists(qc.getQueryData<FlowNodeCatalog>(KEY), "integration-gone"));
    assert.equal(qc.getQueryState(KEY)?.isInvalidated, true);
  } finally {
    qc.clear();
  }
});

test("a delete of an entity the catalog does not list leaves the cached catalog as it is", async () => {
  const { qc, stop } = watchedCatalog(async () => everyList);
  try {
    await until(() => qc.getQueryState(KEY)?.status === "success");
    const before = qc.getQueryData(KEY);
    const updatedAt = qc.getQueryState(KEY)?.dataUpdatedAt;
    dropFromFlowNodeCatalog(qc, "agent", "agent-unknown");
    assert.equal(qc.getQueryData(KEY), before);
    assert.equal(qc.getQueryState(KEY)?.dataUpdatedAt, updatedAt);
  } finally {
    stop();
  }
});
