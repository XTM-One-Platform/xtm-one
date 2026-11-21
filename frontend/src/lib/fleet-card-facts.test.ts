/**
 * The facts of a card of the Agentic Flow > Fleets list - `yarn test:unit` (vitest).
 * Issue #9977 S2: members, what starts it, modules (kept from #9941), entry
 * points, the agents in no fleet and the sorts kept in the URL.
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import {
  agentsNotInAFleet,
  cardStartsAndModules,
  fleetEntryPoints,
  fleetModules,
  fleetStarts,
  moduleDisplayName,
  membersByFleet,
  readFleetSort,
  sortFleets,
  type FleetCardAgent,
  type FleetCardAssignment,
} from "./fleet-card-facts";

const run = (
  agent_id: string,
  enabled: boolean,
  trigger_type = "manual",
  trigger_config: FleetCardAssignment["trigger_config"] = null,
): FleetCardAssignment => ({ agent_id, enabled, trigger_type, trigger_config });

test("the members of a fleet are the agents whose home fleet it is", () => {
  const agents: FleetCardAgent[] = [
    { id: "a", fleet: { fleet_id: "f1" } },
    { id: "b", fleet: { fleet_id: "f1" } },
    { id: "c", fleet: { fleet_id: "f2" } },
    { id: "d", fleet: null },
  ];
  const members = membersByFleet(agents);
  assert.deepEqual(members.get("f1"), ["a", "b"]);
  assert.deepEqual(members.get("f2"), ["c"]);
  assert.equal(members.has("none"), false);
});

test("what starts a fleet counts the automations on when any is on, distinct schedules, no manual", () => {
  const all = [
    run("a", true, "integration_event", { provider: "opencti" }),
    run("a", true, "schedule", { cron: "0 6 * * *" }),
    run("b", true, "schedule", { cron: "0 6 * * *" }),
    run("b", true, "schedule", { cron: "0 7 * * 1" }),
    run("b", false, "schedule", { cron: "0 8 * * *" }),
    run("b", true, "manual"),
    run("z", true, "webhook"),
  ];
  assert.deepEqual(fleetStarts(["a", "b"], all), { events: true, schedules: 2 });
});

test("the Platform Health fleet reads its Settings schedules and covered modules, not its manual assignments (#974)", () => {
  const members = fleetStarts(["h"], [run("h", false, "manual")]);
  assert.deepEqual(members, { events: false, schedules: 0 });
  const health = { events: true, schedules: 4, modules: ["xtm_one", "opencti"] };
  assert.deepEqual(cardStartsAndModules(health, members, []), {
    starts: { events: true, schedules: 4 },
    modules: ["xtm_one", "opencti"],
  });
  // Copilot mode: no module line for the health fleet.
  assert.deepEqual(cardStartsAndModules({ events: false, schedules: 0, modules: [] }, members, ["jira"]).modules, []);
  // Every other fleet keeps its members' facts.
  const own = { events: true, schedules: 2 };
  assert.deepEqual(cardStartsAndModules(null, own, ["jira"]), { starts: own, modules: ["jira"] });
  assert.equal(moduleDisplayName("xtm_one"), "XTM One");
});

test("a fleet with every automation off says what would start it once set up", () => {
  const all = [run("a", false, "schedule", { cron: "0 6 * * *" }), run("a", false, "work_item")];
  assert.deepEqual(fleetStarts(["a"], all), { events: true, schedules: 1 });
  assert.deepEqual(fleetStarts(["a"], [run("a", false, "manual")]), { events: false, schedules: 0 });
});

test("the modules count bindings, per-user templates, element bindings and event providers, deduplicated", () => {
  const agents = new Map<string, FleetCardAgent>([
    ["a", { id: "a", integration_bindings: { opencti: ["i1"], "opencti#2": ["i2"], github: [] } }],
    ["b", { id: "b", integration_template_bindings: ["opencrq"], integration_element_bindings: { "jira#2": [{}] } }],
  ]);
  const all = [run("b", false, "integration_event", { provider: "openaev#2" }), run("z", true, "integration_event", { provider: "slack" })];
  assert.deepEqual(fleetModules(["a", "b", "ghost"], agents, all), ["jira", "openaev", "opencrq", "opencti"]);
});

test("a module shows the integration's display name, never its internal id (UX r1 B1 on #10010)", () => {
  assert.equal(moduleDisplayName("mitre_atlas"), "MITRE ATLAS");
  assert.equal(moduleDisplayName("recorded_future"), "Recorded Future");
  assert.equal(moduleDisplayName("onepassword"), "1Password");
  assert.equal(moduleDisplayName("some_new_vendor"), "some new vendor");
  const agents = new Map([["a", { id: "a", integration_template_bindings: ["onepassword", "mitre_atlas", "recorded_future"] }]]);
  assert.deepEqual(fleetModules(["a"], agents), ["onepassword", "mitre_atlas", "recorded_future"]);
});

test("an entry point hands over to an exposed agent of a fleet it is not a member of", () => {
  const fleets = [
    { id: "f1", exposed_agent_ids: ["o1"] },
    { id: "f2", exposed_agent_ids: ["o2"] },
    { id: "f3", exposed_agent_ids: ["o3"] },
  ];
  const agents: FleetCardAgent[] = [
    { id: "ctem", name: "CTEM Assistant", handover_agent_ids: ["o1", "o2"] },
    { id: "soc", name: "SOC Copilot", handover_agent_ids: ["o3", "nobody"] },
    { id: "o1", name: "Lead", fleet: { fleet_id: "f1" }, handover_agent_ids: ["o1"] },
    { id: "quiet", name: "Quiet", handover_agent_ids: [] },
  ];
  assert.deepEqual(fleetEntryPoints(fleets, agents), [
    { agentId: "ctem", name: "CTEM Assistant", fleetIds: ["f1", "f2"] },
    { agentId: "soc", name: "SOC Copilot", fleetIds: ["f3"] },
  ]);
});

test("the agents not in a fleet are the ones with no home fleet", () => {
  const agents: FleetCardAgent[] = [{ id: "a", fleet: { fleet_id: "f1" } }, { id: "b", fleet: null }, { id: "c" }];
  assert.deepEqual(agentsNotInAFleet(agents).map((a) => a.id), ["b", "c"]);
});

test("the sorts: name, status On first, last run newest first with never run last; an unknown sort is the name", () => {
  const fleets = [
    { name: "Bravo", last_run_at: "2026-11-01T00:00:00Z", status: "paused" },
    { name: "Alpha", last_run_at: null, status: "not_set_up" },
    { name: "Charlie", last_run_at: "2026-11-07T00:00:00Z", status: "on" },
  ];
  const statusOf = (f: (typeof fleets)[number]) => f.status;
  assert.deepEqual(sortFleets(fleets, "name", statusOf).map((f) => f.name), ["Alpha", "Bravo", "Charlie"]);
  assert.deepEqual(sortFleets(fleets, "status", statusOf).map((f) => f.name), ["Charlie", "Bravo", "Alpha"]);
  assert.deepEqual(sortFleets(fleets, "last_run", statusOf).map((f) => f.name), ["Charlie", "Bravo", "Alpha"]);
  assert.equal(readFleetSort("status"), "status");
  assert.equal(readFleetSort("bogus"), "name");
  assert.equal(readFleetSort(null), "name");
});

test("the value sort (#9836 R6c): highest net value first, a fleet without a figure last, ties by name", () => {
  const fleets = [
    { name: "Delta", last_run_at: null, net: null },
    { name: "Bravo", last_run_at: null, net: 120 },
    { name: "Alpha", last_run_at: null, net: -40 },
    { name: "Charlie", last_run_at: null, net: 120 },
    { name: "Echo", last_run_at: null, net: null },
  ];
  const sorted = sortFleets(fleets, "value", () => null, (f) => f.net);
  assert.deepEqual(sorted.map((f) => f.name), ["Bravo", "Charlie", "Alpha", "Delta", "Echo"]);
  assert.equal(readFleetSort("value"), "value");
  // Without a value reader the sort is the name.
  assert.deepEqual(sortFleets(fleets, "value", () => null).map((f) => f.name), ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]);
});
