import { describe, expect, it } from "vitest";
import {
  countCrossings,
  flowPath,
  foldedOutputsOf,
  hoverPath,
  layoutChips,
  layoutFlow,
  MORE_OUTPUTS,
  moreOutputsLit,
  orderFlowLanes,
  sourcePath,
  withLetterSpacing,
  wrapLines,
  type FlowLaneModel,
} from "@/lib/fleet-flow-layout";

// The reference fixture of the approved mockups (UX round 5, #9829): the shape of
// a complex fleet, 7 sources, 42 assignments in 17 trunks, 7 agents, 8 handovers,
// 4 written integrations and 2 read-only ones.
const STARTS: Array<[string, string, number]> = [
  ["Teams", "Comms desk", 3],
  ["Teams", "Orchestrator", 2],
  ["Outlook", "Mail desk", 2],
  ["Outlook", "Orchestrator", 1],
  ["Work items", "Mail desk", 1],
  ["Work items", "Orchestrator", 3],
  ["Work items", "UX reviewer", 2],
  ["Work items", "Developer", 3],
  ["Platform events", "Orchestrator", 5],
  ["Platform events", "Ops desk", 2],
  ["GitHub", "UX reviewer", 1],
  ["GitHub", "Developer", 6],
  ["Schedule", "Orchestrator", 4],
  ["Schedule", "Developer", 2],
  ["Schedule", "Ops desk", 2],
  ["Schedule", "Research desk", 1],
  ["Manual", "Research desk", 2],
];

const COMPLEX: FlowLaneModel = {
  sources: ["Teams", "Outlook", "Work items", "Platform events", "GitHub", "Schedule", "Manual"],
  agents: ["Orchestrator", "Developer", "UX reviewer", "Mail desk", "Comms desk", "Research desk", "Ops desk"],
  writes: ["Teams", "Outlook", "GitHub", "Work items"],
  reads: ["Jira", "Notion"],
  starts: STARTS.map(([s, a]) => [s, a] as const),
  handovers: [
    ["Orchestrator", "Mail desk"],
    ["Mail desk", "Orchestrator"],
    ["Orchestrator", "Comms desk"],
    ["Orchestrator", "UX reviewer"],
    ["Orchestrator", "Developer"],
    ["UX reviewer", "Developer"],
    ["Developer", "UX reviewer"],
    ["Research desk", "Orchestrator"],
  ],
  outs: [
    ["Comms desk", "Teams"],
    ["Orchestrator", "Teams"],
    ["Mail desk", "Outlook"],
    ["Orchestrator", "Work items"],
    ["UX reviewer", "GitHub"],
    ["UX reviewer", "Work items"],
    ["Developer", "GitHub"],
    ["Developer", "Work items"],
    ["Ops desk", "Work items"],
    ["Developer", "Jira"],
    ["Research desk", "Notion"],
  ],
};

const content = (model: FlowLaneModel) => ({
  sources: model.sources.map((id) => ({ id, chips: [{ label: "Event one" }, { label: "Event two" }], statusLines: 0, detailLines: 0 })),
  agents: model.agents.map((id) => ({ id, statusLines: 1 })),
  outputs: [...model.writes, ...model.reads].map((id) => ({ id, statusLines: 0 })),
});

// The weight of each (source, agent) trunk in the reference fixture.
const WEIGHT = new Map(STARTS.map(([s, a, n]) => [`${s}\u0000${a}`, n]));
const startWeight = (s: string, a: string) => WEIGHT.get(`${s}\u0000${a}`) ?? 1;
const TOTAL = STARTS.reduce((n, [, , w]) => n + w, 0);

