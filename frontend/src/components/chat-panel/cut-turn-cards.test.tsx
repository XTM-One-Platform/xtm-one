// A cut turn and the replies that resumed it, on the panel: one card on the
// first interrupted reply, the later cuts left out, and the resumed reply's own
// line dropped since the card says it.

import { describe, expect, it, vi } from "vitest";
import { act, renderHook, screen } from "@testing-library/react";
import type { ChatMessage as PanelMessage } from "@filigran/chatbot";

import { SETTLED_CARD_HOLD_MS, useCutTurnCards } from "@/components/chat-panel/cut-turn-cards";
import { renderWithProviders, testQueryClient, withProviders } from "@/testing/providers";
import type { ChatMessage, InterruptedTurn } from "@/types";

function stored(id: string, sequence: number, fields: Partial<ChatMessage>): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    channel: "web",
    channel_message_id: null,
    sequence,
    created_at: "2026-11-02T12:30:00Z",
    ...fields,
  } as unknown as ChatMessage;
}

const cut = (id: string, sequence: number) =>
  stored(id, sequence, { content: "This turn was interrupted before it could finish.", interrupted: true });
const question = stored("u-1", 1, { role: "user", content: "Hunt the campaign" });
const panel = (serverId: string) => ({ id: `p-${serverId}`, serverId, role: "assistant", content: "" }) as PanelMessage;

function cards(messages: ChatMessage[], options: Partial<Parameters<typeof useCutTurnCards>[1]> = {}) {
  return renderHook(
    () => useCutTurnCards(messages, { resumingTurn: null, interruptedTurn: null, working: false, ...options }),
    { wrapper: withProviders(testQueryClient()) },
  ).result.current;
}

