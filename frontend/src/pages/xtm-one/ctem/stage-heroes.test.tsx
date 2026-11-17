// The S1 hero viz of the CTEM stage pages (dev-docs/ctem-stage-viz.md):
// the outcome matrix follows whichever kill chain the data carries (DISARM and
// ATLAS fixtures, no ATT&CK constant), and a missing source draws the
// not-configured state, never invented marks.
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemOutcomeMatrix } from "@/types";
import { frameworkLabel } from "./ctem-framework-labels";
import { clampMarkIndex, matrixColumns, ScopingRing, ValidationMatrix, DiscoveryArea } from "./stage-heroes";

const item = (id: string, phases: string[], outcome: "prevented" | "detected" | "missed" | "untested") => ({
  id,
  label: `Technique ${id}`,
  code: id,
  phases,
  success: 0,
  failed: 0,
  pending: 0,
  parts: [] as [],
  prevention: { success: outcome === "prevented" ? 1 : 0, total: outcome === "untested" ? 0 : 1 },
  detection: { success: outcome === "detected" ? 1 : 0, total: outcome === "untested" ? 0 : 1 },
  outcome,
});

const DISARM_ATLAS: CtemOutcomeMatrix = {
  chains: [
    { id: "disarm", label: "disarm", count: 3, default: true, groups: [{ id: "p1", label: "Plan strategy" }, { id: "p2", label: "Develop content" }] },
    { id: "mitre-atlas", label: "mitre-atlas", count: 1, groups: [{ id: "a1", label: "Reconnaissance" }] },
    { id: "custom-chain", label: "custom-chain", count: 0, groups: [{ id: "c1", label: "Stage one" }] },
  ],
  items: [item("T0001", ["p1"], "missed"), item("T0002", ["p1"], "prevented"), item("T0003", ["p2"], "detected"), item("AML.T0000", ["a1"], "prevented")],
};

describe("matrixColumns", () => {
  it("takes the default chain and sorts each phase by outcome", () => {
    const { chain, columns } = matrixColumns(DISARM_ATLAS, null);
    expect(chain?.id).toBe("disarm");
    expect(columns.map((c) => c.label)).toEqual(["Plan strategy", "Develop content"]);
    expect(columns[0].items.map((i) => i.outcome)).toEqual(["prevented", "missed"]);
    expect(columns[0].prevented).toBe(1);
  });

  it("switches to a picked chain (ATLAS)", () => {
    const { chain, columns } = matrixColumns(DISARM_ATLAS, "mitre-atlas");
    expect(chain?.id).toBe("mitre-atlas");
    expect(columns[0].items.map((i) => i.id)).toEqual(["AML.T0000"]);
  });
});

describe("frameworkLabel", () => {
  it("labels known names and shows an unknown one as written", () => {
    expect(frameworkLabel("disarm")).toBe("DISARM");
    expect(frameworkLabel("mitre-atlas")).toBe("ATLAS");
    expect(frameworkLabel("custom-chain")).toBe("custom-chain");
  });
});