describe("layoutFlow as a flow chart (#9981)", () => {
  const at = (width: number) => layoutFlow(COMPLEX, content(COMPLEX), { width, startWeight });

  it("sizes every start band by the assignments it carries: the widths sum to the header count", () => {
    for (const width of [1100, 1408, 1888]) {
      const layout = at(width);
      const starts = layout.edges.filter((e) => e.kind === "start");
      expect(starts.reduce((n, e) => n + e.weight, 0)).toBe(TOTAL);
      // One scale: every band is its weight times the unit, so the widths sum to the total times the unit.
      expect(starts.reduce((n, e) => n + e.width, 0)).toBeCloseTo(TOTAL * layout.unit, 5);
      for (const e of starts) expect(e.width).toBeCloseTo(e.weight * layout.unit, 5);
    }
  });

  it("counts every (source, agent) pair exactly once", () => {
    const keys = at(1408).edges.filter((e) => e.kind === "start").map((e) => e.ends.join("->"));
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys.sort()).toEqual(STARTS.map(([s, a]) => `${s}->${a}`).sort());
  });

  it("gives each column at least 22% of the width and the bands the rest", () => {
    for (const width of [1100, 1408, 1888]) {
      const layout = at(width);
      expect(layout.lanes.columnWidth / width).toBeGreaterThanOrEqual(0.22);
      expect(layout.lanes.agentX).toBeGreaterThan(layout.lanes.columnWidth);
      expect(layout.lanes.outputX).toBeGreaterThanOrEqual(layout.lanes.agentX + layout.lanes.columnWidth);
    }
  });

  it("spreads every column over the full height: no empty half canvas", () => {
    const layout = at(1408);
    for (const lane of [layout.sources, layout.agents, layout.outputs]) {
      const top = Math.min(...lane.map((c) => c.y));
      const bottom = Math.max(...lane.map((c) => c.y + c.h));
      // Each column spans at least 70% of the drawing height (the agents piled in the lower half before).
      expect(bottom - top).toBeGreaterThanOrEqual(0.7 * (layout.height - 50));
    }
  });

  it("keeps every band inside the gap between two columns, never over a card", () => {
    const layout = at(1408);
    const w = layout.lanes.columnWidth;
    for (const e of layout.edges.filter((x) => x.kind !== "handover")) {
      const xs = [...e.d.matchAll(/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/g)].map((m) => Number(m[1]));
      const [lo, hi] = e.kind === "start" ? [w, layout.lanes.agentX] : [layout.lanes.agentX + w, layout.lanes.outputX];
      for (const x of xs) {
        expect(x).toBeGreaterThanOrEqual(lo - 0.05);
        expect(x).toBeLessThanOrEqual(hi + 0.05);
      }
    }
  });

  it("stacks the bands of a node side without overlap, inside the card", () => {
    const layout = at(1408);
    for (const card of layout.sources) {
      const bands = layout.edges.filter((e) => e.kind === "start" && e.ends[0] === card.id);
      const tops = bands.map((e) => Number(/^M[\d.-]+,([\d.-]+)/.exec(e.d)![1])).sort((a, b) => a - b);
      expect(tops[0]).toBeGreaterThanOrEqual(card.y);
      const end = tops[tops.length - 1] + bands.find((e) => Number(/^M[\d.-]+,([\d.-]+)/.exec(e.d)![1]) === tops[tops.length - 1])!.width;
      expect(end).toBeLessThanOrEqual(card.y + card.h + 0.05);
    }
  });

  it("gives a long name its second line, never more (no cut at 1440 px)", () => {
    const long = "[Sam/Corporate] Microsoft 365 mailbox and calendar of the CEO office";
    const model: FlowLaneModel = { sources: ["S"], agents: ["A"], writes: ["O"], reads: [], starts: [["S", "A"]], handovers: [], outs: [["A", "O"]] };
    const mk = (name?: string) =>
      layoutFlow(
        model,
        { sources: [{ id: "S", name, chips: [], statusLines: 0, detailLines: 0 }], agents: [{ id: "A", statusLines: 1 }], outputs: [{ id: "O", name, statusLines: 0 }] },
        { width: 900 },
      );
    const short = mk("Teams");
    const wrapped = mk(long);
    expect(wrapped.outputs[0].h - short.outputs[0].h).toBe(18);
    // Measured from the card's top: the lane centres a taller card differently.
    expect(wrapped.sources[0].chipsTop - wrapped.sources[0].y - (short.sources[0].chipsTop - short.sources[0].y)).toBe(18);
  });

  it("counts an output name within 2 px of its rendered box as two lines, never a clipped caption (UX #9992 r3 B1)", () => {
    // 817 px wide: 196 px columns, as at 1440. The rendered name box is 196 - 46 = 150 px.
    const model: FlowLaneModel = { sources: ["S"], agents: ["A"], writes: ["O"], reads: [], starts: [["S", "A"]], handovers: [], outs: [["A", "O"]] };
    const mk = (nameWidth: number) =>
      layoutFlow(
        model,
        {
          sources: [{ id: "S", name: "S", chips: [], statusLines: 0, detailLines: 0 }],
          agents: [{ id: "A", statusLines: 1 }],
          outputs: [{ id: "O", name: "Lab-sample-corporate-misp", caption: "Written by 1 agent", statusLines: 0 }],
        },
        { width: 817, measure: (text, px) => (text === "Lab-sample-corporate-misp" ? nameWidth : text.length * px * 0.5) },
      );
    const one = mk(149);
    const near = mk(152);
    expect(one.outputs[0].w).toBe(196);
    // Top padding 16, name, caption, bottom 8: each 18 px line counted.
    expect(near.outputs[0].h).toBeGreaterThanOrEqual(16 + 18 * 2 + 18 + 8);
    expect(near.outputs[0].h - one.outputs[0].h).toBe(18);
  });
});

