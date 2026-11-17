// A turn woken by finished background work (#11796): its `thinking` ack names
// the work, a line under the transcript says so while it runs, and the stored
// reply carries the marker, so it never reads as a second answer to the last
// user message.

import { describe, expect, it } from "vitest";
import { act, renderHook, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ChatMessage as PanelMessage } from "@filigran/chatbot";

import { MessageExtras } from "@/components/chat-panel/MessageParts";
import { transcriptFooter } from "@/components/chat-panel/transcript-footer";
import { useTurnFrames } from "@/pages/chat-panel/useTurnFrames";
import { readWakeNote } from "@/lib/wake-turn";
import { renderWithProviders, testQueryClient, withProviders } from "@/testing/providers";
import type { ChatMessage } from "@/types";

const NOTE = { source: "background_task_wake", task_id: "t-9", agent_name: "Researcher", kind: "task" };
const PARTNER_NOTE = { source: "background_task_wake", task_id: "a2a-partner", agent_name: "Partner Intake", kind: "partner" };
const LEGACY_NOTE = { source: "background_task_wake", task_id: "t-9", agent_name: "Researcher" };
const WAKE_ACK = { type: "thinking", wake: NOTE, turn_id: "t-1" };

function frames() {
  const hook = renderHook(({ id }) => useTurnFrames(id), {
    wrapper: withProviders(testQueryClient()),
    initialProps: { id: "c-1" as string | null },
  });
  const send = (frame: Record<string, unknown>, end?: "stored") =>
    act(() => hook.result.current.onStreamEvent(frame, { relayed: true, replayed: false, end }));
  const running = (value: boolean) => act(() => hook.result.current.onTurnRunningChange(value));
  return { hook, send, running };
}

function storedReply(wake: ChatMessage["wake"]): ChatMessage {
  return {
    id: "a-1",
    role: "assistant",
    content: "The research is done.",
    channel: "web",
    channel_message_id: null,
    sequence: 4,
    created_at: "2026-11-14T12:00:00Z",
    wake,
  } as unknown as ChatMessage;
}

describe("a turn woken by finished background work", () => {
  it("is read off its thinking ack while it runs, and dropped at its stored end", () => {
    const { hook, send, running } = frames();
    running(true);
    send(WAKE_ACK);
    expect(hook.result.current.wakingTurn).toEqual(NOTE);

    send({ type: "done", turn_id: "t-1", message_id: "a-1" }, "stored");
    expect(hook.result.current.wakingTurn).toBeNull();
  });

  it("is never said for a plain thinking ack", () => {
    const { hook, send, running } = frames();
    running(true);
    send({ type: "thinking", turn_id: "t-2" });
    expect(hook.result.current.wakingTurn).toBeNull();
  });

  it("is not said again when a plain turn follows a finished wake turn", () => {
    const { hook, send, running } = frames();
    running(true);
    send(WAKE_ACK);
    send({ type: "done", turn_id: "t-1", message_id: "a-1" }, "stored");
    running(false);

    running(true);
    expect(hook.result.current.wakingTurn).toBeNull();
    send({ type: "thinking", turn_id: "t-2" });
    expect(hook.result.current.wakingTurn).toBeNull();
  });

  it("is dropped when its turn ends even if its stored end never arrives", () => {
    const { hook, send, running } = frames();
    running(true);
    send(WAKE_ACK);
    running(false);

    // A later turn of the same conversation, before and after its first frame.
    running(true);
    expect(hook.result.current.wakingTurn).toBeNull();
    send({ type: "thinking", turn_id: "t-2" });
    expect(hook.result.current.wakingTurn).toBeNull();
  });

  it("is replaced by a plain ack of another turn while the flag stays on", () => {
    const { hook, send, running } = frames();
    running(true);
    send(WAKE_ACK);
    send({ type: "thinking", turn_id: "t-2" });
    expect(hook.result.current.wakingTurn).toBeNull();
  });

  it("survives a stream taking the same turn over", () => {
    const { hook, send, running } = frames();
    running(true);
    send(WAKE_ACK);
    send({ type: "thinking", resumed: true, turn_id: "t-1" });
    expect(hook.result.current.wakingTurn).toEqual(NOTE);
  });

  it("is dropped by a switch to another conversation", () => {
    const { hook, send, running } = frames();
    running(true);
    send(WAKE_ACK);
    expect(hook.result.current.wakingTurn).toEqual(NOTE);

    hook.rerender({ id: "c-2" });
    expect(hook.result.current.wakingTurn).toBeNull();
  });

  it("says the agent whose task finished under the transcript while the turn runs, and nothing once it ends", () => {
    const { unmount } = renderWithProviders(
      <>{transcriptFooter({ working: true, wakingTurn: NOTE, waitItems: [], interruptedTurn: null })}</>,
    );
    expect(screen.getByTestId("wake-turn-live").textContent).toBe("Background task of Researcher finished");
    unmount();

    renderWithProviders(<>{transcriptFooter({ working: false, wakingTurn: NOTE, waitItems: [], interruptedTurn: null })}</>);
    expect(screen.queryByTestId("wake-turn-live")).toBeNull();
  });

  it("marks the stored reply, on reload too, and no other reply", () => {
    const reply = { id: "m-1", serverId: "a-1", role: "assistant", content: "The research is done.", timestamp: new Date() } as PanelMessage;
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={reply} stored={storedReply(NOTE)} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("wake-turn-marker").textContent).toBe("Background task of Researcher finished");
    unmount();

    renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={reply} stored={storedReply(null)} />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("wake-turn-marker")).toBeNull();
  });

  it("names an A2A partner's answer as the partner's answer, live and on the stored reply", () => {
    const { unmount } = renderWithProviders(
      <>{transcriptFooter({ working: true, wakingTurn: PARTNER_NOTE, waitItems: [], interruptedTurn: null })}</>,
    );
    expect(screen.getByTestId("wake-turn-live").textContent).toBe("Partner Intake answered");
    unmount();

    const reply = { id: "m-2", serverId: "a-1", role: "assistant", content: "Received.", timestamp: new Date() } as PanelMessage;
    renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={reply} stored={storedReply(PARTNER_NOTE)} />
      </MemoryRouter>,
    );
    const marker = screen.getByTestId("wake-turn-marker").textContent;
    expect(marker).toBe("Partner Intake answered");
    expect(marker).not.toContain("Background task");
  });

  it("keeps the task wording for a sub-agent task and for a note stored before the kind", () => {
    const reply = { id: "m-3", serverId: "a-1", role: "assistant", content: "Done.", timestamp: new Date() } as PanelMessage;
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={reply} stored={storedReply(NOTE)} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("wake-turn-marker").textContent).toBe("Background task of Researcher finished");
    unmount();

    renderWithProviders(
      <MemoryRouter>
        <MessageExtras message={reply} stored={storedReply(LEGACY_NOTE)} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("wake-turn-marker").textContent).toBe("Background task of Researcher finished");
  });

  it("reads a note, dropping a foreign or malformed one", () => {
    expect(readWakeNote(NOTE)).toEqual(NOTE);
    expect(readWakeNote(PARTNER_NOTE)).toEqual(PARTNER_NOTE);
    expect(readWakeNote(LEGACY_NOTE)).toEqual({ ...LEGACY_NOTE, kind: null });
    expect(readWakeNote({ source: "background_task_wake", agent_name: 3 })).toEqual({
      source: "background_task_wake",
      task_id: null,
      agent_name: null,
      kind: null,
    });
    expect(readWakeNote({ source: "other" })).toBeNull();
    expect(readWakeNote(null)).toBeNull();
  });
});
