// The read-only summary of a CTEM signal trigger (#1409, UX round 1 B2): the
// same translated labels as the form, never a raw key or a JSON list.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { TriggerConfig } from "@/types";

vi.mock("@/hooks/api/useCtemSignalsApi", () => ({
  useCtemSignalPaths: () => ({
    data: { paths: [{ id: "credential_leak", label: "Credential or secret leak", sources: [{ provider: "opencti" }] }] },
    isLoading: false,
  }),
}));

const { CtemSignalSummary } = await import("./CtemSignalTriggerFields");

const cfg = (value: Record<string, unknown>) => value as unknown as TriggerConfig;

describe("CtemSignalSummary", () => {
  it("shows the filled filters with their labels and no raw key or list", () => {
    const { container } = renderWithProviders(
      <CtemSignalSummary
        config={cfg({
          event: ["ctem_signal.recorded", "ctem_signal.triaged"],
          filter: { trigger_path: ["credential_leak"], verdict: ["confirmed", "to_validate"], source_provider: ["manual"] },
        })}
      />,
    );
    expect(screen.getByText("Recorded or triaged")).toBeTruthy();
    expect(screen.getByText("Credential or secret leak")).toBeTruthy();
    expect(screen.getByText("Recorded by hand")).toBeTruthy();
    expect(screen.getByText("Trigger path")).toBeTruthy();
    expect(screen.getByText("Verdict")).toBeTruthy();
    const text = container.textContent ?? "";
    expect(text).not.toContain("[");
    expect(text).not.toMatch(/trigger_path|source_provider|to_validate|credential_leak|ctem_signal\./);
    expect(screen.queryByText("This agent's objects")).toBeNull();
  });

  it("renders every filter value as the design system Chip", () => {
    renderWithProviders(
      <CtemSignalSummary
        config={cfg({
          event: "ctem_signal.triaged",
          filter: { trigger_path: ["credential_leak"], verdict: ["confirmed", "to_validate"], source_provider: ["manual"] },
        })}
      />,
    );
    const chips = screen.getAllByTestId("ctem-signal-summary-value");
    expect(chips).toHaveLength(4);
    expect(chips.map((c) => c.textContent)).toContain("Credential or secret leak");
  });

  it("reads a trigger without filters as every signal", () => {
    renderWithProviders(<CtemSignalSummary config={cfg({ event: "ctem_signal.triaged" })} />);
    expect(screen.getByText("Triaged")).toBeTruthy();
    expect(screen.getByText("Every signal")).toBeTruthy();
  });
});
