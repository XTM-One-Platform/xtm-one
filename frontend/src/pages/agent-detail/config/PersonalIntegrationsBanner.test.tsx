import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import type { Integration } from "@/types";
import { PersonalIntegrationsBanner } from "./PersonalIntegrationsBanner";
import { lendsAll, unsharedPersonalIntegrations } from "./personal-integrations";

const OWNER = "owner-1";
const GROUP = "group-1";

function integration(id: string, overrides: Partial<Integration> = {}): Integration {
  return {
    id,
    user_id: OWNER,
    owner_name: "Owner",
    provider: "custom_openapi",
    name: `Integration ${id}`,
    is_company_managed: false,
    shared_group_ids: null,
    ...overrides,
  } as Integration;
}

const bound = { custom_openapi: ["i-1", "i-2"] };
const integrations = [integration("i-1"), integration("i-2")];

function agent(lending: { integration_ids: string[]; set_by: string; set_at: string } | null = null) {
  return { user_id: OWNER, owner_name: "Owner", integration_bindings: bound, integration_lending: lending };
}

describe("unsharedPersonalIntegrations", () => {
  it("keeps the bound personal integrations shared with none of the agent's groups", () => {
    const list = [
      integration("i-1"),
      integration("i-2", { shared_group_ids: [GROUP] }),
      integration("i-3", { is_company_managed: true }),
      integration("i-4"),
    ];
    const ids = unsharedPersonalIntegrations({ p: ["i-1", "i-2", "i-3"] }, list, [GROUP]).map((i) => i.id);
    expect(ids).toEqual(["i-1"]);
  });

  it("keeps the ones the people an agent is shared with, without any group, do not have", () => {
    const list = [integration("i-1"), integration("i-2", { shared_user_ids: ["user-2"] })];
    const ids = unsharedPersonalIntegrations({ p: ["i-1", "i-2"] }, list, null, ["user-2"]).map((i) => i.id);
    expect(ids).toEqual(["i-1"]);
  });

  it("keeps a row only some of the agent's groups and people hold", () => {
    const list = [
      integration("i-1", { shared_user_ids: ["alice"] }),
      integration("i-2", { shared_group_ids: [GROUP] }),
      integration("i-3", { shared_group_ids: [GROUP], shared_user_ids: ["alice", "bob"] }),
    ];
    const ids = unsharedPersonalIntegrations({ p: ["i-1", "i-2", "i-3"] }, list, [GROUP], ["alice", "bob"]).map(
      (i) => i.id,
    );
    expect(ids).toEqual(["i-1", "i-2"]);
  });

  it("is empty on an agent shared with nobody", () => {
    expect(unsharedPersonalIntegrations(bound, integrations, [], null)).toEqual([]);
  });
});

describe("lendsAll", () => {
  it("is true only when every personal integration is lent", () => {
    const lending = { integration_ids: ["i-1"], set_by: OWNER, set_at: "2026-11-05T10:00:00Z" };
    expect(lendsAll(lending, ["i-1"])).toBe(true);
    // A newly bound integration brings the full banner back.
    expect(lendsAll(lending, ["i-1", "i-2"])).toBe(false);
    expect(lendsAll(null, ["i-1"])).toBe(false);
  });
});

describe("PersonalIntegrationsBanner", () => {
  it("lets the owner use their integrations through the agent", () => {
    const onLend = vi.fn();
    renderWithProviders(
      <PersonalIntegrationsBanner agent={agent()} integrations={integrations} sharedGroupIds={[GROUP]} currentUserId={OWNER} onLend={onLend} />,
    );
    expect(screen.getByText("Personal integrations on a shared agent")).toBeTruthy();
    expect(screen.queryByText(/recommended/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Use my integrations through this agent" }));
    expect(onLend).toHaveBeenCalledWith(["i-1", "i-2"]);
  });

  it("collapses to one line once lent, and Review reopens every choice", () => {
    const onLend = vi.fn();
    const lending = { integration_ids: ["i-1", "i-2"], set_by: OWNER, set_at: "2026-11-05T10:00:00Z" };
    renderWithProviders(
      <PersonalIntegrationsBanner agent={agent(lending)} integrations={integrations} sharedGroupIds={[GROUP]} currentUserId={OWNER} onLend={onLend} />,
    );
    expect(screen.queryByText("Personal integrations on a shared agent")).toBeNull();
    expect(screen.getByText(/Personal integrations used through this agent: 2, set by Owner on/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(screen.getByText("Personal integrations on a shared agent")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stop using my integrations through this agent" }));
    expect(onLend).toHaveBeenCalledWith([]);
  });

  it("offers the owner the lending on an agent shared with people only", () => {
    const onLend = vi.fn();
    renderWithProviders(
      <PersonalIntegrationsBanner
        agent={agent()}
        integrations={integrations}
        sharedGroupIds={null}
        sharedUserIds={["user-2"]}
        currentUserId={OWNER}
        onLend={onLend}
      />,
    );
    expect(screen.getByText("Personal integrations on a shared agent")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use my integrations through this agent" }));
    expect(onLend).toHaveBeenCalledWith(["i-1", "i-2"]);
  });

  it("offers the lending to the owner only", () => {
    renderWithProviders(
      <PersonalIntegrationsBanner agent={agent()} integrations={integrations} sharedGroupIds={[GROUP]} currentUserId="editor-2" onLend={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Use my integrations through this agent" })).toBeNull();
    expect(screen.getByText("Only the agent's owner can choose to use their integrations through it.")).toBeTruthy();
  });

  it("shows an editor the agent's state without reading the owner's rows", () => {
    const fromAgent = [
      { id: "i-1", name: "Integration i-1", provider: "custom_openapi" },
      { id: "i-2", name: "Integration i-2", provider: "custom_openapi" },
    ];
    const notLent = { ...agent(), shared_personal_integrations: fromAgent };
    const { unmount } = renderWithProviders(
      <PersonalIntegrationsBanner agent={notLent} integrations={[]} sharedGroupIds={[GROUP]} currentUserId="editor-2" onLend={vi.fn()} />,
    );
    expect(screen.getByText("Personal integrations on a shared agent")).toBeTruthy();
    expect(screen.getByText("Only the agent's owner can choose to use their integrations through it.")).toBeTruthy();
    unmount();

    const lending = { integration_ids: ["i-1", "i-2"], set_by: OWNER, set_at: "2026-11-05T10:00:00Z" };
    renderWithProviders(
      <PersonalIntegrationsBanner
        agent={{ ...agent(lending), shared_personal_integrations: fromAgent }}
        integrations={[]}
        sharedGroupIds={[GROUP]}
        currentUserId="editor-2"
      />,
    );
    expect(screen.getByText(/Personal integrations used through this agent: 2, set by Owner on/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
  });

  it("writes the date in the app language", () => {
    const lending = { integration_ids: ["i-1", "i-2"], set_by: OWNER, set_at: "2026-11-05T10:00:00Z" };
    renderWithProviders(
      <PersonalIntegrationsBanner agent={agent(lending)} integrations={integrations} sharedGroupIds={[GROUP]} currentUserId={OWNER} />,
      { locale: "fr" },
    );
    expect(screen.getByTestId("personal-integrations-lent").textContent).toContain("5 nov.");
  });

  it("renders nothing without a personal integration to explain", () => {
    const { container } = renderWithProviders(
      <PersonalIntegrationsBanner agent={agent()} integrations={integrations} sharedGroupIds={null} currentUserId={OWNER} />,
    );
    expect(container.textContent).toBe("");
  });
});
