/**
 * The hubs read their cards from the resolved menu (#9301 N2c, UX r1 B3):
 * for every role, mode, flag set and AI state, each hub's cards are exactly
 * the visible children of its group, in the same order and with the same
 * lock, and a hub's own data covers every child (a child with no card data
 * throws in `hubCards`, so a page added to the menu fails here until its hub
 * knows it).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { resolveNav, type NavFlags } from "./nav-model.ts";
import { findNavGroup, hubCards, hubPages } from "./nav-hub.ts";

const HUBS: Record<string, string> = {
  agents: "../../pages/AgentsHub.tsx",
  work: "../../pages/Work.tsx",
  "agent-studio": "../../pages/AgentStudio.tsx",
  "tools-and-integrations": "../../pages/ToolsAndIntegrations.tsx",
};

/** The routes a hub file keys its card data by: `"/route": {` or `"/route": null`. */
const keyedRoutes = (file: string): { cards: string[]; none: string[] } => {
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  return {
    cards: [...source.matchAll(/^\s+"(\/[a-z0-9/-]*)": \{/gm)].map((m) => m[1]),
    none: [...source.matchAll(/^\s+"(\/[a-z0-9/-]*)": null,/gm)].map((m) => m[1]),
  };
};

const flagSets = (): NavFlags[] => {
  const out: NavFlags[] = [];
  for (const on of [true, false]) {
    for (const xtmOne of [false, true]) {
      for (const [aiEnabled, aiLicensed] of [[true, true], [false, true], [true, false], [false, false]]) {
        out.push({
          chat: on, customVariables: on, relays: on, xtmOne, aiAdoption: on, aiEnabled, aiLicensed,
          deepInvestigation: on, videoStudio: on, dashboardStudio: on, evaluation: on, health: on, intents: on, ctemCases: xtmOne,
        });
      }
    }
  }
  return out;
};

const ROLES: [boolean, boolean][] = [[true, false], [false, true], [false, false]];

test("each hub keys card data for every child its group can show, and no route the group does not hold", () => {
  const all: NavFlags = flagSets()[0];
  for (const [id, file] of Object.entries(HUBS)) {
    const group = findNavGroup(resolveNav(all, true), id);
    assert.ok(group, `${id} is a group with every flag on`);
    assert.equal(group.to !== undefined, true, `${id} opens a hub`);
    const { cards, none } = keyedRoutes(file);
    assert.deepEqual([...cards, ...none].sort(), hubPages(group).map((child) => child.to).sort(), `${id} hub keys`);
  }
});

test("for every role, mode, flag set and AI state, a hub's cards are its group's visible children with their locks", () => {
  let checked = 0;
  for (const flags of flagSets()) {
    for (const [isAdmin, isGroupManager] of ROLES) {
      const sections = resolveNav(flags, isAdmin, isGroupManager);
      for (const [id, file] of Object.entries(HUBS)) {
        const group = findNavGroup(sections, id);
        const { cards, none } = keyedRoutes(file);
        const data = Object.fromEntries([
          ...cards.map((to) => [to, { description: to, items: [] }]),
          ...none.map((to) => [to, null]),
        ]);
        const resources = hubCards(group, data);
        const expected = (group ? hubPages(group) : []).filter((child) => !none.includes(child.to));
        assert.deepEqual(
          resources.map((card) => [card.to, card.locked ? card.lockReason : null]),
          expected.map((child) => [child.to, child.locked ? child.lockReason : null]),
          `${id}, admin ${isAdmin}, group manager ${isGroupManager}, ${JSON.stringify(flags)}`,
        );
        checked += 1;
      }
    }
  }
  assert.equal(checked, flagSets().length * ROLES.length * Object.keys(HUBS).length);
});

test("a visible child without card data fails loudly, never a silent missing card", () => {
  const group = {
    kind: "group" as const, id: "x", label: "X", icon: "bot" as const,
    children: [{ kind: "leaf" as const, to: "/a", label: "A", icon: "bot" as const }],
  };
  assert.throws(() => hubCards(group, {}), /hub "x" has no card data for its child \/a/);
  assert.deepEqual(hubCards(group, { "/a": null }), []);
});

test("Tools & Integrations keeps one card per connection page: the Connections family is flattened (#9301 UX r1 B5)", () => {
  const base = flagSets()[0];
  const routes = (flags: NavFlags) => {
    const group = findNavGroup(resolveNav(flags, false), "tools-and-integrations");
    assert.ok(group);
    return hubPages(group).map((page) => page.to);
  };
  assert.deepEqual(routes(base), ["/mcp-servers", "/tools", "/integrations", "/connections", "/connections/variables", "/connections/relays"]);
  assert.deepEqual(routes({ ...base, customVariables: false, relays: false }), ["/mcp-servers", "/tools", "/integrations", "/connections"]);
});

test("the Agents hub: non-admins get no Intents card, Community Edition reads Fleets unlicensed", () => {
  const base = flagSets()[0];
  const cards = (flags: NavFlags, isAdmin: boolean) => {
    const { cards: keyed } = keyedRoutes(HUBS.agents);
    const data = Object.fromEntries(keyed.map((to) => [to, { description: to, items: [] }]));
    return hubCards(findNavGroup(resolveNav(flags, isAdmin), "agents"), data);
  };
  assert.deepEqual(cards(base, true).map((card) => card.to), ["/agents", "/fleets", "/flows", "/intents"]);
  assert.deepEqual(cards(base, false).map((card) => card.to), ["/agents", "/fleets", "/flows"]);
  const ce = cards({ ...base, aiLicensed: false }, false);
  assert.deepEqual(ce.map((card) => [card.to, card.lockReason ?? null]), [
    ["/agents", "unlicensed"], ["/fleets", "unlicensed"], ["/flows", null],
  ]);
});
