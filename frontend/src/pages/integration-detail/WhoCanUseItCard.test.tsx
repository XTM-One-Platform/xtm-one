// "Who can use it" on an integration: each agent once, one total at the top,
// and an agent whose owner lost access flagged with an unbind.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { Agent, Integration, ResourceAudience, ResourceExposure, ResourceExposurePath } from "@/types";
import { WhoCanUseItCard } from "./WhoCanUseItCard";
import { agentAccessRows, bindingsWithout } from "./who-can-use-it";

const exposure = vi.hoisted(() => ({ data: undefined as unknown }));

vi.mock("@/hooks/api/useExposureApi", () => ({
  useResourceExposure: () => ({ data: exposure.data, isLoading: false, isError: false }),
}));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u-admin" }, isAdmin: true }),
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const render = (ui: ReactElement) => renderWithProviders(<MemoryRouter>{ui}</MemoryRouter>);

const audience = (overrides: Partial<ResourceAudience> = {}): ResourceAudience => ({
  everyone: false,
  people_count: 0,
  people: [],
  groups: [],
  public_link: false,
  anonymous_channels: [],
  mcp_key_holders: false,
  a2a_partners: false,
  webhook_callers: false,
  ...overrides,
});

const agent = (id: string, name: string, overrides: Partial<Agent> = {}) =>
  ({
    id,
    name,
    user_id: `owner-${id}`,
    owner_name: `Owner of ${name}`,
    enabled: true,
    disable_chat: false,
    web_enabled: false,
    channel_config: null,
    assignment_count: 2,
    integration_bindings: { hibob: ["i-1"] },
    can_manage: true,
    ...overrides,
  }) as unknown as Agent;

const agentPath = (id: string, name: string, overrides: Partial<ResourceExposurePath> = {}): ResourceExposurePath => ({
  kind: "agent",
  reasons: ["binding"],
  restricted: false,
  entity: { type: "agent", id, name, visible: true, is_company_managed: false, owner_name: `Owner of ${name}` },
  parent: null,
  usable: true,
  orphaned: false,
  notes: [],
  audience: audience({ people_count: 1, people: [{ id: "p", name: "Pat" }] }),
  lent: audience({ people_count: 1, people: [{ id: "p", name: "Pat" }] }),
  ...overrides,
});

const groupPath: ResourceExposurePath = {
  kind: "group",
  reasons: [],
  restricted: false,
  entity: null,
  parent: null,
  usable: true,
  notes: [],
  audience: audience({ people_count: 4, groups: [{ id: "g", name: "Sensitive Data", member_count: 4 }] }),
};

const integration = { id: "i-1", name: "HiBob", provider: "hibob", can_manage: true } as unknown as Integration;

const answer = (paths: ResourceExposurePath[]): ResourceExposure =>
  ({
    resource: { kind: "integration", id: "i-1", name: "HiBob", provider: "hibob", auth_type: "api_key", is_company_managed: false, owner_id: "u-admin", owner_name: "Admin" },
    secret: { kind: "api_key", holder_id: "u-admin", holder_name: "Admin", is_company: false, account_shared: true, scopes: [] },
    direct: audience({ people_count: 4 }),
    paths,
    reached: audience({ people_count: 5 }),
    truncated: false,
  }) as unknown as ResourceExposure;

describe("agentAccessRows", () => {
  it("lists an agent the answer names and binds once", () => {
    const rows = agentAccessRows([agentPath("a-1", "Investor reporting")], [agent("a-1", "Investor reporting"), agent("a-2", "Dave Assistant")]);
    expect(rows.map((r) => r.key)).toEqual(["a-1", "a-2"]);
  });

  it("keeps the orphan flag of the path", () => {
    const rows = agentAccessRows([agentPath("a-1", "Dave Assistant", { orphaned: true })], [agent("a-1", "Dave Assistant")]);
    expect(rows[0].orphaned).toBe(true);
  });
});

describe("bindingsWithout", () => {
  it("drops the integration and an emptied provider", () => {
    const a = agent("a-1", "X", { integration_bindings: { hibob: ["i-1"], github: ["i-9"] } });
    expect(bindingsWithout(a, "i-1")).toEqual({ github: ["i-9"] });
  });
});

describe("WhoCanUseItCard", () => {
  it("shows each agent once under one total, with no second agents card", () => {
    exposure.data = answer([groupPath, agentPath("a-1", "Investor reporting"), agentPath("a-2", "Dave Assistant")]);
    render(
      <WhoCanUseItCard integration={integration} agents={[agent("a-1", "Investor reporting"), agent("a-2", "Dave Assistant")]} />,
    );

    expect(screen.getAllByTestId("who-can-use-it-agent")).toHaveLength(2);
    expect(screen.getAllByTestId("who-can-use-it-total")).toHaveLength(1);
    expect(screen.getByText("Group Sensitive Data (4 members)")).toBeTruthy();
    expect(screen.queryByText(/Agents Using This/)).toBeNull();
    expect(screen.queryByText("bound")).toBeNull();
    expect(screen.getAllByText("Runs with it: chats, 2 assignments.")).toHaveLength(2);
  });

  it("flags an agent whose owner lost access and offers to unbind it", () => {
    exposure.data = answer([agentPath("a-2", "Dave Assistant", { orphaned: true, usable: false, notes: ["owner_lost_access"] })]);
    render(<WhoCanUseItCard integration={integration} agents={[agent("a-2", "Dave Assistant")]} />);

    expect(screen.getByText("Owner no longer has access")).toBeTruthy();
    expect(screen.getByText("1 agent's owner no longer has access: its runs refuse this integration's tools.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Unbind" })).toBeTruthy();
    // Unbind sits beside the row's open control, never inside it.
    const open = screen.getByTestId("who-can-use-it-agent-open");
    expect(open.textContent).toContain("Dave Assistant");
    expect(open.textContent).not.toContain("Unbind");
  });

  it("opens the agent from the whole row", () => {
    exposure.data = answer([agentPath("a-1", "Investor reporting")]);
    render(<WhoCanUseItCard integration={integration} agents={[agent("a-1", "Investor reporting")]} />);
    const open = screen.getByTestId("who-can-use-it-agent-open");
    expect(open.tagName).toBe("BUTTON");
    expect(open.textContent).toContain("Runs with it");
  });

  it("reads the assignment count at 0 and 1", () => {
    exposure.data = answer([]);
    render(
      <WhoCanUseItCard integration={integration} agents={[agent("a-1", "A", { assignment_count: 0, disable_chat: true }), agent("a-2", "B", { assignment_count: 1, disable_chat: true })]} />,
    );
    expect(screen.getByText("Runs with it: no assignment.")).toBeTruthy();
    expect(screen.getByText("Runs with it: 1 assignment.")).toBeTruthy();
  });
});
