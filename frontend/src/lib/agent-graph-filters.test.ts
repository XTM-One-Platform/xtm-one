/**
 * Filters of the agent graph toolbar - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import {
  DEFAULT_GRAPH_FILTERS,
  activeFilterCount,
  agentKind,
  agentsInVisibility,
  filterGraphAgents,
  type FilterableAgent,
} from "./agent-graph-filters";
import type { VisibilityFields } from "./visibility";

const agent = (id: string, over: Partial<FilterableAgent> = {}): FilterableAgent => ({
  id,
  enabled: true,
  is_builtin: false,
  is_company_managed: false,
  ...over,
});

const AGENTS = [
  agent("built", { is_builtin: true }),
  agent("company", { is_company_managed: true }),
  agent("mine"),
  agent("off", { enabled: false }),
];
const NONE = { withOpenWorkItems: new Set<string>(), withRunsInFlight: new Set<string>() };
const ids = (list: FilterableAgent[]) => list.map((a) => a.id);

test("the default filters keep every agent", () => {
  assert.equal(activeFilterCount(DEFAULT_GRAPH_FILTERS), 0);
  assert.deepEqual(ids(filterGraphAgents(AGENTS, DEFAULT_GRAPH_FILTERS, NONE)), ["built", "company", "mine", "off"]);
});

test("agentKind: built-in first, then company-managed, else personal", () => {
  assert.equal(agentKind(agent("x", { is_builtin: true, is_company_managed: true })), "builtin");
  assert.equal(agentKind(agent("x", { is_company_managed: true })), "company");
  assert.equal(agentKind(agent("x")), "personal");
});

test("kind and enabled filters combine", () => {
  const filters = { ...DEFAULT_GRAPH_FILTERS, kinds: ["personal" as const], enabled: "enabled" as const };
  assert.equal(activeFilterCount(filters), 2);
  assert.deepEqual(ids(filterGraphAgents(AGENTS, filters, NONE)), ["mine"]);
  const disabled = { ...DEFAULT_GRAPH_FILTERS, enabled: "disabled" as const };
  assert.deepEqual(ids(filterGraphAgents(AGENTS, disabled, NONE)), ["off"]);
});

test("every kind selected counts as no kind filter", () => {
  const filters = { ...DEFAULT_GRAPH_FILTERS, kinds: ["builtin" as const, "company" as const, "personal" as const] };
  assert.equal(activeFilterCount(filters), 0);
});

test("activity filters keep the agents with an open work item or a run in flight, and the kept ids", () => {
  const context = { withOpenWorkItems: new Set(["company", "mine"]), withRunsInFlight: new Set(["mine"]), keep: new Set(["built"]) };
  const items = { ...DEFAULT_GRAPH_FILTERS, withOpenWorkItems: true };
  assert.deepEqual(ids(filterGraphAgents(AGENTS, items, context)), ["built", "company", "mine"]);
  const both = { ...items, withRunsInFlight: true };
  assert.deepEqual(ids(filterGraphAgents(AGENTS, both, context)), ["built", "mine"]);
});

test("agentsInVisibility: the toolbar quick filter, Mine | Shared | Company | All, keeps the stand-in", () => {
  const owned = (id: string, over: Partial<VisibilityFields> = {}): VisibilityFields & { id: string } => ({
    id,
    user_id: "other",
    is_company_managed: false,
    shared_group_ids: [],
    shared_user_ids: [],
    ...over,
  });
  const map = [
    owned("mine", { user_id: "me" }),
    owned("by-group", { shared_group_ids: ["g"] }),
    owned("direct", { shared_user_ids: ["me"] }),
    owned("company", { is_company_managed: true }),
    owned("builtin", { user_id: null }),
    owned("stand-in", { user_id: null }),
  ];
  const keep = new Set(["stand-in"]);
  const names = (list: { id: string }[]) => list.map((a) => a.id);
  assert.deepEqual(names(agentsInVisibility(map, "mine", "me", keep)), ["mine", "stand-in"]);
  assert.deepEqual(names(agentsInVisibility(map, "shared", "me", keep)), ["by-group", "direct", "stand-in"]);
  assert.deepEqual(names(agentsInVisibility(map, "company", "me", keep)), ["company", "stand-in"]);
  assert.deepEqual(names(agentsInVisibility(map, "all", "me", keep)), names(map));
  // A built-in agent nobody owns or shares is in All only, as on the Agents page.
  assert.deepEqual(names(agentsInVisibility(map, "mine", "me")), ["mine"]);
});
