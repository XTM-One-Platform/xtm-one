// The card of a cut turn while its resume runs and right after it, on a page
// reloaded during the resume, and when the panel's stream closed on the cut
// before the cut reply was stored (#10910 section A, retest of 11 Nov 2026).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, screen } from "@testing-library/react";
import type { ChatMessage as PanelMessage } from "@filigran/chatbot";

import { PANEL_CATCH_UP_MS, RESUME_SETTLE_MS, useCutTurnCards } from "@/components/chat-panel/cut-turn-cards";
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
    created_at: "2026-11-11T14:20:00Z",
    ...fields,
  } as unknown as ChatMessage;
}

const question = stored("u-1", 1, { role: "user", content: "Hunt the campaign" });
const cut = stored("a-1", 2, { content: "This turn was stopped before it could finish.", interrupted: true });
const answer = stored("a-2", 3, {
  content: "Done.",
  resumed_from: { cause: "stop", resume: 1, of: 100 } as ChatMessage["resumed_from"],
});
const panel = (serverId: string) => ({ id: `p-${serverId}`, serverId, role: "assistant", content: "" }) as PanelMessage;

type Props = {
  messages: ChatMessage[];
  working: boolean;
  processing: boolean;
  interruptedTurn: InterruptedTurn | null;
};

function mount(initial: Props, reloadTranscript = vi.fn()) {
  const hook = renderHook(
    ({ messages, working, processing, interruptedTurn }: Props) =>
      useCutTurnCards(messages, { resumingTurn: null, interruptedTurn, working, processing, reloadTranscript }),
    { wrapper: withProviders(testQueryClient()), initialProps: initial },
  );
  return { hook, reloadTranscript };
}

function stateOf(node: ReturnType<ReturnType<typeof useCutTurnCards>["replaceMessage"]>) {
  const { unmount } = renderWithProviders(<>{node}</>);
  const value = screen.getByTestId("interrupted-reply-card").getAttribute("data-state");
  unmount();
  return value;
}

describe("the cut card while its resume runs", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("says resuming on a page reloaded during the resume, before the stream says so, never not resumed", () => {
    // The conversation is read first: a turn in flight, no interrupted turn served.
    const { hook } = mount({ messages: [question, cut], working: false, processing: true, interruptedTurn: null });
    expect(stateOf(hook.result.current.replaceMessage(panel("a-1")))).toBe("resuming");

    // The panel joins the running turn.
    hook.rerender({ messages: [question, cut], working: true, processing: true, interruptedTurn: null });
    expect(stateOf(hook.result.current.replaceMessage(panel("a-1")))).toBe("resuming");
  });

  it("goes from resuming straight to resumed at the turn's end, never through not resumed", () => {
    const { hook } = mount({ messages: [question, cut], working: true, processing: false, interruptedTurn: null });
    expect(stateOf(hook.result.current.replaceMessage(panel("a-1")))).toBe("resuming");

    // The turn ended on screen; the refetched transcript is not there yet.
    hook.rerender({ messages: [question, cut], working: false, processing: false, interruptedTurn: null });
    expect(stateOf(hook.result.current.replaceMessage(panel("a-1")))).toBe("resuming");

    // The stored transcript holds the reply that answered.
    hook.rerender({ messages: [question, cut, answer], working: false, processing: false, interruptedTurn: null });
    expect(stateOf(hook.result.current.replaceMessage(panel("a-1")))).toBe("resumed");
  });

  it("falls back to the conversation's word once the bound passes without an answer", () => {
    const { hook } = mount({ messages: [question, cut], working: true, processing: false, interruptedTurn: null });
    hook.rerender({ messages: [question, cut], working: false, processing: false, interruptedTurn: null });
    act(() => {
      vi.advanceTimersByTime(RESUME_SETTLE_MS);
    });
    expect(stateOf(hook.result.current.replaceMessage(panel("a-1")))).toBe("dismissed");
  });

  it("holds no Resume or Dismiss while the conversation says a turn is in flight", () => {
    const interruptedTurn = { auto_resume: false } as unknown as InterruptedTurn;
    const onResume = vi.fn();
    const hook = renderHook(
      () =>
        useCutTurnCards([question, cut], {
          resumingTurn: null,
          interruptedTurn,
          working: false,
          processing: true,
          onResume,
          onDismiss: vi.fn(),
        }),
      { wrapper: withProviders(testQueryClient()) },
    );
    renderWithProviders(<>{hook.result.current.replaceMessage(panel("a-1"))}</>);
    expect(screen.queryByRole("button", { name: /Resume/ })).toBeNull();
  });
});

describe("a cut the panel's thread does not hold", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reads the panel's thread back once when the panel never asked for the cut reply", () => {
    const { hook, reloadTranscript } = mount({
      messages: [question, cut],
      working: false,
      processing: true,
      interruptedTurn: null,
    });
    act(() => {
      vi.advanceTimersByTime(PANEL_CATCH_UP_MS);
    });
    expect(reloadTranscript).toHaveBeenCalledOnce();

    // The same cut again: no second read.
    hook.rerender({ messages: [question, cut], working: false, processing: false, interruptedTurn: null });
    act(() => {
      vi.advanceTimersByTime(PANEL_CATCH_UP_MS);
    });
    expect(reloadTranscript).toHaveBeenCalledOnce();

    // The reply that resumed it is stored and the panel does not hold it either.
    hook.rerender({ messages: [question, cut, answer], working: false, processing: false, interruptedTurn: null });
    act(() => {
      vi.advanceTimersByTime(PANEL_CATCH_UP_MS);
    });
    expect(reloadTranscript).toHaveBeenCalledTimes(2);
  });

  it("reads nothing back when the panel holds the cut and its answer, nor while a turn runs", () => {
    const { hook, reloadTranscript } = mount({
      messages: [question, cut, answer],
      working: false,
      processing: false,
      interruptedTurn: null,
    });
    hook.result.current.replaceMessage(panel("a-1"));
    hook.result.current.replaceMessage(panel("a-2"));
    act(() => {
      vi.advanceTimersByTime(PANEL_CATCH_UP_MS);
    });
    expect(reloadTranscript).not.toHaveBeenCalled();

    const running = mount({ messages: [question, cut], working: true, processing: true, interruptedTurn: null });
    act(() => {
      vi.advanceTimersByTime(PANEL_CATCH_UP_MS);
    });
    expect(running.reloadTranscript).not.toHaveBeenCalled();
  });

  it("reads nothing back for an older cut the transcript no longer ends with", () => {
    const later = [question, cut, answer, stored("u-2", 4, { role: "user", content: "Next" }), stored("a-3", 5, {})];
    const { reloadTranscript } = mount({ messages: later, working: false, processing: false, interruptedTurn: null });
    act(() => {
      vi.advanceTimersByTime(PANEL_CATCH_UP_MS);
    });
    expect(reloadTranscript).not.toHaveBeenCalled();
  });
});
