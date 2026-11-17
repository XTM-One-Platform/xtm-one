// A turn the platform cut and resumed draws one compact card: one line,
// the tool calls folded behind a chip, every cut of the turn in the same card.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { cutBreakdown, cutGroupViews, groupCutReplies } from "@/lib/cut-turn-group";
import { InterruptedReplyCard } from "@/pages/chat/InterruptedReplyCard";
import { renderWithProviders } from "@/testing/providers";
import type { ChatMessage, ResumedFromNote } from "@/types";

function message(id: string, extra: Partial<ChatMessage>): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    channel: "web",
    channel_message_id: null,
    sequence: 0,
    created_at: "2026-11-05T05:24:00Z",
    llm_trace_id: null,
    ...extra,
  } as ChatMessage;
}

const trace = (names: string[]) => names.map((name) => ({ name, input: "", output: "", success: true }));

function cut(id: string, names: string[], extra: Partial<ChatMessage> = {}): ChatMessage {
  return message(id, {
    content:
      "This turn was interrupted before it could finish: the server process running it was stopped (a deployment, a scale-in, a node drain or a restart).",
    interrupted: true,
    tool_call_trace: trace(names),
    tool_call_count: names.length,
    ...extra,
  });
}

const user = message("u-1", { role: "user", content: "Do it" });
const SCREENSHOT_TOOLS = [
  "load_tools",
  "get_integration",
  "get_integration",
  "list_integration_catalog",
  "list_integrations",
  "create_integration",
  "list_agents",
  ...Array.from({ length: 14 }, () => "update_agent"),
  "consult_agent",
];
const note = (resume: number): ResumedFromNote => ({ cause: "stop", resume, of: 100, cut_at: null });

