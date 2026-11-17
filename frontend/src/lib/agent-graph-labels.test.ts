/**
 * Names and badges of the agent graph's nodes - `yarn test:unit` (vitest).
 */
import { test } from "vitest";
import assert from "node:assert/strict";

import { badgeAngles } from "./agent-graph-layout";
import {
  BADGE_GAP,
  ROW_GAP_PX,
  agentLabel,
  agentLabelLines,
  agentMatchesQuery,
  badgeCentre,
  distinctLabels,
  ghostGroupLine,
  nodeLabel,
  nodeLabels,
  topBelow,
} from "./agent-graph-labels";

const DSO = { id: "dso", name: "Digital Security Office" };

test("distinctLabels drops the leading words two names share, never making two names equal (#10147 UX r4 B1)", () => {
  const corp = new Map([
    ["hub", "Chief of Staff orchestrator"],
    ["a", "Lab sample corporate assistant"],
    ["d", "Lab sample corporate developer"],
    ["r", "Lab sample corporate design review"],
    ["s", "security watch"],
  ]);
  const out = distinctLabels(corp);
  assert.deepEqual(Object.fromEntries(out), {
    hub: "Chief of Staff orchestrator",
    a: "assistant",
    d: "developer",
    r: "design review",
    s: "security watch",
  });
  // No two nodes draw the same name.
  assert.equal(new Set(out.values()).size, corp.size);
  // A shortened name equal to another node's name keeps its whole form.
  const clash = distinctLabels(new Map([["x", "Lab watch"], ["y", "Lab sample"], ["z", "watch"]]));
  assert.equal(clash.get("x"), "Lab watch");
  assert.equal(new Set(clash.values()).size, 3);
  // One word is always kept, and a lone name is untouched.
  assert.equal(distinctLabels(new Map([["x", "Lab"], ["y", "Lab sample"]])).get("x"), "Lab");
  assert.equal(distinctLabels(new Map([["x", "Lab sample one"]])).get("x"), "Lab sample one");
});
const CEO = { id: "ceo", name: "CEO Office" };

test("nodeLabel: an own-fleet node drops its office, another fleet's node and a lone agent keep the full name", () => {
  // 1. Own fleet, bracket prefix.
  assert.equal(nodeLabel("[Digital Security Office] Compliance Orchestrator", DSO, "dso").label, "Compliance Orchestrator");
  assert.equal(nodeLabel("[Digital Security Office] Assistant", DSO, "dso").label, "Assistant");
  // 1b. Own fleet, no bracket, the name starts with the fleet's name and a space.
  assert.equal(nodeLabel("CEO Office Analyst", CEO, "ceo").label, "Analyst");
  assert.equal(nodeLabel("ceo office  Mail Secretary", CEO, "ceo").label, "Mail Secretary");
  // Not a word boundary, or the name is the fleet name alone: unchanged.
  assert.equal(nodeLabel("CEO Officer", CEO, "ceo").label, "CEO Officer");
  assert.equal(nodeLabel("CEO Office", CEO, "ceo").label, "CEO Office");
  // 2. Another fleet's node keeps the full name without brackets.
  assert.equal(nodeLabel("[CEO Office] Assistant", CEO, "dso").label, "CEO Office Assistant");
  assert.equal(nodeLabel("[Digital Security Office] Assistant", DSO, "ceo").label, "Digital Security Office Assistant");
  // 3. No viewed fleet (the map, vis=all): full name.
  assert.equal(nodeLabel("[Digital Security Office] Assistant", DSO, null).label, "Digital Security Office Assistant");
  // 5. A short name another node already draws: full name.
  assert.equal(nodeLabel("[Digital Security Office] Assistant", DSO, "dso", new Set(["Assistant"])).label, "Digital Security Office Assistant");
  // 6. A lone agent keeps its name.
  assert.equal(nodeLabel("[Digital Security Office] Assistant", null, "dso").label, "Digital Security Office Assistant");
  assert.equal(nodeLabel("Triage", null, "dso").label, "Triage");
});

