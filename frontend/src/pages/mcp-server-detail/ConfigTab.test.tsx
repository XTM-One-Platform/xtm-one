// MCP server Configuration > Edit says what is missing on Save, as the create
// form does (UX round 1 of #9991, B1): an empty name or an empty URL on a
// remote transport shows the inline error, marks the field invalid and sends
// nothing. The Transport picker reads the create form's translated labels (B2).

import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { MCPServer } from "@/types";
import { ConfigTab } from "./ConfigTab";

const mutate = vi.fn();
const authorizeMutate = vi.fn();
const disconnectMutate = vi.fn();

vi.mock("@/hooks/api/useMCPServerApi", () => ({
  useUpdateMCPServer: () => ({ mutate, isPending: false }),
  useRefreshMCPTools: () => ({ mutate: vi.fn(), isPending: false }),
  useTestMCPTool: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useAuthorizeMCPServer: () => ({ mutate: authorizeMutate, isPending: false }),
  useDisconnectMyMCPCredential: () => ({ mutate: disconnectMutate, isPending: false }),
}));
vi.mock("@/hooks/useUserShareGrants", () => ({
  useUserShareGrants: () => ({ reset: () => undefined, toPayload: () => ({}), props: undefined }),
}));
vi.mock("@/hooks/useCredentialSharingConfirm", () => ({
  useCredentialSharingConfirm: () => [null, async () => true],
}));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => false }));
vi.mock("@/components/ui/relay-route-field", () => ({ RelayRouteField: () => null }));
vi.mock("@/components/ui/tool-test-dialog", () => ({ ToolTestDialog: () => null }));
// The card's own chrome (owner, sharing, Save row) is out of scope: render the
// edit view with a Save button that calls the page's handler.
vi.mock("@/components/ui/entity-config-card", () => ({
  EntityConfigCard: ({ editView, onSave }: { editView: ReactNode; onSave: () => void }) => (
    <div>
      {editView}
      <button type="button" onClick={onSave}>Save changes</button>
    </div>
  ),
}));

function server(overrides: Partial<MCPServer> = {}): MCPServer {
  return {
    id: "srv-1",
    name: "Remote server",
    transport: "streamable_http",
    url: "https://mcp.example.com/mcp",
    auth_mode: "headers",
    tags: [],
    tools: [],
    args: [],
    env: {},
    headers: {},
    hide_from_chat: false,
    is_company_managed: false,
    is_public_catalog: false,
    shared_group_ids: null,
    shared_group_permissions: null,
    ...overrides,
  } as unknown as MCPServer;
}

function renderEdit(srv: MCPServer) {
  renderWithProviders(<ConfigTab srv={srv} canManage />);
}

describe("MCP server Configuration edit: required fields", () => {
  it("shows the create form's inline error on Save with an empty name, and sends nothing", () => {
    mutate.mockClear();
    renderEdit(server());
    const name = screen.getByRole("textbox", { name: /^Server Name/ });
    fireEvent.change(name, { target: { value: "  " } });
    const save = screen.getByRole("button", { name: "Save changes" });
    expect((save as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(save);
    expect(screen.getByRole("textbox", { name: /^Server Name/, description: "Enter a name for this server." })).toBe(name);
    expect(name.getAttribute("aria-invalid")).toBe("true");
    expect(mutate).not.toHaveBeenCalled();
  });

  it("shows an inline error on Save with an empty URL on a remote transport, and sends nothing", async () => {
    mutate.mockClear();
    renderEdit(server());
    const url = screen.getByRole("textbox", { name: /^Server URL/ });
    fireEvent.change(url, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByRole("textbox", { name: /^Server URL/, description: "Enter the URL this platform connects to." })).toBe(url);
    expect(url.getAttribute("aria-invalid")).toBe("true");
    expect(mutate).not.toHaveBeenCalled();
  });

  it("clears the name error as soon as a name is typed", () => {
    renderEdit(server());
    const name = screen.getByRole("textbox", { name: /^Server Name/ });
    fireEvent.change(name, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    fireEvent.change(name, { target: { value: "Renamed" } });
    expect(name.getAttribute("aria-invalid")).toBeNull();
    expect(screen.queryByText("Enter a name for this server.")).toBeNull();
  });
});

describe("MCP server OAuth: each person's own account (#443)", () => {
  const perUser = (overrides: Partial<MCPServer> = {}) =>
    server({ auth_mode: "oauth", credential_mode: "per_user", oauth_status: "disconnected", is_company_managed: true, ...overrides });

  it("offers a reader who cannot manage the server to connect their own account", () => {
    authorizeMutate.mockClear();
    renderWithProviders(<ConfigTab srv={perUser({ my_oauth_status: "disconnected" })} canManage={false} />);
    expect(screen.getByText("Connect your account to use this server")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Connect your account" }));
    expect(authorizeMutate).toHaveBeenCalledWith("srv-1", expect.anything());
    expect(screen.queryByRole("button", { name: "Disconnect your account" })).toBeNull();
  });

  it("reads the reader's own connection, never the server row's, and lets them disconnect", () => {
    disconnectMutate.mockClear();
    renderWithProviders(<ConfigTab srv={perUser({ oauth_status: "disconnected", my_oauth_status: "connected" })} canManage={false} />);
    expect(screen.getByText("Your account is connected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Disconnect your account" }));
    expect(disconnectMutate).toHaveBeenCalledWith("srv-1", expect.anything());
  });

  it("offers the credential mode on an OAuth server and warns what saving discards", () => {
    mutate.mockClear();
    renderEdit(server({ auth_mode: "oauth", credential_mode: "shared", oauth_status: "connected" }));
    const perUserButton = screen.getByRole("button", { name: "Each person's own account" });
    expect(perUserButton.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(perUserButton);
    expect(perUserButton.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("Saving discards the shared connection: each person then connects their own account.")).toBeTruthy();
  });
});

describe("MCP server Configuration edit: Transport picker", () => {
  it("reads the create form's translated labels", () => {
    renderEdit(server());
    expect(screen.getByRole("button", { name: "Subprocess (stdio)" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Streamable HTTP" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "SSE (legacy)" })).toBeTruthy();
  });
});