describe("groupCutReplies", () => {
  it("folds consecutive cuts of one turn and the reply that resumed them", () => {
    const messages = [
      user,
      cut("a-1", ["load_tools"]),
      cut("a-2", ["update_agent"], { resumed_from: note(1) }),
      message("a-3", { content: "Done.", resumed_from: note(2) }),
    ];
    const groups = groupCutReplies(messages);
    expect([...groups.byFirstId.keys()]).toEqual(["a-1"]);
    expect([...groups.folded]).toEqual(["a-2"]);
    expect([...groups.resumedIds]).toEqual(["a-3"]);
    expect(groups.trailing).toBeNull();
    const view = cutGroupViews(groups, { live: null, queued: false }).get("a-1");
    expect(view?.state).toBe("resumed");
    expect(view?.by).toBe("itself");
    expect(view?.note?.resume).toBe(2);
  });

  it("keeps the group open across the person's message: one card, resumed by you", () => {
    // Cut, "Continue", then the reply to it: a manual resume, the reply
    // carries no `resumed_from`. One card, never a second marker.
    const messages = [
      user,
      cut("a-1", ["load_tools"]),
      message("u-2", { role: "user", content: "Continue" }),
      message("a-2", { content: "Done." }),
    ];
    const groups = groupCutReplies(messages);
    expect([...groups.byFirstId.keys()]).toEqual(["a-1"]);
    expect([...groups.resumedIds]).toEqual(["a-2"]);
    const view = cutGroupViews(groups, { live: null, queued: false }).get("a-1");
    expect(view?.state).toBe("resumed");
    expect(view?.by).toBe("you");
  });

  it("says resumed by itself when the automatic resume answered a message steered into it (#9650)", () => {
    // The person wrote while the platform resumed the turn: the message was a
    // steer into the running resume, whose reply carries `resumed_from`.
    const messages = [
      user,
      cut("a-1", ["load_tools"]),
      message("u-2", { role: "user", content: "stop and just say OK" }),
      message("a-2", { content: "OK.", resumed_from: note(1) }),
    ];
    const groups = groupCutReplies(messages);
    expect([...groups.byFirstId.keys()]).toEqual(["a-1"]);
    expect([...groups.resumedIds]).toEqual(["a-2"]);
    expect(groups.trailing).toBeNull();
    const view = cutGroupViews(groups, { live: null, queued: false }).get("a-1");
    expect(view?.state).toBe("resumed");
    expect(view?.by).toBe("itself");
  });

  it("says resumed by you from the moment the message is sent", () => {
    const groups = groupCutReplies([user, cut("a-1", ["load_tools"]), message("u-2", { role: "user", content: "Continue" })]);
    expect(groups.trailing).not.toBeNull();
    const view = cutGroupViews(groups, { live: null, queued: true }).get("a-1");
    expect(view?.state).toBe("resumed");
    expect(view?.by).toBe("you");
  });

  it("says resuming by itself while the platform's resume runs, a message steered into it included (#9650)", () => {
    const groups = groupCutReplies([user, cut("a-1", ["load_tools"]), message("u-2", { role: "user", content: "stop" })]);
    const view = cutGroupViews(groups, { live: note(1), queued: false, served: false }).get("a-1");
    expect(view?.state).toBe("resuming");
    expect(view?.by).toBe("itself");
  });

  it("folds a second cut after the person's message into the same card", () => {
    const messages = [
      user,
      cut("a-1", ["load_tools"]),
      message("u-2", { role: "user", content: "Continue" }),
      cut("a-2", ["update_agent"]),
    ];
    const groups = groupCutReplies(messages);
    expect([...groups.byFirstId.keys()]).toEqual(["a-1"]);
    expect([...groups.folded]).toEqual(["a-2"]);
  });

  describe("a turn cut again after the person's Continue", () => {
    // B1 of the UX review of #8949: nothing answered the Continue, so the
    // card never says "resumed by you"; it follows the live, queued, served
    // or not served state of the trailing group instead.
    const cutAgain = () =>
      groupCutReplies([
        user,
        cut("a-1", ["load_tools"]),
        message("u-2", { role: "user", content: "Continue" }),
        cut("a-2", ["update_agent"]),
      ]);

    it("could not resume, with the actions, while the conversation serves the cut", () => {
      const groups = cutAgain();
      expect(groups.trailing?.replies[0]?.id).toBe("a-1");
      const view = cutGroupViews(groups, { live: null, queued: false, served: true }).get("a-1");
      expect(view?.state).toBe("stopped");
      expect(view?.by).toBeNull();
      renderWithProviders(<InterruptedReplyCard view={view!} onResume={vi.fn()} onDismiss={vi.fn()} />);
      expect(screen.getByTestId("interrupted-reply-card").dataset.state).toBe("stopped");
      expect(screen.getByTestId("interrupted-reply-card-text").textContent).toBe(
        "Interrupted by a platform restart, could not resume by itself",
      );
      screen.getByRole("button", { name: "Resume" });
      screen.getByRole("button", { name: "Dismiss" });
    });

    it("resuming by itself with a live resume frame", () => {
      const view = cutGroupViews(cutAgain(), { live: note(2), queued: false }).get("a-1");
      expect(view?.state).toBe("resuming");
      expect(view?.by).toBe("itself");
    });

    it("queued when the platform queued its automatic resume", () => {
      const view = cutGroupViews(cutAgain(), { live: null, queued: true }).get("a-1");
      expect(view?.state).toBe("queued");
      expect(view?.by).toBeNull();
    });

    it("not resumed once the conversation no longer serves it", () => {
      expect(cutGroupViews(cutAgain(), { live: null, queued: false, served: false }).get("a-1")?.state).toBe(
        "dismissed",
      );
    });

    it("resumed by you when a second Continue is answered", () => {
      const groups = groupCutReplies([
        user,
        cut("a-1", ["load_tools"]),
        message("u-2", { role: "user", content: "Continue" }),
        cut("a-2", ["update_agent"]),
        message("u-3", { role: "user", content: "Continue" }),
        message("a-3", { content: "Done." }),
      ]);
      const view = cutGroupViews(groups, { live: null, queued: false }).get("a-1");
      expect(view?.state).toBe("resumed");
      expect(view?.by).toBe("you");
    });
  });

  it("says a dismissed trailing cut once the conversation no longer serves it", () => {
    const groups = groupCutReplies([user, cut("a-1", ["load_tools"])]);
    expect(cutGroupViews(groups, { live: null, queued: false, served: false }).get("a-1")?.state).toBe("dismissed");
  });

  it("says a trailing cut is resuming, queued or stopped", () => {
    const groups = groupCutReplies([user, cut("a-1", ["load_tools"])]);
    expect(cutGroupViews(groups, { live: note(3), queued: false }).get("a-1")?.state).toBe("resuming");
    expect(cutGroupViews(groups, { live: null, queued: true }).get("a-1")?.state).toBe("queued");
    expect(cutGroupViews(groups, { live: null, queued: false }).get("a-1")?.state).toBe("stopped");
  });

  it("reads the cause of a reply stored before resumed_from existed", () => {
    const groups = groupCutReplies([user, cut("a-1", [])]);
    expect(cutGroupViews(groups, { live: null, queued: false }).get("a-1")?.cause).toBe("stop");
  });

  it("counts the calls per tool, the last one and the calls still running", () => {
    const breakdown = cutBreakdown([cut("a-1", SCREENSHOT_TOOLS, { tool_call_count: SCREENSHOT_TOOLS.length + 1 })]);
    expect(breakdown.completed).toBe(22);
    expect(breakdown.tools.find((t) => t.name === "update_agent")?.calls).toBe(14);
    expect(breakdown.last).toEqual({ name: "consult_agent", failed: false });
    expect(breakdown.running).toBe(1);
  });
});

