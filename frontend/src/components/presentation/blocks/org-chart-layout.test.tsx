/**
 * The org chart engine and its renderer - `yarn test:dom`.
 *
 * `layoutOrgChart` is pure, so its geometry is what is pinned: the tidy tree
 * on balanced, unbalanced and single-child trees and on a forty-seat one, the
 * bubbles of a hybrid chart, the rings of a radial one, folding and opening,
 * the spine, the bubble scale, the roll-up maths and the self-check the
 * layout check reads. The renderer is snapshot on every orientation and
 * style, in both schemes. Every fixture is synthetic.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { DeckOrgChartBlock, DeckOrgChartNode } from "@/types";

import { OrgChartBlockView } from "./OrgChart";
import {
  ORG,
  bubbleDiameter,
  buildTree,
  countMismatches,
  fitLine,
  initialsOf,
  layoutOrgChart,
  orgNeighbours,
  rollupText,
  ENGLISH_WORDS,
  type OrgChartLayout,
  type OrgNodeLayout,
  type Rect,
} from "./org-chart-layout";
import { segmentIntersectsRect } from "./architecture-layout";

// The icon catalog, loaded natively (see `metrics.test.tsx`).
vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

/** The drawing the backend's Chromium tests extract and audit (`tests/test_presentation_org_chart.py`); vitest runs from `frontend/`. */
const BACKEND_FIXTURE = resolve(process.cwd(), "../backend/tests/data/org_chart/figure.html");

// ── Fixtures (synthetic) ──────────────────────────────────────────────────────

const person = (id: string, name: string, role: string, manager?: string, extra: Partial<DeckOrgChartNode> = {}): DeckOrgChartNode => ({
  id,
  kind: "person",
  name,
  role,
  ...(manager ? { manager } : {}),
  ...extra,
});

const chart = (nodes: DeckOrgChartNode[], extra: Partial<DeckOrgChartBlock> = {}): DeckOrgChartBlock => ({ type: "org_chart", nodes, ...extra });

/** A root over three leads, each over three reports. */
const BALANCED = chart([
  person("alice", "Alice Martin", "Chief Executive Officer"),
  ...["bob", "carol", "dan"].flatMap((lead, i) => [
    person(lead, `${lead[0].toUpperCase()}${lead.slice(1)} Lead`, `Head of Unit ${i + 1}`, "alice"),
    ...[1, 2, 3].map((n) => person(`${lead}-${n}`, `Member ${i + 1}.${n}`, "Engineer", lead)),
  ]),
]);

/** One lead with six reports, one with none, one with one: subtrees of very different widths. */
const UNBALANCED = chart([
  person("alice", "Alice Martin", "Chief Executive Officer"),
  person("bob", "Bob Stone", "Head of Platform", "alice"),
  ...[1, 2, 3, 4, 5, 6].map((n) => person(`bob-${n}`, `Platform ${n}`, "Engineer", "bob")),
  person("carol", "Carol Diaz", "Head of Finance", "alice"),
  person("dan", "Dan Wells", "Head of Sales", "alice"),
  person("dan-1", "Sales One", "Account executive", "dan"),
  person("erin", "Erin Park", "Head of Legal", "alice"),
  ...[1, 2, 3, 4].map((n) => person(`erin-${n}`, `Legal ${n}`, "Counsel", "erin")),
]);

const CHAIN = chart([
  person("a", "Alice Martin", "CEO"),
  person("b", "Bob Stone", "COO", "a"),
  person("c", "Carol Diaz", "Director", "b"),
  person("d", "Dan Wells", "Manager", "c"),
]);

/** Forty seats: a root, three directors, each over three managers of three reports. */
const FORTY = chart([
  person("root", "Alice Martin", "Chief Executive Officer"),
  ...[1, 2, 3].flatMap((d) => [
    person(`d${d}`, `Director ${d}`, "Director", "root"),
    ...[1, 2, 3].flatMap((m) => [
      person(`d${d}m${m}`, `Manager ${d}.${m}`, "Manager", `d${d}`),
      ...[1, 2, 3].map((r) => person(`d${d}m${m}r${r}`, `Report ${d}.${m}.${r}`, "Analyst", `d${d}m${m}`)),
    ]),
  ]),
]);

