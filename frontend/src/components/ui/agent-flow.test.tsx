// What the agent handover map draws for a focus, in either mode.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { IntlProvider } from "react-intl";

import { renderWithProviders, testQueryClient } from "@/testing/providers";
import type { Agent } from "@/types";
import { ALL_HANDOVER_TYPES } from "@/lib/handover-types";
import { GRAPH_LAYOUTS, wrapLabelWhole } from "@/lib/agent-graph-layout";
import { AgentFlowGraph, STATIC_VIEW_PX, type AgentFlowGraphProps } from "./agent-flow";
import { placePopover, type PopoverAvoidBox } from "./handover-type-popover";
import { fleetMiniGraph } from "@/lib/fleet-mini-graph";

function agent(id: string, handoverIds: string[] = []): Agent {
  return {
    id,
    name: id.toUpperCase(),
    enabled: true,
    is_company_managed: false,
    icon: "bot",
    output_format: "markdown",
    handover_agent_ids: handoverIds,
    handover_types: {},
    handover_conditions: {},
  } as unknown as Agent;
}

// a -> b, a -> c, b -> c (two neighbours of a), e -> d, f alone
const AGENTS = [
  agent("a", ["b", "c"]),
  agent("b", ["c"]),
  agent("c"),
  agent("d"),
  agent("e", ["d"]),
  agent("f"),
];

function draw(props: Partial<AgentFlowGraphProps> = {}) {
  return renderWithProviders(<AgentFlowGraph agents={AGENTS} {...props} />);
}

const drawnIds = (container: HTMLElement) =>
  [...container.querySelectorAll("[data-node-id]")].map((n) => n.getAttribute("data-node-id")).sort();
const dimmedIds = (container: HTMLElement) =>
  [...container.querySelectorAll("[data-node-id]")]
    .filter((n) => n.getAttribute("opacity") === "0.15")
    .map((n) => n.getAttribute("data-node-id"))
    .sort();
const viewBox = (container: HTMLElement) => container.querySelector("svg")!.getAttribute("viewBox");

describe("AgentFlowGraph edge scope", () => {
  const badge = (container: HTMLElement, id: string) =>
    container.querySelector(`[data-node-id="${id}"] text[font-size="6"]`)?.textContent ?? null;

  it("counts on each node badge only the handovers the edge scope draws", () => {
    // a -> b and a -> c are known, only a -> b is in scope; b -> c is out of it.
    const { container } = draw({ edgeIds: new Set(["a->b"]) });
    expect(badge(container, "a")).toBe("1");
    expect(badge(container, "b")).toBeNull();
    expect(badge(container, "e")).toBeNull();
  });

  it("counts every known handover without an edge scope", () => {
    const { container } = draw();
    expect(badge(container, "a")).toBe("2");
    expect(badge(container, "b")).toBe("1");
  });
});

describe("AgentFlowGraph fillHeight", () => {
  // A card taller than the canvas: the svg is drawn 1000 px wide and 700 px
  // tall, so the view must hold 980 units down (1.4 units per px), not 600.
  function drawInTallCard(props: Partial<AgentFlowGraphProps>) {
    vi.stubGlobal("ResizeObserver", class {
      observe() {}
      disconnect() {}
    });
    // jsdom lays nothing out: the svg's width is given on its own prototype.
    Object.defineProperty(SVGElement.prototype, "clientWidth", { configurable: true, get: () => 1000 });
    const rect = vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, top: 0, left: 0, right: 1000, bottom: 700, width: 1000, height: 700, toJSON: () => ({}),
    } as DOMRect);
    try {
      return draw({ layout: "force", ...props });
    } finally {
      rect.mockRestore();
      delete (SVGElement.prototype as { clientWidth?: number }).clientWidth;
      vi.unstubAllGlobals();
    }
  }
  const vbHeight = (container: HTMLElement) => Number(viewBox(container)!.split(" ")[3]);
  const gridHeight = (container: HTMLElement) => Number(container.querySelector("rect[data-grid]")!.getAttribute("height"));

  it("draws the view and the grid down to the bottom of a card taller than the canvas", () => {
    const { container } = drawInTallCard({ fillHeight: true });
    expect(vbHeight(container)).toBeCloseTo(980);
    expect(gridHeight(container)).toBeCloseTo(980);
    // Only the dynamic aspect ratio is inline; growing is styling, not inline flex.
    const svg = container.querySelector("svg[viewBox]") as SVGSVGElement;
    expect(svg.style.flexGrow).toBe("");
    expect(svg.style.flexShrink).toBe("");
  });

  it("keeps the canvas's own height without fillHeight (the standalone map)", () => {
    const { container } = drawInTallCard({});
    expect(vbHeight(container)).toBeLessThan(980);
    expect(gridHeight(container)).toBe(vbHeight(container));
  });
});

describe("AgentFlowGraph poll ticks (the periodic flash of the fleet graph)", () => {
  const live = (status: "running" | "completed") =>
    new Map([["a->b", { status, firedAt: "2026-11-19T13:00:00Z" }]]);
  const particles = (container: HTMLElement) => [...container.querySelectorAll("circle")];

  it("redraws nothing for a tick that rebuilds the same running agents and live edges", () => {
    const props = { layout: "force" as const, activeAgentIds: new Set(["a", "b"]), liveEdges: live("running") };
    const { container, rerender } = renderWithProviders(<AgentFlowGraph agents={AGENTS} {...props} />);
    const svg = container.querySelector("svg[viewBox]")!;
    const before = { viewBox: svg.getAttribute("viewBox"), circles: particles(container), html: svg.innerHTML };
    const toggles: (string | null)[] = [];
    const observer = new MutationObserver((records) => {
      for (const r of records) if (r.attributeName === "data-animating") toggles.push(svg.getAttribute("data-animating"));
    });
    observer.observe(svg, { attributes: true });
    // New references, same content, members in another order: what a poll hands back.
    rerender(<AgentFlowGraph agents={AGENTS.map((a) => ({ ...a }))} layout="force" activeAgentIds={new Set(["b", "a"])} liveEdges={live("running")} />);
    observer.disconnect();
    expect(toggles).toEqual([]);
    expect(svg.getAttribute("viewBox")).toBe(before.viewBox);
    expect(svg.innerHTML).toBe(before.html);
    // The same circle elements: no beam particle or mote was remounted, so none restarts.
    const after = particles(container);
    expect(after.length).toBe(before.circles.length);
    after.forEach((c, i) => expect(c).toBe(before.circles[i]));
  });

  it("still draws a change of the running agents or of a live edge", () => {
    const { container, rerender } = renderWithProviders(
      <AgentFlowGraph agents={AGENTS} layout="force" activeAgentIds={new Set(["a"])} liveEdges={live("running")} />,
    );
    const html = container.querySelector("svg[viewBox]")!.innerHTML;
    rerender(<AgentFlowGraph agents={AGENTS} layout="force" activeAgentIds={new Set(["a", "c"])} liveEdges={live("running")} />);
    const running = container.querySelector("svg[viewBox]")!.innerHTML;
    expect(running).not.toBe(html);
    // The edge leaves the live window: it is drawn still again.
    rerender(<AgentFlowGraph agents={AGENTS} layout="force" activeAgentIds={new Set(["a", "c"])} liveEdges={new Map()} />);
    expect(container.querySelector("svg[viewBox]")!.innerHTML).not.toBe(running);
  });

  it("commits the view of a resized box before the next paint (fillHeight)", () => {
    let resized: (() => void) | null = null;
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: () => void) { resized = cb; }
      observe() {}
      disconnect() {}
    });
    Object.defineProperty(SVGElement.prototype, "clientWidth", { configurable: true, get: () => 1000 });
    let height = 700;
    const rect = vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(() => ({
      x: 0, y: 0, top: 0, left: 0, right: 1000, bottom: height, width: 1000, height, toJSON: () => ({}),
    } as DOMRect));
    try {
      const { container } = draw({ layout: "force", fillHeight: true });
      expect(Number(viewBox(container)!.split(" ")[3])).toBeCloseTo(980);
      // A sibling panel grows the card by 30 px: the observer fires, outside any act().
      height = 730;
      resized!();
      // The view already matches the box, in the observer's own call: the drawing is
      // never painted stretched to the new box with the old view.
      expect(Number(viewBox(container)!.split(" ")[3])).toBeCloseTo(1022);
    } finally {
      rect.mockRestore();
      delete (SVGElement.prototype as { clientWidth?: number }).clientWidth;
      vi.unstubAllGlobals();
    }
  });
});

describe("AgentFlowGraph wrapNames (the fleet About flow, #9647 UX round 2)", () => {
  // A dense left-to-right fleet: one hub handing over to 16 members.
  const DENSE = [
    agent("hub", Array.from({ length: 16 }, (_, i) => `m${i}`)),
    ...Array.from({ length: 16 }, (_, i) => agent(`m${i}`)),
  ];
  const lowestNameBottom = (container: HTMLElement) => {
    let bottom = -Infinity;
    for (const node of container.querySelectorAll("[data-node-id]")) {
      const y = Number(/translate\([^ ]+ ([^)]+)\)/.exec(node.getAttribute("transform") ?? "")?.[1]);
      if (Number.isFinite(y)) bottom = Math.max(bottom, y);
    }
    return bottom;
  };
  const canvasHeight = (container: HTMLElement) => Number(viewBox(container)!.split(" ")[3]);

  it("grows a dense drawing's canvas past the one-line fit and keeps the last row's names inside it", () => {
    const packed = draw({ agents: DENSE, layout: "hierarchy-lr" });
    const packedH = canvasHeight(packed.container);
    packed.unmount();
    const { container } = draw({ agents: DENSE, layout: "hierarchy-lr", wrapNames: true });
    const h = canvasHeight(container);
    expect(h).toBeGreaterThan(packedH);
    // The last row's glyph leaves room under it for three name lines and a
    // two-line group line before the canvas bottom.
    expect(h - lowestNameBottom(container)).toBeGreaterThanOrEqual(60);
  });

  it("shows the full name in a tooltip on keyboard focus and hides it on blur", () => {
    const { container } = draw({ layout: "hierarchy-lr", wrapNames: true });
    const node = container.querySelector<SVGGElement>('[data-node-id="a"]')!;
    fireEvent.focus(node);
    const tip = container.querySelector('[data-testid="agent-flow-tooltip"]');
    expect(tip).not.toBeNull();
    expect(tip!.textContent).toContain(node.getAttribute("aria-label") ?? "");
    fireEvent.blur(node);
    expect(container.querySelector('[data-testid="agent-flow-tooltip"]')).toBeNull();
  });

  // #9647 UX round 3: the wrap stopped at its line cap and cut the rest, and
  // a hovered or focused node drew its name on one long line.
  const LONG = "CTEM Vulnerability Prioritization Orchestrator With A Long Tail";
  const named = (id: string, name: string, handovers: string[] = []) => ({ ...agent(id, handovers), name }) as Agent;
  const nameText = (container: HTMLElement, id: string) =>
    [...container.querySelectorAll(`[data-node-id="${id}"] text tspan`)].map((t) => t.textContent ?? "");

  it("wraps a label on as many lines as it needs and never cuts a word", () => {
    const lines = wrapLabelWhole(LONG, 12);
    expect(lines.length).toBeGreaterThan(3);
    expect(lines.join(" ")).toBe(LONG);
    expect(lines.some((l) => l.includes("\u2026"))).toBe(false);
    expect(wrapLabelWhole("Supercalifragilistic word", 5)).toEqual(["Supercalifragilistic", "word"]);
  });

  it("never draws a cut name at rest, hovered or focused", () => {
    const agents = [named("hub", "Hub", ["x", "y"]), named("x", LONG), named("y", `${LONG} Two`)];
    const { container } = draw({ agents, layout: "hierarchy-lr", wrapNames: true });
    const node = container.querySelector<SVGGElement>('[data-node-id="x"]')!;
    const atRest = nameText(container, "x");
    expect(atRest.join(" ")).toContain("Orchestrator");
    for (const line of atRest) expect(line).not.toContain("\u2026");
    fireEvent.focus(node);
    const focused = nameText(container, "x");
    expect(focused.join(" ")).toContain("Tail");
    for (const line of focused) expect(line).not.toContain("\u2026");
    // Wrapped, not one long line: no drawn line is the whole name.
    expect(focused.some((line) => line === LONG)).toBe(false);
    fireEvent.mouseEnter(node);
    for (const line of nameText(container, "x")) expect(line).not.toContain("\u2026");
  });
});

describe("AgentFlowGraph node context menu", () => {
  it("hands a right click on a node to onNodeContextMenu and suppresses the browser menu", () => {
    const calls: string[] = [];
    const { container } = draw({ onNodeContextMenu: (id) => calls.push(id) });
    const node = container.querySelector('[data-node-id="b"]')!;
    const notPrevented = fireEvent.contextMenu(node);
    expect(calls).toEqual(["b"]);
    expect(notPrevented).toBe(false);
  });

  it("leaves the browser menu alone without the prop", () => {
    const { container } = draw();
    expect(fireEvent.contextMenu(container.querySelector('[data-node-id="b"]')!)).toBe(true);
  });
});