describe("InterruptedReplyCard", () => {
  function renderCard(messages: ChatMessage[], live: ResumedFromNote | null = null, queued = false) {
    const groups = groupCutReplies(messages);
    const [view] = [...cutGroupViews(groups, { live, queued }).values()];
    return renderWithProviders(<InterruptedReplyCard view={view} />);
  }

  it("is one muted line, the tools folded behind a chip", () => {
    renderCard([user, cut("a-1", SCREENSHOT_TOOLS), message("a-2", { content: "Done.", resumed_from: note(1) })]);
    const card = screen.getByTestId("interrupted-reply-card");
    expect(card.dataset.state).toBe("resumed");
    expect(screen.getByTestId("interrupted-reply-card-text").textContent).toBe(
      "Interrupted by a platform restart, resumed by itself",
    );
    expect(screen.queryByTestId("interrupted-reply-card-details")).toBeNull();
    // No stored Markdown recap, no bulleted list.
    expect(card.textContent).not.toContain("Completed tool calls");
    expect(card.querySelector("li")).toBeNull();
  });

  it("unfolds the tools as chips with counts, the last completed one marked, and folds again", () => {
    renderCard([user, cut("a-1", SCREENSHOT_TOOLS)], note(1));
    const toggle = screen.getByTestId("interrupted-reply-card-toggle");
    expect(toggle.textContent).toContain("22 tool calls");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    const chips = screen.getAllByTestId("interrupted-reply-card-tool");
    expect(chips).toHaveLength(8);
    expect(chips.find((c) => c.textContent?.startsWith("update_agent"))?.textContent).toBe("update_agent×14");
    const last = chips.filter((c) => c.dataset.last);
    expect(last.map((c) => c.textContent)).toEqual(["consult_agent"]);
    fireEvent.click(toggle);
    expect(screen.queryByTestId("interrupted-reply-card-details")).toBeNull();
  });

  it("keeps a warning, with Resume and Dismiss on the card, when the turn could not resume by itself", () => {
    const onResume = vi.fn();
    const onDismiss = vi.fn();
    const groups = groupCutReplies([user, cut("a-1", ["load_tools"])]);
    const [view] = [...cutGroupViews(groups, { live: null, queued: false }).values()];
    renderWithProviders(<InterruptedReplyCard view={view} onResume={onResume} onDismiss={onDismiss} />);
    const card = screen.getByTestId("interrupted-reply-card");
    expect(card.dataset.state).toBe("stopped");
    expect(screen.getByTestId("interrupted-reply-card-text").textContent).toBe(
      "Interrupted by a platform restart, could not resume by itself",
    );
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("offers no action while the resume is the platform's", () => {
    const groups = groupCutReplies([user, cut("a-1", ["load_tools"])]);
    const [view] = [...cutGroupViews(groups, { live: null, queued: true }).values()];
    renderWithProviders(<InterruptedReplyCard view={view} onResume={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByTestId("interrupted-reply-card-text").textContent).toBe(
      "Interrupted by a platform restart, resuming by itself",
    );
    expect(screen.queryByTestId("interrupted-reply-card-actions")).toBeNull();
  });

  it("reads resumed by you, never by itself, after the person's message", () => {
    renderCard([
      user,
      cut("a-1", ["load_tools"]),
      message("u-2", { role: "user", content: "Continue" }),
      message("a-2", { content: "Done." }),
    ]);
    const text = screen.getByTestId("interrupted-reply-card-text").textContent;
    expect(text).toBe("Interrupted by a platform restart, resumed by you");
    expect(text).not.toContain("by itself");
  });

  it("collapses to a muted line once dismissed", () => {
    const groups = groupCutReplies([user, cut("a-1", ["load_tools"])]);
    const [view] = [...cutGroupViews(groups, { live: null, queued: false, served: false }).values()];
    renderWithProviders(<InterruptedReplyCard view={view} onResume={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByTestId("interrupted-reply-card").dataset.state).toBe("dismissed");
    expect(screen.getByTestId("interrupted-reply-card-text").textContent).toBe(
      "Interrupted by a platform restart, not resumed",
    );
    expect(screen.queryByTestId("interrupted-reply-card-actions")).toBeNull();
  });

  it("wraps the status line in a narrow column instead of cutting it", () => {
    const groups = groupCutReplies([user, cut("a-1", SCREENSHOT_TOOLS), message("a-2", { content: "Done.", resumed_from: note(2) })]);
    const [view] = [...cutGroupViews(groups, { live: null, queued: false }).values()];
    renderWithProviders(
      <div style={{ width: 262 }}>
        <InterruptedReplyCard view={view} />
      </div>,
    );
    const text = screen.getByText("Interrupted by a platform restart, resumed by itself");
    expect(text.dataset.testid).toBe("interrupted-reply-card-text");
    // The whole line is in the visible text, never moved into a title tooltip no keyboard reaches.
    expect(text.textContent).toBe("Interrupted by a platform restart, resumed by itself");
    expect(text.getAttribute("title")).toBeNull();
    expect(text.closest("[title]")).toBeNull();
  });

  it("draws several cuts of one turn as one card, no counter", () => {
    const messages = [user, cut("a-1", ["load_tools"]), cut("a-2", ["update_agent"], { resumed_from: note(1) })];
    renderCard(messages, note(2));
    expect(screen.getAllByTestId("interrupted-reply-card")).toHaveLength(1);
    expect(screen.getByTestId("interrupted-reply-card-text").textContent).toBe(
      "Interrupted by a platform restart, resuming by itself",
    );
    expect(screen.getByTestId("interrupted-reply-card-toggle").textContent).toContain("2 tool calls");
  });
});