describe("ValidationMatrix", () => {
  it("draws the DISARM matrix with its outcomes", () => {
    renderWithProviders(<ValidationMatrix matrix={DISARM_ATLAS} preventionPct={50} detectionPct={33} hasAev onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-matrix")).toBeTruthy();
    const grid = screen.getByTestId("ctem-matrix");
    expect(Array.from(grid.querySelectorAll('[role="columnheader"] p')).map((h) => h.textContent)).toEqual(["Plan strategy", "Develop content"]);
    expect(document.querySelectorAll('[data-outcome="missed"]').length).toBe(1);
  });

  it("opens on the context framework (dev-docs/ctem-stage-viz.md 8.2)", () => {
    renderWithProviders(
      <ValidationMatrix matrix={DISARM_ATLAS} initialChain="mitre-atlas" preventionPct={50} detectionPct={33} hasAev onConnect={() => {}} />,
    );
    const grid = screen.getByTestId("ctem-matrix");
    expect(grid.querySelectorAll('[role="columnheader"] p').length).toBe(matrixColumns(DISARM_ATLAS, "mitre-atlas").columns.length);
    expect(grid.textContent).toContain("AML.T0000");
  });

  it("is not configured without OpenAEV", () => {
    renderWithProviders(<ValidationMatrix matrix={undefined} preventionPct={null} detectionPct={null} hasAev={false} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-matrix-not-configured")).toBeTruthy();
    expect(screen.queryByTestId("ctem-matrix")).toBeNull();
  });

  it("says no data, never connect, when OpenAEV is connected without a matrix (UX round 8, B2)", () => {
    renderWithProviders(<ValidationMatrix matrix={undefined} preventionPct={null} detectionPct={null} hasAev onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-matrix-no-data")).toBeTruthy();
    expect(screen.queryByTestId("ctem-matrix-not-configured")).toBeNull();
  });

  it("expands a column past eight rows in place, and draws no link without a technique URL (B3)", () => {
    const many = Array.from({ length: 11 }, (_, i) => item(`T1${String(i).padStart(3, "0")}`, ["p1"], "missed"));
    const big: CtemOutcomeMatrix = { chains: [DISARM_ATLAS.chains[0]], items: many };
    renderWithProviders(<ValidationMatrix matrix={big} preventionPct={0} detectionPct={0} hasAev onConnect={() => {}} />);
    expect(document.querySelectorAll('[data-outcome="missed"]').length).toBe(8);
    expect(document.querySelectorAll('a[role="gridcell"]').length).toBe(0);
    const more = screen.getByTestId("ctem-matrix-more-p1");
    expect(more.textContent).toBe("+3 more");
    fireEvent.click(more);
    expect(document.querySelectorAll('[data-outcome="missed"]').length).toBe(11);
    expect(screen.getByTestId("ctem-matrix-more-p1").textContent).toBe("Show fewer");
  });

  it("says no simulation follows a framework with no exercised technique", () => {
    const empty: CtemOutcomeMatrix = { chains: [{ ...DISARM_ATLAS.chains[2], default: true }], items: [] };
    renderWithProviders(<ValidationMatrix matrix={empty} preventionPct={null} detectionPct={null} hasAev onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-matrix-no-simulation")).toBeTruthy();
  });
});

describe("ScopingRing", () => {
  const domains = [
    { name: "Endpoint", covered: true, contracts: 12 },
    { name: "Cloud", covered: false, contracts: 0 },
  ];
  it("draws one segment per domain and the breadth in the centre", () => {
    renderWithProviders(<ScopingRing domains={domains} hasAev breadthPct={50} breadthDelta={4} figures={[]} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-ring-Endpoint")).toBeTruthy();
    expect(screen.getByTestId("ctem-ring-center").textContent).toBe("50%");
  });
  it("is not configured without OpenAEV", () => {
    renderWithProviders(<ScopingRing domains={domains} hasAev={false} breadthPct={null} breadthDelta={null} figures={[]} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-ring-not-configured")).toBeTruthy();
    expect(screen.getByTestId("ctem-ring-center").textContent).toBe("--");
  });
  it("says no data with a dashed ring when OpenAEV is connected without domains (B2)", () => {
    renderWithProviders(<ScopingRing domains={[]} hasAev breadthPct={null} breadthDelta={null} figures={[]} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-ring-no-data")).toBeTruthy();
    expect(screen.queryByTestId("ctem-ring-not-configured")).toBeNull();
    expect(screen.getByTestId("ctem-ring-empty")).toBeTruthy();
  });
  it("keeps every domain label inside the SVG box (B1)", () => {
    renderWithProviders(<ScopingRing domains={domains} hasAev breadthPct={50} breadthDelta={null} figures={[]} onConnect={() => {}} />);
    const svg = screen.getByTestId("ctem-hero-scoping").querySelector("svg[role='group']")!;
    const [, , w] = svg.getAttribute("viewBox")!.split(" ").map(Number);
    for (const t of Array.from(svg.querySelectorAll("g[role='img'] text"))) {
      const x = Number(t.getAttribute("x"));
      const anchor = t.getAttribute("text-anchor");
      if (anchor === "end") expect(x).toBeGreaterThanOrEqual(110);
      if (anchor === "start") expect(w - x).toBeGreaterThanOrEqual(110);
    }
  });
});

describe("DiscoveryArea", () => {
  it("asks for OpenCTI first, then OpenAEV", () => {
    const { unmount } = renderWithProviders(
      <DiscoveryArea points={[]} headlinePct={null} headlineDelta={null} threatTracked={null} hasCti={false} hasAev={false} onConnect={() => {}} />,
    );
    expect(screen.getByTestId("ctem-area-not-configured")).toBeTruthy();
    unmount();
    renderWithProviders(<DiscoveryArea points={[]} headlinePct={null} headlineDelta={null} threatTracked={42} hasCti hasAev={false} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-area-needs-aev")).toBeTruthy();
    // No empty chart frame without OpenAEV (#11998): the count and the gap line only.
    expect(document.querySelector(".border-dashed.h-\\[120px\\]")).toBeNull();
  });
  it("says no data, never connect, when both products are connected without results (UX round 9, B1)", () => {
    renderWithProviders(
      <DiscoveryArea points={[]} headlinePct={null} headlineDelta={null} threatTracked={42} hasCti hasAev aevUrl="https://aev.example.com" onConnect={() => {}} />,
    );
    expect(screen.getByTestId("ctem-area-no-data")).toBeTruthy();
    expect(screen.queryByTestId("ctem-area-needs-aev")).toBeNull();
    expect(screen.queryByText("Connect OpenAEV")).toBeNull();
  });
  it("shows one bar under two days of history", () => {
    renderWithProviders(
      <DiscoveryArea
        points={[{ date: "2026-11-13", label: "13 Nov", threat: 10, tested: 4 }]}
        headlinePct={40}
        headlineDelta={null}
        threatTracked={10}
        hasCti
        hasAev
        onConnect={() => {}}
      />,
    );
    expect(screen.getByTestId("ctem-area-single")).toBeTruthy();
  });
  it("clamps the focused point when the period shrinks while mounted (review P2, #11549)", () => {
    const pts = (n: number) => Array.from({ length: n }, (_, i) => ({ date: `2026-11-${10 + i}`, label: `D${i}`, threat: 10, tested: i + 1 }));
    const props = { headlinePct: 50, headlineDelta: null, threatTracked: 10, hasCti: true, hasAev: true, onConnect: () => {} };
    const { rerender } = renderWithProviders(<DiscoveryArea points={pts(6)} {...props} />);
    fireEvent.keyDown(screen.getByLabelText("Threat techniques tested"), { key: "ArrowLeft" });
    expect(screen.getByLabelText(/^D5:/)).toBeTruthy();
    rerender(<DiscoveryArea points={pts(3)} {...props} />);
    expect(screen.getByLabelText(/^D2:/)).toBeTruthy();
  });
});

describe("clampMarkIndex", () => {
  it("keeps the index inside the drawn marks", () => {
    expect(clampMarkIndex(5, 3)).toBe(2);
    expect(clampMarkIndex(1, 3)).toBe(1);
    expect(clampMarkIndex(-1, 3)).toBe(0);
    expect(clampMarkIndex(4, 0)).toBe(0);
  });
});

describe("no ATT&CK constant in the CTEM viz paths (section 5, R4-B3)", () => {
  it("finds no ATT&CK kill chain name, tactic id or tactic list outside the label table", () => {
    const dir = join(process.cwd(), "src/pages/xtm-one/ctem");
    const offenders = readdirSync(dir)
      .filter((f) => /\.(ts|tsx)$/.test(f) && !/\.test\.(ts|tsx)$/.test(f) && f !== "ctem-framework-labels.ts")
      .filter((f) => /mitre-attack|\bTA00\d\d\b|"Initial Access"|"Lateral Movement"/.test(readFileSync(join(dir, f), "utf8")));
    expect(offenders).toEqual([]);
  });
});
