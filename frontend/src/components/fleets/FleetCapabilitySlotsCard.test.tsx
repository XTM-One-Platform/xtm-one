// The capability slots card inside the fleet setup wizard (#1151, UX r1 B2 and A1,
// r2 B4): nothing leaves the wizard in the same tab. On the Members view a row
// keeps its same-tab link to the member's agent and its Hide slots toggle.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { Fleet } from "@/hooks/api/useFleetApi";
import { renderWithProviders } from "@/testing/providers";

import { FleetCapabilitySlotsCard } from "./FleetCapabilitySlotsCard";

const refetch = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/api/useFleetApi", () => ({
  useFleetCapabilitySlots: () => ({
    data: {
      fleet_id: "f1",
      members: [
        {
          member_id: "m1",
          agent_id: "a1",
          name: "Vuln Triage Analyst",
          role_label: "Triage",
          can_edit: true,
          slots: [{ capability: "ticketing", label: "Ticketing", state: "unavailable", bound: [], candidates: [] }],
        },
      ],
    },
    isLoading: false,
    error: null,
    refetch,
  }),
  useBindFleetSlot: () => ({ mutate: vi.fn(), isPending: false }),
  useUnbindFleetSlot: () => ({ mutate: vi.fn(), isPending: false }),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  refetch.mockClear();
});

const fleet = { id: "f1", name: "Vulnerability management", is_builtin: true } as unknown as Fleet;
const render = (inSetup: boolean) =>
  renderWithProviders(
    <MemoryRouter>
      <FleetCapabilitySlotsCard fleet={fleet} inSetup={inSetup} />
    </MemoryRouter>,
  );

describe("FleetCapabilitySlotsCard", () => {
  it("in the setup wizard: no link out, no collapse, Connect one opens a new tab and refetches on return", () => {
    const opened = vi.spyOn(window, "open").mockReturnValue(null);
    render(true);
    expect(screen.getByText("Vuln Triage Analyst")).toBeInTheDocument();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Hide slots" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Connect one" }));
    expect(opened).toHaveBeenCalledWith("/integrations", "_blank", "noopener,noreferrer");
    window.dispatchEvent(new Event("focus"));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("on the Members view: a row links to the member's agent in the same tab and the card can be collapsed", () => {
    const opened = vi.spyOn(window, "open").mockReturnValue(null);
    render(false);
    const links = screen.getAllByRole("link");
    expect(links.some((link) => link.getAttribute("href") === "/agents/a1")).toBe(true);
    for (const link of links) expect(link).not.toHaveAttribute("target");
    fireEvent.click(screen.getByRole("button", { name: "Connect one" }));
    expect(opened).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Hide slots" })).toBeInTheDocument();
  });
});
