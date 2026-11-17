// The studio rail draws a cut turn as /chat does: ONE card in place of the cut
// reply (`useCutTurnCards`), never the raw interrupted reply with its tool
// list, and the resumed answer without its own "resumed" line (#10910, UX
// production retest of #11136 on the Presentation Studio rail).

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ChatMessageSlotContext, ChatMessage as PanelMessage } from "@filigran/chatbot";

import { StudioRail, type StudioSubject } from "@/components/studio/StudioRail";
import type { XtmChatPanelProps } from "@/components/chat-panel/XtmChatPanel";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessage } from "@/types";

function stored(id: string, sequence: number, fields: Partial<ChatMessage>): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    channel: "web",
    channel_message_id: null,
    sequence,
    created_at: "2026-11-11T12:30:00Z",
    ...fields,
  } as unknown as ChatMessage;
}

const CUT_TRANSCRIPT: ChatMessage[] = [
  stored("u-1", 1, { role: "user", content: "Rework slide 3" }),
  stored("a-1", 2, { content: "This turn was interrupted before it could finish.", interrupted: true }),
  stored("a-2", 3, {
    content: "Slide 3 is reworked.",
    resumed_from: { cause: "stop", resume: 1, of: 100, cut_at: null } as ChatMessage["resumed_from"],
  }),
];

let transcript: ChatMessage[] = CUT_TRANSCRIPT;
let working = false;
// The hooks' answers stay the same objects across renders, as the real ones do.
const RESUMING = { cause: "stop", resume: 1, of: 100, cut_at: null };
const NONE: never[] = [];
const STABLE = { onStreamEvent: vi.fn(), onTurnRunningChange: vi.fn(), mutate: vi.fn() };
const conversations = new Map<ChatMessage[], object>();
const conversationOf = (messages: ChatMessage[]) => {
  if (!conversations.has(messages)) {
    conversations.set(messages, {
      data: { id: "c-1", channel: "web", access: "owner", agent_id: "agent-1", messages, interrupted_turn: null },
      isError: false,
    });
  }
  return conversations.get(messages);
};

vi.mock("@/hooks/api/useChatApi", async (importActual) => ({
  ...(await importActual<typeof import("@/hooks/api/useChatApi")>()),
  useConversation: () => conversationOf(transcript),
  useStudioConversations: () => ({ data: NONE, isLoading: false }),
  useAwaitedWork: () => ({ data: NONE }),
  useDismissInterruptedTurn: () => STABLE,
}));
vi.mock("@/hooks/api/useAgentApi", async (importActual) => ({
  ...(await importActual<typeof import("@/hooks/api/useAgentApi")>()),
  useAgents: () => ({ data: NONE }),
}));
vi.mock("@/hooks/usePlatformConfig", async (importActual) => ({
  ...(await importActual<typeof import("@/hooks/usePlatformConfig")>()),
  useChatSendEnabled: () => true,
}));
vi.mock("@/hooks/useAiDisabled", async (importActual) => ({
  ...(await importActual<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));
vi.mock("@/pages/chat-panel/useTurnFrames", () => ({
  useTurnFrames: () => ({
    onStreamEvent: STABLE.onStreamEvent,
    onTurnRunningChange: STABLE.onTurnRunningChange,
    working,
    waitItems: NONE,
    resumingTurn: working ? RESUMING : null,
  }),
}));
vi.mock("@/components/chat/ContinueWithAgentDialog", () => ({ ContinueWithAgentDialog: () => null }));
// The panel itself: each stored message through the host's `replaceMessage`,
// as `@filigran/chatbot` draws its thread, then the host's footer.
vi.mock("@/components/chat-panel/LazyXtmChatPanel", () => ({
  LazyXtmChatPanel: ({ storedMessages, replaceMessage, resumedShownAbove, transcriptFooter }: XtmChatPanelProps) => (
    <div data-testid="panel-thread">
      {(storedMessages ?? []).map((m) => {
        const panelMessage = { id: `p-${m.id}`, serverId: m.id, role: m.role, content: m.content } as PanelMessage;
        const replaced = replaceMessage?.(panelMessage, {} as ChatMessageSlotContext);
        if (replaced === null) return null;
        if (replaced !== undefined) return <div key={m.id}>{replaced}</div>;
        return (
          <p key={m.id} data-testid="plain-message">
            {m.content}
            {m.resumed_from && !resumedShownAbove?.has(m.id) ? " (resumed line)" : ""}
          </p>
        );
      })}
      {transcriptFooter as React.ReactNode}
    </div>
  ),
}));

const subject = {
  kind: "presentation",
  field: "presentation_id",
  id: "deck-1",
  title: "Deck",
  toolAgents: { agents: [], none: false, resolving: false, defaultAgentId: "agent-1", generalAssistantOffered: true },
  sendContext: {},
  focus: { label: "Slide 3", tip: "Slide 3" },
  suggestions: [],
  testId: "presentation.studio",
  labels: {
    rail: "Presentation Studio",
    history: "Conversations",
    noAgentShort: "No agent",
    noAgentTitle: "No agent",
    blockedHint: "",
    unavailableHint: "",
  },
} as unknown as StudioSubject;

function renderRail() {
  return renderWithProviders(
    <MemoryRouter>
      <StudioRail subject={subject} initialConversationId="c-1" onConversationChange={vi.fn()} onCollapse={vi.fn()} />
    </MemoryRouter>,
  );
}

describe("StudioRail cut turns", () => {
  it("draws the cut reply as its card, not the raw interrupted reply, and the answer without its own resumed line", async () => {
    transcript = CUT_TRANSCRIPT;
    working = false;
    renderRail();

    const card = await screen.findByTestId("interrupted-reply-card");
    expect(card.getAttribute("data-state")).toBe("resumed");
    const plain = screen.getAllByTestId("plain-message").map((p) => p.textContent);
    expect(plain).toEqual(["Rework slide 3", "Slide 3 is reworked."]);
  });

  it("draws the card under the transcript, resuming, while the platform resumes the cut", async () => {
    transcript = CUT_TRANSCRIPT.slice(0, 2);
    working = true;
    renderRail();

    const cards = await screen.findAllByTestId("interrupted-reply-card");
    expect(cards).toHaveLength(1);
    expect(cards[0].getAttribute("data-state")).toBe("resuming");
    expect(screen.queryByText("This turn was interrupted before it could finish.")).toBeNull();
  });
});
