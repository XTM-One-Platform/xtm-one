/**
 * Fleet graph to fleet page drawing - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import type { FleetGraph, FleetGraphEdge, FleetGraphNode } from "@/hooks/api/useFleetApi";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import {
  HEALTH_BADGE_KEYS,
  HEALTH_REASON_KEYS,
  compareHealth,
  healthBadge,
  staleWorkItemsHref,
  crossingEdgeWarnings,
  healthReasons,
  fleetEdgeIds,
  fleetAgentIds,
  fleetGhostNodes,
  fleetScopedAgents,
  internalHandoverCount,
  ungovernedEdgeIds,
} from "./fleet-graph";

function node(id: string, kind: FleetGraphNode["kind"], extra: Partial<FleetGraphNode> = {}): FleetGraphNode {
  return {
    id,
    kind,
    name: id,
    icon: null,
    agent_id: kind === "fleet" ? null : id,
    fleet_id: null,
    fleet_name: "",
    member_id: null,
    parent_agent_id: null,
    is_orchestrator: false,
    is_exposed: false,
    enabled: true,
    visible: true,
    collaboration_instruction: "",
    context_inclusion: null,
    entry_agent_id: null,
    ...extra,
  };
}

function edge(source: string, target: string, scope: FleetGraphEdge["scope"], governed = true, allowed = true): FleetGraphEdge {
  return { source, target, type: "delegate", scope, governed, allowed, source_fleet_id: null, target_fleet_id: null };
}

const GRAPH: FleetGraph = {
  fleet_id: "f1",
  orchestrator_agent_id: "ea",
  nodes: [
    node("ext", "external"),
    node("ux", "connected"),
    node("ea", "member", { is_orchestrator: true }),
    node("dev", "member"),
    node("hidden", "member", { visible: false }),
    node("other-fleet", "fleet"),
  ],
  edges: [
    edge("ea", "dev", "internal"),
    edge("ea", "dev", "internal"),
    edge("dev", "dev", "internal"),
    edge("dev", "ux", "internal"),
    edge("dev", "ext", "outbound"),
  ],
};

test("fleetAgentIds draws home members, then connected, then external agents, never fleet or hidden nodes", () => {
  assert.deepEqual(fleetAgentIds(GRAPH), ["ea", "dev", "ux", "ext"]);
  assert.deepEqual(fleetAgentIds(undefined), []);
});

test("fleetScopedAgents keeps the fleet's order and drops agents the caller cannot list", () => {
  const agents = [{ id: "ext" }, { id: "ux" }, { id: "dev" }, { id: "unrelated" }];
  assert.deepEqual(fleetScopedAgents(agents, GRAPH).map((a) => a.id), ["dev", "ux", "ext"]);
});

test("fleetGhostNodes maps connected and external agents the caller can read to their home fleet", () => {
  const graph: FleetGraph = {
    ...GRAPH,
    nodes: [
      node("ea", "member"),
      node("ux", "connected", { fleet_name: "Design" }),
      node("ext", "external"),
      node("secret", "external", { visible: false, fleet_name: "Hidden" }),
      node("other-fleet", "fleet", { fleet_name: "Other" }),
    ],
  };
  assert.deepEqual([...fleetGhostNodes(graph)], [
    ["ux", "Design"],
    ["ext", ""],
  ]);
  assert.equal(fleetGhostNodes(undefined).size, 0);
});

test("a ghost no fleet graph edge reaches is not drawn, so no drawn node is left without an edge", () => {
  const graph: FleetGraph = {
    ...GRAPH,
    nodes: [
      node("ea", "member"),
      node("dev", "member"),
      node("ux", "connected", { fleet_name: "Design" }),
      node("lonely", "connected", { fleet_name: "Investigation" }),
      node("self", "external"),
    ],
    edges: [edge("ea", "dev", "internal"), edge("dev", "ux", "outbound"), edge("self", "self", "inbound")],
  };
  assert.deepEqual([...fleetGhostNodes(graph).keys()], ["ux"]);
  assert.deepEqual(fleetAgentIds(graph), ["ea", "dev", "ux"]);
  // Every ghost drawn sits at one end of a drawn edge.
  const ends = new Set([...fleetEdgeIds(graph)].flatMap((id) => id.split("->")));
  for (const ghost of fleetGhostNodes(graph).keys()) assert.equal(ends.has(ghost), true, ghost);
});

test("ungovernedEdgeIds keeps crossing edges no connected row governs, never internal or self edges", () => {
  const graph: FleetGraph = {
    ...GRAPH,
    edges: [
      edge("ea", "dev", "internal", false),
      edge("dev", "ext", "outbound", false),
      edge("dev", "ux", "outbound", true),
      edge("ext", "ea", "inbound", false),
      edge("ext", "ext", "inbound", false),
    ],
  };
  assert.deepEqual([...ungovernedEdgeIds(graph)], ["dev->ext", "ext->ea"]);
});

test("fleetEdgeIds keeps only the fleet graph's edges, so a handover between two ghosts is not drawn", () => {
  const graph: FleetGraph = {
    ...GRAPH,
    edges: [edge("ea", "dev", "internal"), edge("dev", "ext", "outbound", false), edge("dev", "dev", "internal")],
  };
  const ids = fleetEdgeIds(graph);
  assert.deepEqual([...ids], ["ea->dev", "dev->ext"]);
  // ext -> ux (two ghosts) is not in the fleet graph: it is not drawn.
  assert.equal(ids.has("ext->ux"), false);
  assert.equal(fleetEdgeIds(undefined).size, 0);
});

test("crossingEdgeWarnings flags a non-exposed target even when governed, then ungoverned edges", () => {
  const graph: FleetGraph = {
    ...GRAPH,
    edges: [
      edge("ea", "dev", "internal", false, false),
      edge("dev", "ux", "outbound", true, false),
      edge("dev", "ext", "outbound", false, true),
      edge("ext", "ea", "inbound", true, true),
      edge("ext", "ext", "inbound", false, false),
    ],
  };
  assert.deepEqual([...crossingEdgeWarnings(graph)], [
    ["dev->ux", "not_exposed"],
    ["dev->ext", "ungoverned"],
  ]);
  assert.equal(crossingEdgeWarnings(undefined).size, 0);
});

test("internalHandoverCount counts each internal pair once and leaves out self and cross-fleet edges", () => {
  assert.equal(internalHandoverCount(GRAPH), 2);
});

test("compareHealth lists what needs a look first, then by name", () => {
  const rows = [
    { status: "ok" as const, name: "b" },
    { status: "failing" as const, name: "z" },
    { status: "ok" as const, name: "a" },
    { status: "stale" as const, name: "c" },
  ];
  assert.deepEqual(rows.sort(compareHealth).map((r) => r.name), ["z", "c", "a", "b"]);
});

const BASE = { status: "ok" as const, last_run_outcome: "ok", failures_24h: 0, recent_runs: 0, recent_interruptions: 0, stale_work_items: 0 };

test("healthReasons says a failing member's count", () => {
  const reasons = healthReasons({ ...BASE, status: "failing", last_run_outcome: "failed", failures_24h: 3, recent_runs: 5 });
  assert.deepEqual(reasons, [{ key: HEALTH_REASON_KEYS.failing, values: { failed: 3, total: 5 } }]);
});

test("healthReasons names an interruption and leaves it out of the count", () => {
  const reasons = healthReasons({ ...BASE, last_run_outcome: "interrupted", recent_runs: 2, recent_interruptions: 4 });
  assert.deepEqual(reasons.map((r) => r.key), [
    HEALTH_REASON_KEYS.clean,
    HEALTH_REASON_KEYS.lastInterrupted,
    HEALTH_REASON_KEYS.interruptions,
  ]);
  assert.deepEqual(reasons[2].values, { count: 4 });
});

test("healthReasons says a failure was followed by a success, and the other states", () => {
  assert.equal(healthReasons({ ...BASE, failures_24h: 1, recent_runs: 3 })[0].key, HEALTH_REASON_KEYS.recovered);
  assert.deepEqual(healthReasons({ ...BASE, status: "idle", last_run_outcome: null }).map((r) => r.key), [HEALTH_REASON_KEYS.idle]);
  assert.deepEqual(healthReasons({ ...BASE, status: "disabled" }).map((r) => r.key), [HEALTH_REASON_KEYS.disabled]);
  assert.deepEqual(healthReasons({ ...BASE, status: "stale", stale_work_items: 2 })[0], { key: HEALTH_REASON_KEYS.stale, values: { count: 2 } });
});

test("healthReasons is empty for an OK member whose only run is in flight: no tooltip, never an empty one", () => {
  assert.deepEqual(healthReasons({ ...BASE, last_run_outcome: "active", recent_runs: 0 }), []);
});

test("the badge names the cause and the count, never a bare Stale", () => {
  assert.deepEqual(healthBadge({ status: "stale", stale_work_items: 1 }), { key: HEALTH_BADGE_KEYS.stale, values: { count: 1 } });
  assert.match(HEALTH_BADGE_KEYS.stale, /\{count, plural, one \{# stale item\} other \{# stale items\}\}/);
  assert.deepEqual(healthBadge({ status: "failing", stale_work_items: 0 }), { key: "Failed run" });
  assert.deepEqual(healthBadge({ status: "ok", stale_work_items: 0 }), { key: "OK" });
  assert.ok(!Object.values(HEALTH_BADGE_KEYS).includes("Stale" as never));
});

test("the stale badge links to the stale work items of that owner", () => {
  assert.equal(staleWorkItemsHref("agent-1"), "/tasks/work-items?view=stale&agent=agent-1");
});

test("the stale link's accessible name starts with the visible badge text in every catalog", () => {
  const link = "{count, plural, one {# stale item} other {# stale items}}: open them for {name}";
  for (const [lang, catalog] of Object.entries({ en, fr, es, ja }) as [string, Record<string, string>][]) {
    const badge = catalog[HEALTH_BADGE_KEYS.stale];
    assert.ok(badge, `${lang}: badge key missing`);
    assert.ok(catalog[link]?.startsWith(badge), `${lang}: "${catalog[link]}" does not start with "${badge}"`);
  }
});

test("the fr and es stale and failed badges stay short (the tooltip carries the sentence)", () => {
  assert.equal((fr as Record<string, string>)[HEALTH_BADGE_KEYS.failing], "Échec");
  assert.equal((es as Record<string, string>)[HEALTH_BADGE_KEYS.failing], "Fallida");
  assert.equal(
    (fr as Record<string, string>)[HEALTH_BADGE_KEYS.stale],
    "{count, plural, one {# en souffrance} other {# en souffrance}}",
  );
  assert.equal((es as Record<string, string>)[HEALTH_BADGE_KEYS.stale], "{count, plural, one {# estancado} other {# estancados}}");
});

test("the run-count reason lines are plural-aware", () => {
  for (const key of [HEALTH_REASON_KEYS.failing, HEALTH_REASON_KEYS.recovered, HEALTH_REASON_KEYS.clean]) {
    assert.match(key, /\{total, plural, one \{[^}]*\} other \{[^}]*\}\}/, key);
  }
});
