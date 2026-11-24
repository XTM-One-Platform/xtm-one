// The Scoping stage page's mixed views (#11998): every module combination draws
// cards, never an empty frame; a missing module is a gap filler marked Sample with
// its Connect action; a list OpenCTI did not answer is not drawn.
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemStageViz } from "@/types";
import { ScopingMix } from "./ScopingMix";

const VIZ: CtemStageViz = {
  observables_by_type: [
    { name: "IPv4-Addr", value: 120 },
    { name: "StixFile", value: 90 },
    { name: "Domain-Name", value: 80 },
  ],
  vulnerabilities_by_severity: [
    { label: "MEDIUM", value: 60 },
    { label: "HIGH", value: 40 },
    { label: "CRITICAL", value: 12 },
  ],
};

describe("ScopingMix", () => {
  it("draws the OpenCTI cards and gap fillers for the two missing modules", () => {
    renderWithProviders(<ScopingMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    const observables = screen.getByTestId("ctem-scoping-observables-bars").textContent ?? "";
    expect(observables).toContain("IPv4 address");
    expect(observables).toContain("File");
    expect(observables).not.toContain("StixFile");
    // The shared SeverityBars: fixed critical-to-low order, not sorted by count (UX round 1 of #12076, B1).
    const severity = screen.getByTestId("ctem-scoping-severity-bars").textContent ?? "";
    expect(severity.indexOf("Critical")).toBeLessThan(severity.indexOf("High"));
    expect(severity.indexOf("High")).toBeLessThan(severity.indexOf("Medium"));
    expect(screen.getByTestId("ctem-scoping-data-row").getAttribute("data-wide-columns")).toBe("2");
    // Equal heights per row (#1612): the row stretches and every card fills its cell.
    expect(screen.getByTestId("ctem-scoping-data-row").getAttribute("data-align")).toBe("stretch");
    expect(screen.getByTestId("ctem-scoping-gap-openaev")).toBeTruthy();
    expect(screen.getByTestId("ctem-scoping-gap-opencrq")).toBeTruthy();
    expect(screen.queryByTestId("ctem-scoping-gap-opencti")).toBeNull();
  });

  it("shows the OpenCTI gap filler when only OpenAEV is connected, with a Sample label and Connect", () => {
    const onConnect = vi.fn();
    renderWithProviders(<ScopingMix viz={undefined} connected={{ opencti: false, openaev: true, opencrq: false }} onConnect={onConnect} />);
    const gap = screen.getByTestId("ctem-scoping-gap-opencti");
    expect(gap.textContent).toContain("What OpenCTI adds here");
    expect(gap.textContent).toContain("Sample");
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenCTI" }));
    expect(onConnect).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("ctem-scoping-data-row")).toBeNull();
  });

  it("shows three gap fillers when no module is connected", () => {
    renderWithProviders(<ScopingMix viz={undefined} connected={{ opencti: false, openaev: false, opencrq: false }} onConnect={() => {}} />);
    expect(screen.getAllByRole("button", { name: /^Connect / })).toHaveLength(3);
    expect(screen.getByTestId("ctem-scoping-gap-row").getAttribute("data-columns")).toBe("3");
  });

  it("draws no gap filler when all three are connected, and skips a list OpenCTI did not answer", () => {
    renderWithProviders(
      <ScopingMix viz={{ observables_by_type: VIZ.observables_by_type }} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />,
    );
    expect(screen.getByTestId("ctem-scoping-observables")).toBeTruthy();
    expect(screen.queryByTestId("ctem-scoping-severity")).toBeNull();
    expect(screen.getByTestId("ctem-scoping-data-row").getAttribute("data-wide-columns")).toBe("1");
    expect(screen.queryByRole("button", { name: /^Connect / })).toBeNull();
  });

  it("keeps the Sample outline and Connect button in a bottom-aligned foot of every gap filler", () => {
    renderWithProviders(<ScopingMix viz={undefined} connected={{ opencti: false, openaev: false, opencrq: false }} onConnect={() => {}} />);
    for (const module of ["opencti", "openaev", "opencrq"]) {
      const gap = screen.getByTestId(`ctem-scoping-gap-${module}`);
      const foot = screen.getByTestId(`ctem-scoping-gap-${module}-foot`);
      // The foot is the last block of the card, after the list of what the module adds.
      expect(gap.contains(foot)).toBe(true);
      expect(foot.nextElementSibling).toBeNull();
      expect(foot.previousElementSibling?.tagName).toBe("UL");
      expect(foot.textContent).toContain("Sample");
      expect(foot.querySelector("button")).toBeTruthy();
    }
  });

  it("renders nothing with all modules connected and no list answered", () => {
    renderWithProviders(<ScopingMix viz={{}} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />);
    expect(screen.queryByTestId("ctem-scoping-mix")).toBeNull();
  });
});