test("an own-fleet role too long for one line wraps on two instead of being cut", () => {
  const evidence = nodeLabel("[Digital Security Office] Evidence Collection & Upload Agent", DSO, "dso");
  assert.equal(evidence.own, true);
  assert.deepEqual(agentLabelLines(evidence, 23), ["Evidence Collection &", "Upload Agent"]);
  assert.deepEqual(agentLabelLines(nodeLabel("[Digital Security Office] Assistant", DSO, "dso"), 23), ["Assistant"]);
  assert.equal(nodeLabel("[CEO Office] Assistant", CEO, "dso").own, undefined);
});

test("nodeLabels: the Digital Security Office fleet view reads its roles, the connected CEO Office node its full name", () => {
  const roles = [
    "Assistant", "Compliance Orchestrator", "CSIRT Orchestrator", "Alert Triage Agent", "Incident Response Agent",
    "Threat Intelligence Agent", "Personnel Compliance Agent", "Policy & Governance Agent",
    "Evidence Collection & Upload Agent", "Risk & Vendor Risk Agent", "SharePoint Domain Monitor",
  ];
  const nodes = [
    ...roles.map((r, i) => ({ id: `d${i}`, name: `[Digital Security Office] ${r}`, fleet: DSO })),
    { id: "c0", name: "[CEO Office] Assistant", fleet: CEO },
    { id: "lone", name: "Triage" },
  ];
  const own = nodeLabels(nodes, "dso");
  assert.deepEqual(roles.map((_, i) => own.get(`d${i}`)?.label), roles);
  assert.equal(own.get("c0")?.label, "CEO Office Assistant");
  assert.equal(own.get("lone")?.label, "Triage");
  const labels = [...own.values()].map((l) => l.label);
  assert.equal(new Set(labels).size, labels.length);
  // The CEO Office fleet view, the other way round.
  const ceo = nodeLabels(
    [
      ...["Assistant", "Analyst", "Mail Secretary", "Governance Clerk", "Communications", "Developer", "UX Director"].map((r, i) => ({ id: `c${i}`, name: `[CEO Office] ${r}`, fleet: CEO })),
      { id: "d0", name: "[Digital Security Office] Assistant", fleet: DSO },
    ],
    "ceo",
  );
  assert.equal(ceo.get("c0")?.label, "Assistant");
  assert.equal(ceo.get("c6")?.label, "UX Director");
  assert.equal(ceo.get("d0")?.label, "Digital Security Office Assistant");
  // vis=all: every node full, as #8394 draws it.
  const all = nodeLabels(nodes, null);
  assert.equal(all.get("d0")?.label, "Digital Security Office Assistant");
  assert.equal(all.get("c0")?.label, "CEO Office Assistant");
});

test("nodeLabels: a short name that would collide draws the full name on every node it meets", () => {
  // Two own agents with the same role after stripping (bracket and plain prefix).
  const twins = nodeLabels(
    [
      { id: "a", name: "[CEO Office] Analyst", fleet: CEO },
      { id: "b", name: "CEO Office Analyst", fleet: CEO },
      { id: "c", name: "[CEO Office] Developer", fleet: CEO },
    ],
    "ceo",
  );
  assert.equal(twins.get("a")?.label, "CEO Office Analyst");
  assert.equal(twins.get("b")?.label, "CEO Office Analyst");
  assert.equal(twins.get("c")?.label, "Developer");
  // An own short name equal to another node's label: the own node draws full.
  const clash = nodeLabels(
    [
      { id: "a", name: "[CEO Office] Triage", fleet: CEO },
      { id: "lone", name: "Triage" },
    ],
    "ceo",
  );
  assert.equal(clash.get("a")?.label, "CEO Office Triage");
  assert.equal(clash.get("lone")?.label, "Triage");
  // A short name dropped draws full and can meet another short name: that one draws full too.
  const chain = nodeLabels(
    [
      { id: "a", name: "[X] Y", fleet: { id: "x", name: "X" } },
      { id: "b", name: "Y" },
      { id: "c", name: "[X] X Y", fleet: { id: "x", name: "X" } },
    ],
    "x",
  );
  const drawn = [...chain.values()].map((l) => l.label);
  assert.equal(new Set(drawn).size, drawn.length);
  assert.equal(chain.get("b")?.label, "Y");
});

