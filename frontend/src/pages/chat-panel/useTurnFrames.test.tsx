// What the chat page does with the frames of a turn the panel does not render
// itself (`onStreamEvent`): the caches a tool changed are refreshed, the machine commands held for approval come and go,
// the agentic actions are counted, and a pause shows what it waits on. Whether
// a turn runs is the panel's word (`onTurnRunningChange`): its end drops the
// turn's state and refreshes what the turn changed.

import { afterEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { QueryClient } from "@tanstack/react-query";
import type { ChatStreamEventInfo } from "@filigran/chatbot";

import { useTurnFrames } from "@/pages/chat-panel/useTurnFrames";
import { testQueryClient, withProviders } from "@/testing/providers";

const CID = "c-1";
const OWN: ChatStreamEventInfo = { relayed: false };
const RELAYED: ChatStreamEventInfo = { relayed: true };

function setup(conversationId: string | null = CID) {
  const client = testQueryClient();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const hook = renderHook(({ id }) => useTurnFrames(id), {
    wrapper: withProviders(client),
    initialProps: { id: conversationId },
  });
  const send = (frame: Record<string, unknown>, info: ChatStreamEventInfo = OWN) => act(() => hook.result.current.onStreamEvent(frame, info));
  const running = (value: boolean) => act(() => hook.result.current.onTurnRunningChange(value));
  return { client, invalidate, hook, send, running };
}

/** The query keys *invalidate* was called with, as JSON for an easy match. */
function invalidatedKeys(invalidate: MockInstance<QueryClient["invalidateQueries"]>): string[] {
  return invalidate.mock.calls.map(([filters]) => JSON.stringify(filters?.queryKey));
}

afterEach(() => {
  vi.useRealTimers();
});

describe("useTurnFrames", () => {
  it("says a turn runs as the panel says it, never from a frame", () => {
    const { hook, send, running } = setup();
    send({ type: "status", status: "thinking" });
    expect(hook.result.current.working).toBe(false);

    running(true);
    expect(hook.result.current.working).toBe(true);
    send({ type: "done", content: "answer" });
    expect(hook.result.current.working).toBe(true);
    running(false);
    expect(hook.result.current.working).toBe(false);
  });

  it("refreshes what a tool changed when it returns", () => {
    const { invalidate, send } = setup();
    send({ type: "status", status: "tool_done", tool: "spawn_background_task" });
    expect(invalidatedKeys(invalidate)).toEqual(
      expect.arrayContaining([
        JSON.stringify(["sub-agent-tasks"]),
        JSON.stringify(["background-tasks"]),
        JSON.stringify(["background-task-count"]),
      ]),
    );

    invalidate.mockClear();
    send({ type: "status", status: "tool_done", tool: "edit_presentation" });
    expect(invalidatedKeys(invalidate)).toContain(JSON.stringify(["presentations"]));

    invalidate.mockClear();
    send({ type: "status", status: "tool_done", tool: "delete_presentation" });
    expect(invalidatedKeys(invalidate)).toContain(JSON.stringify(["presentations"]));

    invalidate.mockClear();
    send({ type: "status", status: "tool_done", tool: "delete_video_renders" });
    expect(invalidatedKeys(invalidate)).toContain(JSON.stringify(["video-studio"]));

    invalidate.mockClear();
    send({ type: "status", status: "tool_done", tool: "delete_video" });
    expect(invalidatedKeys(invalidate)).toContain(JSON.stringify(["video-studio"]));
  });

  it("refreshes the deck on presentation_saved and the video studio on video_saved, mid-tool", () => {
    const { invalidate, send } = setup();
    send({ type: "status", status: "tool_progress", event: "presentation_saved", presentation_id: "p-1", revision: 2 });
    expect(invalidatedKeys(invalidate)).toContain(JSON.stringify(["presentations"]));

    invalidate.mockClear();
    send({ type: "status", status: "tool_progress", event: "video_saved", video_id: "v-1", revision: 3 });
    expect(invalidatedKeys(invalidate)).toContain(JSON.stringify(["video-studio"]));
  });

  it("holds a machine command for approval until it is resolved, dismissed or the turn ends", () => {
    const { hook, send, running } = setup();
    running(true);
    const required = {
      type: "status",
      status: "tool_progress",
      event: "runner_approval_required",
      command_id: "cmd-1",
      runner_id: "r-1",
      runner_name: "Laptop",
      summary: "ls -la",
    };
    send(required);
    // The same request twice is shown once.
    send(required);
    expect(hook.result.current.runnerApprovals.map((r) => r.command_id)).toEqual(["cmd-1"]);

    send({ type: "status", status: "tool_progress", event: "runner_approval_resolved", command_id: "cmd-1" });
    expect(hook.result.current.runnerApprovals).toEqual([]);

    send({ ...required, command_id: "cmd-2" });
    act(() => hook.result.current.dismissRunnerApproval("cmd-2"));
    expect(hook.result.current.runnerApprovals).toEqual([]);

    // One a consulted agent's command asks for arrives wrapped.
    send({ type: "status", status: "consult_progress", inner: { ...required, command_id: "cmd-3" } });
    expect(hook.result.current.runnerApprovals.map((r) => r.command_id)).toEqual(["cmd-3"]);
    // And one a peer the consulted agent consulted in turn asks for, wrapped twice.
    send({
      type: "status",
      status: "consult_progress",
      inner: { status: "consult_progress", agent_id: "a-2", inner: { ...required, command_id: "cmd-4" } },
    });
    expect(hook.result.current.runnerApprovals.map((r) => r.command_id)).toEqual(["cmd-3", "cmd-4"]);
    running(false);
    expect(hook.result.current.runnerApprovals).toEqual([]);
  });

  it("counts the agentic actions the turn is charged, and drops them when it ends", () => {
    const { hook, send, running } = setup();
    running(true);
    send({ type: "agentic_action", actions: 2, period: "2026-10" });
    send({ type: "agentic_action", actions: 1, period: "2026-10" });
    expect(hook.result.current.turnCharges.map((c) => c.count)).toEqual([2, 1]);

    send({ type: "agentic_action", actions: 0 });
    expect(hook.result.current.turnCharges).toHaveLength(2);

    running(false);
    expect(hook.result.current.turnCharges).toEqual([]);
  });

  it("takes a resumed turn's replayed totals in place of the charges it counted", () => {
    const { hook, send, running } = setup();
    running(true);
    send({ type: "agentic_action", actions: 2, period: "2026-10" });
    send({ type: "agentic_action", actions: 1, period: "2026-10" });

    // The panel's stream dropped: the event stream replays the turn, its charges as one total.
    send({ type: "thinking", resumed: true, turn_id: "t-1" }, { relayed: true, replayed: true });
    send({ type: "agentic_action", actions: 3, period: "2026-10" }, RELAYED);
    expect(hook.result.current.turnCharges.map((c) => c.count)).toEqual([3]);

    send({ type: "agentic_action", actions: 1, period: "2026-10", turn_id: "t-1" }, RELAYED);
    send({ type: "thinking", intermediate: true }, RELAYED);
    expect(hook.result.current.turnCharges.map((c) => c.count)).toEqual([3, 1]);
  });

  it("refreshes the conversation and the quota after the turn's end, and not on a done frame", () => {
    vi.useFakeTimers();
    const { invalidate, send, running } = setup();
    running(true);
    send({ type: "status", status: "thinking" });
    send({ type: "done", content: "answer" });
    act(() => vi.runAllTimers());
    expect(invalidatedKeys(invalidate)).not.toContain(JSON.stringify(["conversation", CID]));

    running(false);
    act(() => vi.runAllTimers());
    expect(invalidatedKeys(invalidate)).toEqual(
      expect.arrayContaining([
        JSON.stringify(["my-quotas"]),
        JSON.stringify(["conversations"]),
        JSON.stringify(["conversation", CID]),
        JSON.stringify(["awaited-work", CID]),
      ]),
    );
  });

  it("refreshes the conversation when a stopped or failed reply is stored, after the turn ended on screen", () => {
    vi.useFakeTimers();
    const { invalidate, send, running } = setup();
    running(true);
    running(false);
    act(() => vi.runAllTimers());

    for (const type of ["cancelled", "done"]) {
      invalidate.mockClear();
      send({ type, message_id: "m-1" }, { relayed: true, end: "stored" });
      expect(invalidatedKeys(invalidate)).toEqual(
        expect.arrayContaining([JSON.stringify(["conversation", CID]), JSON.stringify(["conversations"])]),
      );
    }

    // Not an end that stored a reply: an intermediate done, or a provisional end.
    invalidate.mockClear();
    send({ type: "done", content: "x", message_id: "m-2", intermediate: true });
    send({ type: "cancelled", provisional: true }, { relayed: false, end: "provisional" });
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("refreshes the conversation once for a turn that stored its reply as it ended", () => {
    vi.useFakeTimers();
    const { invalidate, send, running } = setup();
    for (const relayed of [false, true]) {
      invalidate.mockClear();
      running(true);
      send({ type: "done", content: "answer", message_id: "m-1" }, { relayed, end: "stored" });
      running(false);
      act(() => vi.runAllTimers());
      const keys = invalidatedKeys(invalidate);
      expect(keys.filter((k) => k === JSON.stringify(["conversation", CID]))).toHaveLength(1);
      expect(keys.filter((k) => k === JSON.stringify(["conversations"]))).toHaveLength(1);
    }
  });

  it("keeps what a pause waits on from wait_start, updates it on wait_status, and clears it when the pause ends", () => {
    const { hook, send } = setup();
    const task = { id: "t-1", kind: "task", label: "Enrich IOCs", status: "running", started_at: "2026-10-22T10:00:00Z" };
    send({ type: "status", status: "wait_start", seconds: 60, items: [task] });
    expect(hook.result.current.waitItems).toEqual([task]);

    // A heartbeat of the waiting tool does not end the pause.
    send({ type: "status", status: "tool_heartbeat", elapsed_s: 10 });
    send({ type: "status", status: "wait_status", items: [{ ...task, status: "completed" }] });
    expect(hook.result.current.waitItems.map((i) => i.status)).toEqual(["completed"]);

    // The loop goes on: the pause is over.
    send({ type: "status", status: "thinking" });
    expect(hook.result.current.waitItems).toEqual([]);

    // A wait_status outside a pause shows nothing.
    send({ type: "status", status: "wait_status", items: [task] });
    expect(hook.result.current.waitItems).toEqual([]);
  });

  it("ends the pause a resumed turn continued once it says the wait is over (wait_continued)", () => {
    const { hook, send } = setup();
    const task = { id: "t-1", kind: "task", label: "Enrich IOCs", status: "running", started_at: null };
    // The resumed turn announces the wait its stopping worker cut, as a normal one.
    send({ type: "status", status: "wait_start", seconds: 120, items: [task] });
    expect(hook.result.current.waitItems).toEqual([task]);
    send({ type: "status", status: "wait_continued" });
    expect(hook.result.current.waitItems).toEqual([]);
  });

  it("clears the wait items when the answer streams, when the turn ends, and on another conversation", () => {
    const { hook, send, running } = setup();
    const task = { id: "t-1", kind: "run", label: null, status: "queued", started_at: null };
    send({ type: "status", status: "wait_start", items: [task] });
    send({ type: "stream", content: "Here" });
    expect(hook.result.current.waitItems).toEqual([]);

    running(true);
    send({ type: "status", status: "wait_start", items: [task] });
    running(false);
    expect(hook.result.current.waitItems).toEqual([]);

    send({ type: "status", status: "wait_start", items: [task] });
    hook.rerender({ id: "c-2" });
    expect(hook.result.current.waitItems).toEqual([]);
  });

  it("follows the investigation a turn plans, until the turn ends", () => {
    const { hook, send, running } = setup();
    running(true);
    send({ type: "status", status: "investigation_planned", investigation_id: "inv-1" });
    expect(hook.result.current.activeInvestigationId).toBe("inv-1");
    running(false);
    expect(hook.result.current.activeInvestigationId).toBeNull();
  });
});