describe("AgentFlowGraph focus", () => {
  it("draws only the focused neighbourhood when isolating", () => {
    const { container } = draw({ focusedAgentIds: new Set(["a"]), focusMode: "isolate" });
    expect(drawnIds(container)).toEqual(["a", "b", "c"]);
    // b -> c joins two neighbours: left out, as the dimmed view dims it.
    expect(screen.getByText("3 agents")).toBeTruthy();
    expect(screen.getByText("2 handovers")).toBeTruthy();
  });

  it("draws an agent with no handover alone", () => {
    const { container } = draw({ focusedAgentIds: new Set(["f"]), focusMode: "isolate" });
    expect(drawnIds(container)).toEqual(["f"]);
    expect(screen.getByText("1 agent")).toBeTruthy();
    expect(screen.getByText("0 handovers")).toBeTruthy();
  });

  it("dims the agents outside the neighbourhood by default", () => {
    const { container } = draw({ focusedAgentIds: new Set(["a"]) });
    expect(drawnIds(container)).toEqual(["a", "b", "c", "d", "e", "f"]);
    expect(dimmedIds(container)).toEqual(["d", "e", "f"]);
    expect(screen.getByText("6 agents")).toBeTruthy();
    expect(screen.getByText("4 handovers")).toBeTruthy();
  });

  it.each(["isolate", "dim"] as const)("shows the whole map for a focus on no agent of it (%s)", (focusMode) => {
    const { container } = draw({ focusedAgentIds: new Set(["deleted-agent"]), focusMode });
    expect(drawnIds(container)).toEqual(["a", "b", "c", "d", "e", "f"]);
    expect(dimmedIds(container)).toEqual([]);
    expect(screen.getByText("6 agents")).toBeTruthy();
  });

  it("ignores the unknown ids of a focus that also names an agent", () => {
    const { container } = draw({ focusedAgentIds: new Set(["deleted-agent", "e"]), focusMode: "isolate" });
    expect(drawnIds(container)).toEqual(["d", "e"]);
  });

  it("resets the view when an isolated focus changes or is cleared", () => {
    const { container, rerender } = draw({ focusedAgentIds: new Set(["a"]), focusMode: "isolate" });
    const initial = viewBox(container);

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(viewBox(container)).not.toBe(initial);
    rerender(<AgentFlowGraph agents={AGENTS} focusedAgentIds={new Set(["e"])} focusMode="isolate" />);
    expect(viewBox(container)).toBe(initial);

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(viewBox(container)).not.toBe(initial);
    rerender(<AgentFlowGraph agents={AGENTS} focusedAgentIds={null} focusMode="isolate" />);
    expect(viewBox(container)).toBe(initial);
    expect(drawnIds(container)).toEqual(["a", "b", "c", "d", "e", "f"]);
  });

  it("keeps the view while the dimmed focus changes", () => {
    const { container, rerender } = draw({ focusedAgentIds: new Set(["a"]) });
    const initial = viewBox(container);
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    const zoomed = viewBox(container);
    expect(zoomed).not.toBe(initial);
    rerender(<AgentFlowGraph agents={AGENTS} focusedAgentIds={new Set(["e"])} />);
    expect(viewBox(container)).toBe(zoomed);
  });

  it("dims the rest of a hub's fleet when one spoke is focused (#9760 UX round 1, B4)", () => {
    // hub -> s1..s4: the focus of s1 alone keeps s1 and the hub, dims the others.
    const hub = [agent("hub", ["s1", "s2", "s3", "s4"]), agent("s1"), agent("s2"), agent("s3"), agent("s4")];
    const { container } = renderWithProviders(
      <AgentFlowGraph agents={hub} layout="hierarchy-tb" focusedAgentIds={new Set(["s1"])} />,
    );
    expect(dimmedIds(container)).toEqual(["s2", "s3", "s4"]);
  });
});

describe("AgentFlowGraph static view of a dense fleet (#9760 UX round 2)", () => {
  // An orchestrator, 10 members of its own fleet and 8 agents of other fleets
  // (ghosts): 19 nodes, as the densest built-in fleet draws them.
  const own = { fleet_id: "tie", fleet_name: "Threat-informed exposure response" };
  const other = { fleet_id: "vul", fleet_name: "Vulnerability prioritization to verified fix" };
  const members = Array.from({ length: 10 }, (_, i) => ({ ...agent(`m${i}`), name: `Threat Exposure Analyst ${i}`, fleet: own }));
  const ghosts = Array.from({ length: 8 }, (_, i) => ({ ...agent(`g${i}`), name: `AEV Detection Rules Generator ${i}`, fleet: other }));
  const hub = { ...agent("hub", [...members, ...ghosts].map((a) => a.id)), name: "Threat Response Orchestrator", fleet: own };
  const DENSE = [hub, ...members, ...ghosts] as Agent[];
  const ghostNodes = new Map(ghosts.map((g) => [g.id, other.fleet_name]));
  const drawDense = () =>
    renderWithProviders(
      <AgentFlowGraph agents={DENSE} staticView layout="hierarchy-tb" hideLegend wrapNames ghostNodes={ghostNodes} viewedFleetId="tie" />,
    );

  it("draws every one of 19 names at rest (B1)", () => {
    const { container } = drawDense();
    const nodes = [...container.querySelectorAll("[data-node-id]")];
    expect(nodes).toHaveLength(19);
    for (const node of nodes) {
      const name = [...node.querySelectorAll("text[data-node-name] tspan")].map((t) => t.textContent ?? "").join("");
      expect(name.trim(), node.getAttribute("data-node-id")!).not.toBe("");
    }
  });

  it("keeps every caption of the last row inside the frame (B2)", () => {
    const { container } = drawDense();
    const [, , , vbH] = viewBox(container)!.split(" ").map(Number);
    for (const node of container.querySelectorAll("[data-node-id]")) {
      const { y } = centre(container, node.getAttribute("data-node-id")!);
      for (const text of node.querySelectorAll("text")) {
        const size = Number(text.getAttribute("font-size"));
        const lines = text.querySelectorAll("tspan").length || 1;
        // The last baseline plus the descender.
        const bottom = y + Number(text.getAttribute("y")) + (lines - 1) * size * 1.15 + size * 0.3;
        expect(bottom).toBeLessThanOrEqual(vbH);
      }
    }
  });

  it("fits a force layout into the fixed frame, never a row per node (#10147 UX r1 B3)", () => {
    const { container } = renderWithProviders(
      <AgentFlowGraph agents={DENSE} staticView layout="force" hideLegend wrapNames ghostNodes={ghostNodes} viewedFleetId="tie" />,
    );
    const [, , vbW, vbH] = viewBox(container)!.split(" ").map(Number);
    // jsdom draws the svg at the canvas width: the frame's height on screen.
    expect(Math.round((vbH * 1400) / vbW)).toBeLessThanOrEqual(STATIC_VIEW_PX + 1);
    expect(container.querySelectorAll("[data-node-id]")).toHaveLength(19);
  });

  it("draws a ghost's group line at 10 px on screen at least (B3)", () => {
    const { container } = drawDense();
    const [, , vbW] = viewBox(container)!.split(" ").map(Number);
    // jsdom draws the svg at the canvas width: one unit per px.
    const pxPerUnit = 1400 / vbW;
    const groupLines = [...container.querySelectorAll('[data-node-id^="g"] text[font-style="italic"]')];
    expect(groupLines.length).toBeGreaterThan(0);
    for (const line of groupLines) expect(Number(line.getAttribute("font-size")) * pxPerUnit).toBeGreaterThanOrEqual(10 - 1e-6);
  });
});

describe("AgentFlowGraph static force view of the corporate fleet (#10147 UX r3 B1)", () => {
  // The corporate fleet as round 3 drew it: an orchestrator with long-named
  // members, two of them with no handover at all.
  const own = { fleet_id: "corp", fleet_name: "Lab sample corporate fleet" };
  const names = ["corporate communications", "finance and procurement office", "legal and compliance counsel", "people operations partner", "sales operations", "security watch"];
  const members = names.map((name, i) => ({ ...agent(`m${i}`, i === 0 ? ["m1"] : []), name, fleet: own }));
  const hub = { ...agent("hub", ["m0", "m2", "m3"]), name: "Chief of Staff orchestrator", fleet: { ...own, is_orchestrator: true } };
  const CORP = [hub, ...members] as Agent[];

  for (const width of [560, 680]) {
    it(`draws every name on at most two lines and spreads the nodes over the frame at ${width} px`, () => {
      vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
      vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
      Object.defineProperty(SVGElement.prototype, "clientWidth", { configurable: true, get: () => width });
      try {
        const { container } = renderWithProviders(
          <AgentFlowGraph agents={CORP} staticView layout="force" hideLegend wrapNames viewedFleetId="corp" />,
        );
        const nodes = [...container.querySelectorAll("[data-node-id]")];
        expect(nodes).toHaveLength(7);
        const ys: number[] = [];
        for (const node of nodes) {
          const lines = [...node.querySelectorAll("text[data-node-name] tspan")].map((t) => t.textContent ?? "").filter((l) => l.trim() !== "");
          expect(lines.length, node.getAttribute("data-node-id")!).toBeGreaterThan(0);
          expect(lines.length, node.getAttribute("data-node-id")!).toBeLessThanOrEqual(2);
          ys.push(Number((/translate\(([^ ,]+)[ ,]+([^)]+)\)/.exec(node.getAttribute("transform") ?? "") ?? [])[2]));
        }
        const [, , , vbH] = viewBox(container)!.split(" ").map(Number);
        // The nodes span at least half the frame: no dead band under them.
        expect(Math.max(...ys) - Math.min(...ys)).toBeGreaterThanOrEqual(vbH / 2);
      } finally {
        delete (SVGElement.prototype as { clientWidth?: number }).clientWidth;
        vi.unstubAllGlobals();
      }
    });
  }

  it("draws no two nodes with the same visible name when members share a long prefix (#10147 UX r4 B1)", () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
    Object.defineProperty(SVGElement.prototype, "clientWidth", { configurable: true, get: () => 560 });
    try {
      const roles = ["assistant", "developer", "design review", "communications", "finance office"];
      const lab = roles.map((role, i) => ({ ...agent(`l${i}`, []), name: `Lab sample corporate ${role}`, fleet: own }));
      const labHub = { ...agent("hub", lab.map((a) => a.id)), name: "Chief of Staff orchestrator", fleet: { ...own, is_orchestrator: true } };
      const { container } = renderWithProviders(
        <AgentFlowGraph agents={[labHub, ...lab] as Agent[]} staticView layout="force" hideLegend wrapNames viewedFleetId="corp" />,
      );
      const shown = [...container.querySelectorAll("[data-node-id]")].map((node) =>
        [...node.querySelectorAll("text[data-node-name] tspan")].map((t) => t.textContent ?? "").join(" ").trim(),
      );
      expect(shown).toHaveLength(6);
      expect(new Set(shown).size, shown.join(" | ")).toBe(6);
      for (const role of roles) expect(shown.some((s) => s.startsWith(role.split(" ")[0])), role).toBe(true);
      // Names render at 12 px or more on screen (UX r4 B2: 11 px).
      const [, , vbW] = viewBox(container)!.split(" ").map(Number);
      const size = Number(container.querySelector("text[data-node-name]")!.getAttribute("font-size"));
      expect(size * (560 / vbW)).toBeGreaterThanOrEqual(11.99);
    } finally {
      delete (SVGElement.prototype as { clientWidth?: number }).clientWidth;
      vi.unstubAllGlobals();
    }
  });

  // The names a node draws, as screen boxes: its centre, the widest line
  // across, the glyph above and the name lines below (12 px, 1.15 apart).
  const nameBoxes = (container: HTMLElement, width: number) => {
    const [, , vbW] = viewBox(container)!.split(" ").map(Number);
    const k = width / vbW;
    return [...container.querySelectorAll("[data-node-id]")].map((node) => {
      const lines = [...node.querySelectorAll("text[data-node-name] tspan")].map((t) => t.textContent ?? "").filter((l) => l.trim() !== "");
      const m = /translate\(([^ ,]+)[ ,]+([^)]+)\)/.exec(node.getAttribute("transform") ?? "") ?? [];
      const x = Number(m[1]) * k, y = Number(m[2]) * k;
      const w = Math.max(...lines.map((l) => [...l].length)) * 12 * 0.56;
      return { id: node.getAttribute("data-node-id")!, lines, x0: x - w / 2, x1: x + w / 2, y0: y + 6, y1: y + 6 + 12 * (1.2 + 1.15 * (lines.length - 1)) };
    });
  };

  for (const width of [504, 584]) {
    it(`never cuts a connected node's name and keeps the names apart at ${width} px (#10147 UX r5 B1)`, () => {
      vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
      vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
      Object.defineProperty(SVGElement.prototype, "clientWidth", { configurable: true, get: () => width });
      try {
        // The corporate lab fleet's real names (r5 A1): every member handed over to.
        const roles = ["assistant", "developer", "design review", "communications", "finance office", "salesoperations"];
        const lab = roles.map((role, i) => ({ ...agent(`l${i}`, []), name: `Lab sample corporate ${role}`, fleet: own }));
        const labHub = { ...agent("hub", lab.map((a) => a.id)), name: "Chief of Staff orchestrator", fleet: { ...own, is_orchestrator: true } };
        const { container } = renderWithProviders(
          <AgentFlowGraph agents={[labHub, ...lab] as Agent[]} staticView layout="force" hideLegend wrapNames viewedFleetId="corp" />,
        );
        const boxes = nameBoxes(container, width);
        expect(boxes).toHaveLength(7);
        for (const b of boxes) {
          expect(b.lines.length, b.id).toBeGreaterThan(0);
          expect(b.lines.join(" "), b.id).not.toContain("…");
        }
        for (let i = 0; i < boxes.length; i++) {
          for (let j = i + 1; j < boxes.length; j++) {
            const a = boxes[i], b = boxes[j];
            const apart = a.x1 <= b.x0 || b.x1 <= a.x0 || a.y1 <= b.y0 || b.y1 <= a.y0;
            expect(apart, `${a.id} [${a.lines.join(" ")}] over ${b.id} [${b.lines.join(" ")}]`).toBe(true);
          }
        }
      } finally {
        delete (SVGElement.prototype as { clientWidth?: number }).clientWidth;
        vi.unstubAllGlobals();
      }
    });
  }

  it("draws a group node of another fleet with a long name whole (#10147 UX r5 B2)", () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
    Object.defineProperty(SVGElement.prototype, "clientWidth", { configurable: true, get: () => 584 });
    try {
      const groupName = "4 agents from Threat-informed exposure response";
      const group = { ...agent("grp", []), name: groupName, fleet: { fleet_id: "tie", fleet_name: "Threat-informed exposure response" } };
      const a1 = { ...agent("a1", ["grp"]), name: "Vulnerability triage", fleet: own };
      const a2 = { ...agent("a2", ["a1"]), name: "Fix verification", fleet: own };
      const hubA = { ...agent("hub", ["a1", "a2", "grp"]), name: "Exposure orchestrator", fleet: { ...own, is_orchestrator: true } };
      const { container } = renderWithProviders(
        <AgentFlowGraph agents={[hubA, a1, a2, group] as Agent[]} staticView layout="force" hideLegend wrapNames viewedFleetId="corp" ghostNodes={new Map([["grp", "Threat-informed exposure response"]])} />,
      );
      const node = container.querySelector('[data-node-id="grp"]')!;
      const drawn = [...node.querySelectorAll("text")].map((t) => [...t.querySelectorAll("tspan")].map((s) => s.textContent ?? "").join(" ") || (t.textContent ?? "")).join(" ");
      expect(drawn).not.toContain("…");
      expect(drawn.replace(/\s+/g, " ")).toContain("Threat-informed exposure response");
    } finally {
      delete (SVGElement.prototype as { clientWidth?: number }).clientWidth;
      vi.unstubAllGlobals();
    }
  });
});