test("agentLabel folds a bracketed office prefix in without its brackets", () => {
  // Prefix with brackets.
  assert.deepEqual(agentLabel("[Digital Security Office] Orchestrator"), {
    prefix: "Digital Security Office",
    base: "Orchestrator",
    label: "Digital Security Office Orchestrator",
  });
  assert.equal(agentLabel("[Support Desk]   Triage  bot").label, "Support Desk Triage bot");
  // A prefix without brackets is just a name: drawn whole, as stored.
  assert.deepEqual(agentLabel("CEO Office Analyst"), { prefix: "", base: "CEO Office Analyst", label: "CEO Office Analyst" });
  // No prefix, or nothing after it: unchanged.
  assert.equal(agentLabel("Triage").label, "Triage");
  assert.equal(agentLabel("[Support]").label, "[Support]");
});

test("two fleets' agents of the same base name never draw the same name; one fleet's members stay apart", () => {
  const offices = ["CEO Office", "Digital Security Office", "Product Management Office", "Legal Office", "Finance Office", "Sales Office"];
  const orchestrators = offices.map((o) => agentLabel(`[${o}] Orchestrator`).label);
  assert.equal(new Set(orchestrators).size, offices.length);
  // Same fleet, different roles.
  assert.notEqual(agentLabel("[CEO Office] Orchestrator").label, agentLabel("[CEO Office] Analyst").label);
  // Cut to a small room on two lines, the role still tells them apart.
  for (const chars of [8, 12, 16, 20]) {
    const cut = ["Orchestrator", "Analyst", "Developer"].map((r) => agentLabelLines(agentLabel(`[Digital Security Office] ${r}`), chars).join("/"));
    assert.equal(new Set(cut).size, 3, `at ${chars} chars: ${cut.join(", ")}`);
  }
});

test("agentLabelLines wraps a name that fits and keeps the role on the last line when it does not", () => {
  const dso = agentLabel("[Digital Security Office] Orchestrator");
  // Room for the whole name on two lines: wrapped at spaces, nothing cut.
  assert.deepEqual(agentLabelLines(dso, 24), ["Digital Security Office", "Orchestrator"]);
  assert.deepEqual(agentLabelLines(dso, 40), ["Digital Security Office Orchestrator"]);
  // Too small: the office cut with an ellipsis, the role whole.
  const small = agentLabelLines(dso, 13);
  assert.equal(small.length, 2);
  assert.equal(small[1], "Orchestrator");
  assert.ok(small[0].endsWith("\u2026"));
  for (const line of small) assert.ok([...line].length <= 13);
  // No prefix: wrapLabel as for any long name.
  assert.deepEqual(agentLabelLines(agentLabel("Triage"), 10), ["Triage"]);
  assert.deepEqual(agentLabelLines(dso, 0), []);
});

test("ghostGroupLine leaves out a home group the drawn name already says", () => {
  assert.equal(ghostGroupLine(agentLabel("[Digital Security Office] Orchestrator"), "digital  security office"), "");
  assert.equal(ghostGroupLine(agentLabel("[CEO Office] Analyst"), "Support"), "Support");
  assert.equal(ghostGroupLine(agentLabel("Analyst"), "Support"), "Support");
});

test("agentMatchesQuery finds an agent by office, by role and by fleet", () => {
  const name = "[Digital Security Office] Orchestrator";
  assert.ok(agentMatchesQuery("", name));
  assert.ok(agentMatchesQuery("digital security office", name));
  assert.ok(agentMatchesQuery("Orchestrator", name));
  assert.ok(agentMatchesQuery("security orchestrator", name));
  assert.ok(agentMatchesQuery("Security Office Orch", name));
  assert.ok(!agentMatchesQuery("CEO", name));
  // The fleet's name finds an agent whose own name does not carry it.
  assert.ok(agentMatchesQuery("CEO Office", "Analyst", "CEO Office"));
  assert.ok(!agentMatchesQuery("Legal", "Analyst", "CEO Office"));
});

