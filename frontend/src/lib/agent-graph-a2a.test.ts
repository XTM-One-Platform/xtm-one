import { test } from "vitest";
import assert from "node:assert/strict";

import type { Agent } from "@/types";
import { HANDOVER_TYPE_CONFIG } from "./constants";
import { contrastOnCanvas, DARK_CANVAS, EDGE_MIN_CONTRAST, EDGE_REST_OPACITY, edgeInk, LIGHT_CANVAS } from "./agent-graph-ink";
import {
  A2A_EDGE_CONFIG,
  a2aEdgeConfig,
  a2aPartnerDetail,
  a2aPartnerPath,
  a2aPartnerSubLabel,
  withA2ALiaisons,
  type A2ALiaisons,
} from "./agent-graph-a2a";

function agent(id: string, handovers: string[] = []): Agent {
  return { id, name: id, enabled: true, is_company_managed: false, icon: "Bot", handover_agent_ids: handovers, handover_types: {}, handover_conditions: {} } as unknown as Agent;
}

const LIAISONS: A2ALiaisons = {
  partners: [
    { id: "a2a:in", name: "Partner intake", integration_id: "in", integration_name: "Partner intake", provider: "oauth_a2a", direction: "inbound", card_urls: [] },
    { id: "a2a:out:c1", name: "Triage", integration_id: "out", integration_name: "Partner desk", provider: "oauth_a2a", direction: "outbound", card_urls: ["https://p.example/a"] },
    { id: "a2a:out:c2", name: "Hunt", integration_id: "out", integration_name: "Partner desk", provider: "oauth_a2a", direction: "outbound", card_urls: ["https://p.example/b"] },
  ],
  edges: [
    { source_id: "a2a:in", target_id: "intake", direction: "inbound", integration_id: "in" },
    { source_id: "a2a:in", target_id: "other", direction: "inbound", integration_id: "in" },
    { source_id: "desk", target_id: "a2a:out:c1", direction: "outbound", integration_id: "out" },
    { source_id: "desk", target_id: "a2a:out:c2", direction: "outbound", integration_id: "out" },
  ],
};

test("no liaison leaves the graph as it is", () => {
  const agents = [agent("a", ["b"]), agent("b")];
  for (const liaisons of [null, undefined, { partners: [], edges: [] }]) {
    const out = withA2ALiaisons(agents, liaisons);
    assert.deepEqual(out.agents, agents);
    assert.equal(out.partners.size, 0);
    assert.equal(out.edgeTypes.size, 0);
  }
});

test("an inbound partner tasks the agent: partner -> agent", () => {
  const out = withA2ALiaisons([agent("intake")], LIAISONS);
  assert.deepEqual([...out.partners.keys()], ["a2a:in"]);
  const partner = out.agents.find((a) => a.id === "a2a:in");
  assert.deepEqual(partner?.handover_agent_ids, ["intake"]);
  assert.equal(out.edgeTypes.get("a2a:in->intake"), "a2a_inbound");
});

test("an outbound agent delegates to each partner card: agent -> partner", () => {
  const desk = agent("desk", ["intake"]);
  const out = withA2ALiaisons([desk, agent("intake")], LIAISONS);
  const drawnDesk = out.agents.find((a) => a.id === "desk");
  // The handover stays first and unchanged; each card is its own node.
  assert.deepEqual(drawnDesk?.handover_agent_ids, ["intake", "a2a:out:c1", "a2a:out:c2"]);
  assert.equal(out.edgeTypes.get("desk->a2a:out:c1"), "a2a_outbound");
  assert.equal(out.edgeTypes.get("desk->a2a:out:c2"), "a2a_outbound");
  assert.equal(out.edgeTypes.has("desk->intake"), false);
  assert.deepEqual(desk.handover_agent_ids, ["intake"], "the input is never mutated");
});

test("one partner fans out to every drawn agent, a liaison of an agent not drawn is left out", () => {
  const both = withA2ALiaisons([agent("intake"), agent("other")], LIAISONS);
  assert.deepEqual(both.agents.find((a) => a.id === "a2a:in")?.handover_agent_ids, ["intake", "other"]);
  const one = withA2ALiaisons([agent("other")], LIAISONS);
  assert.deepEqual(one.agents.find((a) => a.id === "a2a:in")?.handover_agent_ids, ["other"]);
  const none = withA2ALiaisons([agent("elsewhere")], LIAISONS);
  assert.equal(none.partners.size, 0);
  assert.equal(none.agents.length, 1);
});

test("a liaison follows its agent onto a collapsed fleet node", () => {
  const out = withA2ALiaisons([agent("fleet:f")], LIAISONS, (id) => (id === "intake" ? "fleet:f" : id));
  assert.equal(out.edgeTypes.get("a2a:in->fleet:f"), "a2a_inbound");
});

test("a liaison edge is drawn in its own pattern and keeps 3:1 on both canvases", () => {
  const cfg = A2A_EDGE_CONFIG.a2a_inbound;
  for (const handover of Object.values(HANDOVER_TYPE_CONFIG)) assert.notEqual(cfg.dash, handover.dash);
  assert.ok(contrastOnCanvas(edgeInk(cfg, true), EDGE_REST_OPACITY.light, LIGHT_CANVAS) >= EDGE_MIN_CONTRAST);
  assert.ok(contrastOnCanvas(edgeInk(cfg, false), EDGE_REST_OPACITY.dark, DARK_CANVAS) >= EDGE_MIN_CONTRAST);
  assert.equal(a2aEdgeConfig("delegate"), null);
  assert.equal(a2aEdgeConfig("a2a_outbound")?.label, "Delegates");
});

test("a partner opens its integration", () => {
  assert.equal(a2aPartnerPath(LIAISONS.partners[1]), "/integrations/out");
});

test("a partner's sub-label leads with its direction, so a cut line keeps it", () => {
  assert.match(a2aPartnerSubLabel("inbound"), /^Inbound /);
  assert.match(a2aPartnerSubLabel("outbound"), /^Outbound /);
});

test("a partner's tooltip names its integration and its full card URLs, an inbound one nothing more", () => {
  assert.equal(a2aPartnerDetail(LIAISONS.partners[1]), "Partner desk · https://p.example/a");
  assert.equal(a2aPartnerDetail(LIAISONS.partners[0]), "");
});
