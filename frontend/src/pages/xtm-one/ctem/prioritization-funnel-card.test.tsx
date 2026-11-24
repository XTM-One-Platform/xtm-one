// The CTEM prioritization funnel (#11546): the stage drawing and the compact
// widget, with OpenAEV absent and present, compact counts with their exact
// values in the title and the accessible name (UX r2 B1 on #11557), not
// configured layers drawn without a count and leading to Settings, and the
// sample badged.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { CtemFunnel } from "@/types";
import {
  PrioritizationFunnel,
  PrioritizationFunnelCard,
  PrioritizationFunnelPlaceholder,
  PrioritizationFunnelWithCases,
  signalGateCounts,
} from "./PrioritizationFunnel";
import { SAMPLE_FUNNEL, withCasesLayer } from "./prioritization-funnel";

const casesRead = vi.hoisted(() => ({ value: { data: undefined as unknown } as { data: unknown; isError?: boolean }, options: [] as unknown[] }));
vi.mock("@/hooks/api/useCtemCasesApi", () => ({
  CTEM_OPEN_CASE_STATES: ["new", "analyzing", "waiting_decision", "remediating", "validating"],
  useCtemCases: (options?: unknown) => {
    casesRead.options.push(options);
    return casesRead.value;
  },
}));

const signalsRead = vi.hoisted(() => ({ value: { data: undefined } as { data: unknown; isError?: boolean } }));
vi.mock("@/hooks/api/useCtemSignalsApi", () => ({
  useRecentCtemSignals: () => signalsRead.value,
}));

const NO_OPENAEV: CtemFunnel = {
  ...SAMPLE_FUNNEL,
  layers: SAMPLE_FUNNEL.layers.map((layer) => (layer.key === "known" ? { ...layer, asset_findings: null } : layer)),
};

function draw(node: React.ReactNode) {
  return renderWithProviders(<MemoryRouter>{node}</MemoryRouter>);
}

