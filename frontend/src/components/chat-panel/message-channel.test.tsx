// A message of a continued Slack / Teams thread carries its channel, as the
// chat page always showed it: an icon named on hover and keyboard focus.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ChatMessage as PanelMessage } from "@filigran/chatbot";

import { MessageExtras } from "@/components/chat-panel/MessageParts";
import { CHANNEL_TYPE_LABELS } from "@/lib/constants";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessage } from "@/types";

function stored(role: "user" | "assistant", channel: string): ChatMessage {
  return { id: "m-1", role, content: "hello", channel, sequence: 1, created_at: "2026-11-03T10:00:00Z" } as unknown as ChatMessage;
}

function renderExtras(role: "user" | "assistant", channel: string) {
  const message = { id: "p-1", serverId: "m-1", role, content: "hello", timestamp: new Date() } as PanelMessage;
  return renderWithProviders(
    <MemoryRouter>
      <MessageExtras message={message} stored={stored(role, channel)} />
    </MemoryRouter>,
  );
}

describe("a message's channel", () => {
  it("names the channel a user message was typed in", () => {
    renderExtras("user", "slack");
    expect(screen.getByRole("img", { name: `In ${CHANNEL_TYPE_LABELS.slack}` })).toBeTruthy();
  });

  it("names the channel a reply was written in", () => {
    renderExtras("assistant", "teams");
    expect(screen.getByRole("img", { name: `In ${CHANNEL_TYPE_LABELS.teams ?? "teams"}` })).toBeTruthy();
  });

  it("says nothing of the web chat's own messages", () => {
    renderExtras("user", "web");
    expect(screen.queryByRole("img", { name: /^In / })).toBeNull();
  });
});
