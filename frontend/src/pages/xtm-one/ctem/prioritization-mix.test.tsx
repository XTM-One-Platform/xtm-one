// The Prioritization stage page's mixed views (#11998): every module combination
// draws cards, never an empty frame; a missing module is a gap filler marked Sample
// with its Connect action; a list a module did not answer is not drawn.
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemStageViz } from "@/types";
import { PrioritizationMix } from "./PrioritizationMix";

const VIZ: CtemStageViz = {
  top_vulnerabilities: [
    { id: "vuln-3400", name: "CVE-2024-3400", cvss: 10, severity: "CRITICAL", epss: 0.94, kev: true },
    { name: "CVE-2023-4966", cvss: 9.4, severity: "CRITICAL", epss: 0.92, kev: false },
    { name: null, cvss: 9, severity: "CRITICAL", epss: null, kev: false },
  ],
  findings: { total: 89, with_cve: 61 },
};

describe("PrioritizationMix", () => {
  it("draws the top CVEs with OpenCTI only, and gap fillers for OpenAEV and OpenCRQ", () => {
    renderWithProviders(<PrioritizationMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    const list = screen.getByTestId("ctem-prioritization-top-cves-list");
    // A CVE without a name is not drawn.
    expect(list.querySelectorAll("li")).toHaveLength(2);
    expect(list.textContent).toContain("CVE-2024-3400");
    expect(list.textContent).toContain("KEV");
    // Findings are OpenAEV's: not drawn without it, even when the payload carries them.
    expect(screen.queryByTestId("ctem-prioritization-findings")).toBeNull();
    expect(screen.getByTestId("ctem-prioritization-gap-openaev")).toBeTruthy();
    expect(screen.getByTestId("ctem-prioritization-gap-opencrq")).toBeTruthy();
    expect(screen.queryByTestId("ctem-prioritization-gap-opencti")).toBeNull();
    expect(screen.getByTestId("ctem-prioritization-gap-row").getAttribute("data-columns")).toBe("2");
  });

  it("draws OpenAEV's findings split when OpenAEV is connected", () => {
    renderWithProviders(<PrioritizationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: false }} onConnect={() => {}} />);
    const split = screen.getByTestId("ctem-prioritization-findings-split");
    expect(split.textContent).toContain("89");
    expect(split.textContent).toContain("open findings on your assets");
    expect(split.textContent).toContain("61");
    expect(split.textContent).toContain("28");
    // The donut's centre names the total; the line under it gives the CVE share (UX r2 B2).
    expect(split.textContent).toContain("findings");
    expect(split.textContent).toContain("69% carry a CVE");
    expect(screen.getByTestId("ctem-prioritization-data-row").getAttribute("data-wide-columns")).toBe("2");
    // Equal heights per row (#1612): the row stretches and every card fills its cell.
    expect(screen.getByTestId("ctem-prioritization-data-row").getAttribute("data-align")).toBe("stretch");
  });

  it("reads one finding in the singular", () => {
    renderWithProviders(
      <PrioritizationMix viz={{ findings: { total: 1, with_cve: 1 } }} connected={{ opencti: false, openaev: true, opencrq: true }} onConnect={() => {}} />,
    );
    expect(screen.getByTestId("ctem-prioritization-findings-split").textContent).toContain("open finding on your assets");
  });

  it("shows the OpenCTI gap filler when only OpenAEV is connected, with a Sample label and Connect", () => {
    const onConnect = vi.fn();
    renderWithProviders(<PrioritizationMix viz={undefined} connected={{ opencti: false, openaev: true, opencrq: false }} onConnect={onConnect} />);
    const gap = screen.getByTestId("ctem-prioritization-gap-opencti");
    expect(gap.textContent).toContain("What OpenCTI adds here");
    expect(gap.textContent).toContain("Sample");
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenCTI" }));
    expect(onConnect).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("ctem-prioritization-data-row")).toBeNull();
  });

  it("shows three gap fillers when no module is connected", () => {
    renderWithProviders(<PrioritizationMix viz={undefined} connected={{ opencti: false, openaev: false, opencrq: false }} onConnect={() => {}} />);
    expect(screen.getAllByRole("button", { name: /^Connect / })).toHaveLength(3);
    expect(screen.getByTestId("ctem-prioritization-gap-row").getAttribute("data-columns")).toBe("3");
  });

  it("draws no gap filler when all three are connected", () => {
    renderWithProviders(<PrioritizationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-prioritization-top-cves")).toBeTruthy();
    expect(screen.getByTestId("ctem-prioritization-findings")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Connect / })).toBeNull();
  });

  it("keeps every row on the list's column tracks and writes CVSS and EPSS with the locale's formats (UX r1 B1, B3)", () => {
    renderWithProviders(<PrioritizationMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    const rows = screen.getByTestId("ctem-prioritization-top-cves-list").querySelectorAll("li");
    rows.forEach((li) => expect(li.children).toHaveLength(5));
    expect(rows[0].textContent).toContain("EPSS 94%");
    expect(rows[1].textContent).toContain("CVSS 9.4");
    const hidden = screen.getByTestId("ctem-hidden-data-table").textContent ?? "";
    expect(hidden).toContain("94%");
    expect(hidden).not.toContain("0.94");
  });

  it("opens a top CVE in OpenCTI by its id, and draws no pivot without an id or a base URL (#1357)", () => {
    const { unmount } = renderWithProviders(
      <PrioritizationMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} openctiUrl="https://cti.example/" />,
    );
    const pivots = screen.getAllByTestId("ctem-top-cve-pivot");
    // Only the CVE with an OpenCTI id gets the pivot.
    expect(pivots).toHaveLength(1);
    expect(pivots[0].getAttribute("href")).toBe("https://cti.example/dashboard/id/vuln-3400");
    expect(pivots[0].getAttribute("target")).toBe("_blank");
    expect(pivots[0].getAttribute("aria-label")).toBe("Open in OpenCTI");
    unmount();
    renderWithProviders(<PrioritizationMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    expect(screen.queryByTestId("ctem-top-cve-pivot")).toBeNull();
  });

  it("shows five CVEs, then the whole list on Show all, and back (UX r1 B2)", () => {
    const many = Array.from({ length: 7 }, (_, i) => ({ name: `CVE-2026-000${i}`, cvss: 9, severity: "CRITICAL", epss: 0.5, kev: false }));
    renderWithProviders(
      <PrioritizationMix viz={{ top_vulnerabilities: many }} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />,
    );
    const list = () => screen.getByTestId("ctem-prioritization-top-cves-list").querySelectorAll("li");
    expect(list()).toHaveLength(5);
    fireEvent.click(screen.getByRole("button", { name: "Show all (7)" }));
    expect(list()).toHaveLength(7);
    fireEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    expect(list()).toHaveLength(5);
  });

  it("never ends the Top CVEs card on its sr-only table, which kept a phantom bottom margin (UX r3 B2)", () => {
    renderWithProviders(<PrioritizationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />);
    const card = screen.getByTestId("ctem-prioritization-top-cves");
    expect(card.lastElementChild?.getAttribute("data-testid")).not.toBe("ctem-hidden-data-table");
    expect(card.lastElementChild?.tagName).toBe("P");
  });

  it("writes the French gap filler title with a typographic apostrophe, never a raw placeholder (UX r2 B4)", () => {
    renderWithProviders(<PrioritizationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: false }} onConnect={() => {}} />, {
      locale: "fr",
    });
    const gap = screen.getByTestId("ctem-prioritization-gap-opencrq").textContent ?? "";
    expect(gap).toContain("Ce qu’OpenCRQ apporte ici");
    expect(gap).not.toContain("{module}");
  });

  it("draws an empty ring and no share line when OpenAEV reports no finding", () => {
    renderWithProviders(<PrioritizationMix viz={{ findings: { total: 0, with_cve: 0 } }} connected={{ opencti: false, openaev: true, opencrq: true }} onConnect={() => {}} />);
    const split = screen.getByTestId("ctem-prioritization-findings-split").textContent ?? "";
    expect(split).toContain("open findings on your assets");
    expect(split).not.toContain("carry a CVE");
  });

  it("renders nothing with all modules connected and no list answered", () => {
    renderWithProviders(<PrioritizationMix viz={{}} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />);
    expect(screen.queryByTestId("ctem-prioritization-mix")).toBeNull();
  });
});