// The CEO Office size the UX review measured (UX #9992 r1): 7 sources, 61
// assignments, 7 agents, 30 outputs (24 written, 6 read-only).
const BIG_SOURCES = Array.from({ length: 7 }, (_, i) => `S${i}`);
const BIG_AGENTS = Array.from({ length: 7 }, (_, i) => `A${i}`);
const BIG_WRITES = Array.from({ length: 24 }, (_, i) => `W${i}`);
const BIG_READS = Array.from({ length: 6 }, (_, i) => `R${i}`);
const BIG_STARTS: Array<[string, string, number]> = [];
BIG_SOURCES.forEach((s, i) => {
  for (const k of [0, 2, 4]) BIG_STARTS.push([s, BIG_AGENTS[(i + k) % 7], 0]);
});
// 21 trunks carrying 61 assignments: 19 of 3, 2 of 2.
BIG_STARTS.forEach((t, i) => (t[2] = i < 19 ? 3 : 2));
const BIG_OUTS: Array<[string, string]> = [
  ...BIG_WRITES.flatMap((o, i) => [[BIG_AGENTS[i % 7], o] as [string, string], ...(i % 3 === 0 ? [[BIG_AGENTS[(i + 3) % 7], o] as [string, string]] : [])]),
  ...BIG_READS.map((o, i) => [BIG_AGENTS[(i * 2) % 7], o] as [string, string]),
];
const BIG: FlowLaneModel = {
  sources: BIG_SOURCES,
  agents: BIG_AGENTS,
  writes: BIG_WRITES,
  reads: BIG_READS,
  starts: BIG_STARTS.map(([s, a]) => [s, a] as const),
  handovers: [],
  outs: BIG_OUTS,
};
const BIG_WEIGHT = new Map(BIG_STARTS.map(([s, a, n]) => [`${s}\u0000${a}`, n]));
const bigWeight = (s: string, a: string) => BIG_WEIGHT.get(`${s}\u0000${a}`) ?? 1;

