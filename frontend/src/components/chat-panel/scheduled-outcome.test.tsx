// A scheduled one-off's outcome (#11950): the web chat draws the run it reports
// on as its own "View the run" action under the message, opening the run in the
// app, and shows the text without the trailing link the stored message keeps
// (absolute, for every surface outside the web app). A scheduled turn carries
// no wake marker (#11906 must not regress).

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ChatMessage as PanelMessage } from "@filigran/chatbot";

import { MessageExtras } from "@/components/chat-panel/MessageParts";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessage } from "@/types";

const REPLY = {
  id: "m-1",
  serverId: "a-1",
  role: "assistant",
  content: "**Done**: sent at 07:00.",
  timestamp: new Date(),
} as PanelMessage;

function stored(scheduled_outcome: ChatMessage["scheduled_outcome"]): ChatMessage {
  return {
    id: "a-1",
    role: "assistant",
    content: "**Done**: sent at 07:00.\n\n[View the run](https://one.test/runs/r-1)",
    channel: "web",
    channel_message_id: null,
    sequence: 4,
    created_at: "2026-11-15T06:00:00Z",
    llm_trace_id: null,
    scheduled_outcome,
  } as ChatMessage;
}

describe("a scheduled outcome in the web chat", () => {
  it("shows the run as the chat's own action, opening it in the app, and no wake marker", () => {
    renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={REPLY} stored={stored({ work_item_id: "w-1", run_id: "r-1", state: "done" })} />
      </MemoryRouter>,
    );
    const action = screen.getByTestId("scheduled-outcome-run");
    expect(action.textContent).toBe("View the run");
    expect(action.getAttribute("href")).toBe("/operations/executions/r-1");
    expect(action.getAttribute("target")).toBeNull();
    expect(screen.queryByTestId("wake-turn-marker")).toBeNull();
  });

  it("shows no action for a message that is not a scheduled outcome, or one without a run", () => {
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={REPLY} stored={stored(null)} />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("scheduled-outcome-run")).toBeNull();
    unmount();

    renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={REPLY} stored={stored({ work_item_id: "w-1", run_id: null, state: "missed" })} />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("scheduled-outcome-run")).toBeNull();
  });
});
