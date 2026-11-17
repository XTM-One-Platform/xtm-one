// What XTM One shows under a reply: an @mentioned agent that did not answer is
// read from the panel's own message (a reply the panel paged in itself has no
// stored copy on the page), the rest from the page's stored copy; the rating
// already given, from the panel's restored copy when the page has none.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ChatMessage as PanelMessage } from "@filigran/chatbot";

import { FOOTER_ACTIONS_ROW, MessageExtras, MessageFooterActions } from "@/components/chat-panel/MessageParts";
import { REVEALED_WITH_ANSWER } from "@/pages/chat/MessageFeedbackButtons";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessage } from "@/types";

vi.mock("@/hooks/api/useFeedbackCriteriaApi", () => ({
  useRatingCriteria: () => ({ criteria: [], resolveCriteria: async () => [] }),
}));

const post = vi.hoisted(() => vi.fn(async () => ({})));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, post } };
});

const reply = (overrides: Partial<PanelMessage> = {}): PanelMessage =>
  ({ id: "m-1", role: "assistant", content: "", timestamp: new Date(), ...overrides }) as PanelMessage;

function render(message: PanelMessage) {
  return renderWithProviders(
    <MemoryRouter>
      <MessageExtras message={message} stored={undefined} />
    </MemoryRouter>,
  );
}

describe("MessageExtras", () => {
  it("names the mentioned agent that did not answer, with no stored copy of the reply", () => {
    render(reply({ mentionFailed: true, answeredBy: { agentId: "a-1", agentName: "Triage" } }));
    expect(screen.getByText("Triage")).toBeTruthy();
    expect(screen.getByText(/Did not answer/)).toBeTruthy();
  });

  it("says nothing of an agent that answered", () => {
    render(reply({ answeredBy: { agentId: "a-1", agentName: "Triage" } }));
    expect(screen.queryByText("Triage")).toBeNull();
  });
});

describe("MessageFooterActions", () => {
  const footer = (message: PanelMessage) =>
    renderWithProviders(
      <MemoryRouter>
        <MessageFooterActions message={message} stored={undefined} conversationId="c-1" isAdmin={false} canRate />
      </MemoryRouter>,
    );

  it("shows both thumbs on an answer the backend identified", () => {
    footer(reply({ serverId: "s-1" }));
    for (const label of ["Thumbs up", "Thumbs down"]) {
      expect(screen.getByRole("button", { name: label, pressed: false })).toBeTruthy();
    }
  });

  it("reveals the thumbs with the panel's answer, the named group group/msg", () => {
    // An unnamed `group-hover` never matches inside the panel's answer and
    // left the thumbs invisible beside copy and read aloud.
    expect(REVEALED_WITH_ANSWER).toContain("group-hover/msg:opacity-100");
    expect(REVEALED_WITH_ANSWER).toContain("group-focus-within/msg:opacity-100");
    expect(REVEALED_WITH_ANSWER).not.toMatch(/(^|\s)group-hover:/);
  });

  it("spaces the thumbs with the panel's action row gap, no padding of their own", () => {
    // The panel spaces copy and read aloud with gap-0.5; a wider gap or a
    // padding here split the row into two spacings.
    expect(FOOTER_ACTIONS_ROW.split(" ")).toContain("gap-0.5");
    expect(FOOTER_ACTIONS_ROW).not.toMatch(/(^|\s)(gap-(?!0\.5\b)\S+|p[xy]?-\S+)/);
  });

  it("posts a rating where the chat stores it and shows it pressed", async () => {
    post.mockClear();
    footer(reply({ serverId: "s-1" }));
    fireEvent.click(screen.getByLabelText("Thumbs up"));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/chat/conversations/c-1/messages/s-1/feedback", {
        rating: "positive",
        comment: null,
      }),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Thumbs up", pressed: true })).toBeTruthy());
  });

  it("shows the rating stored before a reload, locked, as the panel restored it", () => {
    footer(reply({ serverId: "s-1", feedback: { rating: "negative", comment: null } }));
    const down = screen.getByRole("button", { name: "Thumbs down", pressed: true });
    expect(down.getAttribute("aria-disabled")).toBe("true");
    expect(screen.queryByLabelText("Thumbs up")).toBeNull();
    post.mockClear();
    fireEvent.click(down);
    expect(post).not.toHaveBeenCalled();
  });

  it("offers no rating on a change-of-agent note: no one's answer", () => {
    const { container } = renderWithProviders(
      <MemoryRouter>
        <MessageFooterActions
          message={reply({ serverId: "s-switch" })}
          stored={
            {
              id: "s-switch",
              role: "assistant",
              content: "Continued by Deck Bot on 2026-10-29.",
              created_at: "2026-10-29T10:00:00Z",
              agent_switch: { from_agent: null, to_agent: null, reason: "continued" },
            } as ChatMessage
          }
          conversationId="c-1"
          isAdmin
          canRate
        />
      </MemoryRouter>,
    );
    expect(container.textContent).toBe("");
    expect(screen.queryByLabelText("Thumbs up")).toBeNull();
  });
});
