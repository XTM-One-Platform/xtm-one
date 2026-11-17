// Settings > Agents and automations > Fleets: the member presets alone, no
// fleet list (Sam, 12 Nov 2026); the licence refusal (403) is said as on the
// Fleets list.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { FleetsSettingsSection } from "./FleetsSettingsSection";

let presetsResult: { data?: unknown[]; isLoading: boolean; error: unknown } = { isLoading: false, error: null };
const useFleets = vi.fn();

vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleets: () => useFleets(),
  useFleetMemberPresets: () => presetsResult,
  isFleetsUnavailable: (error: unknown) => (error as { status?: number } | null)?.status === 403,
}));

// The member presets card has its own test; here it is a stub so the section reads alone.
vi.mock("./FleetMemberPresetsSection", () => ({ FleetMemberPresetsSection: () => <div data-testid="presets-stub" /> }));

function mount() {
  return renderWithProviders(
    <MemoryRouter>
      <FleetsSettingsSection />
    </MemoryRouter>,
  );
}

describe("FleetsSettingsSection", () => {
  beforeEach(() => {
    presetsResult = { isLoading: false, error: null };
    useFleets.mockReset();
  });

  it("shows the member presets alone, with no fleet list and no fleet read", () => {
    presetsResult = { data: [], isLoading: false, error: null };
    mount();
    expect(screen.getByTestId("presets-stub")).toBeTruthy();
    expect(screen.queryByTestId("settings-fleets")).toBeNull();
    expect(screen.queryByText("No fleet yet")).toBeNull();
    expect(screen.queryByRole("link", { name: /Open Fleets/ })).toBeNull();
    expect(useFleets).not.toHaveBeenCalled();
  });

  it("says the licence refusal instead of the presets on a 403", () => {
    presetsResult = { data: undefined, isLoading: false, error: Object.assign(new Error("Forbidden"), { status: 403 }) };
    mount();
    expect(screen.getByText("Fleets need the agentic AI licence")).toBeTruthy();
    expect(screen.queryByTestId("presets-stub")).toBeNull();
  });
});