/** A headcount plan: leads as cards over their teams as bubbles, open and planned seats among them. */
const HEADCOUNT = chart(
  [
    person("vp", "Alice Martin", "VP Engineering"),
    person("plat", "Bob Stone", "Platform lead", "vp"),
    person("apps", "Carol Diaz", "Apps lead", "vp", { dotted_to: ["data"] }),
    person("data", "Dan Wells", "Data lead", "vp"),
    { id: "plat-team", kind: "bubble", role: "Platform", count: 9, manager: "plat" },
    { id: "plat-hires", kind: "planned", role: "Platform engineers", count: 4, period: "H2 FY28", manager: "plat" },
    { id: "apps-team", kind: "bubble", role: "Apps", count: 36, manager: "apps" },
    { id: "apps-open", kind: "vacant", role: "Mobile engineer", manager: "apps" },
    { id: "data-team", kind: "group", role: "Data science", name: "Erin Park", count: 5, manager: "data", chips: ["ML", "BI"] },
    { id: "agency", kind: "external", name: "Northwind Agency", role: "Design partner", count: 3, manager: "data" },
  ],
  { style: "hybrid", show_counts: true, count_mode: "both" },
);

// ── Helpers ───────────────────────────────────────────────────────────────────

const nodeOf = (layout: OrgChartLayout, id: string): OrgNodeLayout => {
  const node = layout.nodes.find((n) => n.id === id);
  if (!node) throw new Error(`no seat ${id}`);
  return node;
};
const cx = (n: OrgNodeLayout) => n.x + n.w / 2;
const cy = (n: OrgNodeLayout) => n.y + n.h / 2;
const meet = (a: Rect, b: Rect) => a.x < b.x + b.w - 0.5 && b.x < a.x + a.w - 0.5 && a.y < b.y + b.h - 0.5 && b.y < a.y + a.h - 0.5;

function expectNoOverlap(layout: OrgChartLayout) {
  for (const [i, a] of layout.nodes.entries()) {
    for (const b of layout.nodes.slice(i + 1)) expect(meet(a.footprint, b.footprint), `${a.id} meets ${b.id}`).toBe(false);
  }
}

function expectNoCrossing(layout: OrgChartLayout) {
  expect(layout.facts.crossings).toEqual([]);
  // And independently of the engine's own check: no line through a seat it does not join.
  for (const link of layout.links) {
    const ends = new Set([link.from, ...link.to]);
    for (const node of layout.nodes) {
      if (ends.has(node.id)) continue;
      const box = { x: node.x + 2, y: node.y + 2, w: node.w - 4, h: node.h - 4 };
      for (const line of link.points) {
        for (let i = 1; i < line.length; i += 1) expect(segmentIntersectsRect(line[i - 1], line[i], box), `${link.key} crosses ${node.id}`).toBe(false);
      }
    }
  }
}

function expectInside(layout: OrgChartLayout) {
  for (const node of layout.nodes) {
    expect(node.footprint.x).toBeGreaterThanOrEqual(0);
    expect(node.footprint.y).toBeGreaterThanOrEqual(0);
    expect(node.footprint.x + node.footprint.w).toBeLessThanOrEqual(layout.width + 0.5);
    expect(node.footprint.y + node.footprint.h).toBeLessThanOrEqual(layout.height + 0.5);
  }
}

const level = (layout: OrgChartLayout, l: number) => layout.nodes.filter((n) => n.level === l).sort((a, b) => a.x - b.x);

// ── The tidy tree ─────────────────────────────────────────────────────────────

