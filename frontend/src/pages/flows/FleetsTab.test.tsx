// The Fleets toolbar stays mounted while the archived list loads.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Fleet } from "@/hooks/api/useFleetApi";
import FleetsTab from "./FleetsTab";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "me" }, isAdmin: false }) }));
vi.mock("@/hooks/api/useAgentApi", () => ({ useAgents: () => ({ data: [] }) }));
vi.mock("@/hooks/useToolAgents", () => ({
  useToolAgents: () => ({
    agents: [],
    generalAssistantOffered: true,
    linkedAgentId: null,
    none: false,
    resolving: false,
    defaultAgentId: null,
  }),
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
  useAiDisabledReason: () => null,
}));

const fleet = {
  id: "f1",
  user_id: "me",
  owner_name: "Me",
  name: "Research fleet",
  slug: "research-fleet",
  description: "",
  icon: "",
  color: "",
  workspace_id: "w1",
  workspace_name: "Research",
  orchestrator_agent_id: null,
  skills: null,
  accepted_handover_types: null,
  is_archived: false,
  is_company_managed: false,
  shared_group_ids: null,
  shared_group_permissions: null,
  shared_user_ids: null,
  access_restricted: false,
  can_manage: true,
  member_count: 0,
  connected_count: 0,
  exposed_agent_ids: [],
  // The card names the level only when it gates an action here (#10457).
  gated_action_count: 3,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
} satisfies Fleet;

/** Renders the current query, so a test asserts the URL and not the markup. */
function LocationProbe() {
  return <output aria-label="current query">{useLocation().search}</output>;
}

