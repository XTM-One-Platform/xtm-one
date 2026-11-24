// The CTEM Governance & Risk page (S2e of #11547): the target line and the gap
// when a target is set, the not-configured states with one action otherwise.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import CtemGovernance from "./CtemGovernance";

const state = vi.hoisted(() => ({
  target: null as number | null,
  admin: true,
  risks: [] as { subject: string; scope: string; owner: string; expires_on: string; reason: string }[],
  crq: { present: false } as { present: boolean; url?: string },
}));

vi.mock("@/hooks/api/useAuthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAuthApi")>()),
  useMe: () => ({ data: { ctem_vertical: "cyber" } }),
}));

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: state.admin }) }));

vi.mock("@/hooks/useTick", () => ({ useNow: () => Date.parse("2026-11-13T12:00:00Z") }));

vi.mock("@/hooks/api/useCtemMetricsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useCtemMetricsApi")>()),
  useCtemPosture: () => ({
    isLoading: false,
    data: { posture_score: 62, posture_band: "fair", products: { opencrq: state.crq }, stages: [] },
  }),
  useCtemPostureHistory: () => ({
    data: { history: [{ date: "2026-11-11", posture_score: 58, stage_metrics: {} }, { date: "2026-11-12", posture_score: 62, stage_metrics: {} }] },
  }),
}));

vi.mock("@/hooks/api/useOrganizationContextApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useOrganizationContextApi")>()),
  useOrganizationContext: () => ({
    data: { profile: { posture_target: state.target, prioritization_policy: { accepted_risks: state.risks } } },
  }),
  usePatchOrganizationContext: () => ({ isPending: false, mutate: () => undefined }),
}));

function page() {
  return renderWithProviders(
    <MemoryRouter initialEntries={["/ctem/governance"]}>
      <CtemGovernance />
    </MemoryRouter>,
  );
}

describe("CtemGovernance", () => {
  it("draws the target line, the gap and the headline when a target is set", () => {
    state.target = 75;
    page();
    expect(screen.getByTestId("ctem-governance-target-line")).toBeTruthy();
    expect(screen.getByTestId("ctem-governance-gap-area")).toBeTruthy();
    expect(screen.getByTestId("ctem-governance-gap").textContent).toBe("13 points below the target of 75");
    state.target = null;
  });

  it("asks an administrator to set a target, and a reader without the action", () => {
    page();
    expect(screen.getByTestId("ctem-governance-no-target").textContent).toContain("Set a posture target");
    expect(screen.queryByTestId("ctem-governance-target-line")).toBeNull();
    state.admin = false;
    page();
    expect(screen.getAllByTestId("ctem-governance-no-target")[1].querySelector("button")).toBeNull();
    state.admin = true;
  });

  it("places the accepted risks by expiry, and states SLA adherence needs OpenCRQ", () => {
    state.risks = [{ subject: "CVE-2026-1", scope: "", owner: "CISO", expires_on: "2026-11-10", reason: "" }];
    page();
    expect(screen.getByTestId("ctem-governance-risk-list").textContent).toContain("Expired 3 days ago");
    expect(screen.getByTestId("ctem-governance-sla-not-configured").textContent).toContain("SLA adherence needs OpenCRQ");
    state.risks = [];
  });

  it("labels the target field above it, says why a value is refused, and cancels without saving", () => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Set a posture target" }));
    const field = screen.getByLabelText("Posture target");
    fireEvent.change(field, { target: { value: "150" } });
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toBe("Enter a whole number from 0 to 100");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("ctem-governance-target-form")).toBeNull();
    expect(screen.getByTestId("ctem-governance-no-target")).toBeTruthy();
  });

  it("wraps every hidden data table in an sr-only box, so it cannot widen the page", () => {
    state.target = 75;
    page();
    const tables = screen.getAllByRole("table");
    expect(tables.length).toBeGreaterThan(0);
    for (const table of tables) {
      expect(table.parentElement?.getAttribute("data-testid")).toBe("ctem-hidden-data-table");
    }
    state.target = null;
  });
});