describe("a balanced tree", () => {
  const layout = layoutOrgChart(BALANCED);

  test("centres every parent over its children and every level on one line", () => {
    for (const node of layout.nodes) {
      if (!node.childIds.length) continue;
      const kids = node.childIds.map((id) => nodeOf(layout, id));
      expect(cx(node)).toBeCloseTo((cx(kids[0]) + cx(kids[kids.length - 1])) / 2, 5);
    }
    for (let l = 0; l < layout.levels; l += 1) {
      const row = level(layout, l);
      expect(new Set(row.map((n) => Math.round(cy(n) * 10))).size).toBe(1);
      // Equal card width and height per level.
      expect(new Set(row.map((n) => n.w)).size).toBe(1);
      expect(new Set(row.map((n) => n.h)).size).toBe(1);
    }
  });

  test("spaces siblings evenly and draws one bus per parent", () => {
    for (const parent of layout.nodes.filter((n) => n.childIds.length > 1)) {
      const kids = parent.childIds.map((id) => nodeOf(layout, id));
      const gaps = kids.slice(1).map((kid, i) => kid.x - (kids[i].x + kids[i].w));
      expect(Math.max(...gaps) - Math.min(...gaps)).toBeLessThan(0.5);
    }
    const parents = layout.nodes.filter((n) => n.childIds.length).length;
    expect(layout.links.filter((link) => link.kind === "line")).toHaveLength(parents);
    // A bus: the stub, ONE horizontal line, one drop per child - never one elbow per child.
    for (const link of layout.links) {
      const horizontal = link.points.filter((line) => line.length === 2 && Math.abs(line[0].y - line[1].y) < 0.01 && Math.abs(line[0].x - line[1].x) > 0.01);
      expect(horizontal).toHaveLength(1);
      expect(link.points).toHaveLength(2 + link.to.length);
    }
  });

  test("overlaps nothing, crosses nothing, stays on its canvas, and is deterministic", () => {
    expectNoOverlap(layout);
    expectNoCrossing(layout);
    expectInside(layout);
    expect(layoutOrgChart(BALANCED)).toEqual(layout);
    expect(layout.facts.unequal).toEqual([]);
    expect(layout.facts.broken).toEqual([]);
  });

  test("gives the root the core emphasis", () => {
    expect(nodeOf(layout, "alice").emphasis).toBe("core");
    expect(nodeOf(layout, "bob").emphasis).toBe("normal");
  });
});

describe("an unbalanced tree", () => {
  const layout = layoutOrgChart(UNBALANCED);

  test("pushes subtrees apart wherever they meet, and spreads the small ones between the large", () => {
    expectNoOverlap(layout);
    expectNoCrossing(layout);
    const leads = ["bob", "carol", "dan", "erin"].map((id) => nodeOf(layout, id));
    const gaps = leads.slice(1).map((lead, i) => lead.x - (leads[i].x + leads[i].w));
    // Dan's report is pushed clear of Bob's six: Carol (no reports), between them, is spread to the middle of that room...
    expect(gaps[0]).toBeGreaterThan(ORG.SIBLING_GAP + 1);
    expect(Math.abs(gaps[1] - gaps[0])).toBeLessThan(1);
    // ...and Erin's four are pushed clear of Dan's one, never closer than two siblings.
    expect(gaps[2]).toBeGreaterThanOrEqual(ORG.SIBLING_GAP - 0.5);
    expect(cx(nodeOf(layout, "alice"))).toBeCloseTo((cx(leads[0]) + cx(leads[3])) / 2, 5);
  });
});

describe("a single-child chain", () => {
  const layout = layoutOrgChart(CHAIN);

  test("stands every seat on one vertical line, joined by straight drops", () => {
    const x = cx(nodeOf(layout, "a"));
    for (const id of ["b", "c", "d"]) expect(cx(nodeOf(layout, id))).toBeCloseTo(x, 5);
    for (const link of layout.links) {
      expect(link.points).toHaveLength(1);
      expect(link.points[0][0].x).toBeCloseTo(link.points[0][1].x, 5);
    }
  });
});

describe("a forty-seat tree", () => {
  const layout = layoutOrgChart(FORTY);

  test("lays every seat out once, apart, crossing nothing", () => {
    expect(FORTY.nodes).toHaveLength(40);
    expect(layout.nodes).toHaveLength(40);
    expectNoOverlap(layout);
    expectNoCrossing(layout);
    expectInside(layout);
    expect(layout.levels).toBe(4);
  });

  test("folds its leaves into spines when the room is narrower than its widest level", () => {
    const room = { width: 1700, height: 1000 };
    const fitted = layoutOrgChart(FORTY, { room });
    expect(fitted.facts.spine).toBe(true);
    expect(fitted.width).toBeLessThan(layout.width);
    expectNoOverlap(fitted);
    expectNoCrossing(fitted);
    // A spine's cards stand one under the other, under one rail.
    const reports = ["d1m1r1", "d1m1r2", "d1m1r3"].map((id) => nodeOf(fitted, id));
    expect(new Set(reports.map((r) => r.x)).size).toBe(1);
    expect(reports[1].y).toBeGreaterThan(reports[0].y + reports[0].h);
  });

  test("refuses rather than shrink its type under the minimum", () => {
    const refused = layoutOrgChart(FORTY, { room: { width: 400, height: 300 } });
    expect(refused.refused).not.toBeNull();
    expect(refused.refused?.seats).toBe(40);
    expect(refused.facts.refused).toBe(true);
    expect(refused.facts.minType).toBeLessThan(ORG.MIN_TYPE_PX);
  });
});

