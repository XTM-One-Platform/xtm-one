/**
 * The Platform Health fleet's About page says what starts it (#2194, the lab retest of
 * #980): the starts line, the Platform Health settings link and the Modules row the
 * Fleets list card shows, through the same component - `yarn test:unit`.
 */
import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { FleetDocumentation, FleetHealthStarts } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import { FleetAboutShowcase } from "./FleetAboutShowcase";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(async () => ({})), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), download: vi.fn() },
}));

function doc(): FleetDocumentation {
  return {
    section_keys: ["purpose"],
    sections: { purpose: "Keeps the platform healthy" },
    version: 1,
    updated_at: "2026-11-22T23:00:00Z",
    updated_by: "seed",
    editable: false,
    local: false,
    out_of_date: false,
    structured: { purpose_line: "Keeps the platform healthy" },
  };
}

const STARTS: FleetHealthStarts = {
  events: true,
  schedules: 3,
  next_run_at: null,
  modules: ["opencti", "openaev"],
  settings_route: "/settings/automations/health",
  active: true,
};

function renderAbout(healthStarts: FleetHealthStarts | null) {
  renderWithProviders(
    <MemoryRouter>
      <FleetAboutShowcase fleetId="f-health" doc={doc()} health={undefined} healthStarts={healthStarts} onManageAutomations={() => {}} readOnly />
    </MemoryRouter>,
  );
}

describe("Platform Health fleet About page", () => {
  it("shows the starts line, the settings link and the modules", () => {
    renderAbout(STARTS);
    const line = screen.getByTestId("about-health-starts-line");
    expect(line.textContent).toContain("Events + 3 schedules");
    const link = within(line).getByRole("link", { name: "Platform Health settings" });
    expect(link.getAttribute("href")).toBe("/settings/automations/health");
    const modules = screen.getByTestId("about-health-modules");
    expect(within(modules).getByText("Modules")).toBeTruthy();
    expect(within(modules).getByRole("group").getAttribute("aria-label")).toMatch(/^Modules: /);
  });

  it("says the fleet is off in Settings when Platform Health is off", () => {
    renderAbout({ ...STARTS, active: false });
    expect(screen.getByTestId("about-health-starts-line").textContent).toContain("off in Settings");
  });

  it("leaves the Modules row out in Copilot mode", () => {
    renderAbout({ ...STARTS, modules: [] });
    expect(screen.getByTestId("about-health-starts-line")).toBeTruthy();
    expect(screen.queryByTestId("about-health-modules")).toBeNull();
  });

  it("draws nothing of it on any other fleet", () => {
    renderAbout(null);
    expect(screen.queryByTestId("about-health-starts")).toBeNull();
  });
});