describe("FleetsTab", () => {
  it("a keystroke on page 2 keeps the search in the URL when the page clamps back", async () => {
    const many = Array.from({ length: 30 }, (_, i) => ({
      ...fleet,
      id: `f${i}`,
      name: `Research fleet ${String(i).padStart(2, "0")}`,
      slug: `research-fleet-${i}`,
    }));
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve(path === "/fleets" ? many : []),
    );
    renderWithProviders(
      <MemoryRouter initialEntries={["/fleets?vis=mine&page=1"]}>
        <FleetsTab />
        <LocationProbe />
      </MemoryRouter>,
    );

    const box = await screen.findByPlaceholderText("Search fleets...");
    await screen.findByText("Research fleet 29");
    // "fleet 0" keeps 10 fleets: page 2 empties and the clamp writes page 1 in the same tick.
    fireEvent.change(box, { target: { value: "fleet 0" } });

    await waitFor(() => {
      const query = new URLSearchParams(screen.getByRole("status", { name: "current query" }).textContent ?? "");
      expect(query.toString()).toBe("vis=mine&q=fleet+0");
    });
  });

  it("keeps the toolbar and its open Filters panel while the archived list loads", async () => {
    vi.mocked(api.get).mockImplementation((path: string) => {
      if (path === "/fleets") return Promise.resolve([fleet]);
      // The archived list never answers: the test reads the page while it is pending.
      if (path.startsWith("/fleets?include_archived")) return new Promise(() => {});
      return Promise.resolve([]);
    });
    renderWithProviders(
      <MemoryRouter>
        <FleetsTab />
      </MemoryRouter>,
    );

    const pill = await screen.findByRole("button", { name: /Filters/ });
    fireEvent.click(pill);
    fireEvent.click(screen.getByLabelText("Show archived"));

    expect(api.get).toHaveBeenCalledWith("/fleets?include_archived=true");
    const pillAfter = screen.getByRole("button", { name: /Filters/ });
    expect(pillAfter.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByLabelText("Show archived")).toBeTruthy();
    expect(screen.getByText("Research fleet")).toBeTruthy();
  });

  it("shows only the Built-in badge on a built-in fleet that is also company-managed", async () => {
    const builtin = { ...fleet, name: "CTEM fleet", is_builtin: true, is_company_managed: true };
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve(path === "/fleets" ? [builtin] : []),
    );
    renderWithProviders(
      <MemoryRouter>
        <FleetsTab />
      </MemoryRouter>,
    );

    await screen.findByText("CTEM fleet");
    // Scoped to the card: the Properties filter carries its own "Built-in" option chip (#11836).
    expect(within(screen.getByTestId("fleet-card-builtin")).getByText("Built-in")).toBeTruthy();
    expect(screen.queryByRole("img", { name: "Company-managed fleet" })).toBeNull();
  });

  it("shows the company-managed icon on a company fleet that is not built-in", async () => {
    const company = { ...fleet, name: "Company fleet", is_builtin: false, is_company_managed: true };
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve(path === "/fleets" ? [company] : []),
    );
    renderWithProviders(
      <MemoryRouter>
        <FleetsTab />
      </MemoryRouter>,
    );

    await screen.findByText("Company fleet");
    expect(screen.getByRole("img", { name: "Company-managed fleet" })).toBeTruthy();
    expect(screen.queryByTestId("fleet-card-builtin")).toBeNull();
  });

  it("selects a managed fleet with the shared box and confirms Pause all with the backend's count (#9769 S3c)", async () => {
    const other = { ...fleet, id: "f2", name: "Read-only fleet", can_manage: false };
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve(path === "/fleets" ? [fleet, other] : []),
    );
    vi.mocked(api.post).mockImplementation((_path: string, body: unknown) =>
      Promise.resolve({
        action: "pause",
        dry_run: (body as { dry_run?: boolean }).dry_run ?? false,
        changed: 3,
        refused: 0,
        fleets: [{ fleet_id: "f1", name: "Research fleet", ok: true, changed: 3, detail: "" }],
      }),
    );
    renderWithProviders(
      <MemoryRouter>
        <FleetsTab />
      </MemoryRouter>,
    );

    const box = await screen.findByRole("checkbox", { name: "Select Research fleet" });
    // A fleet the reader does not manage offers no box.
    expect(screen.queryByRole("checkbox", { name: "Select Read-only fleet" })).toBeNull();
    // The card shows the level alone; the full sentence is the screen-reader text and the tooltip (UX r1 B6).
    expect(screen.getAllByText("Human approval: Full human gates").length).toBe(2);
    expect(screen.getAllByText("Full human gates").length).toBe(2);
    fireEvent.click(box);
    expect(box.getAttribute("aria-checked")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Pause all" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.post).toHaveBeenCalledWith("/fleets/bulk", { action: "pause", fleet_ids: ["f1"], dry_run: true });
    const confirm = await within(dialog).findByRole("button", { name: "Pause 3 automations" });
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/fleets/bulk", { action: "pause", fleet_ids: ["f1"], dry_run: false }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("checkbox", { name: "Select Research fleet" }).getAttribute("aria-checked")).toBe("false");
  });

  it("names a skipped fleet's reason from the catalog and counts nothing while loading or at zero (UX r1 B2, B4)", async () => {
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve(path === "/fleets" ? [fleet] : []),
    );
    let release: (value: unknown) => void = () => {};
    vi.mocked(api.post).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    renderWithProviders(
      <MemoryRouter>
        <FleetsTab />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select Research fleet" }));
    fireEvent.click(screen.getByRole("button", { name: "Pause all" }));
    const dialog = await screen.findByRole("dialog");
    // While counting: no number on the confirm button.
    expect(within(dialog).getByRole("button", { name: "Pause all" }).hasAttribute("disabled")).toBe(true);
    expect(within(dialog).queryByText(/Pause 0 automations/)).toBeNull();
    release({
      action: "pause",
      dry_run: true,
      changed: 0,
      refused: 1,
      fleets: [
        { fleet_id: "f1", name: "Research fleet", ok: false, changed: 0, detail: "Only an administrator can pause the automations of a built-in fleet." },
      ],
    });
    expect(await within(dialog).findByText("Nothing to pause: no automation of the selected fleets is switched on.")).toBeTruthy();
    expect(within(dialog).getByText("Research fleet: Built-in fleet: only an administrator switches its automations.")).toBeTruthy();
    expect(within(dialog).queryByText(/Only an administrator can pause/)).toBeNull();
  });

  it("names the instructions position on a card that gates nothing, and No gated actions without one (#10457)", async () => {
    const byInstruction = { ...fleet, id: "f3", name: "Told fleet", gated_action_count: 0, instructions_position: "medium_low" as const };
    const ungated = { ...fleet, id: "f4", name: "Open fleet", gated_action_count: 0, instructions_position: null };
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve(path === "/fleets" ? [byInstruction, ungated] : []),
    );
    renderWithProviders(
      <MemoryRouter>
        <FleetsTab />
      </MemoryRouter>,
    );
    await screen.findByText("Told fleet");
    expect(screen.getAllByTestId("fleet-card-autonomy")).toHaveLength(2);
    expect(screen.getByText("Between Medium and Low, by instruction")).toBeTruthy();
    expect(screen.getByText("Human approval: Between Medium and Low, by instruction")).toBeTruthy();
    expect(screen.getByText("No gated actions")).toBeTruthy();
    expect(screen.getByText("Human approval: No gated actions")).toBeTruthy();
    // Never the stored level on a fleet that gates nothing.
    expect(screen.queryByText("Full human gates")).toBeNull();
  });

  it("offers the every-fleet pause to administrators only", async () => {
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve(path === "/fleets" ? [fleet] : []),
    );
    renderWithProviders(
      <MemoryRouter>
        <FleetsTab />
      </MemoryRouter>,
    );
    await screen.findByText("Research fleet");
    // useAuth is mocked with isAdmin false.
    expect(screen.queryByRole("button", { name: "Pause every fleet automation" })).toBeNull();
  });

  it("keeps the Properties and Filters pills in one group that wraps whole", async () => {
    vi.mocked(api.get).mockImplementation((path: string) =>
      Promise.resolve(path === "/fleets" ? [fleet] : []),
    );
    renderWithProviders(
      <MemoryRouter>
        <FleetsTab />
      </MemoryRouter>,
    );

    const filters = await screen.findByRole("button", { name: /Filters/ });
    const group = filters.closest<HTMLElement>("[data-fleet-filter-pills]");
    expect(group).not.toBeNull();
    const pills = within(group as HTMLElement);
    expect(pills.getByRole("button", { name: /Properties/ })).toBeTruthy();
    expect(pills.getByRole("button", { name: /Filters/ })).toBeTruthy();
  });
});