describe("the spine", () => {
  test("always: every run of leaves in one column; never: none, whatever the room", () => {
    const always = layoutOrgChart({ ...BALANCED, spine: "always" });
    expect(always.facts.spine).toBe(true);
    const kids = ["bob-1", "bob-2", "bob-3"].map((id) => nodeOf(always, id));
    expect(new Set(kids.map((k) => k.x)).size).toBe(1);
    expectNoOverlap(always);
    expectNoCrossing(always);
    const never = layoutOrgChart({ ...FORTY, spine: "never" }, { room: { width: 1700, height: 1000 } });
    expect(never.facts.spine).toBe(false);
    expect(never.facts.wide).not.toBeNull();
  });
});

// ── Mixed kinds ───────────────────────────────────────────────────────────────

describe("cards over bubbles (hybrid)", () => {
  const layout = layoutOrgChart(HEADCOUNT);

  test("draws the people as cards and the teams as circles on the sqrt scale", () => {
    expect(nodeOf(layout, "plat").shape).toBe("card");
    expect(nodeOf(layout, "plat-team").shape).toBe("circle");
    expect(nodeOf(layout, "data-team").shape).toBe("circle");
    const small = nodeOf(layout, "plat-team");
    const large = nodeOf(layout, "apps-team");
    expect(large.w).toBe(ORG.BUBBLE_MAX_D);
    expect(small.w).toBe(bubbleDiameter(9, 36));
    // The area, not the diameter, follows the count.
    expect((small.w / large.w) ** 2).toBeCloseTo(9 / 36, 1);
    expect(large.number?.text).toBe("36");
  });

  test("centres a level's circles on its line and keeps every label off its neighbours", () => {
    const row = level(layout, 2);
    expect(new Set(row.map((n) => Math.round(cy(n) * 10))).size).toBe(1);
    expect(layout.facts.overlaps).toEqual([]);
    expectNoOverlap(layout);
    expectNoCrossing(layout);
  });

  test("routes the dotted line last, around every seat", () => {
    const dotted = layout.links.filter((link) => link.kind === "dotted");
    expect(dotted).toHaveLength(1);
    expect(layout.links[layout.links.length - 1].kind).toBe("dotted");
    expect(dotted[0].from).toBe("data");
    expect(dotted[0].to).toEqual(["apps"]);
    expectNoCrossing(layout);
  });

  test("writes the planned and open seats in their kind's card", () => {
    const planned = nodeOf(layout, "plat-hires");
    expect(planned.main?.full).toBe("+4 Platform engineers");
    expect(planned.badge?.text.text).toBe("H2 FY28");
    expect(planned.tone).toBe("accent");
    const open = nodeOf(layout, "apps-open");
    expect(open.badge?.text.text).toBe(ENGLISH_WORDS.open);
    expect(open.tone).toBe("warning");
    expect(nodeOf(layout, "agency").tone).toBe("muted");
  });

  test("writes each manager's roll-up as today against the target", () => {
    expect(nodeOf(layout, "vp").rollupPill?.text.text).toBe("57 → 62");
    expect(nodeOf(layout, "plat").rollupPill?.text.text).toBe("10 → 14");
  });
});

