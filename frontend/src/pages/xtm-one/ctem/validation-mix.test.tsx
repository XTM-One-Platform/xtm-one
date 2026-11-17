// The Validation stage page's mixed views (#11998): every module combination draws
// cards, never an empty frame; a missing module is a gap filler marked Sample with its
// Connect action; a list a module did not answer is not drawn.
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { CtemStageViz } from "@/types";
import { ValidationMix } from "./ValidationMix";

const VIZ: CtemStageViz = {
  rule_coverage_by_tactic: [
    { tactic: "initial-access", total: 62, covered: 9 },
    { tactic: "execution", total: 118, covered: 21 },
    { tactic: "empty", total: 0, covered: 0 },
  ],
  detection_depth: [
    { name: "indicators", value: 38500 },
    { name: "detection_indicators", value: 21400 },
    { name: "sightings", value: 1820 },
    { name: "unknown", value: 3 },
  ],
};

describe("ValidationMix", () => {
  it("draws OpenCTI's rule coverage and detection content with OpenCTI only, and gap fillers for OpenAEV and OpenCRQ", () => {
    renderWithProviders(<ValidationMix viz={VIZ} connected={{ opencti: true, openaev: false, opencrq: false }} onConnect={() => {}} />);
    const rows = screen.getByTestId("ctem-validation-rule-rows").querySelectorAll("li");
    // A tactic without techniques is not drawn.
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("Initial access");
    expect(rows[0].textContent).toContain("9 of 62");
    const depth = screen.getByTestId("ctem-validation-depth-bars").querySelectorAll("li");
    // A depth figure the page has no label for is not drawn.
    expect(depth).toHaveLength(3);
    // Stacked, never side by side: the two cards' bottoms cannot drift apart (UX r1 of #12142, B1).
    const dataRow = screen.getByTestId("ctem-validation-data-row");
    expect(dataRow.getAttribute("data-wide-columns")).toBe("1");
    expect(Array.from(dataRow.children).map((c) => c.getAttribute("data-testid"))).toEqual(["ctem-validation-rules", "ctem-validation-depth"]);
    expect(screen.getByTestId("ctem-validation-gap-openaev")).toBeTruthy();
    expect(screen.getByTestId("ctem-validation-gap-opencrq")).toBeTruthy();
    expect(screen.queryByTestId("ctem-validation-gap-opencti")).toBeNull();
    expect(screen.getByTestId("ctem-validation-gap-row").getAttribute("data-columns")).toBe("2");
  });

  it("draws no OpenCTI card without OpenCTI, even when the payload carries them, and its gap filler with Connect", () => {
    const onConnect = vi.fn();
    renderWithProviders(<ValidationMix viz={VIZ} connected={{ opencti: false, openaev: true, opencrq: true }} onConnect={onConnect} />);
    expect(screen.queryByTestId("ctem-validation-rules")).toBeNull();
    expect(screen.queryByTestId("ctem-validation-data-row")).toBeNull();
    const gap = screen.getByTestId("ctem-validation-gap-opencti");
    expect(gap.textContent).toContain("What OpenCTI adds here");
    expect(gap.textContent).toContain("Sample");
    fireEvent.click(screen.getByRole("button", { name: "Connect OpenCTI" }));
    expect(onConnect).toHaveBeenCalledTimes(1);
  });

  it("shows three gap fillers when no module is connected", () => {
    renderWithProviders(<ValidationMix viz={undefined} connected={{ opencti: false, openaev: false, opencrq: false }} onConnect={() => {}} />);
    expect(screen.getAllByRole("button", { name: /^Connect / })).toHaveLength(3);
    expect(screen.getByTestId("ctem-validation-gap-row").getAttribute("data-columns")).toBe("3");
  });

  it("draws one wide card when only the rule coverage was answered", () => {
    renderWithProviders(
      <ValidationMix viz={{ rule_coverage_by_tactic: VIZ.rule_coverage_by_tactic }} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />,
    );
    expect(screen.getByTestId("ctem-validation-data-row").getAttribute("data-wide-columns")).toBe("1");
    expect(screen.queryByTestId("ctem-validation-depth")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Connect / })).toBeNull();
  });

  it("never ends a card on its sr-only table (phantom bottom margin, UX r3 of #12096)", () => {
    renderWithProviders(<ValidationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />);
    for (const id of ["ctem-validation-rules", "ctem-validation-depth"]) {
      expect(screen.getByTestId(id).lastElementChild?.tagName).toBe("P");
    }
  });

  it("writes the French covered count and gap filler title, never a raw placeholder", () => {
    renderWithProviders(<ValidationMix viz={VIZ} connected={{ opencti: true, openaev: true, opencrq: false }} onConnect={() => {}} />, { locale: "fr" });
    expect(screen.getByTestId("ctem-validation-rule-rows").textContent).toContain("9 sur 62");
    const gap = screen.getByTestId("ctem-validation-gap-opencrq").textContent ?? "";
    expect(gap).toContain("Ce qu’OpenCRQ apporte ici");
    expect(gap).not.toContain("{module}");
  });

  it("renders nothing with all modules connected and no list answered", () => {
    renderWithProviders(<ValidationMix viz={{}} connected={{ opencti: true, openaev: true, opencrq: true }} onConnect={() => {}} />);
    expect(screen.queryByTestId("ctem-validation-mix")).toBeNull();
  });
});
