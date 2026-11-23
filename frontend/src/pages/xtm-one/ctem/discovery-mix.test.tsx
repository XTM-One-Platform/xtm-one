// The Discovery stage page's mixed views (#11998): every module combination draws
// cards, never an empty frame; a missing module is a gap filler marked Sample with
// its Connect action; a list OpenCTI did not answer is not drawn.
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemStageViz } from "@/types";
import { DiscoveryMix } from "./DiscoveryMix";

const VIZ: CtemStageViz = {
  techniques_by_tactic: [
    { tactic: "initial-access", count: 12 },
    { tactic: "execution", count: 30 },
    { tactic: "command-and-control", count: 4 },
  ],
  top_threats: [{ name: "APT-Example", value: 41 }],
  target_sectors: [{ name: "Finance", value: 9 }],
  target_countries: [{ name: "France", value: 7 }],
};

const all = { opencti: true, openaev: true, opencrq: true };

describe("DiscoveryMix", () => {
  it("draws the OpenCTI cards in the backend's order and gap fillers for the two missing modules", () => {
    renderWithProviders(<DiscoveryMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-discovery-tactics")).toBeTruthy();
    expect(screen.getByTestId("ctem-discovery-threats").textContent).toContain("APT-Example");
    expect(screen.getByTestId("ctem-discovery-targeting").textContent).toContain("Finance");
    const bars = screen.getByTestId("ctem-discovery-tactics-bars").textContent ?? "";
    expect(bars.indexOf("Initial access")).toBeLessThan(bars.indexOf("Execution"));
    expect(bars).toContain("Command and control");
    expect(screen.getByTestId("ctem-discovery-gap-openaev")).toBeTruthy();
    expect(screen.getByTestId("ctem-discovery-gap-opencrq")).toBeTruthy();
    expect(screen.queryByTestId("ctem-discovery-gap-opencti")).toBeNull();
  });

  it("shows the OpenCTI gap filler when only OpenAEV is connected, with a Sample label and Connect", () => {
    const onConnect = vi.fn();
    renderWithProviders(<DiscoveryMix viz={undefined} connected={{ opencti: false, openaev: true, opencrq: false }} onConnect={onConnect} />);
    const gap = screen.getByTestId("ctem-discovery-gap-opencti");
    expect(gap.textContent).toContain("What OpenCTI adds here");
    expect(gap.textContent).toContain("Sample");
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenCTI" }));
    expect(onConnect).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("ctem-discovery-tactics")).toBeNull();
    expect(screen.getByTestId("ctem-discovery-gap-opencrq")).toBeTruthy();
  });

  it("shows three gap fillers when no module is connected", () => {
    renderWithProviders(<DiscoveryMix viz={undefined} connected={{ opencti: false, openaev: false, opencrq: false }} onConnect={() => {}} />);
    expect(screen.getAllByRole("button", { name: /^Connect / })).toHaveLength(3);
  });

  it("draws no gap filler when all three are connected, and skips a list OpenCTI did not answer", () => {
    renderWithProviders(<DiscoveryMix viz={{ techniques_by_tactic: VIZ.techniques_by_tactic }} connected={all} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-discovery-tactics")).toBeTruthy();
    expect(screen.queryByTestId("ctem-discovery-threats")).toBeNull();
    expect(screen.queryByTestId("ctem-discovery-targeting")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Connect / })).toBeNull();
  });

  it("lays the data cards on one row and the gap fillers on their own row (UX round 1 of #12027, B1)", () => {
    renderWithProviders(<DiscoveryMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    const dataRow = screen.getByTestId("ctem-discovery-data-row");
    const gapRow = screen.getByTestId("ctem-discovery-gap-row");
    expect(dataRow.contains(screen.getByTestId("ctem-discovery-tactics"))).toBe(true);
    expect(dataRow.contains(screen.getByTestId("ctem-discovery-threats"))).toBe(true);
    expect(dataRow.contains(screen.getByTestId("ctem-discovery-targeting"))).toBe(true);
    expect(gapRow.contains(screen.getByTestId("ctem-discovery-gap-openaev"))).toBe(true);
    expect(dataRow.contains(screen.getByTestId("ctem-discovery-gap-openaev"))).toBe(false);
  });

  it("ends every card of the data row on the tallest edge, tactics a direct cell of the row (#1612)", () => {
    renderWithProviders(<DiscoveryMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    const dataRow = screen.getByTestId("ctem-discovery-data-row");
    expect(dataRow.getAttribute("data-align")).toBe("stretch");
    expect(dataRow.className).not.toMatch(/\bitems-start\b/);
    expect(dataRow.className).toContain("[&>section]:h-full");
    expect(screen.getByTestId("ctem-discovery-side-column").className).toContain("[&>section:last-child]:grow");
    expect(dataRow.getAttribute("data-wide-columns")).toBe("2");
    expect(screen.getByTestId("ctem-discovery-tactics").parentElement).toBe(dataRow);
    const side = screen.getByTestId("ctem-discovery-side-column");
    expect(side.contains(screen.getByTestId("ctem-discovery-threats"))).toBe(true);
    expect(side.contains(screen.getByTestId("ctem-discovery-targeting"))).toBe(true);
  });

  it("draws every bar row as label, bar and value, in that order (B3)", () => {
    renderWithProviders(<DiscoveryMix viz={VIZ} connected={all} onConnect={() => {}} />);
    const rows = document.querySelectorAll("[data-testid$='-bars'] > li");
    expect(rows.length).toBeGreaterThan(1);
    rows.forEach((li) => {
      expect(li.children).toHaveLength(3);
      expect(li.children[1].getAttribute("aria-hidden")).toBe("true");
    });
  });

  it("gives a lone gap filler the full row (UX round 2, B3)", () => {
    renderWithProviders(<DiscoveryMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: false }} onConnect={() => {}} />);
    const gapRow = screen.getByTestId("ctem-discovery-gap-row");
    expect(gapRow.children).toHaveLength(1);
    expect(gapRow.getAttribute("data-columns")).toBe("1");
  });

  it("puts two gap fillers on two columns and three on three", () => {
    const { unmount } = renderWithProviders(
      <DiscoveryMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />,
    );
    expect(screen.getByTestId("ctem-discovery-gap-row").getAttribute("data-columns")).toBe("2");
    unmount();
    renderWithProviders(<DiscoveryMix viz={undefined} connected={{ opencti: false, openaev: false, opencrq: false }} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-discovery-gap-row").getAttribute("data-columns")).toBe("3");
  });

  it("renders nothing when every module is connected and OpenCTI answered no list", () => {
    renderWithProviders(<DiscoveryMix viz={{}} connected={all} onConnect={() => {}} />);
    expect(screen.queryByTestId("ctem-discovery-mix")).toBeNull();
  });
});
