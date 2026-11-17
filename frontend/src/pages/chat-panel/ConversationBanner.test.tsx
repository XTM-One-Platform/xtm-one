// The handover banner: "Return" gives the conversation back to the agent that
// handed it over, and only once the server has done so is the panel told to
// read the conversation again, so its header names the agent now answering.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { ConversationBanner } from "@/pages/chat-panel/ConversationBanner";
import { renderWithProviders } from "@/testing/providers";
import type { ConversationDetail } from "@/types";

const returned = vi.hoisted(() => ({ succeed: true, calls: [] as string[] }));

vi.mock("@/hooks/api/useChatApi", () => ({
  useAwaitedWork: () => ({ data: [] }),
  useSharedConversations: () => ({ data: [] }),
  useReturnToSource: () => ({
    isPending: false,
    mutate: (id: string, options?: { onSuccess?: () => void }) => {
      returned.calls.push(id);
      if (returned.succeed) options?.onSuccess?.();
    },
  }),
}));
vi.mock("@/hooks/api/useAgentApi", () => ({ useAgents: () => ({ data: [] }), useSubAgentTasks: () => ({ data: [] }) }));

const handedOver = {
  id: "conv-1",
  transfer_source_agent_id: "agent-source",
  transfer_source_agent_name: "AEV Detection Rules Generator",
} as ConversationDetail;

function clickReturn(onReturned: () => void) {
  renderWithProviders(<ConversationBanner conversation={handedOver} agentId={null} working={false} aiOff={false} onReturned={onReturned} />);
  fireEvent.click(screen.getByRole("button", { name: "Return" }));
}

describe("ConversationBanner", () => {
  it("tells the panel to read the conversation again once it is back with the agent that handed it over", () => {
    returned.succeed = true;
    returned.calls = [];
    const onReturned = vi.fn();
    clickReturn(onReturned);
    expect(returned.calls).toEqual(["conv-1"]);
    expect(onReturned).toHaveBeenCalledTimes(1);
  });

  it("leaves the panel as it is when the return fails", () => {
    returned.succeed = false;
    const onReturned = vi.fn();
    clickReturn(onReturned);
    expect(onReturned).not.toHaveBeenCalled();
  });
});
