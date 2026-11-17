// The Mobilization stage page's mixed views (#11998): every module combination draws
// cards, never an empty frame; a missing module is a gap filler marked Sample with its
// Connect action; a list a module did not answer is not drawn.
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemStageViz } from "@/types";
import { MobilizationMix } from "./MobilizationMix";

const VIZ: CtemStageViz = {
  response_mix: [
    { name: "case_incidents", value: 41 },
    { name: "case_rfis", value: 18 },
    { name: "unknown", value: 3 },
  ],
  response_progress: { cases_closed: 61, cases_total: 83, playbooks_running: 3, playbooks_total: 5 },
  fix_backlog: [
    { name: "open_findings", value: 89 },
    { name: "failed_partial_expectations", value: 330 },
  ],
};

describe("MobilizationMix", () => {
  it("draws OpenCTI's response progress and cases by kind with OpenCTI only, and gap fillers for OpenAEV and OpenCRQ", () => {
    renderWithProviders(<MobilizationMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    const rows = screen.getByTestId("ctem-mobilization-progress-rows").querySelectorAll("li");
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("61 of 83");
    expect(rows[1].textContent).toContain("3 of 5");
    // A kind the page has no label for is not drawn.
    expect(screen.getByTestId("ctem-mobilization-mix-bars").querySelectorAll("li")).toHaveLength(2);
    // OpenAEV's backlog is never drawn without OpenAEV, even when the payload carries it.
    expect(screen.queryByTestId("ctem-mobilization-backlog")).toBeNull();
    const dataRow = screen.getByTestId("ctem-mobilization-data-row");
    expect(dataRow.getAttribute("data-wide-columns")).toBe("1");
    expect(Array.from(dataRow.children).map((c) => c.getAttribute("data-testid"))).toEqual([
      "ctem-mobilization-progress",
      "ctem-mobilization-mix-cases",
    ]);
    expect(screen.getByTestId("ctem-mobilization-gap-openaev")).toBeTruthy();
    expect(screen.getByTestId("ctem-mobilization-gap-opencrq")).toBeTruthy();
    expect(screen.queryByTestId("ctem-mobilization-gap-opencti")).toBeNull();
    expect(screen.getByTestId("ctem-mobilization-gap-row").getAttribute("data-columns")).toBe("2");
  });

  it("draws OpenAEV's fix backlog with OpenCTI and OpenAEV, and only the OpenCRQ gap filler", () => {
    renderWithProviders(<MobilizationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: false }} onConnect={() => {}} />);
    expect(screen.getByTestId("ctem-mobilization-backlog-bars").querySelectorAll("li")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /^Connect / })).toHaveLength(1);
    expect(screen.getByTestId("ctem-mobilization-gap-row").getAttribute("data-columns")).toBe("1");
  });

  it("draws no OpenCTI card without OpenCTI, and its gap filler with Connect", () => {
    const onConnect = vi.fn();
    renderWithProviders(<MobilizationMix viz={VIZ} connected={{ opencti: false, openaev: true, opencrq: true }} onConnect={onConnect} />);
    expect(screen.queryByTestId("ctem-mobilization-progress")).toBeNull();
    expect(screen.queryByTestId("ctem-mobilization-mix-cases")).toBeNull();
    expect(screen.getByTestId("ctem-mobilization-backlog")).toBeTruthy();
    const gap = screen.getByTestId("ctem-mobilization-gap-opencti");
    expect(gap.textContent).toContain("What OpenCTI adds here");
    expect(gap.textContent).toContain("Sample");
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenCTI" }));
    expect(onConnect).toHaveBeenCalledTimes(1);
  });

  it("shows three gap fillers when no module is connected", () => {
    renderWithProviders(<MobilizationMix viz={undefined} connected={{ opencti: false, openaev: false, opencrq: false }} onConnect={() => {}} />);
    expect(screen.getAllByRole("button", { name: /^Connect / })).toHaveLength(3);
    expect(screen.getByTestId("ctem-mobilization-gap-row").getAttribute("data-columns")).toBe("3");
  });

  it("draws no progress bar on an empty case queue", () => {
    renderWithProviders(
      <MobilizationMix
        viz={{ response_progress: { cases_closed: 0, cases_total: 0, playbooks_running: 3, playbooks_total: 5 } }}
        connected={{ opencti: true, openaev: true, opencrq: true }}
        onConnect={() => {}}
      />,
    );
    const rows = screen.getByTestId("ctem-mobilization-progress-rows").querySelectorAll("li");
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain("Playbooks running");
  });

  it("never ends a card on its sr-only table (phantom bottom margin, UX r3 of #12096)", () => {
    renderWithProviders(<MobilizationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />);
    for (const id of ["ctem-mobilization-progress", "ctem-mobilization-mix-cases", "ctem-mobilization-backlog"]) {
      expect(screen.getByTestId(id).lastElementChild?.tagName).toBe("P");
    }
  });

  it("writes the French progress and gap filler title, never a raw placeholder", () => {
    renderWithProviders(<MobilizationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: false }} onConnect={() => {}} />, { locale: "fr" });
    expect(screen.getByTestId("ctem-mobilization-progress-rows").textContent).toContain("61 sur 83");
    const gap = screen.getByTestId("ctem-mobilization-gap-opencrq").textContent ?? "";
    expect(gap).toContain("Ce qu’OpenCRQ apporte ici");
    expect(gap).not.toContain("{module}");
  });

  it("renders nothing with all modules connected and no list answered", () => {
    renderWithProviders(<MobilizationMix viz={{}} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />);
    expect(screen.queryByTestId("ctem-mobilization-mix")).toBeNull();
  });
});