test("badgeCentre stands a badge off the outer ring by the gap", () => {
  const p = badgeCentre({ x: 0, y: 0 }, 13, 0, 5, 5);
  assert.equal(p.y, 0);
  assert.ok(Math.abs(p.x - (13 + BADGE_GAP + 5)) < 1e-9);
});

test("no badge of a node covers its name, whatever the edges leave free", () => {
  const node = { x: 100, y: 100 };
  const r = 8;
  const ring = r + 3.5;
  // A wide name under the node, as drawn: its top at r + 3 under the centre.
  const nameSize = 9;
  const name: { x: number; y: number; w: number; h: number } = { x: 100, y: 100 + r + 3 + nameSize * 0.6, w: 140, h: nameSize * 1.2 };
  // Edges on every upper direction push the badges to the lower ones.
  const edgeAngles = [-Math.PI / 4, (-3 * Math.PI) / 4, -Math.PI / 2];
  for (const a of badgeAngles(edgeAngles, 4)) {
    const c = badgeCentre(node, ring, a, 12, 4, [name]);
    const overlapX = Math.abs(c.x - name.x) * 2 < 24 + name.w;
    const overlapY = Math.abs(c.y - name.y) * 2 < 8 + name.h;
    assert.ok(!(overlapX && overlapY), `badge at ${a.toFixed(2)} covers the name`);
    // And it stays outside the ring.
    assert.ok(Math.hypot(c.x - node.x, c.y - node.y) >= ring + BADGE_GAP);
  }
});

test("a row under a node starts below its name and ghost line, never over them", () => {
  // Nothing reaches the default top: it is kept.
  assert.equal(topBelow(30, [{ x: 0, y: 15, w: 40, h: 10 }]), 30);
  // A tall name at a fitted zoom (200 units wide, its bottom at 34) and a ghost line under it.
  const name = { x: 0, y: 25, w: 200, h: 18 };
  const sub = { x: 0, y: 40, w: 120, h: 6 };
  const top = topBelow(21, [name, sub]);
  assert.equal(top, 43 + BADGE_GAP);
  for (const q of [name, sub]) assert.ok(top >= q.y + q.h / 2 + BADGE_GAP);
  assert.equal(topBelow(21, []), 21);
});

test("a row under a node keeps at least ROW_GAP_PX on screen below the name at a small zoom", () => {
  const name = { x: 0, y: 25, w: 200, h: 18 };
  const bottom = name.y + name.h / 2;
  // A fitted map at 1024 px: well under one screen px per drawing unit.
  for (const pxPerUnit of [0.2, 0.5, 0.9]) {
    const top = topBelow(0, [name], pxPerUnit);
    assert.ok((top - bottom) * pxPerUnit >= ROW_GAP_PX - 1e-9, `gap under 2 px at ${pxPerUnit} px per unit`);
  }
  // Zoomed in, the drawing-unit gap already gives more than 2 px: unchanged.
  assert.equal(topBelow(0, [name], 4), bottom + BADGE_GAP);
  // No zoom given: the drawing-unit gap, as before.
  assert.equal(topBelow(0, [name]), bottom + BADGE_GAP);
});

test("a badge clears a name far wider than the node, as at a fitted zoom", () => {
  const node = { x: 0, y: 0 };
  const r = 6;
  const ring = r + 1;
  // At a small zoom the name is drawn about 200 units wide under a node of radius 6.
  const nameH = 14;
  const name = { x: 0, y: r + 3 + nameH / 2, w: 200, h: nameH };
  const sub = { x: 0, y: name.y + nameH, w: 120, h: 12 };
  for (let i = 0; i < 64; i++) {
    const a = (i / 64) * 2 * Math.PI;
    const c = badgeCentre(node, ring, a, 7, 5, [name, sub]);
    for (const q of [name, sub]) {
      const clearX = Math.abs(c.x - q.x) - (q.w / 2 + 7);
      const clearY = Math.abs(c.y - q.y) - (q.h / 2 + 5);
      assert.ok(Math.max(clearX, clearY) >= BADGE_GAP - 1e-9, `badge at ${a.toFixed(2)} sits on a label`);
    }
    assert.ok(Math.hypot(c.x, c.y) >= ring + BADGE_GAP);
  }
});
