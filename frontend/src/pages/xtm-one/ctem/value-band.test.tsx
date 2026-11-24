// The value band of the CTEM command center (C2 slice V1): fleets off is the
// primary case, so no tile shows an empty or zero state because nothing ran,
// no sample figure is drawn, and the band starts no run.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { ValueBandCard, type ValueBandProps } from "./ValueBandCard";

const state = vi.hoisted(() => ({
  report: { data: undefined as unknown, isError: false, refetch: () => undefined },
  waiting: { count: 0, oldest: null },
}));

vi.mock("@/hooks/api/useRoiReportApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useRoiReportApi")>()),
  useValueReport: () => state.report,
}));
vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItemsWaitingForMe: () => ({ data: state.waiting }),
}));
vi.mock("@/components/ui/enterprise-upsell", () => ({
  AiLockedFooter: ({ reason }: { reason: string }) => <p>AI locked: {reason}</p>,
}));

const figures = (runs: number) => ({
  currency: "USD",
  runs,
  units: runs,
  hours_saved: runs * 2,
  llm_cost: 1,
  value: runs * 100,
  net_value: runs * 99,
  roi: 10,
  reviewed: 0,
  unreviewed: runs,
  rejected: 0,
});

function renderBand(overrides: Partial<ValueBandProps> = {}) {
  return renderWithProviders(
    <MemoryRouter>
      <ValueBandCard {...bandProps(overrides)} />
    </MemoryRouter>,
  );
}

function band(overrides: Partial<ValueBandProps> = {}) {
  const props = bandProps(overrides);
  renderWithProviders(
    <MemoryRouter>
      <ValueBandCard {...props} />
    </MemoryRouter>,
  );
  return props;
}

function bandProps(overrides: Partial<ValueBandProps> = {}): ValueBandProps {
  return {
    score: 64,
    band: "at_risk",
    coveredStages: 4,
    totalStages: 5,
    history: [58, 60, 64],
    kev: 12,
    briefing: null,
    syncedAt: undefined,
    syncing: false,
    onForceSync: vi.fn(),
    onExplain: vi.fn(),
    onConnectOpenCti: vi.fn(),
    runsThisMonth: 7,
    aiLockReason: null,
    isAdmin: true,
    ...overrides,
  };
}

describe("ValueBandCard", () => {
  beforeEach(() => {
    // The labels the API sends (backend/app/services/roi_report.py rates_for), never a made-up one (UX r1 B5).
    state.report = { data: { currency: "USD", rate_label: "catalog default", estimated: true, organization: figures(0) }, isError: false, refetch: () => undefined };
    state.waiting = { count: 0, oldest: null };
  });

  it("writes a deterministic headline from the posture when no briefing is ready", () => {
    band();
    const headline = screen.getByTestId("ctem-value-band-headline").textContent ?? "";
    expect(headline).toContain("Exposure posture 64/100 across 4 of 5 CTEM stages.");
    expect(headline).toContain("12 known-exploited exposures.");
  });

  it("writes a large known-exploited count compact in the headline, like the Risk tile (UX B2 on #10529)", () => {
    band({ kev: 620_965 });
    expect(screen.getByTestId("ctem-value-band-headline").textContent).toContain("621K known-exploited exposures.");
  });

  it("says there is no posture yet and what to connect, never a claim, when no score exists", () => {
    band({ score: null, history: [], kev: null });
    expect(screen.getByTestId("ctem-value-band-headline").textContent).toBe("No exposure posture yet. Connect OpenCTI or OpenAEV to build it.");
  });

  it("quotes the ready briefing's headline and its author instead", () => {
    band({ briefing: { headline: "Two edge VPNs are exploited", agentName: "Exposure Briefing Analyst" } });
    expect(screen.getByTestId("ctem-value-band-headline").textContent).toBe("Two edge VPNs are exploited");
    expect(screen.getByText(/By Exposure Briefing Analyst/)).toBeTruthy();
  });

  it("with no value recorded says so and counts the month's runs, never 0 h", () => {
    band();
    const tile = screen.getByTestId("ctem-value-band-value");
    expect(tile.textContent).toContain("No value recorded yet");
    expect(tile.textContent).toContain("7 agentic runs in the last 30 days");
    expect(tile.textContent).not.toMatch(/\b0 h\b/);
    expect(screen.getByRole("link", { name: "How value is computed" }).getAttribute("href")).toBe("/tasks/value/methodology");
  });

  it("with value recorded names each figure, labels the catalog default rate and links the Value page", () => {
    state.report = { ...state.report, data: { currency: "USD", rate_label: "catalog default", estimated: true, organization: figures(3) } };
    band();
    const tile = screen.getByTestId("ctem-value-band-value");
    expect(screen.getByText("Hours saved")).toBeTruthy();
    expect(screen.getByText("Net value")).toBeTruthy();
    expect(screen.getByTestId("ctem-value-band-value-basis").textContent).toBe("Last 30 days · Estimated · Default rate");
    expect(tile.textContent).toContain("Estimated");
    expect(screen.getByRole("link", { name: "See the value" }).getAttribute("href")).toBe("/tasks/value");
  });

  it("says no default rate once the organization set its own, role rates included", () => {
    for (const rate_label of ["organization override", "organization override; organization rates by role"]) {
      state.report = { ...state.report, data: { currency: "USD", rate_label, estimated: false, organization: figures(3) } };
      const { unmount } = renderBand();
      expect(screen.getByTestId("ctem-value-band-value-basis").textContent).toBe("Last 30 days");
      unmount();
    }
  });

  it("prints the signed period delta against the first recorded day", () => {
    band({ score: 59, history: [118, 90, 59] });
    expect(screen.getByText(/-59 points vs start of period/)).toBeTruthy();
  });

  it("counts the agentic runs of the last 30 days, the Agentic executions card's figure, not the valued runs", () => {
    state.report = { ...state.report, data: { currency: "USD", rate_label: "catalog default", estimated: true, organization: figures(20) } };
    band({ runsThisMonth: 52 });
    expect(screen.getByTestId("ctem-value-band-runs").textContent).toBe("52 agentic runs in the last 30 days");
  });

  it("with AI locked says why no agent runs instead of a zero", () => {
    band({ aiLockReason: "ai_off" });
    expect(screen.getByTestId("ctem-value-band-runs").textContent).toBe("Agents cannot run until AI is available");
  });

  it("without OpenCTI offers to connect it and draws no sample risk figure", () => {
    const props = band({ kev: null });
    const tile = screen.getByTestId("ctem-value-band-risk");
    expect(tile.textContent).not.toMatch(/\$|4\.2M/);
    screen.getByRole("button", { name: "Connect OpenCTI" }).click();
    expect(props.onConnectOpenCti).toHaveBeenCalledTimes(1);
  });

  it("with AI locked shows the lock reason in the value tile", () => {
    band({ aiLockReason: "ai_off" });
    expect(screen.getByTestId("ctem-value-band-value").textContent).toContain("AI locked: ai_off");
  });

  it("says what waits for the reader, and the gauge opens the posture explanation", () => {
    state.waiting = { count: 3, oldest: null };
    const props = band();
    expect(screen.getByText("3 waiting for you")).toBeTruthy();
    screen.getByRole("button", { name: "How the Exposure Posture is computed" }).click();
    expect(props.onExplain).toHaveBeenCalledTimes(1);
  });
});
