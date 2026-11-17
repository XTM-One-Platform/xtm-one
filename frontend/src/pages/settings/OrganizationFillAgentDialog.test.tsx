// "From a connected integration" on Settings > Organization (#11888, slice C PR 2b): pick a connected
// integration, optionally the daily sync, then the chat with the fill agent opens on the preview prompt.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { chatDraftStorageKey } from "@/lib/chat-launch";
import { CHAT_PENDING_AGENT_STORAGE_KEY } from "@/lib/constants";
import { renderWithProviders } from "@/testing/providers";
import { OrganizationFillAgentDialog } from "./OrganizationFillAgentDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(), postForm: vi.fn() },
}));

const navigate = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigate,
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const integration = (id: string, name: string, status: string) => ({ id, name, provider: "intune", status, auth_type: "oauth" });

function open(onOpenChange = vi.fn()) {
  renderWithProviders(<OrganizationFillAgentDialog open onOpenChange={onOpenChange} />);
  return onOpenChange;
}

async function pick(name: string) {
  const field = await screen.findByRole("combobox");
  await waitFor(() => expect((field as HTMLInputElement).disabled).toBe(false));
  fireEvent.click(field);
  fireEvent.click(await screen.findByRole("option", { name }));
}

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  get.mockImplementation(async (path: string) => {
    if (path.startsWith("/integrations")) return [integration("i-1", "Intune", "connected"), integration("i-2", "Broken", "error")];
    throw new Error(`unexpected GET ${path}`);
  });
});

describe("OrganizationFillAgentDialog (#11888)", () => {
  it("offers only the integrations an agent can read and keeps Open the chat disabled until one is picked", async () => {
    open();
    const field = await screen.findByRole("combobox");
    await waitFor(() => expect((field as HTMLInputElement).disabled).toBe(false));
    expect((screen.getByRole("button", { name: "Open the chat" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(field);
    expect(await screen.findByRole("option", { name: "Intune" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Broken" })).toBeNull();
  });

  it("creates the fill agent, primes the chat with its preview prompt and opens it", async () => {
    post.mockResolvedValue({ agent_id: "a-1", agent_name: "Organization assets from Intune", created: true, assignment_id: null, prompt: "Preview them" });
    const onOpenChange = open();
    await pick("Intune");
    fireEvent.click(screen.getByRole("button", { name: "Open the chat" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/chat"));
    expect(post).toHaveBeenCalledWith("/organization-context/fill-agent", expect.objectContaining({ integration_id: "i-1", schedule: false }));
    expect(sessionStorage.getItem(chatDraftStorageKey(null))).toBe("Preview them");
    expect(sessionStorage.getItem(CHAT_PENDING_AGENT_STORAGE_KEY)).toBe("a-1");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("refetches the agents list so the chat finds the new fill agent (UX round 1, B1)", async () => {
    post.mockResolvedValue({ agent_id: "a-1", agent_name: "Organization assets from Intune", created: true, assignment_id: null, prompt: "Preview them" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // The chat's cached list, made before the fill agent existed; its next fetch carries it.
    client.setQueryDefaults(["agents"], { queryFn: async () => [{ id: "a-1" }] });
    client.setQueryData(["agents", "mine"], []);
    let agentsAtNavigation: unknown;
    navigate.mockImplementationOnce(() => {
      agentsAtNavigation = client.getQueryData(["agents", "mine"]);
    });
    renderWithProviders(<OrganizationFillAgentDialog open onOpenChange={vi.fn()} />, client);
    await pick("Intune");
    fireEvent.click(screen.getByRole("button", { name: "Open the chat" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/chat"));
    expect(agentsAtNavigation).toEqual([{ id: "a-1" }]);
  });

  it("marks the integration as required and says what the daily sync does under its switch", async () => {
    open();
    expect(await screen.findByLabelText("Required")).toBeTruthy();
    expect(screen.getByText("The agent applies the changes every day at 06:00, rows written by hand kept.")).toBeTruthy();
  });

  it("asks for the daily sync when the switch is on", async () => {
    post.mockResolvedValue({ agent_id: "a-1", agent_name: "x", created: false, assignment_id: "s-1", prompt: "Preview them" });
    open();
    await pick("Intune");
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(screen.getByRole("button", { name: "Open the chat" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/organization-context/fill-agent", expect.objectContaining({ schedule: true })));
  });

  it("says a failure inline and stays open", async () => {
    post.mockRejectedValue(new Error("Integration not found."));
    const onOpenChange = open();
    await pick("Intune");
    fireEvent.click(screen.getByRole("button", { name: "Open the chat" }));
    expect((await screen.findByRole("alert")).textContent).toBe("The agent could not be set up. Try again.");
    expect(navigate).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