describe("radial", () => {
  const block = chart(
    [
      person("ceo", "Alice Martin", "Chief Executive Officer"),
      ...["Bob Stone", "Carol Diaz", "Dan Wells", "Erin Park", "Frank Hale", "Grace Lin"].map((name, i) => person(`r${i}`, name, `Executive ${i + 1}`, "ceo")),
    ],
    { orientation: "radial" },
  );
  const layout = layoutOrgChart(block);

  test("puts the root in the middle and its reports on a ring around it", () => {
    const root = nodeOf(layout, "ceo");
    expect(root.shape).toBe("card");
    const ring = layout.nodes.filter((n) => n.level === 1);
    const radii = ring.map((n) => Math.hypot(cx(n) - cx(root), cy(n) - cy(root)));
    expect(Math.max(...radii) - Math.min(...radii)).toBeLessThan(1);
    expect(ring.every((n) => n.shape === "circle")).toBe(true);
  });

  test("keeps every label upright beside its circle, off the other seats", () => {
    expect(layout.facts.overlaps).toEqual([]);
    expectNoOverlap(layout);
    expectNoCrossing(layout);
    expectInside(layout);
    for (const node of layout.nodes.filter((n) => n.level === 1)) {
      // Beside the circle, not on it.
      expect(meet({ x: node.x, y: node.y, w: node.w, h: node.h }, { x: node.main!.x - 1, y: node.main!.y - 1, w: 2, h: 2 })).toBe(false);
    }
  });
});

describe("left to right", () => {
  const layout = layoutOrgChart({ ...BALANCED, orientation: "left_right" });

  test("lays the levels out as columns, each parent centred beside its children", () => {
    for (let l = 0; l < layout.levels; l += 1) expect(new Set(level(layout, l).map((n) => Math.round(cx(n) * 10))).size).toBe(1);
    const alice = nodeOf(layout, "alice");
    const leads = alice.childIds.map((id) => nodeOf(layout, id));
    expect(cy(alice)).toBeCloseTo((cy(leads[0]) + cy(leads[leads.length - 1])) / 2, 5);
    expectNoOverlap(layout);
    expectNoCrossing(layout);
  });
});

// ── Folding ───────────────────────────────────────────────────────────────────

describe("collapse and expand", () => {
  test("collapse_below folds a manager's reports into one card with their count", () => {
    const layout = layoutOrgChart({ ...BALANCED, collapse_below: 2 });
    expect(layout.nodes).toHaveLength(7);
    const fold = nodeOf(layout, "bob~fold");
    expect(fold.kind).toBe("fold");
    expect(fold.main?.text).toBe("3 people");
    expect(fold.sourceId).toBe("bob");
    expect(nodeOf(layout, "bob").foldable).toBe(true);
    expect(layout.facts.folded).toBe(true);
  });

  test("expand keeps a seat open, and the viewer opens one in place", () => {
    const kept = layoutOrgChart({ ...BALANCED, collapse_below: 2, expand: ["carol"] });
    expect(kept.nodes.some((n) => n.id === "carol-1")).toBe(true);
    expect(kept.nodes.some((n) => n.id === "bob-1")).toBe(false);
    const opened = layoutOrgChart({ ...BALANCED, collapse_below: 2 }, { opened: new Set(["bob"]) });
    expect(opened.nodes.some((n) => n.id === "bob-1")).toBe(true);
    expect(nodeOf(opened, "bob").open).toBe(true);
  });

  test("a collapsed team holds its reports in its own count", () => {
    const layout = layoutOrgChart(
      chart([
        person("a", "Alice Martin", "VP"),
        { id: "team", kind: "group", role: "Platform", manager: "a", collapsed: true },
        person("t1", "Bob Stone", "Engineer", "team"),
        person("t2", "Carol Diaz", "Engineer", "team"),
      ]),
    );
    expect(layout.nodes.map((n) => n.id)).toEqual(["a", "team"]);
    expect(nodeOf(layout, "team").badge?.text.text).toBe("2");
  });

  test("depth_limit draws that many levels and says what the last one cuts", () => {
    const layout = layoutOrgChart({ ...BALANCED, depth_limit: 2 });
    expect(layout.levels).toBe(2);
    expect(nodeOf(layout, "bob").badge?.text.text).toBe("+3 below");
  });
});

// ── Numbers ───────────────────────────────────────────────────────────────────

describe("the bubble scale", () => {
  test("is bounded, and its area grows with the count", () => {
    expect(bubbleDiameter(0, 100)).toBe(ORG.BUBBLE_MIN_D);
    expect(bubbleDiameter(1, 10000)).toBe(ORG.BUBBLE_MIN_D);
    expect(bubbleDiameter(100, 100)).toBe(ORG.BUBBLE_MAX_D);
    expect(bubbleDiameter(500, 100)).toBe(ORG.BUBBLE_MAX_D);
    const quarter = bubbleDiameter(25, 100);
    expect(quarter).toBe(Math.round(ORG.BUBBLE_MAX_D / 2));
  });
});

