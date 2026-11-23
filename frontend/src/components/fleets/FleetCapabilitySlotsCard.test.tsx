// The capabilities card (#1201): one row per capability type, a row expands
// to its members (Custom and Reset to fleet choice), a type change asks before
// it replaces a custom member's choice. Inside the setup wizard (#1151, UX r1
// B2 and A1, r2 B4) nothing leaves the wizard in the same tab; on the Members
// view a member links to its agent and the card can be collapsed.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { Fleet, FleetCapabilityType } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import { FleetCapabilitySlotsCard } from "./FleetCapabilitySlotsCard";

const api = vi.hoisted(() => ({
  refetch: vi.fn(),
  setType: vi.fn(),
  setMember: vi.fn(),
  types: [] as unknown[],
}));

vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleetCapabilitySlots: () => ({
    data: { fleet_id: "f1", members: [], types: api.types },
    isLoading: false,
    error: null,
    refetch: api.refetch,
  }),
  useSetFleetCapability: () => ({ mutate: api.setType, isPending: false }),
  useSetFleetMemberSlot: () => ({ mutate: api.setMember, isPending: false }),
}));

const JIRA = { integration_id: "i1", provider: "jira", name: "Jira ITS" };
const SNOW = { integration_id: "i2", provider: "servicenow", name: "ServiceNow" };

const TICKETING_NONE: FleetCapabilityType = {
  capability: "ticketing",
  label: "Ticketing",
  state: "none_connected",
  choice: [],
  candidates: [],
  builtin: null,
  members: [{ member_id: "m1", agent_id: "a1", name: "Vuln Triage Analyst", role_label: "Triage", can_edit: true, bound: [], custom: false }],
};

const TICKETING_CUSTOM: FleetCapabilityType = {
  capability: "ticketing",
  label: "Ticketing",
  state: "connected",
  choice: [JIRA],
  candidates: [JIRA, SNOW],
  builtin: null,
  members: [
    { member_id: "m1", agent_id: "a1", name: "Mobilizer", role_label: "Mobilization", can_edit: true, bound: [JIRA], custom: false },
    { member_id: "m2", agent_id: "a2", name: "Validator", role_label: "Validation", can_edit: true, bound: [SNOW], custom: true },
  ],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  api.refetch.mockClear();
  api.setType.mockClear();
  api.setMember.mockClear();
});

const fleet = { id: "f1", name: "Vulnerability management", is_builtin: true } as unknown as Fleet;
const render = (inSetup: boolean) =>
  renderWithProviders(
    <MemoryRouter>
      <FleetCapabilitySlotsCard fleet={fleet} inSetup={inSetup} />
    </MemoryRouter>,
  );

describe("FleetCapabilitySlotsCard", () => {
  it("draws one row per capability type with its member count", () => {
    api.types = [TICKETING_CUSTOM];
    render(false);
    expect(screen.getAllByTestId("fleet-capability-slot-row")).toHaveLength(1);
    expect(screen.getByText("2 members")).toBeInTheDocument();
    expect(screen.getByText("Custom")).toBeInTheDocument();
  });

  it("in the setup wizard: no link out, no collapse, Connect one opens a new tab and refetches on return", () => {
    api.types = [TICKETING_NONE];
    const opened = vi.spyOn(window, "open").mockReturnValue(null);
    render(true);
    expect(screen.getByText("1 member")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Hide Capabilities" })).toBeNull();
    fireEvent.click(screen.getByText("Ticketing"));
    expect(screen.getByText("Vuln Triage Analyst")).toBeInTheDocument();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Connect one" }));
    expect(opened).toHaveBeenCalledWith("/integrations", "_blank", "noopener,noreferrer");
    window.dispatchEvent(new Event("focus"));
    expect(api.refetch).toHaveBeenCalledTimes(1);
  });

  it("on the Members view: a member links to its agent in the same tab and the card can be collapsed", () => {
    api.types = [TICKETING_NONE];
    const opened = vi.spyOn(window, "open").mockReturnValue(null);
    render(false);
    fireEvent.click(screen.getByText("Ticketing"));
    const link = screen.getByRole("link", { name: "Vuln Triage Analyst" });
    expect(link).toHaveAttribute("href", "/agents/a1");
    expect(link).not.toHaveAttribute("target");
    fireEvent.click(screen.getByRole("button", { name: "Connect one" }));
    expect(opened).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Hide Capabilities" })).toHaveAttribute("aria-expanded", "true");
  });

  it("a Not usable row names its first blocked integration in one line, the rest behind +N (UX r2 B4 of #1376)", () => {
    api.types = [
      {
        ...TICKETING_NONE,
        state: "not_usable",
        blocked: [
          { ...JIRA, reason: "not_company_managed" },
          { ...SNOW, reason: "not_connected" },
          { integration_id: "i3", provider: "youtrack", name: "YouTrack", reason: "not_company_managed" },
        ],
      },
    ];
    render(false);
    const cell = screen.getByTestId("type-blocked-chip-ticketing");
    expect(screen.getAllByTestId("type-blocked-chip-ticketing")).toHaveLength(1);
    expect(within(cell).getByText("Jira ITS · Not company-managed")).toBeInTheDocument();
    expect(within(cell).queryByText("ServiceNow · Sign-in not finished")).toBeNull();
    const more = screen.getByTestId("type-blocked-chip-more-ticketing");
    expect(more).toHaveTextContent("+2");
    expect(more).toHaveAttribute("aria-label", "ServiceNow · Sign-in not finished\nYouTrack · Not company-managed");
  });

  it("resets a custom member to the fleet choice", () => {
    api.types = [TICKETING_CUSTOM];
    render(false);
    fireEvent.click(screen.getByText("Ticketing"));
    const rows = screen.getAllByTestId("fleet-capability-member-row");
    expect(within(rows[0]).queryByRole("button", { name: "Reset to fleet choice" })).toBeNull();
    fireEvent.click(within(rows[1]).getByRole("button", { name: "Reset to fleet choice" }));
    expect(api.setMember).toHaveBeenCalledWith({ memberId: "m2", capability: "ticketing", integrationIds: ["i1"] }, expect.anything());
  });

  it("asks before a type change replaces a custom member's choice, and keeps it on Keep their choice", async () => {
    api.types = [{ ...TICKETING_CUSTOM, candidates: [JIRA, SNOW] }];
    render(false);
    // The fleet choice picker of the row (a plain select: two options): pick ServiceNow.
    fireEvent.click(screen.getByRole("combobox", { name: "Fleet choice for Ticketing" }));
    fireEvent.click(await screen.findByRole("option", { name: "ServiceNow" }));
    expect(await screen.findByText("Replace the custom choices?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Keep their choice" }));
    await waitFor(() =>
      expect(api.setType).toHaveBeenCalledWith({ capability: "ticketing", integrationIds: ["i2"], overrideCustom: false }, expect.anything()),
    );
  });
});