describe("PrioritizationFunnel", () => {
  it("draws every layer in order with compact counts, OpenAEV present", () => {
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    const rows = screen.getAllByTestId(/^ctem-funnel-layer-/).map((row) => row.getAttribute("data-testid"));
    expect(rows).toEqual([
      "ctem-funnel-layer-known",
      "ctem-funnel-layer-relevant",
      "ctem-funnel-layer-exploitable",
      "ctem-funnel-layer-exposed",
      "ctem-funnel-layer-business_impact",
      "ctem-funnel-layer-policy",
      "ctem-funnel-layer-open_issues",
    ]);
    expect(screen.getByTestId("ctem-funnel-count-known").textContent).toBe("4.2K");
    expect(screen.getByTestId("ctem-funnel-count-exploitable").textContent).toBe("21");
    // What a layer removed leaves as a branch, labelled with its count and reason.
    const relevant = screen.getByTestId("ctem-flow-label-relevant");
    expect(relevant.textContent).toContain("3.7K");
    expect(relevant.textContent).toContain("not in your software");
    expect(screen.getByTestId("ctem-flow-label-exploitable").textContent).toContain("not exploitable");
    expect(screen.getByText("89 CVE findings on your assets (OpenAEV)")).toBeTruthy();
    expect(screen.getByTestId("ctem-funnel-needs-action").textContent).toBe("21 vulnerabilities need action");
  });

  it("draws without the OpenAEV line when OpenAEV is absent", () => {
    draw(<PrioritizationFunnel variant="stage" funnel={NO_OPENAEV} openctiUrl="https://cti.example" />);
    expect(screen.queryByText(/CVE findings on your assets/)).toBeNull();
    expect(screen.getByTestId("ctem-funnel-count-known").textContent).toBe("4.2K");
  });

  it("draws a not configured layer without a count, with the call to action to Settings", () => {
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    const exposed = screen.getByTestId("ctem-funnel-layer-exposed");
    expect(exposed.getAttribute("data-state")).toBe("not_configured");
    expect(screen.getByTestId("ctem-funnel-count-exposed").textContent).toBe("Not configured");
    // A not configured exclusion removes nothing: its branch has no thickness.
    expect(screen.getByTestId("ctem-flow-branch-exposed").getAttribute("data-branch")).toBe("0");
    // Each call to action opens the tab and card of its field (Exposure for assets, Business for crown jewels...), never the first tab.
    expect(screen.getByRole("link", { name: "Add your assets and exposure" }).getAttribute("href")).toBe("/ctem/inventory?card=assets_exposure");
    expect(screen.getByRole("link", { name: "Map crown jewels to assets" }).getAttribute("href")).toBe("/settings/organization/profile/business?card=crown_jewels");
    expect(screen.getByRole("link", { name: "Set a prioritization policy" }).getAttribute("href")).toBe("/ctem/inventory/exposure?card=prioritization_policy");
    expect(screen.getByTestId("ctem-funnel-count-open_issues").textContent).toBe("Not available yet");
  });

  it("draws Exposed with assets read but not linked without the call to action, its split in the tip", async () => {
    const reachability = { total: 211, reachable: 150, unreachable: 40, unknown: 21, endpoints: 120 };
    const funnel: CtemFunnel = {
      ...SAMPLE_FUNNEL,
      layers: SAMPLE_FUNNEL.layers.map((layer) =>
        layer.key === "exposed" ? { ...layer, state: "not_available", category: null, reason: "assets_not_linked", reachability } : layer,
      ),
    };
    draw(<PrioritizationFunnel variant="stage" funnel={funnel} openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-layer-exposed").getAttribute("data-state")).toBe("not_available");
    expect(screen.getByTestId("ctem-funnel-count-exposed").textContent).toBe("Not linked");
    expect(screen.queryByRole("link", { name: "Add your assets and exposure" })).toBeNull();
    await userEvent.hover(screen.getByTestId("ctem-funnel-count-exposed"));
    expect(
      (await screen.findAllByText("211 assets read. Reachable: 150 (user endpoints: 120). Proven unreachable: 40. Unknown: 21.")).length,
    ).toBeGreaterThan(0);
  });

  it("links a live layer to the OpenCTI list it counts, its accessible name exact", () => {
    // #11954: the layer's filter reaches OpenCTI in the shape its list URL keeps.
    const filters = { mode: "and", filters: [{ key: ["regardingOf"], values: [{ key: "id", values: ["software-1"] }] }], filterGroups: [] };
    const funnel = { ...SAMPLE_FUNNEL, layers: SAMPLE_FUNNEL.layers.map((layer) => (layer.key === "relevant" ? { ...layer, filters } : layer)) };
    draw(<PrioritizationFunnel variant="stage" funnel={funnel} openctiUrl="https://cti.example" />);
    const link = screen.getByRole("link", { name: "Relevant to you, 512, 3,702 fewer than Known vulnerabilities" });
    const href = new URL(link.getAttribute("href") as string);
    expect(href.pathname).toBe("/dashboard/arsenal/vulnerabilities");
    expect(JSON.parse(href.searchParams.get("filters") as string).filters[0].key).toBe("regardingOf");
  });

  it("badges the sample and links nothing from it", () => {
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} sample openctiUrl="https://cti.example" />);
    expect(screen.getByText("Sample")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Relevant to you/ })).toBeNull();
    expect(screen.queryByRole("link", { name: "Add your assets and exposure" })).toBeNull();
  });

  // Guard for UX r3 B1 on #11557: the compact count's exact value opens on
  // hover and on keyboard focus, on a link bar and on a sample (non-link) bar.
  it("opens the exact value on hover of a link bar", async () => {
    const user = userEvent.setup();
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    await user.hover(screen.getByRole("link", { name: /^Known vulnerabilities, 4,214/ }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("Known vulnerabilities: 4,214");
  });

  // #11954: a layer that is a link says where it leads and when its count was read.
  it("names the destination and the read time of a link layer in its tooltip", async () => {
    const user = userEvent.setup();
    const readAt = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" readAt={readAt} />);
    const link = screen.getByRole("link", { name: /^Known vulnerabilities, 4,214/ });
    await user.hover(link);
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.textContent).toContain("Opens in OpenCTI, Arsenal > Vulnerabilities");
    expect(tooltip.textContent).toContain("Read 2 hours ago");
  });

  it("reads just now, never now, in the tooltip under 45 seconds after the posture read", async () => {
    const user = userEvent.setup();
    const readAt = new Date(Date.now() - 10 * 1000).toISOString();
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" readAt={readAt} />);
    await user.hover(screen.getByRole("link", { name: /^Known vulnerabilities, 4,214/ }));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.textContent).toContain("Read just now");
    expect(tooltip.textContent).not.toContain("Read now");
  });

  it("says a not configured layer opens CTEM > Inventory, where the assets live (#1178)", async () => {
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    fireEvent.focus(screen.getByRole("link", { name: /^Exposed, Not configured/ }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("Opens CTEM > Inventory");
  });

  it("opens the exact value on keyboard focus of a sample bar, which is not a link", async () => {
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} sample />);
    const bar = screen.getByRole("group", { name: /^Known vulnerabilities, 4,214/ });
    expect(bar.getAttribute("tabindex")).toBe("0");
    fireEvent.focus(bar);
    expect((await screen.findByRole("tooltip")).textContent).toContain("Known vulnerabilities: 4,214");
  });
});