describe("AgentFlowGraph mini graph of a dense fleet (#9760 UX rounds 3 and 4, B1)", () => {
  // The real shape of the densest built-in fleet (Threat-informed exposure
  // response, round 4): 14 members of its own fleet and 5 lone agents, each
  // lent by a different fleet, as the fleet About page draws it.
  const own = { fleet_id: "tie", fleet_name: "Threat-informed exposure response" };
  const homes = ["AI security", "Vulnerability prioritization to verified fix", "Third-party risk", "Lab fork one", "Lab fork two"];
  const ghosts = homes.map((home, i) => ({ ...agent(`g${i}`), name: `AEV Detection Rules Generator ${i}`, fleet: { fleet_id: `f${i}`, fleet_name: home } }));
  const leaves = [4, 3, 2].flatMap((n, lead) =>
    Array.from({ length: n }, (_, j) => ({ ...agent(`l${lead}${j}`), name: `OpenCTI Threat Hunt Creator ${lead}${j}`, fleet: own })),
  );
  const leads = [0, 1, 2, 3].map((lead) => ({
    ...agent(`s${lead}`, [...leaves.filter((l) => l.id.startsWith(`l${lead}`)).map((l) => l.id), ...(lead < 2 ? [`g${lead}`] : [])]),
    name: `Threat Exposure Analyst ${lead}`,
    fleet: own,
  }));
  const hub = { ...agent("hub", [...leads.map((l) => l.id), "g2", "g3", "g4"]), name: "Threat Response Orchestrator", fleet: { ...own, is_orchestrator: true } };
  const ALL = [hub, ...leads, ...leaves, ...ghosts] as Agent[];
  const ghostIds = new Map(ghosts.map((g, i) => [g.id, homes[i]] as [string, string]));
  const edges = new Set(ALL.flatMap((a) => (a.handover_agent_ids ?? []).map((t) => `${a.id}->${t}`)));
  const mini = fleetMiniGraph(ALL, ghostIds, edges, new Set(), {
    fromFleet: (n, f) => `${n} agents from ${f}`,
    fromOtherFleets: (n) => `${n} agents from other fleets`,
    more: (n) => `${n} more agents`,
  });

  it("folds 14 members and 5 lone ghosts into a frame of at most 320 px, every drawn node named", () => {
    expect(ALL).toHaveLength(19);
    expect(ALL.filter((a) => !ghostIds.has(a.id))).toHaveLength(14);
    const { container } = renderWithProviders(
      <AgentFlowGraph agents={mini.agents} staticView layout="hierarchy-tb" hideLegend wrapNames ghostNodes={mini.ghostNodes} viewedFleetId="tie" edgeIds={mini.edgeIds} />,
    );
    const nodes = [...container.querySelectorAll("[data-node-id]")];
    expect(nodes.length).toBeLessThanOrEqual(10);
    for (const node of nodes) {
      const name = [...node.querySelectorAll("text[data-node-name] tspan")].map((t) => t.textContent ?? "").join("");
      expect(name.trim(), node.getAttribute("data-node-id")!).not.toBe("");
    }
    // jsdom draws the svg at the canvas width, 1400 px: the frame on screen.
    const [, , vbW, vbH] = viewBox(container)!.split(" ").map(Number);
    expect(vbH * (1400 / vbW)).toBeLessThanOrEqual(320);
  });
});

describe("AgentFlowGraph mini graph of Threat-informed exposure response (#9760 UX round 5, B1 and B2)", () => {
  // The built-in fleet as seeded: 7 members (the orchestrator, four stage
  // leads, a hunt creator under relevance and a rule converter under
  // mobilization, the hunt creator and the risk quantifier also handing to the
  // converter) and 12 agents from other fleets, folded into one group.
  const own = { fleet_id: "tie", fleet_name: "Threat-informed exposure response" };
  const member = (id: string, name: string, handovers: string[] = []) => ({ ...agent(id, handovers), name, fleet: own });
  const outsiders = Array.from({ length: 12 }, (_, i) => ({
    ...agent(`x${i}`),
    name: `Outside Agent ${i}`,
    fleet: { fleet_id: `f${i}`, fleet_name: `Other fleet ${i}` },
  }));
  const hub = {
    ...member("orch", "Threat Response Orchestrator", ["rel", "scen", "risk", "rem", "x0", "x1"]),
    fleet: { ...own, is_orchestrator: true },
  };
  const ALL = [
    hub,
    member("rel", "Threat Relevance Analyst", ["hunt", "x2"]),
    member("scen", "OpenAEV Scenario Creator", ["x3", "x4", "x5"]),
    member("risk", "Risk Quantifier", ["conv", "x6"]),
    member("rem", "Remediation Coordinator", ["conv", "x6", "x7"]),
    member("hunt", "OpenCTI Threat Hunt Creator", ["conv", "x8"]),
    member("conv", "OpenCTI Detection Rule Converter", ["x8", "x9"]),
    ...outsiders.map((o, i) => (i === 10 ? { ...o, handover_agent_ids: ["scen"] } : o)),
  ] as Agent[];
  const ghostIds = new Map(outsiders.map((o) => [o.id, o.fleet.fleet_name] as [string, string]));
  const edges = new Set(ALL.flatMap((a) => (a.handover_agent_ids ?? []).map((t) => `${a.id}->${t}`)));
  const mini = fleetMiniGraph(ALL, ghostIds, edges, new Set(), {
    fromFleet: (n, f) => `${n} agents from ${f}`,
    fromOtherFleets: (n) => `${n} agents from other fleets`,
    more: (n) => `${n} more agents`,
  });
  const drawMini = () =>
    renderWithProviders(
      <AgentFlowGraph agents={mini.agents} staticView layout="hierarchy-tb" hideLegend wrapNames ghostNodes={mini.ghostNodes} viewedFleetId="tie" edgeIds={mini.edgeIds} />,
    );

  it("draws 8 nodes on at most three rows in a frame of at most 320 px", () => {
    const { container } = drawMini();
    const ids = [...container.querySelectorAll("[data-node-id]")].map((n) => n.getAttribute("data-node-id")!);
    expect(ids).toHaveLength(8);
    expect(new Set(ids.map((id) => centre(container, id).y)).size).toBeLessThanOrEqual(3);
    // jsdom draws the svg at the canvas width, 1400 px: the frame on screen.
    const [, , vbW, vbH] = viewBox(container)!.split(" ").map(Number);
    expect(vbH * (1400 / vbW)).toBeLessThanOrEqual(320);
  });

  it("wires the group of other fleets to the orchestrator only, one edge per direction", () => {
    const group = [...mini.edgeIds].filter((id) => id.includes("group:other-fleets")).sort();
    expect(group).toEqual(["group:other-fleets->orch", "orch->group:other-fleets"]);
  });

  it("puts the group on the orchestrator's first row, beside the stage leads, not in the chain", () => {
    const { container } = drawMini();
    const y = (id: string) => centre(container, id).y;
    expect(y("group:other-fleets")).toBe(y("rel"));
    expect(y("group:other-fleets")).toBeLessThan(y("conv"));
  });

  it("draws the two-way link between the orchestrator and the group once, an arrowhead at each end (#9760 UX round 6, B1)", () => {
    const { container } = drawMini();
    const paths = [...container.querySelectorAll("path[data-edge-id]")];
    const named = (p: Element) => p.getAttribute("aria-label") ?? "";
    const groupPaths = paths.filter((p) => named(p).includes("agents from other fleets"));
    expect(groupPaths).toHaveLength(1);
    expect(groupPaths[0].getAttribute("data-edge-two-way")).toBe("true");
    expect(groupPaths[0].getAttribute("marker-start")).toBe(groupPaths[0].getAttribute("marker-end"));
    // The path kept leaves the orchestrator, a member, toward the group.
    expect(named(groupPaths[0]).startsWith("Handover Threat Response Orchestrator to")).toBe(true);
  });
});

describe("AgentFlowGraph mini graph of AI security (#9760 UX round 6, B2)", () => {
  // The built-in fleet as seeded: the orchestrator and the ATLAS mapping
  // analyst, four agents lent by Threat-informed exposure response (folded
  // into one group), the entry assistant, the deep investigation agent and
  // the TTP harvester the analyst consults, on a row of its own.
  const own = { fleet_id: "ais", fleet_name: "AI security" };
  const tie = { fleet_id: "tie", fleet_name: "Threat-informed exposure response" };
  const lent = ["rel", "scen", "risk", "rem"].map((id) => ({ ...agent(id), name: `Threat ${id} agent`, fleet: tie }));
  const ALL = [
    { ...agent("assist", ["orch"]), name: "CTEM Assistant", fleet: { fleet_id: "con", fleet_name: "Concierge" } },
    { ...agent("orch", ["atlas", "rel", "scen", "risk", "rem", "deep"]), name: "AI Security Orchestrator", fleet: { ...own, is_orchestrator: true } },
    { ...agent("atlas", ["harv"]), name: "AI Threat ATLAS Analyst", fleet: own },
    { ...agent("harv"), name: "CTI TTP Harvester", fleet: null },
    { ...agent("deep"), name: "Deep Investigation Agent", fleet: { fleet_id: "inv", fleet_name: "Investigations" } },
    ...lent,
  ] as Agent[];
  const ghostIds = new Map<string, string>([
    ["assist", "Concierge"],
    ["deep", "Investigations"],
    ...lent.map((a) => [a.id, tie.fleet_name] as [string, string]),
  ]);
  const edges = new Set(ALL.flatMap((a) => (a.handover_agent_ids ?? []).map((t) => `${a.id}->${t}`)));
  const mini = fleetMiniGraph(ALL, ghostIds, edges, new Set(), {
    fromFleet: (n, f) => `${n} agents from ${f}`,
    fromOtherFleets: (n) => `${n} agents from other fleets`,
    more: (n) => `${n} more agents`,
  });

  it("fits a frame of at most 320 px, every drawn node named", () => {
    const { container } = renderWithProviders(
      <AgentFlowGraph agents={mini.agents} staticView layout="hierarchy-tb" hideLegend wrapNames ghostNodes={mini.ghostNodes} viewedFleetId="ais" edgeIds={mini.edgeIds} />,
    );
    const nodes = [...container.querySelectorAll("[data-node-id]")];
    expect(nodes.length).toBeGreaterThanOrEqual(6);
    for (const node of nodes) {
      const name = [...node.querySelectorAll("text[data-node-name] tspan")].map((t) => t.textContent ?? "").join("");
      expect(name.trim(), node.getAttribute("data-node-id")!).not.toBe("");
    }
    // jsdom draws the svg at the canvas width, 1400 px: the frame on screen.
    const [, , vbW, vbH] = viewBox(container)!.split(" ").map(Number);
    expect(vbH * (1400 / vbW)).toBeLessThanOrEqual(320);
  });
});

