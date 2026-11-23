/**
 * "What starts it" on the fleet About tab (#1319): the documented triggers are
 * listed in the "How it works" card, the same list as the setup wizard's first
 * step, so a Platform Health reader sees "Module checks" - `yarn test:unit`.
 */
import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";

import type { FleetDocumentation } from "@/hooks/api/useFleetApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

import { FleetAboutShowcase } from "./FleetAboutShowcase";
import { FleetTriggerList } from "./FleetTriggerList";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(async () => ({})), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), download: vi.fn() },
}));

/** The six triggers Platform Health ships (`PLATFORM_HEALTH_STRUCTURED` in builtin_fleet_docs.py). */
const PLATFORM_HEALTH_TRIGGERS = [
  { module: "platform", name: "Signals", filter: "Feedback drop, failure rate, cost increase or model change" },
  { module: "schedule", name: "Memory review", filter: "Daily, on the schedule set in Settings" },
  { module: "schedule", name: "Quality review", filter: "Weekly, on the schedule set in Settings" },
  { module: "schedule", name: "Flow review", filter: "Weekly, on the schedule set in Settings" },
  { module: "schedule", name: "Weekly digest", filter: "On the schedule set in Settings" },
  { module: "schedule", name: "Module checks", filter: "Daily, on every registered module (XTM One)" },
];

function healthDoc(): FleetDocumentation {
  return {
    section_keys: ["purpose"],
    sections: { purpose: "Keeps the platform healthy" },
    version: 1,
    updated_at: "2026-11-22T23:00:00Z",
    updated_by: "seed",
    editable: false,
    local: false,
    out_of_date: false,
    structured: { purpose_line: "Keeps the platform healthy", triggers: PLATFORM_HEALTH_TRIGGERS },
  };
}

describe("What starts it", () => {
  it("lists every Platform Health trigger on the About tab, Module checks included", () => {
    vi.mocked(api.get).mockImplementation(async () => ({}));
    renderWithProviders(
      <FleetAboutShowcase fleetId="f-health" doc={healthDoc()} health={undefined} onManageAutomations={() => {}} readOnly />,
    );
    const block = screen.getByTestId("about-triggers");
    expect(within(block).getByRole("heading", { name: "What starts it" })).toBeTruthy();
    const list = within(block).getByRole("list", { name: "What starts it" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(6);
    expect(within(list).getByText("Module checks: Daily, on every registered module (XTM One)")).toBeTruthy();
    expect(within(list).getByText("Memory review: Daily, on the schedule set in Settings")).toBeTruthy();
  });

  it("says a trigger whose automation is switched off is off", () => {
    renderWithProviders(
      <FleetTriggerList triggers={[{ module: "schedule", name: "Nightly sweep", enabled: false }, { module: "opencti", name: "New report" }]} />,
    );
    const rows = screen.getAllByRole("listitem");
    expect(within(rows[0]).getByText("Off")).toBeTruthy();
    expect(within(rows[1]).queryByText("Off")).toBeNull();
  });

  it("prefers the generated detail over the shipped filter", () => {
    renderWithProviders(<FleetTriggerList triggers={[{ module: "opencti", name: "New report", detail: "every report", filter: "type = report" }]} />);
    expect(screen.getByText("New report: every report")).toBeTruthy();
  });

  it("draws nothing without a trigger", () => {
    renderWithProviders(<FleetTriggerList triggers={[]} testId="empty-triggers" />);
    expect(screen.queryByTestId("empty-triggers")).toBeNull();
    expect(screen.queryByRole("list", { name: "What starts it" })).toBeNull();
  });
});
