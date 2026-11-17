// A turn the platform resumed by itself after a cut: its
// `turn_resuming` frame shows a line under the transcript while it runs, and
// the stored reply carries the marker once the conversation is read back.

import { describe, expect, it } from "vitest";
import { act, renderHook, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ChatMessage as PanelMessage } from "@filigran/chatbot";

import { MessageExtras } from "@/components/chat-panel/MessageParts";
import { transcriptFooter } from "@/components/chat-panel/transcript-footer";
import { readResumingFrame } from "@/lib/resumed-turn";
import { useTurnFrames } from "@/pages/chat-panel/useTurnFrames";
import { renderWithProviders, testQueryClient, withProviders } from "@/testing/providers";
import type { ChatMessage, ResumedFromNote } from "@/types";

const RESUMING = { type: "turn_resuming", cause: "stop", resume: 1, of: 20, relayed: true };

function interruptedReply(): ChatMessage {
  return {
    id: "a-0",
    role: "assistant",
    content: "This turn was interrupted before it could finish.",
    channel: "web",
    channel_message_id: null,
    sequence: 2,
    created_at: "2026-11-02T12:30:00Z",
    interrupted: true,
    tool_call_trace: Array.from({ length: 15 }, (_, i) => ({ name: `tool_${i}` })),
  } as unknown as ChatMessage;
}

function frames() {
  const hook = renderHook(({ id }) => useTurnFrames(id), {
    wrapper: withProviders(testQueryClient()),
    initialProps: { id: "c-1" as string | null },
  });
  const send = (frame: Record<string, unknown>, replayed = false, end?: "stored") =>
    act(() => hook.result.current.onStreamEvent(frame, { relayed: true, replayed, end }));
  const running = (value: boolean) => act(() => hook.result.current.onTurnRunningChange(value));
  return { hook, send, running };
}

function renderFooter(working: boolean, resumingTurn: ResumedFromNote | null) {
  return renderWithProviders(<>{transcriptFooter({ working, resumingTurn, waitItems: [], interruptedTurn: null })}</>);
}

describe("a turn the platform resumed by itself", () => {
  it("is read off its frame and dropped when the turn ends", async () => {
    const { hook, send, running } = frames();
    running(true);
    send(RESUMING);
    await waitFor(() =>
      expect(hook.result.current.resumingTurn).toEqual({ cause: "stop", resume: 1, of: 20, cut_at: null }),
    );

    running(false);
    expect(hook.result.current.resumingTurn).toBeNull();
  });

  it("survives a replay of the same turn, and is dropped by a conversation switch", async () => {
    const { hook, send, running } = frames();
    running(true);
    send({ ...RESUMING, turn_id: "t-1" });
    send({ type: "status", status: "thinking", resumed: true, turn_id: "t-1" }, true);
    await waitFor(() => expect(hook.result.current.resumingTurn).not.toBeNull());

    hook.rerender({ id: "c-2" });
    expect(hook.result.current.resumingTurn).toBeNull();
  });

  it("is said only once the thread is read back, so the cut card says it first (#10910)", async () => {
    // Before the read-back the transcript may not hold the cut yet: the line
    // under it said "resumed by itself" for a moment, then the cut card took
    // over with "resuming by itself".
    const { hook, send, running } = frames();
    running(true);
    send({ ...RESUMING, turn_id: "t-1" });
    expect(hook.result.current.resumingTurn).toBeNull();
    await waitFor(() => expect(hook.result.current.resumingTurn).not.toBeNull());
  });

  it("survives a new conversation taking its id and the running flag blinking meanwhile (#10910)", async () => {
    const { hook, send, running } = frames();
    hook.rerender({ id: null });
    running(true);
    hook.rerender({ id: "c-new" });
    running(false);
    running(true);
    send({ type: "thinking", resumed: true, turn_id: "t-1" }, true);
    send({ ...RESUMING, turn_id: "t-1" });
    running(false);
    running(true);
    await waitFor(() =>
      expect(hook.result.current.resumingTurn).toEqual({ cause: "stop", resume: 1, of: 20, cut_at: null }),
    );
  });

  it("is dropped once another turn runs or its own reply is stored", () => {
    const { hook, send, running } = frames();
    running(true);
    send({ ...RESUMING, turn_id: "t-1" });
    send({ type: "thinking", turn_id: "t-2" });
    expect(hook.result.current.resumingTurn).toBeNull();

    send({ ...RESUMING, turn_id: "t-2" });
    send({ type: "done", message_id: "a-1", turn_id: "t-2" }, false, "stored");
    expect(hook.result.current.resumingTurn).toBeNull();
  });

  it("is not said on a later turn whose own frames have started, even when its stored end was missed", async () => {
    const { hook, send, running } = frames();
    running(true);
    send({ ...RESUMING, turn_id: "t-1" });
    await waitFor(() => expect(hook.result.current.resumingTurn).not.toBeNull());
    running(false);

    running(true);
    send({ type: "thinking", turn_id: "t-2" });
    send({ type: "status", status: "tool_start", turn_id: "t-2" });
    expect(hook.result.current.resumingTurn).toBeNull();
  });

  it("says so under the transcript while it runs, naming what cut it", () => {
    renderFooter(true, readResumingFrame(RESUMING));
    expect(screen.getByTestId("resumed-turn-live").textContent).toBe("Resumed by itself after a platform restart");
  });

  it("names a worker recycle", () => {
    renderFooter(true, { cause: "recycle", resume: 3, of: 20, cut_at: null });
    expect(screen.getByTestId("resumed-turn-live").textContent).toBe("Resumed by itself after a worker recycle");
  });

  it("shows no line once no turn runs", () => {
    renderFooter(false, readResumingFrame(RESUMING));
    expect(screen.queryByTestId("resumed-turn-live")).toBeNull();
  });

  it("marks the stored reply it ended with, and no other", () => {
    const reply = { id: "m-1", serverId: "a-1", role: "assistant", content: "Here is the answer.", timestamp: new Date() } as PanelMessage;
    const stored = { ...interruptedReply(), id: "a-1", interrupted: false, resumed_from: { cause: "stop", resume: 1, of: 20, cut_at: "2026-11-02T12:30:00Z" } };
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={reply} stored={stored} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("resumed-turn-marker").textContent).toBe("Resumed by itself after a platform restart");
    unmount();

    renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={reply} stored={{ ...stored, resumed_from: null }} />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("resumed-turn-marker")).toBeNull();
  });
});

describe("resumed-turn helpers", () => {
  it("reads a frame, dropping what is malformed", () => {
    expect(readResumingFrame({ cause: "stop", resume: 2, of: 20 })).toEqual({ cause: "stop", resume: 2, of: 20, cut_at: null });
    expect(readResumingFrame({ cause: 4, resume: "2", of: -1 })).toEqual({ cause: null, resume: null, of: null, cut_at: null });
    expect(readResumingFrame(null)).toBeNull();
  });
});
