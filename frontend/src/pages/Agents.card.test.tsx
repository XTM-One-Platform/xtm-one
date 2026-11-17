// An agent card shows the whole agent name: it wraps instead of cutting at one
// line, and carries the full name for hover even when a very long one clamps.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { Agent } from "@/types";
import { AgentCard } from "./Agents";

vi.mock("@/hooks/api/useAgentApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAgentApi")>()),
  useUpdateAgent: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

const NAME = "OpenCTI Dashboard Widget Creator";

function agent(): Agent {
  return {
    id: "a1",
    name: NAME,
    description: "Builds dashboard widgets",
    icon: "Bot",
    enabled: true,
    disable_chat: false,
    user_id: "me",
    tags: [],
    integration_bindings: {},
    mcp_server_ids: [],
    knowledge_base_ids: [],
    custom_tool_ids: [],
    assignment_count: 0,
  } as unknown as Agent;
}

describe("AgentCard name", () => {
  it("carries the full agent name as its title and inside the card link", () => {
    renderWithProviders(
      <MemoryRouter>
        <AgentCard agent={agent()} isAdmin userId="me" isFavorite={false} onToggleFavorite={() => {}} />
      </MemoryRouter>,
    );
    const name = screen.getByTestId("agent-card-name");
    expect(name.textContent).toBe(NAME);
    expect(name.getAttribute("title")).toBe(NAME);
    expect(name.closest("a")?.getAttribute("href")).toBe("/agents/a1");
  });
});