describe("AgentFlowGraph mini graph of CTEM posture and reporting (#9760 UX round 7, B1)", () => {
  // The built-in fleet as seeded: the orchestrator delegates to the five
  // stage analysts and the briefing analyst and consults the widget creator;
  // the concierge assistant hands over to it. 9 nodes, 1 hub, 7 children.
  const own = { fleet_id: "ctem", fleet_name: "CTEM posture and reporting" };
  const kids = ["CTEM Scoping Analyst", "CTEM Discovery Analyst", "CTEM Prioritization Analyst", "CTEM Validation Analyst", "CTEM Mobilization Analyst", "Exposure Briefing Analyst", "OpenCTI Dashboard Widget Creator"];
  const ALL = [
    { ...agent("assist", ["orch"]), name: "CTEM Assistant", fleet: { fleet_id: "con", fleet_name: "Concierge" } },
    { ...agent("orch", kids.map((_, i) => `k${i}`)), name: "Posture Orchestrator", fleet: { ...own, is_orchestrator: true } },
    ...kids.map((name, i) => ({ ...agent(`k${i}`), name, fleet: own })),
  ] as Agent[];
  const ghostNodes = new Map([["assist", "Concierge"]]);

  // Points along an svg path of M, L and Q commands (drawing units).
  const samples = (d: string) => {
    const tokens = d.match(/[MLQ]|-?[\d.]+(?:e-?\d+)?/g) ?? [];
    const pts: { x: number; y: number }[] = [];
    let cur = { x: 0, y: 0 };
    let i = 0;
    const num = () => Number(tokens[i++]);
    while (i < tokens.length) {
      const cmd = tokens[i++];
      if (cmd === "M") { cur = { x: num(), y: num() }; continue; }
      if (cmd === "L") {
        const to = { x: num(), y: num() };
        for (let k = 1; k <= 40; k++) pts.push({ x: cur.x + ((to.x - cur.x) * k) / 40, y: cur.y + ((to.y - cur.y) * k) / 40 });
        cur = to;
      } else if (cmd === "Q") {
        const c = { x: num(), y: num() };
        const to = { x: num(), y: num() };
        for (let k = 1; k <= 40; k++) {
          const t = k / 40, u = 1 - t;
          pts.push({ x: u * u * cur.x + 2 * u * t * c.x + t * t * to.x, y: u * u * cur.y + 2 * u * t * c.y + t * t * to.y });
        }
        cur = to;
      }
    }
    return pts;
  };

  for (const width of [778, 978, 1400]) {
    it(`runs no edge through a third node's name at ${width} px`, () => {
      vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
      // Reduced motion: the nodes are drawn where the layout puts them, no tween.
      vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
      Object.defineProperty(SVGElement.prototype, "clientWidth", { configurable: true, get: () => width });
      try {
        const { container } = renderWithProviders(
          <AgentFlowGraph agents={ALL} staticView layout="hierarchy-tb" hideLegend wrapNames ghostNodes={ghostNodes} viewedFleetId="ctem" />,
        );
        const [, , vbW] = viewBox(container)!.split(" ").map(Number);
        const unitsPerPx = vbW / width;
        // Each node's name box: centred under its glyph, as wide as its
        // longest line at 11 px, as tall as its lines.
        const boxes = [...container.querySelectorAll("[data-node-id]")].map((node) => {
          const [x, y] = (/translate\(([^ ,]+)[ ,]+([^)]+)\)/.exec(node.getAttribute("transform") ?? "") ?? []).slice(1).map(Number);
          const text = node.querySelector("text[data-node-name]")!;
          const lines = [...text.querySelectorAll("tspan")].map((t) => t.textContent ?? "");
          const half = (Math.max(...lines.map((l) => l.length)) * 11 * 0.56 * unitsPerPx) / 2;
          const top = y + Number(text.getAttribute("y")) - 11 * unitsPerPx;
          return { id: node.getAttribute("data-node-id")!, x0: x - half, x1: x + half, y0: top, y1: top + lines.length * 11 * 1.15 * unitsPerPx };
        });
        expect(boxes).toHaveLength(9);
        const hits: string[] = [];
        for (const edge of container.querySelectorAll("path[data-edge-id]")) {
          const label = edge.getAttribute("aria-label") ?? edge.getAttribute("data-edge-id")!;
          const pts = samples(edge.getAttribute("d") ?? "");
          for (const box of boxes) {
            if (label.includes(ALL.find((a) => a.id === box.id)!.name)) continue;
            const hit = pts.find((p) => p.x > box.x0 && p.x < box.x1 && p.y > box.y0 && p.y < box.y1);
            if (hit) hits.push(`${label} through ${box.id} at ${Math.round(hit.x)},${Math.round(hit.y)} (box ${Math.round(box.x0)}-${Math.round(box.x1)} x ${Math.round(box.y0)}-${Math.round(box.y1)})`);
          }
        }
        expect(hits).toEqual([]);
      } finally {
        delete (SVGElement.prototype as { clientWidth?: number }).clientWidth;
        vi.unstubAllGlobals();
      }
    });
  }
});

describe("AgentFlowGraph static view keyboard (#9760 UX round 1, B3)", () => {
  it.each(["Enter", " "])("%j on a node opens the full graph on it", (key) => {
    const onStaticOpen = vi.fn();
    const { container } = draw({ staticView: true, onStaticOpen, layout: "hierarchy-tb", wrapNames: true });
    fireEvent.keyDown(container.querySelector('[data-node-id="b"]')!, { key });
    expect(onStaticOpen).toHaveBeenCalledWith("b");
  });
});

// a -> b -> c: one chain, laid out along the flow.
const CHAIN = [agent("a", ["b"]), agent("b", ["c"]), agent("c")];
// A node's body is drawn about its own origin; its wrapper carries the place.
const centre = (container: HTMLElement, id: string) => {
  const transform = container.querySelector(`[data-node-id="${id}"]`)!.getAttribute("transform") ?? "";
  const [, x, y] = /translate\(([-\d.e]+)[ ,]+([-\d.e]+)\)/.exec(transform) ?? [];
  return { x: Number(x), y: Number(y) };
};

describe("AgentFlowGraph orientation", () => {
  it("lays the handovers out left to right by default", () => {
    const { container } = renderWithProviders(<AgentFlowGraph agents={CHAIN} />);
    const [a, b, c] = ["a", "b", "c"].map((id) => centre(container, id));
    expect(a.x).toBeLessThan(b.x);
    expect(b.x).toBeLessThan(c.x);
    expect(new Set([a.y, b.y, c.y]).size).toBe(1);
  });

  it("lays the handovers out top to bottom when vertical", () => {
    const { container } = renderWithProviders(<AgentFlowGraph agents={CHAIN} orientation="vertical" />);
    const [a, b, c] = ["a", "b", "c"].map((id) => centre(container, id));
    expect(a.y).toBeLessThan(b.y);
    expect(b.y).toBeLessThan(c.y);
    expect(new Set([a.x, b.x, c.x]).size).toBe(1);
  });

  it("stacks disconnected clusters side by side when vertical", () => {
    const { container } = renderWithProviders(<AgentFlowGraph agents={AGENTS} orientation="vertical" />);
    // {a, b, c} and {d, e} never share a column.
    const first = ["a", "b", "c"].map((id) => centre(container, id).x);
    const second = ["d", "e"].map((id) => centre(container, id).x);
    expect(Math.max(...first) < Math.min(...second) || Math.max(...second) < Math.min(...first)).toBe(true);
  });

  it("is deterministic", () => {
    const one = renderWithProviders(<AgentFlowGraph agents={AGENTS} orientation="vertical" />);
    const two = renderWithProviders(<AgentFlowGraph agents={[...AGENTS].reverse()} orientation="vertical" />);
    for (const id of ["a", "b", "c", "d", "e", "f"]) {
      expect(centre(two.container, id)).toEqual(centre(one.container, id));
    }
  });
});

// A node reached from the keyboard shows the design-system focus ring.
describe("AgentFlowGraph keyboard focus ring", () => {
  it("gives every focusable node a 2 px accent ring shown on :focus-visible only", () => {
    const { container } = draw({ layout: "force" });
    const nodes = [...container.querySelectorAll("g[data-node-id]")];
    expect(nodes.length).toBeGreaterThan(0);
    for (const n of nodes) {
      expect(n.getAttribute("tabindex")).toBe("0");
      const ring = n.querySelector(":scope > circle[data-focus-ring]")!;
      expect(ring).not.toBeNull();
      expect(ring.getAttribute("stroke")).toBe("var(--color-accent)");
      expect(ring.getAttribute("stroke-width")).toBe("2");
      expect(ring.getAttribute("vector-effect")).toBe("non-scaling-stroke");
    }
    const css = container.querySelector("svg > style")!.textContent ?? "";
    expect(css).toContain(".af-focus-ring{display:none}");
    expect(css).toContain("g[data-node-id]:focus-visible>.af-focus-ring{display:inline}");
  });

  it("draws no ring on the standalone flow, whose nodes take no focus", () => {
    const { container } = draw();
    expect(container.querySelector("circle[data-focus-ring]")).toBeNull();
  });
});