describe("the roll-up", () => {
  test("counts people 1, teams their count, open and planned seats apart", () => {
    const tree = buildTree(HEADCOUNT);
    const vp = tree.byId.get("vp")!.rollup;
    // 4 people + 9 + 36 + 5 + 3 external today; 1 open; 4 planned.
    expect(vp).toEqual({ current: 57, open: 1, planned: 4, target: 62 });
    expect(tree.byId.get("plat")!.rollup).toEqual({ current: 10, open: 0, planned: 4, target: 14 });
    const format = (n: number) => String(n);
    expect(rollupText(vp, "current", ENGLISH_WORDS, format)).toBe("57 · +1 open · +4 planned");
    expect(rollupText(vp, "target", ENGLISH_WORDS, format)).toBe("62");
    expect(rollupText(vp, "both", ENGLISH_WORDS, format)).toBe("57 → 62");
  });

  test("holds a team's declared total to what its reports add up to", () => {
    const tree = buildTree(
      chart([
        person("a", "Alice Martin", "VP"),
        { id: "team", kind: "group", role: "Platform", count: 12, manager: "a" },
        person("t1", "Bob Stone", "Engineer", "team"),
        { id: "t2", kind: "bubble", role: "Core", count: 8, manager: "team" },
      ]),
    );
    expect(countMismatches(tree)).toEqual([{ id: "team", declared: 12, computed: 9 }]);
  });
});

describe("the layout check's facts", () => {
  test("name a planned level with no period, and a word cut inside", () => {
    const layout = layoutOrgChart(
      chart([
        person("a", "Alice Martin", "VP"),
        { id: "p1", kind: "planned", role: "Engineer", manager: "a" },
        { id: "p2", kind: "planned", role: "Designer", manager: "a" },
        person("b", "Bartholomewsonofsomeoneverylongname", "Engineer", "a"),
      ]),
    );
    expect(layout.facts.undated).toEqual([2]);
    expect(layout.facts.broken).toEqual(["b"]);
  });

  test("count what is drawn and whether anything folds", () => {
    const layout = layoutOrgChart(FORTY);
    expect(layout.facts.seats).toBe(40);
    expect(layout.facts.drawn).toBe(40);
    expect(layout.facts.folded).toBe(false);
  });
});

describe("text", () => {
  test("a name stays on one line: cut at a word with an ellipsis, inside a word only when one word is too wide", () => {
    const short = fitLine("Alice Martin", 23, 600, 400);
    expect(short).toMatchObject({ text: "Alice Martin", truncated: false, broken: false });
    const cut = fitLine("Alice Beatrice Martin-Rodriguez", 23, 600, 220);
    expect(cut.truncated).toBe(true);
    expect(cut.broken).toBe(false);
    expect(cut.text.endsWith("…")).toBe(true);
    expect(fitLine("Supercalifragilistic", 23, 600, 80).broken).toBe(true);
    expect(initialsOf("Alice Martin")).toBe("AM");
    expect(initialsOf("Bob")).toBe("BO");
  });
});

describe("keyboard navigation", () => {
  test("walks to the manager, the first report and the neighbours on the level", () => {
    const layout = layoutOrgChart(BALANCED);
    expect(orgNeighbours(layout, "carol")).toEqual({ parent: "alice", firstChild: "carol-1", previous: "bob", next: "dan" });
    expect(orgNeighbours(layout, "bob-3").next).toBe("carol-1");
  });
});

// ── The renderer ──────────────────────────────────────────────────────────────

/** The drawing as markup, condensed to what a reader sees: each seat, each line, the legend. */
function drawn(container: HTMLElement) {
  const svg = container.querySelector("svg.deck-org-svg");
  if (!svg) return { refused: container.querySelector(".deck-org-refused")?.textContent };
  return {
    viewBox: svg.getAttribute("viewBox"),
    seats: Array.from(svg.querySelectorAll(".deck-org-node")).map((g) => ({
      id: g.getAttribute("data-org-node"),
      kind: g.getAttribute("data-kind"),
      shape: g.getAttribute("data-shape"),
      level: g.getAttribute("data-level"),
      tone: g.getAttribute("data-tone") ?? g.getAttribute("data-color"),
      box: (() => {
        const shape = g.querySelector(".deck-org-shape")!;
        return shape.tagName === "circle"
          ? ["circle", shape.getAttribute("cx"), shape.getAttribute("cy"), shape.getAttribute("r")]
          : ["rect", shape.getAttribute("x"), shape.getAttribute("y"), shape.getAttribute("width"), shape.getAttribute("height")];
      })(),
      text: Array.from(g.querySelectorAll("text")).map((t) => t.textContent),
    })),
    lines: Array.from(svg.querySelectorAll(".deck-org-link")).map((p) => `${p.getAttribute("data-kind")} ${p.getAttribute("d")}`),
    legend: Array.from(svg.querySelectorAll(".deck-org-legend-text")).map((t) => t.textContent),
  };
}

