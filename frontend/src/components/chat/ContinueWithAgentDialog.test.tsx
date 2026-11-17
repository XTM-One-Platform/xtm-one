// Continuing a conversation with another agent, in place.

import type { RefObject } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatPanelHandle } from "@filigran/chatbot";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { AgentSwitchDivider } from "@/pages/chat/AgentSwitchDivider";
import { renderWithProviders } from "@/testing/providers";
import type { Agent } from "@/types";
import { ContinueWithAgentDialog } from "./ContinueWithAgentDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);

function agent(id: string, name: string, over: Partial<Agent> = {}): Agent {
  return { id, name, slug: id, icon: "Bot", enabled: true, disable_chat: false, is_company_managed: false, ...over } as Agent;
}

/** The panel showing the conversation, as far as the dialog uses it. */
function panelHandle() {
  const reloadConversation = vi.fn();
  const panel = { current: { reloadConversation } as unknown as ChatPanelHandle } as RefObject<ChatPanelHandle | null>;
  return { panel, reloadConversation };
}

async function continueWith(agentName: RegExp) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("combobox", { name: "Agent" }));
  await user.click(screen.getByRole("option", { name: agentName }));
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue([
    agent("a-video", "Video Bot"),
    agent("a-deck", "Deck Bot"),
    agent("a-off", "Disabled Bot", { enabled: false }),
    agent("a-runs", "Runs Only", { disable_chat: true }),
  ]);
  put.mockResolvedValue({ id: "c-1", agent_id: "a-deck" });
});

describe("ContinueWithAgentDialog", () => {
  it("offers the chat agents other than the current one, and continues the conversation in place", async () => {
    const user = userEvent.setup();
    const { panel, reloadConversation } = panelHandle();
    renderWithProviders(
      <ContinueWithAgentDialog conversationId="c-1" currentAgentId="a-video" open onClose={vi.fn()} panel={panel} />,
    );
    await user.click(await screen.findByRole("combobox", { name: "Agent" }));
    expect(screen.queryByRole("option", { name: /Video Bot/ })).toBeNull();
    expect(screen.queryByRole("option", { name: /Disabled Bot/ })).toBeNull();
    expect(screen.queryByRole("option", { name: /Runs Only/ })).toBeNull();
    await user.click(screen.getByRole("option", { name: /Deck Bot/ }));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/chat/conversations/c-1/agent", { agent_id: "a-deck" }));
    // The panel reads the conversation back: the new agent, and the note saying so.
    await waitFor(() => expect(reloadConversation).toHaveBeenCalledTimes(1));
  });

  it("leaves the panel as it is when the agent could not be saved", async () => {
    put.mockRejectedValue(new Error("Agent not found"));
    const { panel, reloadConversation } = panelHandle();
    const onClose = vi.fn();
    renderWithProviders(
      <ContinueWithAgentDialog conversationId="c-1" currentAgentId="a-video" open onClose={onClose} panel={panel} />,
    );
    await continueWith(/Deck Bot/);
    expect(await screen.findByText("The conversation could not be continued with this agent")).toBeTruthy();
    expect(reloadConversation).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("names the agent a deletion took from the conversation", async () => {
    renderWithProviders(
      <ContinueWithAgentDialog
        conversationId="c-1"
        currentAgentId={null}
        deletedAgentName="Video Bot"
        open
        onClose={vi.fn()}
        panel={panelHandle().panel}
      />,
    );
    expect(await screen.findByText("Its agent Video Bot was deleted.")).toBeTruthy();
  });
});

describe("AgentSwitchDivider", () => {
  it("says who continued the conversation, and that its agent was deleted", () => {
    renderWithProviders(
      <AgentSwitchDivider
        at="2026-10-29T10:00:00Z"
        note={{
          from_agent: { agent_id: "a-video", agent_name: "Video Bot", general_assistant: false, deleted: true },
          to_agent: { agent_id: "a-deck", agent_name: "Deck Bot", general_assistant: false, deleted: false },
          reason: "agent_deleted",
        }}
      />,
    );
    expect(screen.getByRole("note").textContent).toMatch(/^Video Bot was deleted - continued by Deck Bot on /);
  });

  it("names the General Assistant when it takes over", () => {
    renderWithProviders(
      <AgentSwitchDivider
        at="2026-10-29T10:00:00Z"
        note={{
          from_agent: null,
          to_agent: { agent_id: null, agent_name: null, general_assistant: true, deleted: false },
          reason: "continued",
        }}
      />,
    );
    expect(screen.getByRole("note").textContent).toMatch(/^Continued by General Assistant on /);
  });
});