describe("AgentFlowGraph layouts", () => {
  for (const layout of ["force", "hierarchy-tb", "hierarchy-lr", "radial", "orgchart"] as const) {
    it(`places every agent, orphans included, the same way whatever the input order (${layout})`, () => {
      const one = renderWithProviders(<AgentFlowGraph agents={AGENTS} layout={layout} />);
      const two = renderWithProviders(<AgentFlowGraph agents={[...AGENTS].reverse()} layout={layout} />);
      expect(drawnIds(one.container)).toEqual(["a", "b", "c", "d", "e", "f"]);
      for (const id of ["a", "b", "c", "d", "e", "f"]) {
        expect(centre(two.container, id)).toEqual(centre(one.container, id));
      }
    });
  }

  it("draws the org chart's handovers as orthogonal lanes, never a curve", () => {
    const { container } = renderWithProviders(<AgentFlowGraph agents={AGENTS} layout="orgchart" />);
    const paths = [...container.querySelectorAll("path[marker-end]")].map((p) => p.getAttribute("d") ?? "");
    expect(paths.length).toBeGreaterThan(0);
    for (const d of paths) {
      expect(d).not.toMatch(/[QC]/);
      expect(d).toMatch(/^M [-\d.]+,[-\d.]+( L [-\d.]+,[-\d.]+)+$/);
    }
  });

  it("draws names at 11 screen px and edge labels at 9 with a layout", () => {
    // jsdom lays nothing out: the svg is measured at its 1400-unit default width,
    // one unit per px, so the drawing-unit sizes are the screen sizes.
    const { container } = renderWithProviders(<AgentFlowGraph agents={CHAIN} layout="hierarchy-lr" />);
    const name = container.querySelector('[data-node-id="a"] text[font-size="11"]');
    expect(name).toBeTruthy();
    // Edge labels are drawn on hover or focus only: none at rest.
    expect(container.querySelector('text[font-size="9"]')).toBeNull();
    fireEvent.mouseEnter(container.querySelector('[data-node-id="a"]')!);
    expect(container.querySelector('text[font-size="9"]')).toBeTruthy();
  });

  it("names every agent node for assistive technology, whatever the drawn label", () => {
    // The drawn name can be cut or left out for lack of room: the node itself
    // carries the full name, the drawn text is hidden from assistive technology.
    const { container } = renderWithProviders(<AgentFlowGraph agents={CHAIN} layout="force" />);
    for (const id of ["a", "b", "c"]) {
      const node = container.querySelector(`[data-node-id="${id}"]`)!;
      expect(node.getAttribute("role")).toBe("group");
      expect(node.getAttribute("aria-label")).toBe(id.toUpperCase());
      expect(node.querySelector('text[font-size="11"]')?.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("leaves the legend bar out when the page docks the toolbar", () => {
    const shown = renderWithProviders(<AgentFlowGraph agents={CHAIN} layout="force" />);
    expect(shown.container.querySelector('ul[aria-label="Legend"]')).toBeTruthy();
    const hidden = renderWithProviders(<AgentFlowGraph agents={CHAIN} layout="force" hideLegend />);
    expect(hidden.container.querySelector('ul[aria-label="Legend"]')).toBeNull();
  });

  it("draws a chain top to bottom in the hierarchy-tb layout", () => {
    const { container } = renderWithProviders(<AgentFlowGraph agents={CHAIN} layout="hierarchy-tb" />);
    const [a, b, c] = ["a", "b", "c"].map((id) => centre(container, id));
    expect(a.y).toBeLessThan(b.y);
    expect(b.y).toBeLessThan(c.y);
  });

  it("spreads a tall top-down drawing across the canvas width, so row neighbours keep a label's room", () => {
    // a -> b, a -> c, then a chain of six under b: seven layers, a narrow
    // and tall drawing whose uniform fit is height-bound. b and c share a row
    // and keep at least the 150 units of a label between them.
    const tall = [
      agent("a", ["b", "c"]), agent("b", ["d"]), agent("c"), agent("d", ["e"]),
      agent("e", ["f"]), agent("f", ["g"]), agent("g", ["h"]), agent("h"),
    ];
    const { container } = renderWithProviders(<AgentFlowGraph agents={tall} layout="hierarchy-tb" />);
    const [b, c] = ["b", "c"].map((id) => centre(container, id));
    expect(b.y).toBe(c.y);
    expect(Math.abs(b.x - c.x)).toBeGreaterThanOrEqual(150);
    const ys = ["a", "b", "d", "e", "f", "g", "h"].map((id) => centre(container, id).y);
    expect([...ys].sort((p, q) => p - q)).toEqual(ys);
  });
});

// A node dragged onto another: the caller decides what the drop means.
describe("AgentFlowGraph node drag", () => {
  const node = (container: HTMLElement, id: string) => container.querySelector(`[data-node-id="${id}"]`)!;
  const drag = (container: HTMLElement, from: string, to: string) => {
    fireEvent.pointerDown(node(container, from), { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(node(container, to), { clientX: 60, clientY: 40, pointerId: 1 });
    fireEvent.pointerUp(node(container, to), { clientX: 60, clientY: 40, pointerId: 1 });
  };

  it("calls onNodeDrop when a draggable node is released on a node it may drop on", () => {
    const onNodeDrop = vi.fn();
    const { container } = draw({ layout: "force", draggableNodeIds: new Set(["b"]), canDropNode: (_, t) => t === "d", onNodeDrop });
    // The test DOM lays nothing out: give the drawing a size so the line can be placed.
    vi.spyOn(container.querySelector("svg")!, "getBoundingClientRect").mockReturnValue(
      { left: 0, top: 0, width: 1400, height: 600, right: 1400, bottom: 600, x: 0, y: 0, toJSON: () => ({}) } as DOMRect,
    );
    fireEvent.pointerDown(node(container, "b"), { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(node(container, "d"), { clientX: 60, clientY: 40, pointerId: 1 });
    // While over the valid target: it is ringed and a line follows the pointer.
    expect(node(container, "d").getAttribute("data-drop-target")).toBe("true");
    expect(container.querySelector("[data-node-drag-line]")).not.toBeNull();
    fireEvent.pointerUp(node(container, "d"), { clientX: 60, clientY: 40, pointerId: 1 });
    expect(onNodeDrop).toHaveBeenCalledWith("b", "d");
    expect(container.querySelector("[data-node-drag-line]")).toBeNull();
  });

  it("drops nothing on a node it may not drop on, from a node that is not draggable, or without a move", () => {
    const onNodeDrop = vi.fn();
    const { container } = draw({ layout: "force", draggableNodeIds: new Set(["b"]), canDropNode: (_, t) => t === "d", onNodeDrop });
    drag(container, "b", "c");
    expect(node(container, "c").getAttribute("data-drop-target")).toBeNull();
    drag(container, "a", "d");
    // A press and release without a move is a click, not a drop.
    fireEvent.pointerDown(node(container, "b"), { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(node(container, "b"), { clientX: 11, clientY: 10, pointerId: 1 });
    expect(onNodeDrop).not.toHaveBeenCalled();
  });

  it("cancels a drag on Escape", () => {
    const onNodeDrop = vi.fn();
    const { container } = draw({ layout: "force", draggableNodeIds: new Set(["b"]), onNodeDrop });
    fireEvent.pointerDown(node(container, "b"), { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(node(container, "d"), { clientX: 60, clientY: 40, pointerId: 1 });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.pointerUp(node(container, "d"), { clientX: 60, clientY: 40, pointerId: 1 });
    expect(onNodeDrop).not.toHaveBeenCalled();
  });

  it("draws a solid line over a valid target and a dashed one with a not-allowed cursor over a refused node", () => {
    const { container } = draw({ layout: "force", draggableNodeIds: new Set(["b"]), canDropNode: (_, t) => t === "d", onNodeDrop: vi.fn() });
    vi.spyOn(container.querySelector("svg")!, "getBoundingClientRect").mockReturnValue(
      { left: 0, top: 0, width: 1400, height: 600, right: 1400, bottom: 600, x: 0, y: 0, toJSON: () => ({}) } as DOMRect,
    );
    fireEvent.pointerDown(node(container, "b"), { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(node(container, "d"), { clientX: 60, clientY: 40, pointerId: 1 });
    const valid = container.querySelector("[data-node-drag-line]")!;
    expect(valid.getAttribute("data-valid")).toBe("true");
    expect(valid.getAttribute("stroke-dasharray")).toBeNull();
    expect(valid.getAttribute("opacity")).toBeNull();
    const validStroke = valid.getAttribute("stroke");
    expect(node(container, "d").querySelector("[data-drop-ring]")?.getAttribute("stroke")).toBe(validStroke);
    fireEvent.pointerMove(node(container, "c"), { clientX: 90, clientY: 40, pointerId: 1 });
    const refused = container.querySelector("[data-node-drag-line]")!;
    expect(refused.getAttribute("data-valid")).toBe("false");
    expect(refused.getAttribute("stroke-dasharray")).toBe("3 3");
    expect(refused.getAttribute("stroke")).not.toBe(validStroke);
    expect(node(container, "c").getAttribute("data-drop-refused")).toBe("true");
    expect((node(container, "c") as SVGGElement).style.cursor).toBe("not-allowed");
    expect(node(container, "c").querySelector("[data-drop-ring]")).toBeNull();
  });

  it("rings the node dropped on for a moment after a drop", () => {
    vi.useFakeTimers();
    try {
      const { container } = draw({ layout: "force", draggableNodeIds: new Set(["b"]), canDropNode: (_, t) => t === "d", onNodeDrop: vi.fn() });
      drag(container, "b", "d");
      expect(node(container, "d").getAttribute("data-drop-flash")).toBe("true");
      expect(node(container, "d").querySelector("[data-drop-flash-ring]")).not.toBeNull();
      act(() => { vi.advanceTimersByTime(1300); });
      expect(node(container, "d").getAttribute("data-drop-flash")).toBeNull();
      // A refused drop rings nothing.
      drag(container, "b", "c");
      expect(container.querySelector("[data-drop-flash]")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  // The rings never paint over the member's name.
  it("draws the drop and new parent rings under the name, the name with a halo", () => {
    vi.useFakeTimers();
    try {
      const { container } = draw({ layout: "force", draggableNodeIds: new Set(["b"]), canDropNode: (_, t) => t === "d", onNodeDrop: vi.fn() });
      vi.spyOn(container.querySelector("svg")!, "getBoundingClientRect").mockReturnValue(
        { left: 0, top: 0, width: 1400, height: 600, right: 1400, bottom: 600, x: 0, y: 0, toJSON: () => ({}) } as DOMRect,
      );
      const ringUnderName = (ringSelector: string) => {
        const target = node(container, "d");
        const ring = target.querySelector(ringSelector)!;
        const name = target.querySelector("[data-node-name]")!;
        // Later in the group paints on top: the name must come after the ring.
        expect(ring.compareDocumentPosition(name) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(name.getAttribute("stroke")).not.toBeNull();
        expect(name.getAttribute("paint-order")).toBe("stroke");
      };
      fireEvent.pointerDown(node(container, "b"), { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
      fireEvent.pointerMove(node(container, "d"), { clientX: 60, clientY: 40, pointerId: 1 });
      ringUnderName("[data-drop-ring]");
      fireEvent.pointerUp(node(container, "d"), { clientX: 60, clientY: 40, pointerId: 1 });
      ringUnderName("[data-drop-flash-ring]");
      act(() => { vi.advanceTimersByTime(1300); });
      // The ring is gone; the name keeps the canvas halo every node name
      // wears.
      expect(node(container, "d").querySelector("[data-drop-flash-ring]")).toBeNull();
      expect(node(container, "d").querySelector("[data-node-name]")!.getAttribute("paint-order")).toBe("stroke");
    } finally {
      vi.useRealTimers();
    }
  });

  // A drop whose save fails shows no new parent.
  it("rings the target only once a drop returning a promise is saved", async () => {
    {
      const saved = draw({ layout: "force", draggableNodeIds: new Set(["b"]), canDropNode: (_, t) => t === "d", onNodeDrop: () => Promise.resolve(true) });
      drag(saved.container, "b", "d");
      await act(async () => { await Promise.resolve(); });
      expect(node(saved.container, "d").getAttribute("data-drop-flash")).toBe("true");
      saved.unmount();

      const failed = draw({ layout: "force", draggableNodeIds: new Set(["b"]), canDropNode: (_, t) => t === "d", onNodeDrop: () => Promise.resolve(false) });
      drag(failed.container, "b", "d");
      await act(async () => { await Promise.resolve(); });
      expect(failed.container.querySelector("[data-drop-flash]")).toBeNull();
      expect(failed.container.querySelector("[data-drop-flash-ring]")).toBeNull();
      failed.unmount();

      const rejected = draw({ layout: "force", draggableNodeIds: new Set(["b"]), canDropNode: (_, t) => t === "d", onNodeDrop: () => Promise.reject(new Error("500")) });
      drag(rejected.container, "b", "d");
      await act(async () => { await Promise.resolve(); });
      expect(rejected.container.querySelector("[data-drop-flash]")).toBeNull();
    }
  });

  it("draws nothing draggable without onNodeDrop", () => {
    const { container } = draw({ layout: "force", draggableNodeIds: new Set(["b"]) });
    drag(container, "b", "d");
    expect(container.querySelector("[data-drop-target]")).toBeNull();
    expect(container.querySelector("[data-node-drag-line]")).toBeNull();
  });
});

// The connect hint, the hover tooltip and the edge names on both graphs.
describe("AgentFlowGraph handover editing", () => {
  const node = (container: HTMLElement, id: string) => container.querySelector(`[data-node-id="${id}"]`)!;
  const editable = () => draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
  // A single click is deferred 250 ms to tell it from a double click.
  const click = (el: Element) => {
    fireEvent.click(el, { detail: 1 });
    act(() => { vi.advanceTimersByTime(300); });
  };

  it("shows a translated connect hint in two sentences, with no dash", () => {
    vi.useFakeTimers();
    try {
      const { container } = editable();
      click(node(container, "a"));
      const hint = screen.getByText("Click a target agent to connect. Press Esc to cancel.");
      expect(hint.textContent).not.toMatch(/[\u2013\u2014]/);
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.queryByText("Click a target agent to connect. Press Esc to cancel.")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("draws no hover tooltip over the type picker once it opens", () => {
    vi.useFakeTimers();
    try {
      const { container } = editable();
      click(node(container, "a"));
      fireEvent.mouseEnter(node(container, "c"));
      expect(screen.getByText("Click to connect from A")).toBeTruthy();
      click(node(container, "c"));
      expect(container.querySelector("[data-type-picker]")).not.toBeNull();
      expect(screen.queryByText("Click to connect from A")).toBeNull();
      // The pointer still over the node, a new hover does not draw over the picker either.
      fireEvent.mouseEnter(node(container, "c"));
      expect(screen.queryByText(/Click to start handover|Click to connect from/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("names every handover edge, as a button where a click edits it", () => {
    const readOnly = draw({ layout: "force" });
    const named = readOnly.getAllByRole("img", { name: /^Handover [A-F] to [A-F], / });
    expect(named.map((p) => p.getAttribute("aria-label")!.split(",")[0]).sort()).toEqual([
      "Handover A to B", "Handover A to C", "Handover B to C", "Handover E to D",
    ]);
    readOnly.unmount();
    const { getAllByRole } = editable();
    // a and b are editable: their three edges are buttons; e -> d is not.
    const buttons = getAllByRole("button", { name: /^Handover / }).map((p) => p.getAttribute("aria-label")!.split(",")[0]);
    expect(buttons.sort()).toEqual(["Handover A to B", "Handover A to C", "Handover B to C"]);
    expect(getAllByRole("img", { name: /^Handover E to D, / })).toHaveLength(1);
  });

  it("keeps the connect hint on one line, cut to the graph's width", () => {
    vi.useFakeTimers();
    try {
      const { container } = editable();
      click(node(container, "a"));
      const hint = container.querySelector<HTMLElement>("[data-connect-hint]")!;
      expect(hint.style.width).toBe("max-content");
      expect(hint.style.maxWidth).toBe("calc(100% - 2rem)");
      expect(hint.style.whiteSpace).toBe("nowrap");
      // The whole sentence stays reachable when it is cut.
      expect(hint.getAttribute("title")).toBe("Click a target agent to connect. Press Esc to cancel.");
    } finally {
      vi.useRealTimers();
    }
  });

  it("names an edge in French with its translated type", () => {
    const fr = {
      "Handover {source} to {target}, {type}": "Transfert de {source} vers {target}, {type}",
      Delegate: "Déléguer",
      "All types": "Tous les types",
    };
    // a -> b has no type yet (every type), c -> d is a delegation.
    const typed = [agent("a", ["b"]), agent("b"), { ...agent("c", ["d"]), handover_types: { d: "delegate" } } as Agent, agent("d")];
    const view = render(
      <QueryClientProvider client={testQueryClient()}>
        <IntlProvider locale="fr" messages={fr} onError={() => {}}>
          <AgentFlowGraph agents={typed} layout="force" />
        </IntlProvider>
      </QueryClientProvider>,
    );
    const names = view.getAllByRole("img", { name: /^Transfert de / }).map((p) => p.getAttribute("aria-label"));
    expect(names.sort()).toEqual(["Transfert de A vers B, tous les types", "Transfert de C vers D, déléguer"]);
  });

  it("translates the type picker, and tips the source that a click cancels", () => {
    const fr = {
      "Select handover type": "Choisir le type de transfert",
      "Edit handover type": "Modifier le type de transfert",
      Delegate: "Déléguer",
      "Async background task": "Tâche asynchrone en arrière-plan",
      "Mandatory transfer": "Transfert obligatoire",
      Cancel: "Annuler",
      Save: "Enregistrer",
      Delete: "Supprimer",
      "Click to cancel": "Cliquer pour annuler",
    };
    vi.useFakeTimers();
    try {
      const view = render(
        <QueryClientProvider client={testQueryClient()}>
          <IntlProvider locale="fr" messages={fr} onError={() => {}}>
            <AgentFlowGraph agents={AGENTS} layout="force" onAddEdge={vi.fn()} onEditEdge={vi.fn()} editableAgentIds={new Set(["a", "b", "c"])} />
          </IntlProvider>
        </QueryClientProvider>,
      );
      const { container } = view;
      click(node(container, "a"));
      // In connect mode the source's own tip says the next click cancels.
      fireEvent.mouseEnter(node(container, "a"));
      expect(view.getByText(/^A · Cliquer pour annuler/)).toBeTruthy();
      expect(view.queryByText(/Click to start handover/)).toBeNull();
      click(node(container, "f"));
      const picker = container.querySelector<HTMLElement>("[data-type-picker]")!;
      expect(picker.textContent).toContain("Choisir le type de transfert");
      expect(picker.textContent).toContain("Déléguer");
      expect(picker.textContent).toContain("Tâche asynchrone en arrière-plan");
      expect(picker.textContent).toContain("Transfert obligatoire");
      expect(picker.textContent).toContain("Annuler");
      expect(picker.textContent).toContain("Enregistrer");
      expect(picker.textContent).not.toMatch(/Select handover type|Async background task|Save/);
    } finally {
      vi.useRealTimers();
    }
  });

  it("translates the edge editor opened from an edge", () => {
    const fr = { "Edit handover type": "Modifier le type de transfert", Delete: "Supprimer", Save: "Enregistrer" };
    const view = render(
      <QueryClientProvider client={testQueryClient()}>
        <IntlProvider locale="fr" messages={fr} onError={() => {}}>
          <AgentFlowGraph agents={AGENTS} layout="force" onAddEdge={vi.fn()} onEditEdge={vi.fn()} editableAgentIds={new Set(["a", "b", "c"])} />
        </IntlProvider>
      </QueryClientProvider>,
    );
    fireEvent.keyDown(view.getAllByRole("button", { name: /^Handover A to B, / })[0], { key: "Enter" });
    const editor = view.container.querySelector<HTMLElement>("[data-edge-editor]")!;
    expect(editor.textContent).toContain("Modifier le type de transfert");
    expect(editor.textContent).toContain("Supprimer");
    expect(editor.textContent).toContain("Enregistrer");
    expect(editor.textContent).not.toMatch(/Edit handover type|Delete/);
  });

  it("reaches an editable edge with Tab and opens its editor with Enter", () => {
    const onEditEdge = vi.fn();
    const { container, getByRole } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge, editableAgentIds: new Set(["a", "b", "c"]) });
    const edge = getByRole("button", { name: /^Handover A to B, / });
    expect(edge.getAttribute("tabindex")).toBe("0");
    edge.focus();
    expect(document.activeElement).toBe(edge);
    fireEvent.keyDown(edge, { key: "Enter" });
    expect(container.querySelector("[data-edge-editor]")).not.toBeNull();
  });

  it("opens an edge's editor with Space too, and leaves a read-only edge out of the tab order", () => {
    const { container, getByRole } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    fireEvent.keyDown(getByRole("button", { name: /^Handover B to C, / }), { key: " " });
    expect(container.querySelector("[data-edge-editor]")).not.toBeNull();
    expect(getByRole("img", { name: /^Handover E to D, / }).getAttribute("tabindex")).toBeNull();
  });

  it("draws the focused edge band opaque enough to read 3:1 on the canvas", () => {
    const { container } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    const css = [...container.querySelectorAll("style")].map((s) => s.textContent ?? "").join("\n");
    const rule = css.split("\n").find((line) => line.startsWith("path[data-edge-hit]:focus-visible"));
    expect(rule).toBeDefined();
    const opacity = Number(/stroke-opacity:([0-9.]+)/.exec(rule ?? "")?.[1]);
    expect(opacity).toBeGreaterThanOrEqual(0.8);
  });

  // #12347 UX r3 B3: a liaison is never a handover, so never the read-only handover tip.
  it("gives an A2A liaison edge its own tip, never the handover read-only tip", () => {
    const { container } = renderWithProviders(
      <AgentFlowGraph
        agents={[agent("p", ["a"]), agent("a")]}
        layout="force"
        onAddEdge={vi.fn()}
        onEditEdge={vi.fn()}
        editableAgentIds={new Set(["a"])}
        ghostNodes={new Map([["p", "Partner"]])}
        edgeTypeOverrides={new Map([["p->a", "a2a_inbound"]])}
      />,
    );
    const hits = [...container.querySelectorAll<SVGPathElement>('path[stroke="transparent"]')];
    expect(hits.length).toBeGreaterThan(0);
    fireEvent.mouseEnter(hits[0]);
    const tip = container.querySelector('[data-testid="agent-flow-tooltip"]');
    expect(tip?.textContent).toBe("A2A liaison: this partner tasks the agent through an A2A integration");
  });

  it("keeps the edge tooltip while the pointer moves from the hit path onto the label it reveals (#9106)", () => {
    const { container, getByRole } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    const hit = getByRole("button", { name: /^Handover A to B, / });
    fireEvent.mouseEnter(hit);
    expect(container.querySelector('[data-testid="agent-flow-tooltip"]')).not.toBeNull();
    const label = container.querySelector<SVGTextElement>("text[data-edge-label]");
    expect(label).not.toBeNull();
    fireEvent.mouseLeave(hit, { relatedTarget: label });
    fireEvent.mouseEnter(label!, { relatedTarget: hit });
    expect(container.querySelector('[data-testid="agent-flow-tooltip"]')).not.toBeNull();
    expect(container.querySelector("text[data-edge-label]")).not.toBeNull();
    fireEvent.mouseLeave(label!, { relatedTarget: hit });
    expect(container.querySelector('[data-testid="agent-flow-tooltip"]')).not.toBeNull();
    // Leaving the edge for the canvas hides it.
    fireEvent.mouseLeave(hit, { relatedTarget: container.querySelector("svg") });
    expect(container.querySelector('[data-testid="agent-flow-tooltip"]')).toBeNull();
  });

  it("lets the pointer through a read-only edge label and the travelling particles (#9106)", () => {
    const { container } = draw({ layout: "force" });
    fireEvent.mouseEnter(container.querySelector('[data-node-id="a"]')!);
    const label = container.querySelector("text[data-edge-label]");
    expect(label).not.toBeNull();
    expect(label!.getAttribute("pointer-events")).toBe("none");
    for (const circle of container.querySelectorAll("circle")) {
      if (!(circle as SVGCircleElement).style.offsetPath) continue;
      expect(circle.closest('g[pointer-events="none"]')).not.toBeNull();
    }
  });

  it("paints the focused edge in the design system's focus colour", () => {
    const { container } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    const css = [...container.querySelectorAll("style")].map((s) => s.textContent ?? "").join("\n");
    const rule = css.split("\n").find((line) => line.startsWith("path[data-edge-hit]:focus-visible"));
    // ring-focus (BUTTON_ROLE_FOCUS_RING) paints --color-filigran-brand-primary.
    expect(rule).toContain("stroke:var(--color-filigran-brand-primary)");
  });

  it("moves the focus into the editor opened by Enter, and back to the edge on close", () => {
    const { container, getByRole } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    const edge = getByRole("button", { name: /^Handover A to B, / });
    edge.focus();
    fireEvent.keyDown(edge, { key: "Enter" });
    const editor = container.querySelector<HTMLElement>("[data-edge-editor]")!;
    expect(editor.contains(document.activeElement)).toBe(true);
    expect(document.activeElement?.getAttribute("data-handover-option")).not.toBeNull();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(container.querySelector("[data-edge-editor]")).toBeNull();
    expect(document.activeElement).toBe(edge);
  });

  it("keeps the editor opened by Enter clear of the focused edge", () => {
    const { container, getByRole } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    fireEvent.keyDown(getByRole("button", { name: /^Handover A to B, / }), { key: "Enter" });
    const editor = container.querySelector<HTMLElement>("[data-edge-editor]")!;
    // The edge's whole box is handed to placePopover, whose geometry is tested below.
    for (const side of ["top", "bottom", "left", "right"]) {
      expect(Number.isFinite(Number(editor.getAttribute(`data-avoid-${side}`)))).toBe(true);
    }
    expect(parseFloat(editor.style.top)).not.toBeNaN();
  });

  it("opens no keep-clear box for an editor opened by a click", () => {
    const { container, getByRole } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    fireEvent.click(getByRole("button", { name: /^Handover A to B, / }), { clientX: 10, clientY: 10 });
    const editor = container.querySelector<HTMLElement>("[data-edge-editor]")!;
    expect(editor.hasAttribute("data-avoid-top")).toBe(false);
  });

  it("gives the focus to the source node after Delete from the keyboard", () => {
    const onEditEdge = vi.fn();
    const { container, getByRole } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge, editableAgentIds: new Set(["a", "b", "c"]) });
    const edge = getByRole("button", { name: /^Handover A to B, / });
    edge.focus();
    fireEvent.keyDown(edge, { key: "Enter" });
    const editor = getByRole("dialog", { name: "Edit handover type" });
    fireEvent.click(within(editor).getByRole("button", { name: "Delete" }));
    expect(onEditEdge).toHaveBeenCalledWith("a", "b", "delete");
    expect(container.querySelector("[data-edge-editor]")).toBeNull();
    expect(document.activeElement).toBe(node(container, "a"));
  });

  it("starts and ends a handover with Space on the nodes, then focuses the picker", () => {
    const onAddEdge = vi.fn();
    const { container } = draw({ layout: "force", onAddEdge, onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    fireEvent.keyDown(node(container, "a"), { key: " " });
    expect(screen.getByText("Click a target agent to connect. Press Esc to cancel.")).toBeTruthy();
    const target = node(container, "f") as SVGGElement;
    target.focus();
    fireEvent.keyDown(target, { key: " " });
    const picker = container.querySelector<HTMLElement>("[data-type-picker]")!;
    expect(picker).not.toBeNull();
    expect(picker.contains(document.activeElement)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    // A's targets run every type, so the picker keeps them so by default.
    expect(onAddEdge).toHaveBeenCalledWith("a", "f", ALL_HANDOVER_TYPES);
    expect(document.activeElement).toBe(target);
  });

  it("ends a handover being drawn with Enter, while Enter at rest still opens the drawer", () => {
    const onShowDetails = vi.fn();
    const { container } = draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), onShowDetails, editableAgentIds: new Set(["a", "b", "c"]) });
    fireEvent.keyDown(node(container, "a"), { key: "Enter" });
    expect(onShowDetails).toHaveBeenCalledWith("a");
    fireEvent.keyDown(node(container, "a"), { key: " " });
    fireEvent.keyDown(node(container, "c"), { key: "Enter" });
    expect(container.querySelector("[data-type-picker]")).not.toBeNull();
    expect(onShowDetails).toHaveBeenCalledTimes(1);
  });
});

// One popover for the type picker and the edge editor.
describe("AgentFlowGraph handover type popover", () => {
  const editable = () => draw({ layout: "force", onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });

  it("draws the edge editor's Delete in the error tone that reads 4.5:1, never error-primary", () => {
    const { getByRole } = editable();
    fireEvent.keyDown(getByRole("button", { name: /^Handover A to B, / }), { key: "Enter" });
    const editor = getByRole("dialog", { name: "Edit handover type" });
    const remove = within(editor).getByRole("button", { name: "Delete" });
    expect(remove.getAttribute("data-tone")).toBe("error");
    // The token is pinned in the source: error-tertiary reads 4.5:1 on the popover in both
    // themes, error-primary only 4.26:1 in dark.
    const source = readFileSync(resolve(process.cwd(), "src/components/ui/handover-type-popover.tsx"), "utf8");
    expect(source).toContain("text-feedback-error-tertiary");
    expect(source).not.toMatch(/(^|[\s:])text-feedback-error-primary/);
  });

  it("shares the header, option rows and footer between the picker and the editor", () => {
    vi.useFakeTimers();
    try {
      const { container, getByRole } = editable();
      fireEvent.keyDown(getByRole("button", { name: /^Handover A to B, / }), { key: "Enter" });
      const editor = getByRole("dialog", { name: "Edit handover type" });
      const optionKeys = (dialog: HTMLElement) =>
        [...dialog.querySelectorAll("[data-handover-option]")].map((b) => b.getAttribute("data-handover-option"));
      const footer = (dialog: HTMLElement) =>
        within(dialog).getAllByRole("button").filter((b) => !b.hasAttribute("data-handover-option")).map((b) => b.textContent);
      const editorOptions = optionKeys(editor);
      expect(editorOptions.length).toBeGreaterThan(0);
      expect(within(editor).getByText("Edit handover type")).toBeTruthy();
      expect(footer(editor)).toEqual(["Delete", "Cancel", "Save"]);
      fireEvent.keyDown(window, { key: "Escape" });
      fireEvent.keyDown(container.querySelector('[data-node-id="a"]')!, { key: " " });
      fireEvent.keyDown(container.querySelector('[data-node-id="f"]')!, { key: " " });
      const picker = getByRole("dialog", { name: "Select handover type" });
      expect(within(picker).getByText("Select handover type")).toBeTruthy();
      // Same option rows (one per type, in the same order) and the same footer, without Delete.
      expect(optionKeys(picker)).toEqual(editorOptions);
      expect(footer(picker)).toEqual(["Cancel", "Save"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps Tab and Shift+Tab inside the editor", () => {
    const { getByRole } = editable();
    fireEvent.keyDown(getByRole("button", { name: /^Handover A to B, / }), { key: "Enter" });
    const editor = getByRole("dialog", { name: "Edit handover type" });
    expect(editor.getAttribute("aria-modal")).toBe("true");
    const buttons = within(editor).getAllByRole("button");
    buttons[0].focus();
    fireEvent.keyDown(buttons[0], { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(buttons[buttons.length - 1]);
    fireEvent.keyDown(buttons[buttons.length - 1], { key: "Tab" });
    expect(document.activeElement).toBe(buttons[0]);
  });

  it("names the mandatory transfer in sentence case", () => {
    const { container, getByRole } = editable();
    fireEvent.keyDown(getByRole("button", { name: /^Handover A to B, / }), { key: "Enter" });
    const editor = container.querySelector<HTMLElement>("[data-edge-editor]")!;
    expect(editor.textContent).toContain("Mandatory transfer");
    expect(editor.textContent).not.toContain("Mandatory Transfer");
  });
});

// Where a handover type popover goes: never over the box it keeps clear.
describe("placePopover", () => {
  const intersects = (p: { left: number; top: number }, w: number, h: number, b: PopoverAvoidBox) =>
    p.left < b.right && p.left + w > b.left && p.top < b.bottom && p.top + h > b.top;
  const inside = (p: { left: number; top: number }, w: number, h: number, cw: number, ch: number) =>
    p.left >= 8 && p.top >= 8 && p.left + w <= cw - 8 && p.top + h <= ch - 8;
  const W = 288;
  const H = 288;

  it("hangs under the pointer, centred and clamped, with no box to keep clear", () => {
    expect(placePopover({ x: 500, y: 100, width: W, height: H, containerWidth: 1200, containerHeight: 600 })).toEqual({ left: 356, top: 108 });
    expect(placePopover({ x: 1190, y: 590, width: W, height: H, containerWidth: 1200, containerHeight: 600 })).toEqual({ left: 904, top: 304 });
  });

  it("goes below the box when it fits, else above it", () => {
    const high = { top: 100, bottom: 130, left: 400, right: 600 };
    const p1 = placePopover({ x: 500, y: 115, width: W, height: H, containerWidth: 1200, containerHeight: 600, avoid: high });
    expect(p1.top).toBe(138);
    expect(intersects(p1, W, H, high)).toBe(false);
    const low = { top: 500, bottom: 530, left: 400, right: 600 };
    const p2 = placePopover({ x: 500, y: 515, width: W, height: H, containerWidth: 1200, containerHeight: 600, avoid: low });
    expect(p2.top).toBe(204);
    expect(intersects(p2, W, H, low)).toBe(false);
  });

  it("goes beside the box on a canvas too short for below and above (1440 dark, 512 px)", () => {
    const edge = { top: 235, bottom: 272, left: 420, right: 580 };
    const p = placePopover({ x: 500, y: 253, width: W, height: H, containerWidth: 1194, containerHeight: 512, avoid: edge });
    expect(intersects(p, W, H, edge)).toBe(false);
    expect(inside(p, W, H, 1194, 512)).toBe(true);
  });

  it("goes beside the box near the bottom of a 334 px canvas (1024 light)", () => {
    const edge = { top: 290, bottom: 314, left: 280, right: 340 };
    const p = placePopover({ x: 310, y: 302, width: W, height: H, containerWidth: 778, containerHeight: 334, avoid: edge });
    expect(intersects(p, W, H, edge)).toBe(false);
    expect(inside(p, W, H, 778, 334)).toBe(true);
  });

  it("goes to the left of a box at the right edge of a short canvas", () => {
    const node = { top: 150, bottom: 190, left: 700, right: 760 };
    const p = placePopover({ x: 730, y: 170, width: W, height: H, containerWidth: 778, containerHeight: 334, avoid: node });
    expect(p.left + W).toBeLessThanOrEqual(node.left);
    expect(intersects(p, W, H, node)).toBe(false);
  });

  it("keeps clear of the edge's end nodes as well as the edge when a placement allows it", () => {
    // A short edge between two nodes: below the edge alone would land on the lower node.
    const edge = { top: 200, bottom: 240, left: 480, right: 560 };
    const source = { top: 150, bottom: 200, left: 440, right: 500 };
    const target = { top: 240, bottom: 290, left: 540, right: 600 };
    const p = placePopover({ x: 520, y: 220, width: W, height: H, containerWidth: 1194, containerHeight: 512, avoid: edge, alsoAvoid: [source, target] });
    for (const b of [edge, source, target]) expect(intersects(p, W, H, b)).toBe(false);
    expect(inside(p, W, H, 1194, 512)).toBe(true);
  });

  it("never covers an end node on the 1024 canvas whenever one free place exists", () => {
    // The rendered sizes at 1024: an editor 288x337 taller than the 778x333 canvas.
    const w = 288;
    const h = 337;
    const edge = { top: 120, bottom: 160, left: 300, right: 380 };
    const source = { top: 80, bottom: 130, left: 260, right: 320 };
    const target = { top: 150, bottom: 200, left: 360, right: 420 };
    const p = placePopover({ x: 340, y: 140, width: w, height: h, containerWidth: 778, containerHeight: 333, avoid: edge, alsoAvoid: [source, target] });
    for (const b of [edge, source, target]) expect(intersects(p, w, h, b)).toBe(false);
    expect(p).toEqual({ left: 428, top: 8 });
    expect(p.left + w).toBeLessThanOrEqual(778 - 8);
  });

  it("goes left of the end nodes on the 1024 canvas when the right has no room", () => {
    const w = 288;
    const h = 337;
    const edge = { top: 120, bottom: 160, left: 480, right: 560 };
    const source = { top: 80, bottom: 130, left: 440, right: 500 };
    const target = { top: 150, bottom: 200, left: 540, right: 600 };
    const p = placePopover({ x: 520, y: 140, width: w, height: h, containerWidth: 778, containerHeight: 333, avoid: edge, alsoAvoid: [source, target] });
    for (const b of [edge, source, target]) expect(intersects(p, w, h, b)).toBe(false);
    expect(p).toEqual({ left: 144, top: 8 });
  });

  it("still keeps the edge clear when no place clears the end nodes too", () => {
    const edge = { top: 150, bottom: 180, left: 340, right: 440 };
    // End nodes wider than the canvas leaves room for: no place clears them all.
    const source = { top: 8, bottom: 326, left: 8, right: 330 };
    const target = { top: 8, bottom: 326, left: 450, right: 770 };
    const p = placePopover({ x: 390, y: 165, width: W, height: H, containerWidth: 778, containerHeight: 334, avoid: edge, alsoAvoid: [source, target] });
    expect(p).toEqual(placePopover({ x: 390, y: 165, width: W, height: H, containerWidth: 778, containerHeight: 334, avoid: edge }));
  });
});

// A click on a node and Enter open the agent drawer alike.
describe("AgentFlowGraph agent drawer affordances", () => {
  const disabled = { ...agent("z"), enabled: false } as Agent;
  const nodeEl = (container: HTMLElement, id: string) => container.querySelector(`[data-node-id="${id}"]`)!;
  // A single click is deferred 250 ms to tell it from a double click.
  const click = (el: Element) => {
    fireEvent.click(el, { detail: 1 });
    act(() => { vi.advanceTimersByTime(300); });
  };

  it("opens the drawer on a disabled agent from a click and from Enter", () => {
    vi.useFakeTimers();
    try {
      const onShowDetails = vi.fn();
      const { container } = renderWithProviders(<AgentFlowGraph agents={[...AGENTS, disabled]} onShowDetails={onShowDetails} />);
      click(nodeEl(container, "z"));
      fireEvent.keyDown(nodeEl(container, "z"), { key: "Enter" });
      expect(onShowDetails.mock.calls).toEqual([["z"], ["z"]]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("draws no \"i\" on any node", () => {
    const { container } = draw({ onShowDetails: () => {}, onAddEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });
    expect(container.querySelector("[data-node-info]")).toBeNull();
  });
});

// A click selects and opens the drawer; the connect handle draws the handover.
describe("AgentFlowGraph click to select, handle to connect", () => {
  const nodeEl = (container: HTMLElement, id: string) => container.querySelector(`[data-node-id="${id}"]`)!;
  const handle = (container: HTMLElement, id: string) => container.querySelector(`[data-node-id="${id}"] [data-node-connect]`);
  const HINT = "Click a target agent to connect. Press Esc to cancel.";
  const mapProps = () => ({ onShowDetails: vi.fn(), onAddEdge: vi.fn(), onEditEdge: vi.fn(), editableAgentIds: new Set(["a", "b", "c"]) });

  it("opens the drawer on a plain click and starts no handover", () => {
    vi.useFakeTimers();
    try {
      const props = mapProps();
      const { container } = draw(props);
      fireEvent.click(nodeEl(container, "a"), { detail: 1 });
      act(() => { vi.advanceTimersByTime(300); });
      expect(props.onShowDetails).toHaveBeenCalledWith("a");
      expect(screen.queryByText(HINT)).toBeNull();
      expect(container.querySelector("[data-type-picker]")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("draws a connect handle on the agents a handover may start from only", () => {
    const { container } = draw(mapProps());
    expect(handle(container, "a")).not.toBeNull();
    expect(handle(container, "b")).not.toBeNull();
    // d is not editable: no handover starts from it.
    expect(handle(container, "d")).toBeNull();
  });

  it("creates the handover when the handle is dragged onto another agent", () => {
    const props = mapProps();
    const { container } = draw(props);
    fireEvent.pointerDown(handle(container, "a")!, { button: 0, pointerId: 1 });
    expect(screen.getByText(HINT)).toBeTruthy();
    fireEvent.pointerUp(nodeEl(container, "f"), { button: 0, pointerId: 1 });
    expect(container.querySelector("[data-type-picker]")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(props.onAddEdge).toHaveBeenCalledWith("a", "f", ALL_HANDOVER_TYPES);
    expect(props.onShowDetails).not.toHaveBeenCalled();
  });

  it("keeps the handover started when the handle is released in place, and ends it on the next click", () => {
    vi.useFakeTimers();
    try {
      const props = mapProps();
      const { container } = draw(props);
      fireEvent.pointerDown(handle(container, "a")!, { button: 0, pointerId: 1 });
      fireEvent.pointerUp(handle(container, "a")!, { button: 0, pointerId: 1 });
      fireEvent.click(handle(container, "a")!, { detail: 1 });
      expect(screen.getByText(HINT)).toBeTruthy();
      fireEvent.click(nodeEl(container, "f"), { detail: 1 });
      act(() => { vi.advanceTimersByTime(300); });
      expect(container.querySelector("[data-type-picker]")).not.toBeNull();
      expect(props.onShowDetails).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("tips the connect handle through the catalog", () => {
    const fr = { "Drag to an agent to start a handover": "Faire glisser vers un agent pour lancer un transfert" };
    const view = render(
      <QueryClientProvider client={testQueryClient()}>
        <IntlProvider locale="fr" messages={fr} onError={() => {}}>
          <AgentFlowGraph agents={AGENTS} {...mapProps()} />
        </IntlProvider>
      </QueryClientProvider>,
    );
    fireEvent.mouseEnter(handle(view.container, "b")!);
    expect(view.getByText("Faire glisser vers un agent pour lancer un transfert")).toBeTruthy();
  });

  it("still starts a handover with Space, with the drawer on the node", () => {
    const props = mapProps();
    const { container } = draw({ ...props, layout: "force" });
    fireEvent.keyDown(nodeEl(container, "a"), { key: " " });
    expect(screen.getByText(HINT)).toBeTruthy();
    expect(props.onShowDetails).not.toHaveBeenCalled();
  });
});

describe("AgentFlowGraph drawer reveal", () => {

  it("pans a node drawn under the drawer back into view, once per selection", () => {
    const rect = { left: 0, top: 0, right: 1000, bottom: 400, width: 1000, height: 400, x: 0, y: 0, toJSON: () => ({}) };
    const spy = vi.spyOn(SVGElement.prototype, "getBoundingClientRect").mockReturnValue(rect as DOMRect);
    const width = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1000 });
    try {
      const { container, rerender } = renderWithProviders(<AgentFlowGraph agents={AGENTS} />);
      const before = viewBox(container);
      // The rightmost drawn node is under a 420 px drawer on a 1000 px window.
      const xs = [...container.querySelectorAll("[data-node-id]")].map((n) => ({
        id: n.getAttribute("data-node-id")!,
        x: Number(/translate\(([-\d.]+)/.exec(n.getAttribute("transform") ?? "")?.[1] ?? 0),
      }));
      const right = xs.reduce((a, b) => (b.x > a.x ? b : a));
      rerender(<AgentFlowGraph agents={AGENTS} selectedNodeId={right.id} />);
      const after = viewBox(container);
      expect(after).not.toBe(before);
      expect(Number(after!.split(" ")[0])).toBeGreaterThan(Number(before!.split(" ")[0]));
      // Re-rendering the same selection does not move the view again.
      rerender(<AgentFlowGraph agents={AGENTS} selectedNodeId={right.id} />);
      expect(viewBox(container)).toBe(after);
    } finally {
      spy.mockRestore();
      Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    }
  });
});

// Six offices, each an orchestrator and its desks: every orchestrator shares
// the base name "Orchestrator".
const OFFICES = ["CEO Office", "Digital Security Office", "Product Management Office", "Legal Office", "Finance Office", "Sales Office"];
function offices(desksPerOffice: number): Agent[] {
  return OFFICES.flatMap((office, o) => {
    const fleet = { fleet_id: `f${o}`, fleet_name: office };
    const desks = Array.from({ length: desksPerOffice }, (_, d) => ({
      ...agent(`o${o}d${d}`),
      name: `[${office}] Desk ${d}`,
      fleet,
    }));
    const head = { ...agent(`o${o}`, desks.map((d) => d.id)), name: `[${office}] Orchestrator`, fleet };
    return [head, ...desks] as Agent[];
  });
}
// The drawn names: their lines, and their box in drawing units from the
// node's place, the name's baseline and its font size, as the graph draws it.
function drawnNames(container: HTMLElement) {
  return [...container.querySelectorAll("[data-node-id]")].flatMap((node) => {
    const text = node.querySelector("text[data-node-name]");
    const lines = [...(text?.querySelectorAll("tspan") ?? [])].map((t) => t.textContent ?? "").filter(Boolean);
    if (!text || lines.length === 0) return [];
    const size = Number(text.getAttribute("font-size"));
    const { x, y } = centre(container, node.getAttribute("data-node-id")!);
    const top = y + Number(text.getAttribute("y")) - size;
    const h = size * (1.2 + (lines.length - 1) * 1.15);
    const w = Math.max(...lines.map((l) => [...l].length)) * size * 0.56;
    return [{ id: node.getAttribute("data-node-id")!, label: lines.join(" "), box: { x0: x - w / 2, x1: x + w / 2, y0: top, y1: top + h } }];
  });
}

describe("AgentFlowGraph names across fleets", () => {
  it("draws the office into each name, the stored name and the fleet in the accessible name", () => {
    const { container } = renderWithProviders(<AgentFlowGraph agents={offices(1)} />);
    const names = drawnNames(container);
    const labels = names.map((n) => n.label);
    expect(labels).toContain("CEO Office Orchestrator");
    expect(labels).toContain("Digital Security Office Orchestrator");
    expect(labels.some((l) => l.includes("["))).toBe(false);
    const dso = container.querySelector('[data-node-id="o1"]')!;
    expect(dso.getAttribute("aria-label")).toBe("[Digital Security Office] Orchestrator · Digital Security Office");
  });

  it("on a fleet view, draws its members without the office and other fleets' agents in full", () => {
    const { container } = renderWithProviders(<AgentFlowGraph agents={offices(1)} viewedFleetId="f1" />);
    const labels = drawnNames(container).map((n) => n.label);
    expect(labels).toContain("Orchestrator");
    expect(labels).toContain("Desk 0");
    expect(labels).toContain("CEO Office Orchestrator");
    expect(labels).not.toContain("Digital Security Office Orchestrator");
    expect(new Set(labels).size).toBe(labels.length);
    // The stored name stays the accessible name.
    const dso = container.querySelector('[data-node-id="o1"]')!;
    expect(dso.getAttribute("aria-label")).toBe("[Digital Security Office] Orchestrator · Digital Security Office");
  });

  for (const viewed of [undefined, "f1"]) for (const desks of [0, 24]) {
    for (const layout of GRAPH_LAYOUTS) {
      it(`no two drawn names alike or overlapping, ${6 * (desks + 1)} nodes (${layout}${viewed ? ", fleet view" : ""})`, () => {
        const { container } = renderWithProviders(<AgentFlowGraph agents={offices(desks)} layout={layout} viewedFleetId={viewed} />);
        const names = drawnNames(container);
        // A fitted 150-node map may leave every name out for lack of room
        // (the hover draws it); the small one draws them.
        if (desks === 0) expect(names.length).toBeGreaterThan(0);
        // Every orchestrator drawn says its office.
        const heads = names.filter((n) => /^o\d$/.test(n.id)).map((n) => n.label);
        expect(new Set(heads).size).toBe(heads.length);
        expect(new Set(names.map((n) => n.label)).size).toBe(names.length);
        for (const a of names) for (const b of names) {
          if (a.id >= b.id) continue;
          const apart = a.box.x1 <= b.box.x0 || b.box.x1 <= a.box.x0 || a.box.y1 <= b.box.y0 || b.box.y1 <= a.box.y0;
          expect(apart, `${a.label} over ${b.label}`).toBe(true);
        }
      });
    }
  }

  // A ghost whose office differs from its fleet draws
  // a two-line name and its group line under it; neither may meet another
  // node's name or glyph, on any layout.
  const CEO = { fleet_id: "ceo", fleet_name: "CEO Office" };
  const FLEET_PAGE = [
    { ...agent("orc", ["ana", "dev", "uxd", "cou"]), name: "[CEO Office] Orchestrator", fleet: CEO },
    { ...agent("ana"), name: "[CEO Office] Analyst", fleet: CEO },
    { ...agent("dev"), name: "[CEO Office] Developer", fleet: CEO },
    { ...agent("uxd"), name: "[CEO Office] UX Director", fleet: CEO },
    { ...agent("cou"), name: "[Product Management Office] Counsel", fleet: { fleet_id: "legal", fleet_name: "Legal Counsel" } },
  ] as Agent[];
  const ghosts = new Map([["cou", "Legal Counsel"]]);
  const box = (x: number, top: number, w: number, h: number) => ({ x0: x - w / 2, x1: x + w / 2, y0: top, y1: top + h });
  const meet = (a: ReturnType<typeof box>, b: ReturnType<typeof box>) => !(a.x1 <= b.x0 || b.x1 <= a.x0 || a.y1 <= b.y0 || b.y1 <= a.y0);
  for (const layout of GRAPH_LAYOUTS) {
    it(`a ghost's group line meets no other name or glyph (${layout})`, () => {
      const { container } = renderWithProviders(<AgentFlowGraph agents={FLEET_PAGE} layout={layout} ghostNodes={ghosts} />);
      const names = drawnNames(container);
      const subs = [...container.querySelectorAll("[data-node-id]")].flatMap((node) => {
        const text = node.querySelector('text[font-style="italic"]');
        if (!text?.textContent) return [];
        const size = Number(text.getAttribute("font-size"));
        const { x, y } = centre(container, node.getAttribute("data-node-id")!);
        const w = [...text.textContent].length * size * 0.56;
        return [{ id: node.getAttribute("data-node-id")!, box: box(x, y + Number(text.getAttribute("y")) - size, w, size * 1.2) }];
      });
      const glyphs = [...container.querySelectorAll("[data-node-id]")].map((node) => {
        const id = node.getAttribute("data-node-id")!;
        const { x, y } = centre(container, id);
        const r = Math.max(0, ...[...node.querySelectorAll(":scope circle[r]")].map((c) => Number(c.getAttribute("r")) || 0).filter((v) => v < 60));
        return { id, box: box(x, y - r, 2 * r, 2 * r) };
      });
      for (const sub of subs) {
        for (const name of names) if (name.id !== sub.id) expect(meet(sub.box, name.box), `group line of ${sub.id} over ${name.label}`).toBe(false);
        for (const glyph of glyphs) if (glyph.id !== sub.id) expect(meet(sub.box, glyph.box), `group line of ${sub.id} over ${glyph.id}`).toBe(false);
      }
      for (const name of names) for (const glyph of glyphs) {
        if (glyph.id !== name.id) expect(meet(name.box, glyph.box), `${name.label} over ${glyph.id}`).toBe(false);
      }
    });
  }

  // A ghost with free room keeps its group line at
  // rest; the line is never dropped for meeting the ghost's own name.
  const ROOMY = [
    { ...agent("orc", ["cou"]), name: "[CEO Office] Orchestrator", fleet: CEO },
    { ...agent("cou"), name: "[Product Management Office] Counsel", fleet: { fleet_id: "legal", fleet_name: "Legal Counsel" } },
  ] as Agent[];
  for (const layout of GRAPH_LAYOUTS) {
    it(`a ghost with free room keeps its group line at rest (${layout})`, () => {
      const { container } = renderWithProviders(<AgentFlowGraph agents={ROOMY} layout={layout} ghostNodes={ghosts} />);
      const ghost = container.querySelector('[data-node-id="cou"]')!;
      expect(ghost.querySelector('text[font-style="italic"]')?.textContent).toBe("Legal Counsel");
    });
  }
});

// A large map keeps its frame rate: only the hot edges animate.
describe("AgentFlowGraph animation cost", () => {
  const animatedEdges = (container: HTMLElement) =>
    [...container.querySelectorAll("path[marker-end]")].filter((p) => (p as SVGElement).style.animation !== "").length;

  it("animates every edge of a small map", () => {
    const { container } = renderWithProviders(<AgentFlowGraph agents={AGENTS} layout="force" />);
    expect(animatedEdges(container)).toBe(4);
  });

  it("animates only the edges of the hovered node on a large map", () => {
    // 41 hubs each handing over to one spoke: 41 edges, over the limit.
    const many = Array.from({ length: 41 }, (_, i) => [agent(`h${i}`, [`s${i}`]), agent(`s${i}`)]).flat();
    const { container } = renderWithProviders(<AgentFlowGraph agents={many} layout="force" />);
    expect(container.querySelectorAll("path[marker-end]").length).toBe(41);
    expect(animatedEdges(container)).toBe(0);
    fireEvent.mouseEnter(container.querySelector('[data-node-id="h0"]')!);
    expect(animatedEdges(container)).toBe(1);
  });
});