describe("layoutFlow at the CEO Office size (UX #9992 r1)", () => {
  const at = (width: number, outputLimit?: number) => layoutFlow(BIG, content(BIG), { width, startWeight: bigWeight, outputLimit });

  it("carries 61 assignments in the start bands", () => {
    expect(BIG_STARTS.reduce((n, [, , w]) => n + w, 0)).toBe(61);
    expect(at(1408).edges.filter((e) => e.kind === "start").reduce((n, e) => n + e.weight, 0)).toBe(61);
  });

  it("folds the outputs past twelve into one +N more card, every output counted once (B1)", () => {
    const layout = at(1408);
    expect(layout.outputs).toHaveLength(12);
    expect(layout.outputs[layout.outputs.length - 1].id).toBe(MORE_OUTPUTS);
    const drawn = layout.outputs.filter((o) => o.id !== MORE_OUTPUTS).map((o) => o.id);
    // The output counts add up to the header: drawn + folded = 30, no output twice (B2).
    expect(drawn.length + layout.hiddenOutputs.length).toBe(BIG_WRITES.length + BIG_READS.length);
    expect(new Set([...drawn, ...layout.hiddenOutputs]).size).toBe(30);
    // Writes are kept before reads: the folded card holds the read-only outputs first.
    expect(drawn.every((o) => BIG_WRITES.includes(o))).toBe(true);
    expect(layout.hiddenOutputs).toEqual(expect.arrayContaining(BIG_READS));
  });

  it("keeps the chart near one screen tall at 1408 px (B1: 2752 px before)", () => {
    expect(at(1408).height).toBeLessThanOrEqual(1100);
  });

  it("spreads every lane over at least 70% of the drawing height (B1)", () => {
    for (const width of [1100, 1408, 1888]) {
      for (const limit of [undefined, Infinity]) {
        const layout = at(width, limit);
        const drawing = layout.height - 50;
        for (const lane of [layout.sources, layout.agents, layout.outputs]) {
          const top = Math.min(...lane.map((c) => c.y));
          const bottom = Math.max(...lane.map((c) => c.y + c.h));
          expect(bottom - top).toBeGreaterThanOrEqual(0.7 * drawing);
        }
      }
    }
  });

  it("draws every output when asked, and every (agent, output) pair once", () => {
    const layout = at(1408, Infinity);
    expect(layout.outputs).toHaveLength(30);
    expect(layout.hiddenOutputs).toEqual([]);
    const pairs = layout.edges.filter((e) => e.kind === "write" || e.kind === "read").map((e) => e.ends.join("->"));
    expect(pairs.sort()).toEqual(BIG_OUTS.map(([a, o]) => `${a}->${o}`).sort());
  });

  it("claims no assignment count on an output band: the API does not carry it (B2)", () => {
    for (const layout of [at(1408), at(1408, Infinity)]) {
      for (const e of layout.edges.filter((x) => x.kind === "write" || x.kind === "read")) expect(e.weight).toBe(0);
    }
  });

  it("merges an agent's folded outputs into one band to the +N more card", () => {
    const layout = at(1408);
    const toMore = layout.edges.filter((e) => e.ends[1] === MORE_OUTPUTS).map((e) => e.ends[0]);
    expect(new Set(toMore).size).toBe(toMore.length);
    const expected = new Set(BIG_OUTS.filter(([, o]) => layout.hiddenOutputs.includes(o)).map(([a]) => a));
    expect(new Set(toMore)).toEqual(expected);
  });

  it("names on each band to +N more exactly its agent's folded outputs, writes and reads apart (UX #9992 r2 B1)", () => {
    const layout = at(1408);
    const hidden = layout.hiddenOutputs;
    const toMore = layout.edges.filter((e) => e.ends[1] === MORE_OUTPUTS).map((e) => e.ends[0]);
    let shared = 0;
    for (const agent of toMore) {
      const own = foldedOutputsOf(BIG, hidden, agent);
      const expected = BIG_OUTS.filter(([a, o]) => a === agent && hidden.includes(o)).map(([, o]) => o);
      expect([...own.writes, ...own.reads].sort()).toEqual([...new Set(expected)].sort());
      expect(own.writes.every((o) => BIG_WRITES.includes(o))).toBe(true);
      expect(own.reads.every((o) => BIG_READS.includes(o))).toBe(true);
      if (own.writes.length + own.reads.length < hidden.length) shared += 1;
    }
    // No band claims every folded output when its agent reaches only some of them.
    expect(shared).toBe(toMore.length);
  });

  it("splits by the agent's own edges when told which pairs are reads", () => {
    const hidden = ["W1", "R0"];
    const model: FlowLaneModel = { ...BIG, outs: [["A0", "W1"], ["A0", "R0"], ["A1", "W1"]] };
    expect(foldedOutputsOf(model, hidden, "A0")).toEqual({ writes: ["W1"], reads: ["R0"] });
    expect(foldedOutputsOf(model, hidden, "A0", () => true)).toEqual({ writes: [], reads: ["W1", "R0"] });
    expect(foldedOutputsOf(model, hidden, "A1")).toEqual({ writes: ["W1"], reads: [] });
  });

  it("lights the +N more card when the lit path reaches a folded output (UX #9992 r2 B2)", () => {
    const layout = at(1408);
    const hidden = layout.hiddenOutputs;
    expect(moreOutputsLit(null, hidden)).toBe(true);
    const reaching = layout.edges.find((e) => e.ends[1] === MORE_OUTPUTS)?.ends[0] ?? "";
    expect(moreOutputsLit(hoverPath(BIG, reaching), hidden)).toBe(true);
    const notReaching = BIG_AGENTS.find((a) => !BIG_OUTS.some(([x, o]) => x === a && hidden.includes(o)));
    if (notReaching) expect(moreOutputsLit(hoverPath(BIG, notReaching), hidden)).toBe(false);
    expect(moreOutputsLit(new Set(["S0"]), hidden)).toBe(false);
  });
});

