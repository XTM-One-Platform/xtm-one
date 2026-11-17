/**
 * The Fleets list toolbar - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import type { Fleet } from "@/hooks/api/useFleetApi";

import {
  FLEET_PROPERTIES,
  NO_FLEET_FILTERS,
  applyFleetFilters,
  distinctOptions,
  narrowFleets,
  hasFleetFilters,
  readFleetFilters,
  writeFleetFilters,
} from "./fleet-list-filter";

function fleet(id: string, patch: Partial<Fleet> = {}): Fleet {
  return {
    id,
    name: id,
    workspace_id: "w1",
    workspace_name: "Ops",
    orchestrator_agent_id: "a1",
    is_archived: false,
    member_count: 0,
    connected_count: 0,
    ...patch,
  } as Fleet;
}

const fleets = [
  fleet("ceo", { member_count: 3 }),
  fleet("soc", { workspace_id: "w2", workspace_name: "SOC", orchestrator_agent_id: "a2", connected_count: 1 }),
  fleet("old", { is_archived: true }),
  fleet("bare", { orchestrator_agent_id: null }),
];

test("archived fleets are hidden unless asked for", () => {
  assert.deepEqual(applyFleetFilters(fleets, NO_FLEET_FILTERS).map((f) => f.id), ["ceo", "soc", "bare"]);
  assert.deepEqual(
    applyFleetFilters(fleets, { ...NO_FLEET_FILTERS, includeArchived: true }).map((f) => f.id),
    ["ceo", "soc", "old", "bare"],
  );
});

test("workspace and orchestrator filters combine", () => {
  assert.deepEqual(applyFleetFilters(fleets, { ...NO_FLEET_FILTERS, workspaceId: "w1" }).map((f) => f.id), ["ceo", "bare"]);
  assert.deepEqual(applyFleetFilters(fleets, { ...NO_FLEET_FILTERS, orchestratorId: "a2" }).map((f) => f.id), ["soc"]);
  assert.deepEqual(applyFleetFilters(fleets, { ...NO_FLEET_FILTERS, workspaceId: "w1", orchestratorId: "a2" }), []);
});

test("a loading list stays undefined, so the visibility landing waits for it", () => {
  assert.equal(narrowFleets(undefined, NO_FLEET_FILTERS), undefined);
  assert.deepEqual(narrowFleets([], NO_FLEET_FILTERS), []);
  assert.deepEqual(narrowFleets(fleets, { ...NO_FLEET_FILTERS, workspaceId: "w2" })?.map((f) => f.id), ["soc"]);
});

test("the has: properties read the counts and the orchestrator", () => {
  const has = (key: string) => fleets.filter(FLEET_PROPERTIES.find((p) => p.key === key)!.matches).map((f) => f.id);
  assert.deepEqual(has("members"), ["ceo"]);
  assert.deepEqual(has("connected"), ["soc"]);
  assert.deepEqual(has("orchestrator"), ["ceo", "soc", "old"]);
});

test("has members does not count the orchestrator", () => {
  const members = FLEET_PROPERTIES.find((p) => p.key === "members")!.matches;
  assert.equal(members(fleet("solo", { member_count: 1 })), false);
  assert.equal(members(fleet("pair", { member_count: 2 })), true);
  assert.equal(members(fleet("headless", { member_count: 1, orchestrator_agent_id: null })), true);
});

test("has connections counts a fleet connected only to another fleet", () => {
  const connected = FLEET_PROPERTIES.find((p) => p.key === "connected")!;
  assert.equal(connected.label, "Has connections");
  assert.equal(connected.matches(fleet("linked", { connected_count: 1 })), true);
});

test("the filters round-trip through the URL and keep the other parameters", () => {
  const filters = { workspaceId: "w2", orchestratorId: "a2", includeArchived: true };
  const params = writeFleetFilters(new URLSearchParams("tab=fleets&q=soc"), filters);
  assert.equal(params.get("tab"), "fleets");
  assert.equal(params.get("q"), "soc");
  assert.deepEqual(readFleetFilters(params), filters);
  const cleared = writeFleetFilters(params, NO_FLEET_FILTERS);
  assert.equal(cleared.toString(), "tab=fleets&q=soc");
  assert.equal(hasFleetFilters(readFleetFilters(cleared)), false);
  assert.equal(hasFleetFilters(filters), true);
});

test("picker options are distinct and sorted by label", () => {
  const options = distinctOptions(fleets, (f) => ({ value: f.workspace_id, label: f.workspace_name }));
  assert.deepEqual(options, [
    { value: "w1", label: "Ops" },
    { value: "w2", label: "SOC" },
  ]);
  const orchestrators = distinctOptions(fleets, (f) => ({ value: f.orchestrator_agent_id, label: null }));
  assert.deepEqual(orchestrators.map((o) => o.value), ["a1", "a2"]);
});