describe("useCutTurnCards", () => {
  it("draws one card for a cut, its later cut and the reply that resumed it", () => {
    const resumed = stored("a-3", 4, {
      content: "Done.",
      resumed_from: { cause: "stop", resume: 2, of: 20 } as ChatMessage["resumed_from"],
    });
    const result = cards([question, cut("a-1", 2), cut("a-2", 3), resumed]);

    expect(result.replaceMessage(panel("a-1"))).not.toBeNull();
    expect(result.replaceMessage(panel("a-1"))).not.toBeUndefined();
    expect(result.replaceMessage(panel("a-2")), "a later cut of the same turn is left out").toBeNull();
    expect(result.replaceMessage(panel("a-3")), "the resumed reply is drawn by the panel").toBeUndefined();
    expect(result.replaceMessage(panel("u-1"))).toBeUndefined();
    expect([...result.resumedIds]).toEqual(["a-3"]);
    expect(result.endsWithCut).toBe(false);
  });

  it("never draws a reply the panel knows was cut as a plain reply, whichever of the history and the stored transcript comes first", () => {
    const cutPanel = { ...panel("a-1"), interrupted: true } as PanelMessage;
    // The panel's history came first: the stored transcript does not name it yet.
    const before = cards([question]);
    expect(before.replaceMessage(cutPanel), "nothing until its card can be drawn").toBeNull();
    // The stored transcript arrives: the card from then on.
    const after = cards([question, cut("a-1", 2)]);
    expect(after.replaceMessage(cutPanel)).not.toBeNull();
    expect(after.replaceMessage(cutPanel)).not.toBeUndefined();
    // The reverse order: the stored transcript first, the panel's flag still unread.
    expect(after.replaceMessage(panel("a-1")), "the card from the first paint").not.toBeUndefined();
    expect(after.replaceMessage(panel("a-1"))).not.toBeNull();
  });

  it("holds Resume and Dismiss on the trailing card of a turn that could not resume, not while a turn runs", () => {
    const onResume = vi.fn();
    const onDismiss = vi.fn();
    const interruptedTurn = { auto_resume: false } as unknown as InterruptedTurn;
    const messages = [question, cut("a-1", 2)];

    const idle = cards(messages, { interruptedTurn, onResume, onDismiss });
    expect(idle.endsWithCut).toBe(true);
    const { unmount } = renderWithProviders(<>{idle.replaceMessage(panel("a-1"))}</>);
    screen.getByRole("button", { name: /Resume/ }).click();
    screen.getByRole("button", { name: /Dismiss/ }).click();
    expect(onResume).toHaveBeenCalledOnce();
    expect(onDismiss).toHaveBeenCalledOnce();
    unmount();

    const running = cards(messages, { interruptedTurn, onResume, onDismiss, working: true });
    renderWithProviders(<>{running.replaceMessage(panel("a-1"))}</>);
    expect(screen.queryByRole("button", { name: /Resume/ })).toBeNull();
  });

  it("draws the card of a cut being resumed under the transcript while the turn runs, resuming then gone (#10910)", () => {
    const resumingTurn = { cause: "stop", resume: 1, of: 20, cut_at: null };
    const messages = [question, cut("a-1", 2)];

    const live = cards(messages, { resumingTurn, working: true });
    expect(live.liveCard).not.toBeNull();
    expect(live.replaceMessage(panel("a-1")), "the panel's copy is left out while the card is live").toBeNull();
    const { unmount } = renderWithProviders(<>{live.liveCard}</>);
    expect(screen.getByTestId("interrupted-reply-card").getAttribute("data-state")).toBe("resuming");
    unmount();

    const ended = cards(messages, { resumingTurn: null, working: false });
    expect(ended.liveCard).toBeNull();
    expect(ended.replaceMessage(panel("a-1"))).not.toBeNull();
  });

  it("keeps the card under the transcript at the turn's end, resumed once stored, and reads the panel's thread back (#10910)", () => {
    vi.useFakeTimers();
    try {
      const resumingTurn = { cause: "stop", resume: 1, of: 20, cut_at: null };
      const reloadTranscript = vi.fn();
      const during = [question, cut("a-1", 2)];
      const answered = [
        ...during,
        stored("a-2", 3, { content: "Done.", resumed_from: resumingTurn as ChatMessage["resumed_from"] }),
      ];
      const hook = renderHook(
        ({ messages, working, resuming }: { messages: ChatMessage[]; working: boolean; resuming: typeof resumingTurn | null }) =>
          useCutTurnCards(messages, { resumingTurn: resuming, interruptedTurn: null, working, reloadTranscript }),
        {
          wrapper: withProviders(testQueryClient()),
          initialProps: { messages: during, working: true, resuming: resumingTurn as typeof resumingTurn | null },
        },
      );
      expect(hook.result.current.liveCard).not.toBeNull();

      // The turn ends: the stored transcript is not read back yet, the card stays.
      hook.rerender({ messages: during, working: false, resuming: null });
      expect(hook.result.current.liveCard, "never gone between resuming and resumed").not.toBeNull();
      expect(reloadTranscript).not.toHaveBeenCalled();

      // The stored transcript holds the reply that answered: resumed, in place.
      hook.rerender({ messages: answered, working: false, resuming: null });
      const { unmount } = renderWithProviders(<>{hook.result.current.liveCard}</>);
      expect(screen.getByTestId("interrupted-reply-card").getAttribute("data-state")).toBe("resumed");
      unmount();
      expect(reloadTranscript).toHaveBeenCalledOnce();

      // The panel draws the cut reply again: its card goes in place of the held one,
      // and in the same render the held copy draws nothing (UX round 2 B2).
      const inPlace = hook.result.current.replaceMessage(panel("a-1"));
      expect(inPlace).not.toBeNull();
      const sameRender = renderWithProviders(<>{inPlace}{hook.result.current.liveCard}</>);
      expect(screen.getAllByTestId("interrupted-reply-card"), "never two cards at once").toHaveLength(1);
      sameRender.unmount();
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(hook.result.current.liveCard).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("drops the held card when the panel never draws the cut reply again", () => {
    vi.useFakeTimers();
    try {
      const resumingTurn = { cause: "stop", resume: 1, of: 20, cut_at: null };
      const during = [question, cut("a-1", 2)];
      const answered = [
        ...during,
        stored("a-2", 3, { content: "Done.", resumed_from: resumingTurn as ChatMessage["resumed_from"] }),
      ];
      const hook = renderHook(
        ({ messages, working, resuming }: { messages: ChatMessage[]; working: boolean; resuming: typeof resumingTurn | null }) =>
          useCutTurnCards(messages, { resumingTurn: resuming, interruptedTurn: null, working }),
        {
          wrapper: withProviders(testQueryClient()),
          initialProps: { messages: during, working: true, resuming: resumingTurn as typeof resumingTurn | null },
        },
      );
      hook.rerender({ messages: answered, working: false, resuming: null });
      expect(hook.result.current.liveCard).not.toBeNull();
      act(() => {
        vi.advanceTimersByTime(SETTLED_CARD_HOLD_MS);
      });
      expect(hook.result.current.liveCard).toBeNull();

      // Another conversation: nothing held.
      const other = renderHook(
        ({ messages, working, resuming }: { messages: ChatMessage[]; working: boolean; resuming: typeof resumingTurn | null }) =>
          useCutTurnCards(messages, { resumingTurn: resuming, interruptedTurn: null, working }),
        {
          wrapper: withProviders(testQueryClient()),
          initialProps: { messages: during, working: true, resuming: resumingTurn as typeof resumingTurn | null },
        },
      );
      other.rerender({ messages: [question], working: false, resuming: null });
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(other.result.current.liveCard).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("moves the card above the resumed reply once it streams, never under the transcript at the same time, and keeps it there after the turn (#10910)", () => {
    vi.useFakeTimers();
    try {
      const resumingTurn = { cause: "stop", resume: 1, of: 20, cut_at: null };
      const during = [question, cut("a-1", 2)];
      const answered = [
        ...during,
        stored("a-2", 3, { content: "Done.", resumed_from: resumingTurn as ChatMessage["resumed_from"] }),
      ];
      const hook = renderHook(
        ({ messages, working, resuming }: { messages: ChatMessage[]; working: boolean; resuming: typeof resumingTurn | null }) =>
          useCutTurnCards(messages, { resumingTurn: resuming, interruptedTurn: null, working }),
        {
          wrapper: withProviders(testQueryClient()),
          initialProps: { messages: during, working: true, resuming: resumingTurn as typeof resumingTurn | null },
        },
      );
      // Before the reply exists: under the transcript.
      expect(hook.result.current.liveCard).not.toBeNull();
      const reply = { id: "local-reply", role: "assistant", content: "" } as PanelMessage;
      const user = { id: "local-user", role: "user", content: "Hunt the campaign" } as PanelMessage;
      expect(hook.result.current.renderAboveMessage(user, { streaming: false })).toBeUndefined();
      // The resumed reply streams: the card goes above it, out of the footer, in the
      // very render that first draws the reply (UX round 3 B3: no frame in the footer).
      const firstFrame = renderWithProviders(
        <>
          <div data-testid="row-above">{hook.result.current.renderAboveMessage(reply, { streaming: true })}</div>
          <div data-testid="footer">{hook.result.current.liveCard}</div>
        </>,
      );
      expect(screen.getAllByTestId("interrupted-reply-card"), "one card on the first stream frame").toHaveLength(1);
      expect(screen.getByTestId("row-above").querySelector('[data-testid="interrupted-reply-card"]')).not.toBeNull();
      expect(screen.getByTestId("footer").querySelector('[data-testid="interrupted-reply-card"]')).toBeNull();
      firstFrame.unmount();
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(hook.result.current.liveCard, "never in the footer and above the reply at once").toBeNull();
      const above = hook.result.current.renderAboveMessage(reply, { streaming: true });
      expect(above).toBeTruthy();
      const { unmount } = renderWithProviders(<>{above}</>);
      expect(screen.getByTestId("interrupted-reply-card").getAttribute("data-state")).toBe("resuming");
      unmount();
      expect(hook.result.current.renderAboveMessage(user, { streaming: false })).toBeUndefined();

      // The turn ends and the answer is stored: same row, now resumed.
      hook.rerender({ messages: answered, working: false, resuming: null });
      expect(hook.result.current.liveCard).toBeNull();
      const settled = hook.result.current.renderAboveMessage({ ...reply, content: "Done." }, { streaming: false });
      const second = renderWithProviders(<>{settled}</>);
      expect(screen.getByTestId("interrupted-reply-card").getAttribute("data-state")).toBe("resumed");
      second.unmount();

      // The panel reads the thread back: the cut reply's own card goes in place, nothing above,
      // from the same render on (UX round 2 B2: exactly one card at every step).
      const inPlace = hook.result.current.replaceMessage(panel("a-1"));
      expect(inPlace).not.toBeNull();
      const sameRender = renderWithProviders(
        <>
          {inPlace}
          {hook.result.current.renderAboveMessage({ ...reply, content: "Done." }, { streaming: false })}
          {hook.result.current.liveCard}
        </>,
      );
      expect(screen.getAllByTestId("interrupted-reply-card"), "never two cards at once").toHaveLength(1);
      sameRender.unmount();
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(hook.result.current.renderAboveMessage(reply, { streaming: false })).toBeUndefined();
      expect(hook.result.current.liveCard).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("draws no live card without a resume, nor when the transcript has no cut", () => {
    const resumingTurn = { cause: "stop", resume: 1, of: 20, cut_at: null };
    expect(cards([question, cut("a-1", 2)], { working: true }).liveCard).toBeNull();
    expect(cards([question], { resumingTurn, working: true }).liveCard).toBeNull();
  });
});