describe("orderFlowLanes", () => {
  it("orders the reference fleet with at most 28 crossings (the approved mockups)", () => {
    const order = orderFlowLanes(COMPLEX);
    expect(order.crossings.total).toBeLessThanOrEqual(28);
    expect(order.crossings.arcArc).toBe(0);
  });

  it("keeps every node of each lane exactly once, reads after writes", () => {
    const order = orderFlowLanes(COMPLEX);
    expect([...order.sources].sort()).toEqual([...COMPLEX.sources].sort());
    expect([...order.agents].sort()).toEqual([...COMPLEX.agents].sort());
    expect(order.outputs.slice(-2).sort()).toEqual(["Jira", "Notion"]);
  });

  it("is deterministic", () => {
    expect(orderFlowLanes(COMPLEX)).toEqual(orderFlowLanes(COMPLEX));
  });

  it("does better than the order the configuration lists", () => {
    const given = countCrossings(COMPLEX.sources, COMPLEX.agents, [...COMPLEX.writes, ...COMPLEX.reads], COMPLEX);
    expect(orderFlowLanes(COMPLEX).crossings.total).toBeLessThan(given.total);
  });

  it("keeps the given agent order past eight agents", () => {
    const agents = Array.from({ length: 9 }, (_, i) => `A${i}`);
    const order = orderFlowLanes({ sources: ["S"], agents, writes: [], reads: [], starts: agents.map((a) => ["S", a] as const), handovers: [], outs: [] });
    expect(order.agents).toEqual(agents);
  });
});

describe("layoutFlow", () => {
  const layout = layoutFlow(COMPLEX, content(COMPLEX), { width: 820 });

  it("draws one start band per (source, agent) trunk, no stub", () => {
    expect(layout.edges.filter((e) => e.kind === "start")).toHaveLength(STARTS.length);
    expect(layout.edges.some((e) => (e.kind as string) === "stub")).toBe(false);
    const github = layout.edges.find((e) => e.kind === "start" && e.ends[0] === "GitHub" && e.ends[1] === "Developer");
    expect(github?.weight).toBe(1);
  });

  it("never mixes start edges and handover arcs in one trunk (UX round 2 Q2)", () => {
    for (const edge of layout.edges) {
      if (edge.kind === "handover") expect(edge.ends.every((n) => COMPLEX.agents.includes(n))).toBe(true);
      if (edge.kind === "start") expect(COMPLEX.sources.includes(edge.ends[0])).toBe(true);
    }
  });

  it("keeps every handover arrowhead at least 4 px clear of a trunk or an output fan (UX round 4 B2)", () => {
    expect(layout.minHandoverClearance).not.toBeNull();
    expect(layout.minHandoverClearance!).toBeGreaterThanOrEqual(4);
  });

  it("draws reads as read edges and writes as write edges", () => {
    expect(layout.edges.filter((e) => e.kind === "read")).toHaveLength(2);
    expect(layout.edges.filter((e) => e.kind === "write")).toHaveLength(9);
    expect(layout.lanes.readsLabelY).not.toBeNull();
  });

  it("stacks the cards of a lane without overlap", () => {
    for (const lane of [layout.sources, layout.agents, layout.outputs]) {
      const sorted = [...lane].sort((a, b) => a.y - b.y);
      for (let i = 1; i < sorted.length; i++) expect(sorted[i].y).toBeGreaterThanOrEqual(sorted[i - 1].y + sorted[i - 1].h);
    }
    expect(Math.max(...layout.agents.map((a) => a.y + a.h))).toBeLessThanOrEqual(layout.height);
  });
});