// #11545 S5b: the last layer is the open cases, exact, leading to the Cases list.
describe("the cases layer", () => {
  it("shows the exact count of open cases, leading to the Cases list", () => {
    draw(<PrioritizationFunnel variant="stage" funnel={withCasesLayer(SAMPLE_FUNNEL, { open: 1, total: 3 })} openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-layer-open_issues").getAttribute("data-state")).toBe("live");
    expect(screen.getByTestId("ctem-funnel-count-open_issues").textContent).toBe("1");
    expect(screen.getByRole("link", { name: "Cases, 1 open case" }).getAttribute("href")).toBe("/ctem/cases");
  });

  it("says no open case at 0", () => {
    draw(<PrioritizationFunnel variant="stage" funnel={withCasesLayer(SAMPLE_FUNNEL, { open: 0, total: 0 })} openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-count-open_issues").textContent).toBe("0");
    expect(screen.getByRole("link", { name: "Cases, No open case" })).toBeTruthy();
  });

  it("reads the counts of the Cases API: every open status (#1503)", () => {
    casesRead.value = { data: { vertical: "cyber", counts: { waiting_decision: 2, new: 1, remediating: 2, resolved: 4, accepted_risk: 1 }, cases: [], details_unavailable: [] } };
    draw(<PrioritizationFunnelWithCases funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-count-open_issues").textContent).toBe("5");
    expect(screen.getByRole("link", { name: "Cases, 5 open cases" })).toBeTruthy();
    // The node fans out into what waits on a person and what the agents work, linked to the list.
    expect(screen.getByRole("link", { name: "Waiting for decision: 2 cases" }).getAttribute("href")).toBe("/ctem/cases?state=waiting_decision");
    expect(screen.getByRole("link", { name: "In work: 3 cases" }).getAttribute("href")).toBe("/ctem/cases?state=open");
    expect(screen.getByTestId("ctem-flow-cases-closed").textContent).toBe("4 resolved, 1 accepted risk");
    expect(screen.getByTestId("ctem-flow-outcome").textContent).toBe("21 issues grouped into 5 open cases");
  });

  // #865 prod retest D1: a watched case is drawn, first of the closed line, linked to its list.
  it("shows the mitigated, watching cases first on the closed line", () => {
    casesRead.value = {
      data: { vertical: "cyber", counts: { new: 1, mitigated_watching: 2, resolved: 4, accepted_risk: 1 }, cases: [], details_unavailable: [] },
    };
    draw(<PrioritizationFunnelWithCases funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    // Every count shows, never clamped: mitigated on its own line above the
    // closed line (UX round 2 on #897, B4).
    const outcomes = screen.getByTestId("ctem-flow-cases-outcomes");
    expect(outcomes.querySelectorAll("p")).toHaveLength(2);
    expect(screen.getByTestId("ctem-flow-cases-mitigated").textContent).toBe("2 mitigated, watching");
    expect(screen.getByTestId("ctem-flow-cases-closed").textContent).toBe("4 resolved, 1 accepted risk");
    expect(outcomes.textContent).toBe("2 mitigated, watching4 resolved, 1 accepted risk");
    expect(screen.getByTestId("ctem-flow-cases-mitigated").getAttribute("href")).toBe("/ctem/cases?state=mitigated_watching");
    casesRead.value = { data: undefined };
  });

  it("puts one tab stop on the flow and moves it with the arrow keys", async () => {
    const user = userEvent.setup();
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    const marks = Array.from(screen.getByTestId("ctem-funnel-flow").querySelectorAll("[data-flow-mark]"));
    expect(marks.filter((mark) => mark.getAttribute("tabindex") === "0")).toHaveLength(1);
    (marks[0] as HTMLElement).focus();
    await user.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(marks[1]);
  });

  it("lists every layer in the hidden data table", () => {
    draw(<PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    const table = screen.getByTestId("ctem-hidden-data-table");
    expect(table.querySelectorAll("tbody tr")).toHaveLength(SAMPLE_FUNNEL.layers.length);
    expect(table.textContent).toContain("3,702");
  });

  it("shows no count until the Cases API answers", () => {
    casesRead.value = { data: undefined };
    draw(<PrioritizationFunnelWithCases funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-count-open_issues").textContent).toBe("Not available yet");
  });

  // UX r1 B2 on #11678: a failed Cases read stays in its layer, never the page's error boundary.
  it("keeps the layer not available, with no fan, when the Cases API fails", () => {
    casesRead.options = [];
    casesRead.value = {
      data: { vertical: "cyber", counts: { waiting_decision: 2, remediating: 3, resolved: 4, accepted_risk: 1 }, cases: [], details_unavailable: [] },
      isError: true,
    };
    draw(<PrioritizationFunnelWithCases funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    expect(casesRead.options.every((options) => (options as { throwOnError?: boolean })?.throwOnError === false)).toBe(true);
    expect(screen.getByTestId("ctem-funnel-count-open_issues").textContent).toBe("Not available yet");
    expect(screen.queryByRole("link", { name: /^Waiting for decision:/ })).toBeNull();
    expect(screen.queryByTestId("ctem-flow-cases-closed")).toBeNull();
    casesRead.value = { data: undefined };
  });
});

// #874: the two gates of the loop as numbers under the funnel, "10 flagged, 3 real".
describe("the gate counts", () => {
  it("counts signals in, confirmed, in scope and cases from the counts by state", () => {
    expect(signalGateCounts({ new: 1, to_validate: 2, discarded: 1, watchlist: 3, confirmed: 1, case_opened: 2 })).toEqual({
      total: 10,
      confirmed: 6,
      inScope: 3,
      cases: 2,
      watchlist: 3,
    });
  });

  it("draws the line under the stage funnel, and nothing before the signals API answers", () => {
    casesRead.value = { data: undefined };
    signalsRead.value = { data: undefined };
    const { unmount } = draw(<PrioritizationFunnelWithCases funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    expect(screen.queryByTestId("ctem-funnel-gate-counts")).toBeNull();
    unmount();
    signalsRead.value = { data: { total: 3, counts: { to_validate: 1, watchlist: 1, confirmed: 1 }, signals: [] } };
    draw(<PrioritizationFunnelWithCases funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-gate-counts").textContent).toBe(
      "Signals: 3 in, 2 confirmed, 1 in scope, 0 opened a case, 1 on the watchlist",
    );
    signalsRead.value = { data: undefined };
  });

  // UX r1 B1 on #875: a count that shares a noun with another count on the same
  // card names its scope; the gate line never reads as a second total of cases.
  it("names its scope so its cases never read as the card's open cases", () => {
    casesRead.value = { data: undefined };
    signalsRead.value = { data: { total: 4, counts: { to_validate: 1, discarded: 1, watchlist: 1, case_opened: 1 }, signals: [] } };
    draw(<PrioritizationFunnelWithCases funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />);
    const line = screen.getByTestId("ctem-funnel-gate-counts").textContent ?? "";
    expect(line.startsWith("Signals: ")).toBe(true);
    expect(line).toContain("1 opened a case");
    expect(line).not.toMatch(/\d+ cases?\b/);
    signalsRead.value = { data: undefined };
  });
});

// UX r1 B1 on #11678: the flow box mounts after the empty state, the loading or
// the placeholder; it is measured when it mounts, not left at the fallback width.
describe("the flow width", () => {
  it("lays the flow out at the box's width when it mounts after the empty state", () => {
    const observed: Element[] = [];
    class Observer {
      private readonly callback: ResizeObserverCallback;
      constructor(callback: ResizeObserverCallback) {
        this.callback = callback;
      }
      observe(node: Element) {
        observed.push(node);
        this.callback([{ contentRect: { width: 1458 } } as ResizeObserverEntry], this as unknown as ResizeObserver);
      }
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", Observer);
    const box = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 1458 } as DOMRect);
    try {
      const empty: CtemFunnel = {
        ...SAMPLE_FUNNEL,
        layers: SAMPLE_FUNNEL.layers.map((layer) => (layer.key === "known" ? { ...layer, count: 0 } : layer)),
      };
      const view = draw(<PrioritizationFunnel variant="stage" funnel={empty} openctiUrl="https://cti.example" />);
      expect(screen.queryByTestId("ctem-funnel-flow")).toBeNull();
      view.rerender(
        <MemoryRouter>
          <PrioritizationFunnel variant="stage" funnel={SAMPLE_FUNNEL} openctiUrl="https://cti.example" />
        </MemoryRouter>,
      );
      const flow = screen.getByTestId("ctem-funnel-flow");
      expect(observed).toContain(flow);
      expect(flow.querySelector("svg")?.getAttribute("viewBox")).toMatch(/^0 0 1458 /);
    } finally {
      box.mockRestore();
      vi.unstubAllGlobals();
    }
  });
});

describe("PrioritizationFunnelCard", () => {
  it("summarises the live layers and links to the stage page", () => {
    draw(<PrioritizationFunnelCard funnel={SAMPLE_FUNNEL} sample={false} openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-count-known").textContent).toBe("4.2K");
    expect(screen.getByRole("link", { name: /^Known vulnerabilities, 4,214/ })).toBeTruthy();
    expect(screen.queryByTestId("ctem-funnel-count-exposed")).toBeNull();
    expect(screen.getByTestId("ctem-funnel-compact-unconfigured").textContent).toBe("3 layers not configured");
    expect(screen.getByRole("link", { name: "Open Prioritization" }).getAttribute("href")).toBe("/ctem/prioritization");
  });

  it("opens the exact value on keyboard focus of a widget step", async () => {
    draw(<PrioritizationFunnelCard funnel={SAMPLE_FUNNEL} sample={false} openctiUrl="https://cti.example" />);
    fireEvent.focus(screen.getByRole("link", { name: /^Known vulnerabilities, 4,214/ }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("Known vulnerabilities: 4,214");
  });

  // #11662: OpenCTI connected and no funnel yet never draws the sample's counts.
  it("draws the pending placeholder, never the Sample badge or its counts", () => {
    draw(<PrioritizationFunnelCard funnel={SAMPLE_FUNNEL} sample={false} placeholder="pending" openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-pending")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("Reading your vulnerabilities from OpenCTI");
    expect(screen.queryByText("Sample")).toBeNull();
    expect(screen.queryByTestId("ctem-funnel-count-known")).toBeNull();
  });

  // UX r1 B1 on #11676: the pending block takes the live compact strip's shape
  // (a column per live layer of the default funnel, each a name, a count and a
  // bar line), so the widget keeps one height when the funnel lands.
  it("pending keeps the live compact strip's shape", () => {
    const { unmount } = draw(<PrioritizationFunnelCard funnel={SAMPLE_FUNNEL} sample={false} openctiUrl="https://cti.example" />);
    const liveColumns = screen.getByTestId("ctem-funnel-compact").querySelectorAll("ol > li").length;
    unmount();
    draw(<PrioritizationFunnelCard funnel={SAMPLE_FUNNEL} sample={false} placeholder="pending" openctiUrl="https://cti.example" />);
    const columns = screen.getAllByTestId("ctem-funnel-pending-column");
    expect(columns).toHaveLength(liveColumns);
    for (const column of columns) expect(column.children).toHaveLength(3);
  });
});

// #11662: the stage page's funnel slot for a connected tenant without a funnel.
describe("PrioritizationFunnelPlaceholder", () => {
  it("pending: a status line and skeleton rows, no Sample badge, no count", () => {
    draw(<PrioritizationFunnelPlaceholder display="pending" variant="stage" openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-pending")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("Reading your vulnerabilities from OpenCTI");
    expect(screen.queryByText("Sample")).toBeNull();
    expect(screen.queryByTestId(/^ctem-funnel-layer-/)).toBeNull();
  });

  it("unavailable: says OpenCTI sent no count and opens OpenCTI", () => {
    draw(<PrioritizationFunnelPlaceholder display="unavailable" variant="stage" openctiUrl="https://cti.example" />);
    expect(screen.getByTestId("ctem-funnel-unavailable")).toBeTruthy();
    expect(screen.getByText("OpenCTI sent no vulnerability count")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open OpenCTI" }).getAttribute("href")).toBe("https://cti.example/");
    expect(screen.queryByText("Sample")).toBeNull();
  });
});