const SMALL = chart([
  person("alice", "Alice Martin", "Chief Executive Officer"),
  person("bob", "Bob Stone", "Chief Technology Officer", "alice"),
  { id: "eng", kind: "bubble", role: "Engineering", count: 18, manager: "bob" },
  { id: "hire", kind: "planned", role: "Staff engineer", count: 2, period: "Q3", manager: "bob" },
  person("carol", "Carol Diaz", "Chief Financial Officer", "alice", { dotted_to: ["bob"] }),
  { id: "fin", kind: "group", role: "Finance", name: "Dan Wells", count: 4, manager: "carol" },
  { id: "open", kind: "vacant", role: "Controller", period: "Q4", manager: "carol" },
  { id: "audit", kind: "external", name: "Northwind Audit", role: "Auditor", manager: "carol" },
]);

describe("the rendered chart", () => {
  const orientations = ["top_down", "left_right", "radial"] as const;
  const styles = ["cards", "compact", "bubbles", "hybrid"] as const;
  for (const orientation of orientations) {
    for (const style of styles) {
      test(`draws ${orientation} / ${style} the same in both schemes`, () => {
        const block = { ...SMALL, orientation, style, show_counts: true };
        const renders = (["dark", "light"] as const).map((scheme) => {
          const { container, unmount } = renderWithProviders(
            <div className="deck-root" data-scheme={scheme}>
              <OrgChartBlockView block={block} />
            </div>,
          );
          const out = drawn(container);
          unmount();
          return out;
        });
        // The scheme is the stylesheet's: the geometry and the words are the same in both.
        expect(renders[1]).toEqual(renders[0]);
        expect(renders[0]).toMatchSnapshot();
      });
    }
  }

  test("is the fixture the backend's export and layout-check tests draw in Chromium", () => {
    // `ORG_CHART_FIXTURE=write yarn test:dom` rewrites it after a deliberate change to the drawing.
    const { container } = renderWithProviders(
      <div className="deck-root">
        <OrgChartBlockView block={{ ...SMALL, show_counts: true }} />
      </div>,
    );
    const html = `${new XMLSerializer().serializeToString(container.querySelector("figure.deck-org-chart")!)}\n`;
    if (process.env.ORG_CHART_FIXTURE === "write") writeFileSync(BACKEND_FIXTURE, html);
    expect(readFileSync(BACKEND_FIXTURE, "utf8").replace(/\r\n/g, "\n")).toBe(html);
  });

  test("publishes the engine's geometry and facts for the exports, the layout check and the video", () => {
    const { container } = renderWithProviders(
      <div className="deck-root">
        <OrgChartBlockView block={{ ...SMALL, show_counts: true }} />
      </div>,
    );
    const figure = container.querySelector("figure.deck-org-chart")!;
    expect(figure.getAttribute("data-deck-org-chart")).toBe("true");
    const facts = JSON.parse(figure.getAttribute("data-org-facts")!);
    expect(facts).toMatchObject({ seats: 8, drawn: 8, refused: false });
    const line = container.querySelector(".deck-org-link")!;
    expect(line.getAttribute("data-points")).toMatch(/^[\d.]+,[\d.]+ /);
    expect(container.querySelector('[data-org-node="eng"] [data-count-to="18"]')).not.toBeNull();
    for (const text of Array.from(container.querySelectorAll(".deck-org-name"))) expect(Number(text.getAttribute("data-max-w"))).toBeGreaterThan(0);
    expect(container.querySelector('[data-org-node="bob"]')!.getAttribute("data-level")).toBe("1");
    expect(container.querySelector(".deck-org-link[data-dotted]")!.getAttribute("data-level")).toBeNull();
  });
});