describe("layoutChips", () => {
  const chips = ["a", "b", "c", "d", "e", "f", "g"].map((label) => ({ label }));

  it("collapses past four chips to three and +N more", () => {
    const { chips: shown } = layoutChips(chips, 170, false);
    expect(shown.map((c) => c.label)).toEqual(["a", "b", "c", "+4 more"]);
    expect(shown[3].toggle).toBe("more");
  });

  it("shows every chip and Show less once expanded (UX round 2 B6)", () => {
    const { chips: shown } = layoutChips(chips, 170, true);
    expect(shown).toHaveLength(8);
    expect(shown[7]).toMatchObject({ label: "Show less", toggle: "less" });
  });

  it("gives a label wider than the card the full width on two lines, never a cut (UX #9882 r2 B3)", () => {
    const long = [{ label: "Every weekday at 07:30 (Europe/Paris)" }, { label: "x" }];
    const { chips: shown, height } = layoutChips(long, 172, false);
    expect(shown[0]).toMatchObject({ x: 0, y: 0, w: 172, h: 36 });
    expect(shown[1]).toMatchObject({ x: 0, y: 40, h: 20 });
    expect(height).toBe(60);
  });

  it("sizes a label that needs three lines by its measured lines (UX #9882 r3 B2)", () => {
    const fr = [{ label: "Prise en charge d'un élément de travail libérée" }];
    const { chips: shown, height } = layoutChips(fr, 100, false);
    expect(shown[0].w).toBe(100);
    expect(shown[0].h).toBe(20 + 16 * (wrapLines(fr[0].label, 100 - 16, 12) - 1));
    expect(shown[0].h).toBeGreaterThanOrEqual(52);
    expect(height).toBe(shown[0].h);
  });

  it("never wraps the +N more toggle (UX #9882 r3 B3)", () => {
    const { chips: shown } = layoutChips(chips, 20, false, () => 30, { more: (n) => `他${n}件`, less: "x" });
    const toggle = shown.find((c) => c.toggle === "more")!;
    expect(toggle.h).toBe(20);
  });

  it("wraps a row that would pass the width", () => {
    const { chips: shown, height } = layoutChips(chips, 60, true, () => 30);
    expect(new Set(shown.map((c) => c.y)).size).toBeGreaterThan(1);
    expect(height).toBeGreaterThan(20);
  });
});

describe("wrapLines", () => {
  it("counts the lines of a greedy word wrap", () => {
    const measure = (text: string) => text.length * 10;
    expect(wrapLines("", 100, 12, measure)).toBe(0);
    expect(wrapLines("aaaa bbbb", 100, 12, measure)).toBe(1);
    expect(wrapLines("aaaa bbbb cccc", 100, 12, measure)).toBe(2);
    expect(wrapLines("aaaaaaaaaaaaaaaaaaaaaaaaa", 100, 12, measure)).toBe(3);
  });

  it("gives a not-connected source the status lines its text measures (UX #9882 r3 B1)", () => {
    const model: FlowLaneModel = { sources: ["Teams"], agents: ["A"], writes: [], reads: [], starts: [["Teams", "A"]], handovers: [], outs: [] };
    const content = (statusText?: string) => ({
      sources: [{ id: "Teams", chips: [{ label: "New email" }], statusLines: 2, statusText, detailLines: 0 }],
      agents: [{ id: "A", statusLines: 1 }],
      outputs: [],
    });
    const two = layoutFlow(model, content(), { width: 900 });
    const long = "Non connecté : reconnectez-vous dans la page Intégrations de la plateforme pour reprendre";
    const three = layoutFlow(model, content(long), { width: 900 });
    const lines = wrapLines(long, three.lanes.columnWidth - 16, 12);
    expect(lines).toBeGreaterThan(2);
    expect(three.sources[0].chipsTop - three.sources[0].y - (two.sources[0].chipsTop - two.sources[0].y)).toBe((lines - 2) * 18);
    expect(three.sources[0].h - two.sources[0].h).toBe((lines - 2) * 18);
  });

  it("counts the letter-spacing the page renders with (UX #9882 r4 B1)", () => {
    // 140 px of line: 138 px of glyphs fits on one line, 138 + 0.09 x 26 characters does not.
    const glyphs = (text: string) => (text === " " ? 4 : (text.length / 26) * 138);
    const word = "abcdefghijklmnopqrstuvwxyz";
    expect(wrapLines(word, 140, 12, glyphs)).toBe(1);
    expect(wrapLines(word, 140, 12, withLetterSpacing(glyphs, 0.09))).toBe(2);
    expect(withLetterSpacing(glyphs, 0)).toBe(glyphs);
  });

  it("sizes a chip on the lines letter-spacing gives it", () => {
    // 63 px of glyphs + 18 px of padding fits a 90 px card on one line; 9 x 1.5 px of spacing does not.
    const glyphs = (text: string) => text.length * 7;
    const chip = [{ label: "aaaa bbbb" }];
    const plain = layoutChips(chip, 90, false, glyphs);
    const spaced = layoutChips(chip, 90, false, withLetterSpacing(glyphs, 1.5));
    expect(plain.chips[0].h).toBe(20);
    expect(spaced.chips[0].h).toBeGreaterThan(20);
    expect(spaced.height).toBe(spaced.chips[0].h);
  });
});

describe("measured card texts (UX #9882 r4 B2, B3)", () => {
  const model: FlowLaneModel = { sources: ["Gmail"], agents: ["A"], writes: [], reads: [], starts: [["Gmail", "A"]], handovers: [], outs: [] };

  it("gives a Cannot start agent the lines its reason measures", () => {
    const at = (statusText?: string) =>
      layoutFlow(
        model,
        { sources: [{ id: "Gmail", chips: [], statusLines: 0, detailLines: 0 }], agents: [{ id: "A", statusLines: 2, statusText }], outputs: [] },
        { width: 900 },
      );
    const reason = "Lab sample data flow Gmail and Lab sample data flow Outlook not connected";
    const lines = wrapLines(reason, at().lanes.columnWidth - 24, 12);
    expect(lines).toBeGreaterThan(1);
    expect(at(reason).agents[0].h).toBeGreaterThanOrEqual(30 + 18 * (2 + lines) + 12);
  });

  it("sizes an open source by the wrapped lines of its detail", () => {
    const at = (detailTexts?: { text: string; medium?: boolean }[]) =>
      layoutFlow(
        model,
        { sources: [{ id: "Gmail", chips: [], statusLines: 0, detailLines: 1, detailTexts }], agents: [{ id: "A", statusLines: 1 }], outputs: [] },
        { width: 900 },
      );
    const short = at([{ text: "Triage → A", medium: true }, { text: "New email" }]);
    const events = Array.from({ length: 12 }, (_, i) => `Event number ${i}`).join(", ");
    const long = at([{ text: "Triage of the inbound mail of the lab → A", medium: true }, { text: events }]);
    const w = long.lanes.columnWidth - 16;
    const extra = wrapLines("Triage of the inbound mail of the lab → A", w, 12, undefined, 500) + wrapLines(events, w, 12) - 2;
    expect(extra).toBeGreaterThan(2);
    expect(long.sources[0].h - short.sources[0].h).toBe(extra * 18);
  });
});

describe("hover and selection paths", () => {
  it("lights the hovered agent's own sources, outputs and handover partners only (UX round 5 B1)", () => {
    const lit = hoverPath(COMPLEX, "Developer");
    expect([...lit].sort()).toEqual(["Developer", "GitHub", "Jira", "Orchestrator", "Schedule", "UX reviewer", "Work items"].sort());
    expect(lit.has("Teams")).toBe(false);
  });

  it("lights the full path through a source or an output (#9981)", () => {
    expect([...flowPath(COMPLEX, "Manual")].sort()).toEqual(["Manual", "Notion", "Research desk"]);
    const jira = flowPath(COMPLEX, "Jira");
    expect([...jira].sort()).toEqual(["Developer", "GitHub", "Jira", "Schedule", "Work items"].sort());
  });

  it("lights a selected source and the agents it starts", () => {
    expect([...sourcePath(COMPLEX, "Manual")].sort()).toEqual(["Manual", "Research desk"]);
  });
});
