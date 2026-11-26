// What a work item says to the person reading it: who acts on it and
// whether anything will, where it came from, why they see it, and the
// confirmed ways to close it. Never a placeholder where an agent should be.

import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { IntlProvider } from "react-intl";
import { MemoryRouter } from "react-router-dom";

import { ToastProvider } from "@/components/ui/toast";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import { answeredKeepsWaiting, canClose, canRequeue, pullRequestsOf, visiblePayload, waitsForSomeone } from "@/lib/work-item-context";
import type { WorkItem, WorkItemContext } from "@/types";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "Europe/Paris" }, isAdmin: false }),
}));

const workItemQuery = vi.hoisted(() => ({ current: { data: undefined as unknown, isLoading: false, isError: false } }));
const complete = vi.hoisted(() => ({ mutateAsync: vi.fn(async () => ({})), isPending: false }));
const answerMutation = vi.hoisted(() => ({ mutateAsync: vi.fn(async () => ({})), isPending: false }));
const askMutation = vi.hoisted(() => ({
  mutateAsync: vi.fn(async () => ({ item: {}, agent: { id: "agent-1", name: "Notion Agent" }, via: "run" })),
  isPending: false,
}));
vi.mock("@/hooks/api/useWorkItemApi", () => ({
  useWorkItem: () => workItemQuery.current,
  useCompleteWorkItem: () => complete,
  useAnswerWorkItem: () => answerMutation,
  useAskWorkItem: () => askMutation,
  useCancelScheduledWorkItem: () => ({ mutate: vi.fn(), isPending: false }),
  useRescheduleScheduledWorkItem: () => ({ mutate: vi.fn(), isPending: false }),
}));

const { WorkItemDetailDialog } = await import("./WorkItemDetailDialog");
const { WorkItemCloseDialog } = await import("./WorkItemCloseDialog");
const { WorkItemPullRequestChips, WorkItemBlockedWhy } = await import("./WorkItemContextParts");

const OWNER = { id: "agent-1", name: "Notion Agent", icon: "bot", accessible: true };

function context(overrides: Partial<WorkItemContext> = {}): WorkItemContext {
  return {
    owner_agent: OWNER,
    created_by_agent: { id: "agent-2", name: "EMT Assistant", icon: "bot", accessible: true },
    claimed_by_agent: null,
    person: { id: "u1", name: "Sam", is_you: true },
    visibility: { reason: "own", workspace: null },
    origin: {
      kind: "chat",
      source: "agent",
      conversation: { id: "conv-1", title: "Notion import" },
      conversation_hidden: false,
      run: null,
      flow_run: null,
    },
    pickup: {
      state: "manual",
      inboxes: [],
      available_at: null,
      auto_cancel_at: "2026-11-03T10:00:00Z",
      purge_at: null,
      dispatch_attempts: 0,
      max_dispatch_attempts: 5,
    },
    ...overrides,
  };
}

function item(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    id: "11111111-2222-3333-4444-555555555555",
    user_id: "u1",
    is_company_managed: false,
    owner_agent_id: "agent-1",
    created_by_agent_id: "agent-2",
    title: "Load page records into the Notion Marketing Graph knowledge base",
    description: "Read the 120 pages and store each one.",
    payload: { _dispatch_attempts: 0, database_id: "db-1" },
    status: "open",
    priority: "high",
    correlation_key: "notion-marketing-graph",
    due_at: null,
    claimed_by_agent_id: null,
    claimed_at: null,
    source: "agent",
    result: null,
    is_stale: false,
    scheduled: null,
    last_activity_at: "2026-10-20T10:00:00Z",
    created_at: "2026-10-20T10:00:00Z",
    updated_at: "2026-10-20T10:00:00Z",
    context: context(),
    ...overrides,
  };
}

function openDialog(data: WorkItem, actions = {}, onClose: () => void = () => {}) {
  workItemQuery.current = { data, isLoading: false, isError: false };
  return renderWithProviders(
    <MemoryRouter>
      <WorkItemDetailDialog itemId={data.id} onClose={onClose} actionsFor={() => actions} />
    </MemoryRouter>,
  );
}

describe("the files a work item carries (#12092)", () => {
  const capture = { file_id: "f-1", filename: "round-2-dark.png", content_type: "image/png", size: 2048 };
  const brief = { file_id: "f-2", filename: "brief.md", content_type: "text/markdown", size: 300 };

  it("lists each file in the order attached, with its type, size and a download button", () => {
    openDialog(item({ attachments: { files: [capture, brief], gone: 0 } }));
    const files = screen.getByTestId("work-item-files");
    const rows = within(files).getAllByRole("listitem");
    expect(rows.map((row) => row.getAttribute("data-file-id"))).toEqual(["f-1", "f-2"]);
    expect(rows[0].textContent).toContain("round-2-dark.png");
    expect(rows[0].textContent).toContain("PNG");
    expect(rows[0].textContent).toContain("2.0 KB");
    expect(within(rows[0]).getByRole("button", { name: "Download round-2-dark.png" })).toBeTruthy();
    expect(screen.queryByTestId("work-item-files-gone")).toBeNull();
  });

  it("counts a file no longer stored with a plural, one included", () => {
    openDialog(item({ attachments: { files: [capture], gone: 1 } }));
    expect(screen.getByTestId("work-item-files-gone").textContent).toBe("1 attached file is no longer stored.");
    cleanup();
    openDialog(item({ attachments: { files: [], gone: 3 } }));
    expect(screen.getByTestId("work-item-files-gone").textContent).toBe("3 attached files are no longer stored.");
  });

  it("cuts a long name to its row with one tab stop per row (UX r1 B1 on #12103)", () => {
    const long = { ...capture, file_id: "f-3", filename: `${"capture-of-the-work-item-dialog-".repeat(6)}dark.png` };
    const width = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(900);
    const client = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(300);
    try {
      openDialog(item({ attachments: { files: [long, brief], gone: 0 } }));
      const rows = within(screen.getByTestId("work-item-files")).getAllByRole("listitem");
      const name = within(rows[0]).getByText(long.filename);
      expect(name.hasAttribute("data-cut-text")).toBe(true);
      for (const row of rows) {
        // The cut name and the Download button: two stops, never a second wrapper around the name.
        const stops = row.querySelectorAll("[tabindex='0'], button");
        expect(stops.length).toBeLessThanOrEqual(2);
      }
      expect(rows[0].querySelectorAll("[tabindex='0']").length).toBe(1);
    } finally {
      width.mockRestore();
      client.mockRestore();
    }
  });

  it("shows no Files section on an item without files", () => {
    openDialog(item({ attachments: { files: [], gone: 0 } }));
    expect(screen.queryByTestId("work-item-files")).toBeNull();
    cleanup();
    openDialog(item());
    expect(screen.queryByTestId("work-item-files")).toBeNull();
  });
});

describe("the work item detail", () => {
  it("names the owner, says nothing will pick it up, and links the chat it came from", () => {
    openDialog(item());
    const detail = screen.getByTestId("work-item-detail");
    expect(screen.getAllByText("Notion Agent")[0].closest("a")?.getAttribute("href")).toBe("/agents/agent-1");
    expect(detail.textContent).toContain(
      "No assignment or flow drains the queue of Notion Agent, so nothing picks this item up automatically",
    );
    expect(detail.textContent).toContain("If nobody acts on it, it is cancelled automatically around");
    expect(screen.getByText("Notion import").closest("a")?.getAttribute("href")).toBe("/chat?c=conv-1");
    expect(detail.textContent).toContain("It was created for you");
    // The full description, and the payload without the platform's bookkeeping.
    expect(detail.textContent).toContain("Read the 120 pages and store each one.");
    expect(detail.textContent).toContain('"database_id": "db-1"');
    expect(detail.textContent).not.toContain("_dispatch_attempts");
  });

  it("names the agents its completion is routed to, what it changes and what to do", () => {
    const ea = { id: "agent-9", name: "Executive Assistant", icon: "bot", accessible: true };
    openDialog(
      item({
        context: context({
          fleet_impact: { agents: [ea], kind: "skill", instructions: "Rewrite section 20.", inferred: false },
        }),
      }),
    );
    const impact = screen.getByTestId("work-item-fleet-impact");
    expect(within(impact).getByText("Executive Assistant").closest("a")?.getAttribute("href")).toBe("/agents/agent-9");
    expect(impact.textContent).toContain("Skill");
    expect(impact.textContent).toContain("Rewrite section 20.");
    expect(impact.textContent).toContain("Its completion is routed to these agents too.");
  });

  it("says an impact read from a follow-through section goes to the creator, and shows nothing without one", () => {
    const ea = { id: "agent-2", name: "EMT Assistant", icon: "bot", accessible: true };
    openDialog(
      item({ context: context({ fleet_impact: { agents: [ea], kind: "capability", instructions: null, inferred: true } }) }),
    );
    expect(screen.getByTestId("work-item-fleet-impact").textContent).toContain(
      "Read from the follow-through section of its description: its completion is routed to its creator.",
    );
    cleanup();
    openDialog(item());
    expect(screen.queryByTestId("work-item-fleet-impact")).toBeNull();
  });

  it("never prints a placeholder for an agent the reader cannot open", () => {
    const hidden = { id: null, name: null, icon: null, accessible: false };
    openDialog(item({ context: context({ owner_agent: hidden, created_by_agent: hidden }) }));
    expect(screen.getAllByText("an agent you cannot open").length).toBeGreaterThan(0);
    // The label sits alone in its own element beside the lock icon, so a narrow
    // column ellipsizes it like a linked agent's name instead of clipping it
    // mid-word.
    for (const label of screen.getAllByText("an agent you cannot open")) {
      expect(label.getAttribute("data-testid")).toBe("work-item-agent-hidden-label");
      expect(label.querySelector("svg")).toBeNull();
      expect(label.parentElement?.querySelector("svg")).not.toBeNull();
    }
    expect(screen.queryByText(/owner agent$/)).toBeNull();
    expect(screen.getByTestId("work-item-detail").textContent).toContain(
      "No assignment or flow drains the queue of an agent you cannot open",
    );
  });

  it("names a long agent the reader cannot open in full in its tooltip", async () => {
    const longName = "Digital Security Office desk that triages every policy, watch and detection hand-off";
    const hidden = { id: "agent-9", name: longName, icon: null, accessible: false };
    openDialog(item({ context: context({ owner_agent: hidden }) }));
    const label = screen.getAllByTestId("work-item-agent-hidden-label").find((node) => node.textContent === longName);
    expect(label).toBeDefined();
    fireEvent.focus(label?.closest("[tabindex]") as HTMLElement);
    const tip = await screen.findByRole("tooltip");
    expect(tip.textContent).toContain(longName);
    expect(tip.textContent).toContain("This agent is not shared with you");
  });

  it("names the inbox that takes the item, and the one whose filters leave it out", () => {
    openDialog(
      item({
        context: context({
          pickup: {
            ...context().pickup,
            state: "automatic",
            inboxes: [
              { kind: "assignment", id: "as-1", name: "Notion inbox", accessible: true, accepts: true, priority_min: null, correlation_prefix: null },
              { kind: "flow", id: null, name: null, accessible: false, accepts: false, priority_min: "urgent", correlation_prefix: null },
            ],
          },
        }),
      }),
    );
    const [taken, leftOut] = screen.getAllByTestId("work-item-inbox");
    expect(taken.querySelector("a")?.getAttribute("href")).toBe("/agents/assignments/as-1");
    expect(taken.textContent).toContain("takes this item");
    expect(leftOut.textContent).toContain("a flow you cannot open");
    expect(leftOut.textContent).toContain("leaves this item out");
    expect(leftOut.textContent).toContain("priority Urgent and above");
  });

  it("closes an item only through the page's confirmed actions", () => {
    const close = vi.fn();
    openDialog(item(), { close, requeue: vi.fn() });
    fireEvent.click(screen.getByText("Mark done"));
    expect(close).toHaveBeenCalledWith(expect.objectContaining({ id: item().id }), "done");
    fireEvent.click(screen.getByText("Cancel work item"));
    expect(close).toHaveBeenLastCalledWith(expect.objectContaining({ id: item().id }), "cancelled");
    // An open, unclaimed item is already in the queue.
    expect(screen.queryByText("Put back in the queue")).toBeNull();
  });

  // #1845: an item the reader may read but not change keeps its write
  // actions visible, disabled, with the reason the server gives.
  it("disables the write actions on an item the reader cannot change, with why", () => {
    const close = vi.fn();
    const remove = vi.fn();
    const reason = "Read-only for you: it was created in another person's chat.";
    openDialog(item({ can_write: false, read_only_reason: reason }), { close, remove, requeue: vi.fn() });
    for (const name of ["Mark done", "Cancel work item", "Delete"]) {
      const button = screen.getByRole("button", { name });
      expect((button as HTMLButtonElement).disabled).toBe(true);
      expect(button.closest("[aria-disabled='true']")).not.toBeNull();
    }
    fireEvent.click(screen.getByText("Cancel work item"));
    expect(close).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
  });

  // UX round 1 of #1850, B1: the unassigned callout never asks a read-only
  // reader to complete the item the dialog refuses to let them change.
  it("tells a read-only reader of an unassigned item who can act on it", () => {
    const unassigned = (can_write: boolean) =>
      item({
        can_write,
        read_only_reason: can_write ? null : "Read-only for you.",
        context: context({ owner_agent: null, pickup: { ...context().pickup, state: "unassigned" } }),
      });
    const { unmount } = openDialog(unassigned(false), { close: vi.fn() });
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("Complete it yourself");
    expect(text).toContain("Someone who can change it must complete it or assign an agent.");
    unmount();
    openDialog(unassigned(true), { close: vi.fn() });
    expect(document.body.textContent).toContain("Complete it yourself, or ask an agent to take it over.");
  });

  // #1910: the decision recipient may cancel, and nothing else.
  it("enables Cancel alone for the decision recipient", () => {
    const close = vi.fn();
    const reason = "Read-only for you: it belongs to another person.";
    openDialog(item({ can_write: false, can_cancel: true, read_only_reason: reason }), { close, remove: vi.fn() });
    expect((screen.getByRole("button", { name: "Cancel work item" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByText("Cancel work item"));
    expect(close).toHaveBeenCalledWith(expect.objectContaining({ can_cancel: true }), "cancelled");
  });

  it("keeps the write actions enabled when the reader may change the item", () => {
    openDialog(item({ can_write: true, read_only_reason: null }), { close: vi.fn() });
    expect((screen.getByRole("button", { name: "Cancel work item" }) as HTMLButtonElement).disabled).toBe(false);
  });

  // UX round 1 of #9890, B1: an always-enabled button is never wrapped in a
  // focusable span that reports it disabled (two tab stops, refused clicks).
  it("leaves the always-enabled actions out of any aria-disabled wrapper", () => {
    openDialog(item(), { close: vi.fn(), requeue: vi.fn() });
    for (const name of ["Mark done", "Cancel work item"]) {
      expect(screen.getByRole("button", { name }).closest("[aria-disabled='true']")).toBeNull();
    }
    const askButtons = screen.queryAllByTestId(/^work-item-ask-agent-(explain|recheck|solved|do)$/);
    for (const button of askButtons) {
      expect(button.closest("[aria-disabled='true']")).toBeNull();
    }
  });

  // B2: the stale badge names its cause, the tooltip keeps the sentence.
  it("names the stale cause on the badge itself", () => {
    openDialog(item({ is_stale: true, stale_reason: "past_due" }), {});
    expect(screen.getByTestId("work-item-stale-badge").textContent).toContain("Past due");
  });

  describe("the decision an item waits for (#9437)", () => {
    const ask = {
      question: "Which pricing tier ships first?",
      options: [
        { key: "A", label: "Starter", effect: "the starter tier ships first" },
        { key: "B", label: "Enterprise" },
      ],
      recommendation: "A",
      deadline: "2026-11-08T09:00:00Z",
    };
    const waiting = (overrides: Partial<WorkItem> = {}) =>
      item({ status: "blocked", needs_human: true, blocked_reason: "pick a tier", description: "run notes", ...overrides });
    afterEach(() => {
      cleanup();
      answerMutation.mutateAsync.mockClear();
    });

    it("leads with the question, one button per option, and folds the description", () => {
      openDialog(waiting({ ask }), { answer: true });
      const card = screen.getByTestId("work-item-decision");
      expect(card.textContent).toContain("Which pricing tier ships first?");
      expect(card.textContent).toContain("the starter tier ships first");
      expect(card.textContent).toContain("Recommended");
      expect(within(card).getByTestId("work-item-decision-deadline")).toBeTruthy();
      const buttons = within(card).getAllByTestId("work-item-decision-option");
      const labels = within(card).getAllByTestId("work-item-decision-option-label");
      expect(labels.map((b) => b.textContent)).toEqual(["Starter", "Enterprise"]);
      // The card comes before the facts and the description is folded.
      const detail = screen.getByTestId("work-item-detail");
      expect(detail.firstElementChild).toBe(card);
      expect(screen.getByTestId("work-item-description-folded").hasAttribute("open")).toBe(false);
      fireEvent.click(buttons[1]);
      expect(answerMutation.mutateAsync).toHaveBeenCalledWith({ id: item().id, option: "B", text: undefined });
    });

    it("takes a free-text answer on a legacy item, its blocked reason as the question", () => {
      openDialog(waiting(), { answer: true });
      const card = screen.getByTestId("work-item-decision");
      expect(card.textContent).toContain("pick a tier");
      expect(within(card).queryAllByTestId("work-item-decision-option")).toHaveLength(0);
      const send = within(card).getByRole("button", { name: /Send answer/ });
      expect((send as HTMLButtonElement).disabled).toBe(true);
      fireEvent.change(within(card).getByRole("textbox"), { target: { value: "production" } });
      fireEvent.click(send);
      expect(answerMutation.mutateAsync).toHaveBeenCalledWith({ id: item().id, option: undefined, text: "production" });
    });

    it("shows the question without answer controls to a reader who may not answer", () => {
      openDialog(waiting({ ask }), {});
      const card = screen.getByTestId("work-item-decision");
      for (const button of within(card).getAllByTestId("work-item-decision-option")) {
        expect((button as HTMLButtonElement).disabled).toBe(true);
      }
      expect(card.textContent).toContain("Only an administrator can answer a company-managed work item.");
    });

    it("shows an option label at the 120 character cap whole, inside its button", () => {
      const label = "L".repeat(119) + "Z";
      openDialog(waiting({ ask: { question: "Pick one", options: [{ key: "A", label }] } }), { answer: true });
      const [button] = within(screen.getByTestId("work-item-decision")).getAllByTestId("work-item-decision-option");
      expect(button.textContent).toBe(label);
      expect(within(button).getByText(label)).toBeTruthy();
    });

    // #834: sources as buttons, Markdown context, recommended option first.
    it("shows the ask's sources as links, its context as Markdown and the recommended option first", () => {
      const jira = "https://example.atlassian.net/browse/FIN-798";
      openDialog(
        waiting({
          ask: {
            ...ask,
            recommendation: "B",
            context: "Legal approves with **changes**.\nSee [the review](https://example.com/review).",
            links: [{ label: "FIN-798", url: jira }],
          },
        }),
        { answer: true },
      );
      const card = screen.getByTestId("work-item-decision");
      const link = within(card).getByTestId("work-item-decision-link") as HTMLAnchorElement;
      expect(link.textContent).toBe("FIN-798");
      expect(link.getAttribute("href")).toBe(jira);
      expect(link.getAttribute("target")).toBe("_blank");
      const context = within(card).getByTestId("work-item-decision-context");
      expect(context.querySelector("strong")?.textContent).toBe("changes");
      expect(context.querySelector("a")?.getAttribute("href")).toBe("https://example.com/review");
      expect(context.querySelectorAll("p")).toHaveLength(2);
      const buttons = within(card).getAllByTestId("work-item-decision-option");
      expect(within(card).getAllByTestId("work-item-decision-option-label").map((b) => b.textContent)).toEqual([
        "Enterprise",
        "Starter",
      ]);
      expect(buttons[0].closest("[data-testid=work-item-decision-tile]")?.getAttribute("data-recommended")).toBe("true");
      expect(within(card).queryByTestId("work-item-decision-fallback")).toBeNull();
    });

    // #842, J.1: every option a full-width tile, label then effect, one Recommended pill.
    it("renders every option as a full-width tile of equal width, label over effect", () => {
      openDialog(
        waiting({
          ask: {
            question: "Pick one",
            options: [
              { key: "A", label: "Go", effect: "Recommended: posts the prepared comment now." },
              { key: "B", label: "Edit first", effect: "opens the comment for changes" },
              { key: "C", label: "No" },
            ],
            recommendation: "A",
          },
        }),
        { answer: true },
      );
      const card = screen.getByTestId("work-item-decision");
      expect(within(card).getByTestId("work-item-decision-options").className).toContain("grid-cols-1");
      const tiles = within(card).getAllByTestId("work-item-decision-option");
      expect(tiles).toHaveLength(3);
      // Equal widths: every tile is a block button spanning its single-column cell.
      const widths = new Set(tiles.map((tile) => tile.className.split(" ").filter((c) => /^(w-|max-w-|flex-1|shrink)/.test(c)).join(" ")));
      expect([...widths]).toEqual(["w-full"]);
      for (const tile of tiles) expect(tile.tagName).toBe("BUTTON");
      // One pill, on the recommended tile only; the agent's own "Recommended:" prefix is dropped.
      expect(within(card).getAllByTestId("work-item-decision-recommended")).toHaveLength(1);
      const effects = within(card).getAllByTestId("work-item-decision-effect");
      expect(effects[0].textContent).toBe("posts the prepared comment now.");
      expect(card.textContent?.match(/Recommended/g)).toHaveLength(1);
      // The effect is the tile's description for a screen reader.
      expect(tiles[0].getAttribute("aria-describedby")).toBe(effects[0].id);
    });

    it("clamps a clipped effect to 3 lines with Show more, outside the tile button", () => {
      const effect = "This option does a long list of things the reader may want in full. ".repeat(5);
      // The clamped span is cut: 72 px of text in a 54 px box.
      const height = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(72);
      const client = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(54);
      try {
        openDialog(waiting({ ask: { question: "Pick one", options: [{ key: "A", label: "Go", effect }, { key: "B", label: "No" }] } }), {
          answer: true,
        });
        const shown = screen.getByTestId("work-item-decision-effect");
        expect(shown.getAttribute("data-clamped")).toBe("true");
        const more = screen.getByRole("button", { name: "Show more" });
        expect(more.closest("[data-testid=work-item-decision-option]")).toBeNull();
        fireEvent.click(more);
        expect(shown.getAttribute("data-clamped")).toBeNull();
        expect(screen.getByRole("button", { name: "Show less" }).getAttribute("aria-expanded")).toBe("true");
        expect(answerMutation.mutateAsync).not.toHaveBeenCalled();
      } finally {
        height.mockRestore();
        client.mockRestore();
      }
    });

    it("shows no Show more for a long effect that fits in 3 lines (UX r1 B1 on #973)", () => {
      // 255 characters that fit: scrollHeight equals clientHeight (54 px).
      const effect = "This option does a long list of things the reader may want. ".repeat(5).slice(0, 255);
      const height = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(54);
      const client = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(54);
      try {
        openDialog(waiting({ ask: { question: "Pick one", options: [{ key: "A", label: "Go", effect }, { key: "B", label: "No" }] } }), {
          answer: true,
        });
        expect(screen.getByTestId("work-item-decision-effect").getAttribute("data-clamped")).toBeNull();
        expect(within(screen.getByTestId("work-item-decision")).queryByRole("button", { name: "Show more" })).toBeNull();
      } finally {
        height.mockRestore();
        client.mockRestore();
      }
    });

    it("folds the free-text answer behind Answer differently when the ask has options", () => {
      openDialog(waiting({ ask }), { answer: true });
      const folded = screen.getByTestId("work-item-decision-free-text");
      expect(folded.tagName).toBe("DETAILS");
      expect(folded.hasAttribute("open")).toBe(false);
      expect(within(folded).getByText("Answer differently")).toBeTruthy();
    });

    it("names the agent that asks, when the reader may see it", () => {
      openDialog(
        waiting({
          ask,
          context: context({ owner_agent: { id: "a1", name: "Developer", icon: null, accessible: true } }),
        }),
        { answer: true },
      );
      expect(screen.getByTestId("work-item-decision-asker").textContent).toBe("Asked by Developer");
    });

    it("shows the brief, its links and the incomplete line when the ask is missing, not the bare title", () => {
      openDialog(
        waiting({
          card_fallback: {
            brief: "The People Team asks Legal to review the payroll request.",
            links: [{ label: "FIN-798", url: "https://example.atlassian.net/browse/FIN-798" }],
            missing: ["ask.question"],
          },
        }),
        { answer: true },
      );
      const card = screen.getByTestId("work-item-decision");
      const fallback = within(card).getByTestId("work-item-decision-fallback");
      expect(fallback.textContent).toContain("The People Team asks Legal to review the payroll request.");
      expect(within(card).getByTestId("work-item-decision-incomplete").textContent).toContain("This card is incomplete");
      expect(within(card).getByTestId("work-item-decision-link").textContent).toBe("FIN-798");
    });

    describe("an item on a CTEM case opens the case first (#2370)", () => {
      const CASE_ID = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f";
      const kase = { id: CASE_ID, title: "Patch the VPN gateways", state: "validating", path: `/ctem/cases/${CASE_ID}` };

      it("leads the card with Open the case above the options, in the app, and drops the agent's link to it", () => {
        openDialog(
          waiting({
            correlation_key: `ctem:validation:${CASE_ID}`,
            ctem_case: kase,
            ask: {
              ...ask,
              links: [
                { label: "The case", url: `https://one.example.test/xtm-one/ctem/cases/${CASE_ID}` },
                { label: "FIN-798", url: "https://example.atlassian.net/browse/FIN-798" },
              ],
            },
          }),
          { answer: true },
        );
        const card = screen.getByTestId("work-item-decision");
        const open = within(card).getByTestId("work-item-case-link");
        expect(open.textContent).toContain("Open the case");
        expect(open.getAttribute("href")).toBe(`/ctem/cases/${CASE_ID}`);
        expect(open.getAttribute("target")).toBeNull();
        expect(within(card).getByTestId("work-item-case-title").textContent).toBe("Patch the VPN gateways");
        // Above the options: the link comes first in document order.
        const options = within(card).getByTestId("work-item-decision-options");
        expect(open.compareDocumentPosition(options) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(within(card).getAllByTestId("work-item-decision-link").map((a) => a.textContent)).toEqual(["FIN-798"]);
      });

      it("shows the link on an answered item too, outside the card", () => {
        openDialog(item({ status: "done", correlation_key: `ctem:review:${CASE_ID}`, ctem_case: kase }), {});
        expect(screen.queryByTestId("work-item-decision")).toBeNull();
        expect(screen.getByTestId("work-item-case-link").getAttribute("href")).toBe(`/ctem/cases/${CASE_ID}`);
      });

      it("shows no link when the reader may not read the case, and a non-CTEM item is unchanged", () => {
        openDialog(waiting({ correlation_key: `ctem:validation:${CASE_ID}`, ctem_case: null, ask }), { answer: true });
        expect(screen.queryByTestId("work-item-case-link")).toBeNull();
        cleanup();
        openDialog(waiting({ ask }), { answer: true });
        expect(screen.queryByTestId("work-item-case")).toBeNull();
        expect(screen.getByTestId("work-item-decision-question").textContent).toBe("Which pricing tier ships first?");
      });
    });

    it("asks to confirm a terminal option before sending it, Back sends nothing (#2150)", () => {
      const terminal = {
        question: "Close the case?",
        options: [
          { key: "keep", label: "Keep it open" },
          { key: "cancel", label: "Cancel the case", confirm: true },
        ],
        recommendation: "keep",
      };
      answerMutation.mutateAsync.mockClear();
      openDialog(waiting({ ask: terminal }), { answer: true });
      const card = screen.getByTestId("work-item-decision");
      const cancel = within(card)
        .getAllByTestId("work-item-decision-option")
        .find((b) => b.textContent?.includes("Cancel the case"));
      expect(cancel).toBeTruthy();
      fireEvent.click(cancel!);
      expect(answerMutation.mutateAsync).not.toHaveBeenCalled();
      expect(within(card).getByTestId("work-item-decision-confirm").getAttribute("data-option")).toBe("cancel");
      fireEvent.click(within(card).getByText("Back"));
      expect(within(card).queryByTestId("work-item-decision-confirm")).toBeNull();
      expect(answerMutation.mutateAsync).not.toHaveBeenCalled();
      fireEvent.click(cancel!);
      fireEvent.click(within(card).getByTestId("work-item-decision-confirm-send"));
      expect(answerMutation.mutateAsync).toHaveBeenCalledWith(expect.objectContaining({ option: "cancel" }));
    });

    it.each([
      ["409", "409 Conflict: the work item changed since it was read"],
      ["500", "500 Internal Server Error: database unavailable"],
    ])("shows a translated hint, never the raw backend detail, when the answer fails with a %s", async (_status, detail) => {
      const logged = vi.spyOn(console, "error").mockImplementation(() => {});
      answerMutation.mutateAsync.mockRejectedValueOnce(new Error(detail));
      openDialog(waiting({ ask }), { answer: true });
      fireEvent.click(within(screen.getByTestId("work-item-decision")).getAllByTestId("work-item-decision-option")[0]);
      expect(await screen.findByText("Try again, or reload the item if it changed.")).toBeTruthy();
      expect(screen.getByText("Failed to send the answer")).toBeTruthy();
      expect(screen.queryByText(detail)).toBeNull();
      expect(logged).toHaveBeenCalled();
      logged.mockRestore();
    });

    it("Explain opens the owner agent's chat with the message sent on arrival, and closes the dialog (#774)", () => {
      sessionStorage.clear();
      const onClose = vi.fn();
      openDialog(waiting({ ask }), { answer: true }, onClose);
      const ask_agent = screen.getByTestId("work-item-ask-agent");
      expect(within(ask_agent).getByTestId("work-item-ask-freshness").textContent).toContain("Last activity");
      expect(ask_agent.textContent).toContain("are sent to Notion Agent");
      fireEvent.click(within(ask_agent).getByTestId("work-item-ask-agent-explain"));
      const draft = sessionStorage.getItem("copilot:chat-input-draft:new") ?? "";
      expect(draft).toContain(item().id);
      expect(draft).toContain("Explain in plain words");
      expect(sessionStorage.getItem("copilot:chat-input-draft:agent")).toBe("agent-1");
      expect(sessionStorage.getItem("copilot:chat-input-draft:autosend")).toBe("1");
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it("Re-check is sent to the item's agent, no chat, toast and the dialog closes (#774)", async () => {
      sessionStorage.clear();
      askMutation.mutateAsync.mockClear();
      const onClose = vi.fn();
      openDialog(waiting({ ask }), { answer: true }, onClose);
      fireEvent.click(screen.getByTestId("work-item-ask-agent-recheck"));
      expect(askMutation.mutateAsync).toHaveBeenCalledWith({ id: item().id, action: "recheck", text: undefined });
      expect(await screen.findByText("Sent to Notion Agent")).toBeTruthy();
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(sessionStorage.getItem("copilot:chat-input-draft:new")).toBeNull();
    });

    it("It's solved takes an optional line, then sends it with the action (#774)", async () => {
      askMutation.mutateAsync.mockClear();
      const onClose = vi.fn();
      openDialog(waiting({ ask }), { answer: true }, onClose);
      fireEvent.click(screen.getByTestId("work-item-ask-agent-solved"));
      expect(askMutation.mutateAsync).not.toHaveBeenCalled();
      const form = screen.getByTestId("work-item-ask-solved-form");
      fireEvent.change(within(form).getByRole("textbox"), { target: { value: "key rotated" } });
      fireEvent.click(screen.getByTestId("work-item-ask-solved-send"));
      expect(askMutation.mutateAsync).toHaveBeenCalledWith({ id: item().id, action: "solved", text: "key rotated" });
      await screen.findByText("Sent to Notion Agent");
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it("disables the sends while one waits for the agent, and says so", () => {
      const at = new Date().toISOString();
      openDialog(waiting({ ask, payload: { _ask: { at } }, last_activity_at: at }), { answer: true });
      expect((screen.getByTestId("work-item-ask-agent-recheck") as HTMLButtonElement).disabled).toBe(true);
      expect(screen.getByTestId("work-item-ask-effect").textContent).toContain("Already sent, waiting for Notion Agent");
    });

    it("a decision option answer closes the dialog (#774)", async () => {
      const onClose = vi.fn();
      openDialog(waiting({ ask }), { answer: true }, onClose);
      fireEvent.click(within(screen.getByTestId("work-item-decision")).getAllByTestId("work-item-decision-option")[0]);
      await vi.waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    });

    it("sends to the agent it cannot open, without Explain, and says why", () => {
      const hidden = { id: "agent-9", name: "Desk", icon: null, accessible: false };
      openDialog(waiting({ ask, context: context({ owner_agent: hidden, created_by_agent: hidden }) }), { answer: true });
      const ask_agent = screen.getByTestId("work-item-ask-agent");
      expect(screen.queryByTestId("work-item-ask-agent-explain")).toBeNull();
      expect(screen.getByTestId("work-item-ask-agent-recheck")).toBeTruthy();
      expect(ask_agent.textContent).toContain("so Explain is not offered");
      cleanup();
      openDialog(waiting({ ask, context: context({ owner_agent: null, created_by_agent: null }) }), { answer: true });
      const no_agent = screen.getByTestId("work-item-ask-agent");
      expect(within(no_agent).queryAllByRole("button")).toHaveLength(0);
      expect(no_agent.textContent).toContain("No agent owns or created this work item, so there is no agent to ask.");
      expect(no_agent.textContent).not.toContain("You cannot open");
    });

    it("draws no card on an item that waits for nobody, and keeps its description open", () => {
      openDialog(item(), { answer: true });
      expect(screen.queryByTestId("work-item-decision")).toBeNull();
      expect(screen.queryByTestId("work-item-description-folded")).toBeNull();
      expect(screen.getByTestId("work-item-detail").textContent).toContain("Read the 120 pages and store each one.");
    });
  });

  it("names the run holding the claim, links it, and says it is alive", () => {
    const runId = "2f8112bc-e184-4698-9e2b-49b6b30b0e85";
    openDialog(
      item({
        status: "in_progress",
        claimed_by_run_id: runId,
        claimed_by_run: { id: runId, kind: "run", status: "running", alive: true },
        context: context({ pickup: { ...context().pickup, state: "in_progress" } }),
      }),
    );
    const holder = screen.getByTestId("work-item-holding-run");
    expect(holder.textContent).toContain("run 2f8112bc");
    expect(holder.textContent).toContain("alive");
    expect(holder.querySelector("a")?.getAttribute("href")).toBe(`/operations/executions/${runId}`);
  });

  it("says the holding run ended with its status, and shows the latest claim note", () => {
    const runId = "f7db27b2-8d27-4090-9a9d-8abd5d90cba4";
    openDialog(
      item({
        status: "claimed",
        claimed_by_run_id: runId,
        claimed_by_run: { id: runId, kind: "background_task", status: "failed", alive: false },
        claim_notes: [
          { at: "2026-10-31T18:54:38Z", text: "released: run f062a614 ended failed" },
          { at: "2026-10-31T19:07:00Z", text: "released: run f7db27b2 ended failed" },
        ],
      }),
    );
    const holder = screen.getByTestId("work-item-holding-run");
    expect(holder.textContent).toContain("run f7db27b2");
    expect(holder.textContent).toContain("ended (failed)");
    // A background task has no run page: named, not linked.
    expect(holder.querySelector("a")).toBeNull();
    const note = screen.getByTestId("work-item-claim-note");
    expect(note.textContent).toContain("released: run f7db27b2 ended failed");
    expect(note.textContent).not.toContain("f062a614");
  });

  it("names the items a blocked item waits for, linked, and never says it is not picked up", () => {
    const blockerId = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
    openDialog(
      item({
        status: "blocked",
        blocked_on: [blockerId, "hidden-id"],
        context: context({
          pickup: { ...context().pickup, state: "blocked_on_items", auto_cancel_at: null },
          blocked_on: [
            { id: blockerId, title: "Ship the migration", status: "in_progress", accessible: true },
            { id: null, title: null, status: null, accessible: false },
          ],
        }),
      }),
    );
    // Both still open, none cancelled: a waiting item.
    expect(screen.getByTestId("work-item-pickup").textContent).toContain("Waits for 2 items");
    expect(screen.queryByText(/not picked up/)).toBeNull();
    const blockers = screen.getByTestId("work-item-blockers");
    const link = blockers.querySelector("a");
    expect(link?.textContent).toContain("Ship the migration");
    expect(link?.getAttribute("href")).toBe(`/tasks/work-items/${blockerId}`);
    expect(blockers.textContent).toContain("a work item you cannot open");
    expect(waitsForSomeone("blocked_on_items")).toBe(false);
  });

  // An item that waits for open dependencies says so in one word, and
  // its footer offers what its row offers: no Mark done, Cancel kept.
  it("reads Waits for N items and hides Mark done on an item waiting for open dependencies", () => {
    const close = vi.fn();
    openDialog(
      item({
        status: "blocked",
        blocked_on: ["b1", "b2"],
        context: context({
          pickup: { ...context().pickup, state: "blocked_on_items", auto_cancel_at: null },
          blocked_on: [
            { id: "b1", title: "Ship the migration", status: "in_progress", accessible: true },
            { id: "b2", title: "Write the notes", status: "done", accessible: true },
          ],
        }),
      }),
      { close },
    );
    expect(screen.getByTestId("work-item-pickup").textContent).toContain("Waits for 1 item");
    expect(screen.getByTestId("work-item-pickup").textContent).not.toContain("Blocked");
    // UX B1: the "Waits for" list shows what the badge counts, the done one left out.
    const blockers = screen.getByTestId("work-item-blockers");
    expect(blockers.querySelectorAll("a")).toHaveLength(1);
    expect(blockers.textContent).toContain("Ship the migration");
    expect(blockers.textContent).not.toContain("Write the notes");
    expect(screen.queryByText("Mark done")).toBeNull();
    fireEvent.click(screen.getByText("Cancel work item"));
    expect(close).toHaveBeenCalledWith(expect.objectContaining({ status: "blocked" }), "cancelled");
  });

  // #861: an item that waits only for references is released by the platform:
  // never "Blocked: not picked up", one sentence per kind it waits for.
  it("reads Waiting for a change on an item waiting for references, with one sentence per kind", () => {
    openDialog(
      item({
        status: "blocked",
        blocked_on_refs: [
          { repo: "o/r", kind: "pull_request", number: 7, release_on: "merged_or_closed" },
          { repo: "o/r", kind: "deployment", number: 7, release_on: "deployed" },
          { repo: "o/r", kind: "workflow_run", workflow: "deploy.yml", release_on: "succeeded" },
          { repo: "o/r", kind: "issue", number: 8, release_on: "closed", state: "closed" },
        ],
        context: context({ pickup: { ...context().pickup, state: "waiting_for_refs", auto_cancel_at: null } }),
      }),
      { close: vi.fn() },
    );
    expect(screen.getByTestId("work-item-pickup").textContent).toContain("Waiting for a change");
    expect(screen.queryByText(/not picked up/)).toBeNull();
    expect(screen.getByTestId("work-item-blocked-status").getAttribute("data-kind")).toBe("waiting");
    // The closed issue is satisfied: its sentence is left out.
    const kinds = screen.getAllByTestId("work-item-ref-release").map((el) => el.getAttribute("data-kind"));
    expect(kinds).toEqual(["pull_request", "deployment", "workflow_run"]);
    const why = screen.getByTestId("work-item-blocked-why");
    expect(why.querySelector('[data-blocked-ref="o/r: deploy.yml"]')).toBeTruthy();
    expect(why.querySelector('[data-blocked-ref="o/r#8"]')).toBeNull();
    expect(screen.queryByText("Mark done")).toBeNull();
  });

  // #842, J.2: one sentence per kind, and the pull request sentence follows release_on.
  it("says merged, not merged or closed, when every pull request waits for its merge", () => {
    openDialog(
      item({
        status: "blocked",
        blocked_on_refs: [
          { repo: "o/r", kind: "pull_request", number: 7, release_on: "merged" },
          { repo: "o/r", kind: "pull_request", number: 9, release_on: "merged" },
        ],
        context: context({ pickup: { ...context().pickup, state: "waiting_for_refs", auto_cancel_at: null } }),
      }),
      { close: vi.fn() },
    );
    const sentences = screen.getAllByTestId("work-item-ref-release");
    expect(sentences).toHaveLength(1);
    expect(sentences[0].textContent).toBe("A pull request releases it once merged.");
    const why = screen.getByTestId("work-item-blocked-why");
    expect(why.querySelector('[data-blocked-ref="o/r#7"]')).toBeTruthy();
    expect(why.querySelector('[data-blocked-ref="o/r#9"]')).toBeTruthy();
  });

  it("reads Waiting for a decision on an item waiting for a person, Mark done kept", () => {
    openDialog(
      item({
        status: "blocked",
        needs_human: true,
        context: context({ pickup: { ...context().pickup, state: "waiting_for_person", auto_cancel_at: null } }),
      }),
      { close: vi.fn() },
    );
    expect(screen.getByTestId("work-item-pickup").textContent).toContain("Waiting for a decision");
    expect(screen.queryByText(/not picked up/)).toBeNull();
    expect(screen.getByText("Mark done")).toBeTruthy();
  });

  // #2500 UX round 1 B1: after a keeps_waiting answer "Who acts on it" no
  // longer says it waits for a decision nor goes back once the card is answered.
  it("says the answer is recorded in Who acts on it after a keeps_waiting answer", () => {
    const ask = {
      question: "Ship it?",
      options: [
        { key: "ship", label: "Ship", effect: "ships" },
        { key: "later", label: "Later", effect: "waits", keeps_waiting: true },
      ],
      recommendation: "ship",
    };
    const waiting = {
      status: "blocked" as const,
      needs_human: true,
      ask,
      context: context({ pickup: { ...context().pickup, state: "waiting_for_person" as const, auto_cancel_at: null } }),
    };
    expect(answeredKeepsWaiting(item(waiting as Partial<WorkItem>))).toBe(false);
    const answeredItem = item({
      ...waiting,
      payload: { answer: { option: "LATER", label: "Later", answered_by: "Ada", answered_at: "2026-11-26T09:00:00Z" } },
    } as Partial<WorkItem>);
    expect(answeredKeepsWaiting(answeredItem)).toBe(true);
    // Any other option, or a finished item, is not a recorded keeps_waiting answer.
    expect(answeredKeepsWaiting({ ...answeredItem, payload: { answer: { option: "ship", answered_at: "2026-11-26T09:00:00Z" } } })).toBe(false);
    expect(answeredKeepsWaiting({ ...answeredItem, status: "done" })).toBe(false);
    openDialog(answeredItem, { close: vi.fn() });
    const pickup = screen.getByTestId("work-item-pickup");
    expect(pickup.textContent).toContain("Answer recorded");
    expect(pickup.textContent).not.toContain("Waiting for a decision");
    expect(screen.queryByText(/goes back to .* once the decision card is answered/)).toBeNull();
    expect(screen.getByText(/Your answer is recorded; it keeps waiting until .* asks again\./)).toBeTruthy();
    expect(screen.queryByTestId("work-item-blocked-person")).toBeNull();
  });

  // #864 UX round 1 B1: a person's decision plus an item it waits for keeps
  // the blocker item visible, the person on the "Also waits for" row.
  it("lists the blocker items of an item waiting for a person, the person under Also waits for", () => {
    openDialog(
      item({
        status: "blocked",
        needs_human: true,
        blocked_on: ["b1"],
        context: context({
          pickup: { ...context().pickup, state: "waiting_for_person", auto_cancel_at: null },
          blocked_on: [{ id: "b1", title: "Ship the migration", status: "in_progress", accessible: true }],
        }),
      }),
      { close: vi.fn() },
    );
    const blockers = screen.getByTestId("work-item-blockers");
    expect(blockers.textContent).toContain("Ship the migration");
    expect(screen.getByText("Also waits for")).toBeTruthy();
    expect(screen.getByTestId("work-item-blocked-why")).toBeTruthy();
  });

  it("keeps Blocked: not picked up on a legacy item that waits for a reason only", () => {
    openDialog(
      item({
        status: "blocked",
        blocked_reason: "waits for the partner",
        context: context({ pickup: { ...context().pickup, state: "blocked", auto_cancel_at: null } }),
      }),
    );
    expect(screen.getByTestId("work-item-pickup").textContent).toContain("Blocked: not picked up");
    expect(screen.getByTestId("work-item-blocked-status").getAttribute("data-kind")).toBe("action");
  });

  it("keeps Blocked on N items and Mark done when a dependency was cancelled", () => {
    openDialog(
      item({
        status: "blocked",
        blocked_on: ["b1", "b2"],
        context: context({
          pickup: { ...context().pickup, state: "blocked_on_items", auto_cancel_at: null },
          blocked_on: [
            { id: "b1", title: "Open step", status: "open", accessible: true },
            { id: "b2", title: "Dropped step", status: "cancelled", accessible: true },
          ],
        }),
      }),
      { close: vi.fn() },
    );
    expect(screen.getByTestId("work-item-pickup").textContent).toContain("Blocked on 2 items");
    expect(screen.getByText("Mark done")).toBeTruthy();
  });

  it("lists the item's pull requests with their state, and names every one whose CI it waits for", () => {
    const waiting = [
      { repo: "acme/widget", number: 8, url: "https://example.test/pr/8" },
      { repo: "acme/other", number: 10, url: null },
    ];
    openDialog(
      item({
        status: "blocked",
        payload: {
          pull_requests: [
            { repo: "acme/widget", number: 7, url: "https://example.test/pr/7", state: "merged" },
            { repo: "acme/widget", number: 8, url: "https://example.test/pr/8", state: "open" },
            { repo: "acme/other", number: 9, state: "closed" },
            { repo: "acme/other", number: 10, state: "open" },
          ],
        },
        context: context({
          pickup: { ...context().pickup, state: "waiting_for_ci", pull_request: waiting[0], pull_requests: waiting },
        }),
      }),
    );
    expect(screen.getByTestId("work-item-pickup").textContent).toContain("Waiting for CI on #8 and 1 more");
    // Its tooltip names more than the label, so it is a tab stop.
    expect(screen.getByTestId("work-item-pickup").getAttribute("tabindex")).toBe("0");
    // Both sentences of the paragraph agree on the count.
    expect(screen.getByTestId("work-item-detail").textContent).toContain(
      "The CI of its 2 pull requests is running: when they complete, the platform hands the item to",
    );
    expect(screen.getByTestId("work-item-detail").textContent).not.toContain("The CI of its pull request is running");
    expect(screen.getByTestId("work-item-detail").textContent).toContain(
      "It stays blocked until the CI of its 2 pull requests completes and the platform hands it over.",
    );
    const chips = screen.getByTestId("work-item-pull-requests");
    const states = Array.from(chips.querySelectorAll("[data-state]")).map((el) => [
      el.getAttribute("data-state"),
      el.getAttribute("aria-label"),
    ]);
    expect(states).toEqual([
      ["merged", "Merged pull request: acme/widget#7"],
      ["open", "Open pull request: acme/widget#8"],
      ["closed", "Closed pull request: acme/other#9"],
      ["open", "Open pull request: acme/other#10"],
    ]);
    expect(chips.querySelector('[data-state="merged"]')?.getAttribute("href")).toBe("https://example.test/pr/7");
    // The dialog keeps the full owner/name#N.
    expect(chips.textContent).toContain("acme/widget#7");
  });

  it("says the blocked sentence in the singular when one pull request waits", () => {
    const waiting = [{ repo: "acme/widget", number: 8, url: null }];
    openDialog(
      item({
        status: "blocked",
        context: context({
          pickup: { ...context().pickup, state: "waiting_for_ci", pull_request: waiting[0], pull_requests: waiting },
        }),
      }),
    );
    expect(screen.getByTestId("work-item-detail").textContent).toContain(
      "It stays blocked until the CI of its pull request completes and the platform hands it over.",
    );
  });

  it("shows name#N in a short chip and keeps owner/name#N in its accessible name", () => {
    renderWithProviders(
      <MemoryRouter>
        <WorkItemPullRequestChips pullRequests={[{ repo: "xtm-one-platform/xtm-one", number: 7123, url: null, state: "open" }]} short />
      </MemoryRouter>,
    );
    const chip = screen.getByTestId("work-item-pull-requests");
    expect(chip.textContent).toBe("xtm-one#7123");
    expect(chip.querySelector("[data-state]")?.getAttribute("aria-label")).toBe("Open pull request: xtm-one-platform/xtm-one#7123");
  });

  it("shows two chips in full and a +N chip for the rest when capped, never a cut number", () => {
    const prs = [7123, 7124, 7125].map((number) => ({ repo: "xtm-one-platform/xtm-one", number, url: null, state: "open" as const }));
    renderWithProviders(
      <MemoryRouter>
        <WorkItemPullRequestChips pullRequests={prs} nowrap tips={false} short max={2} />
      </MemoryRouter>,
    );
    const strip = screen.getByTestId("work-item-pull-requests");
    expect(Array.from(strip.querySelectorAll("[data-state]")).map((el) => el.textContent)).toEqual(["xtm-one#7123", "xtm-one#7124"]);
    const more = screen.getByTestId("work-item-pull-requests-more");
    expect(more.textContent).toBe("+1");
    expect(more.getAttribute("aria-label")).toBe("1 more pull request");
  });

  it("keeps whole references on one line and names the rest in a +N, never a cut number", () => {
    // jsdom lays nothing out: the line reports 60 px per reference shown plus
    // 100 px of reason, in a 200 px box, so only one reference fits whole.
    const proto = HTMLElement.prototype;
    const scroll = Object.getOwnPropertyDescriptor(proto, "scrollWidth");
    const client = Object.getOwnPropertyDescriptor(proto, "clientWidth");
    const isLine = (el: HTMLElement) => el.dataset.testid === "work-item-blocked-why";
    Object.defineProperty(proto, "scrollWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return isLine(this) ? 100 + 60 * this.querySelectorAll("[data-blocked-ref]").length : 0;
      },
    });
    Object.defineProperty(proto, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return isLine(this) ? 200 : 0;
      },
    });
    try {
      const refs = [
        { kind: "pull_request" as const, repo: "xtm-one-platform/xtm-one", number: 8309, release_on: "merged" as const },
        { kind: "pull_request" as const, repo: "opencti-platform/opencti", number: 4512, release_on: "merged" as const },
        { kind: "issue" as const, repo: "xtm-one-platform/xtm-one", number: 8279, release_on: "merged" as const },
      ];
      renderWithProviders(<WorkItemBlockedWhy refs={refs} reason="Waiting for the platform" nowrap />);
      const line = screen.getByTestId("work-item-blocked-why");
      const shown = Array.from(line.querySelectorAll("[data-blocked-ref]"));
      expect(shown.map((el) => el.textContent)).toEqual(["xtm-one#8309"]);
      const more = screen.getByTestId("work-item-blocked-refs-more");
      expect(more.textContent).toBe("+2");
      expect(more.getAttribute("aria-label")).toBe("2 more pull requests or issues");
      expect(more.getAttribute("title")).toContain("opencti-platform/opencti#4512");
      expect(more.getAttribute("title")).toContain("xtm-one-platform/xtm-one#8279");
      expect(screen.getByTestId("work-item-blocked-reason").textContent).toBe("Waiting for the platform");
    } finally {
      // jsdom defines both on Element.prototype: drop the overrides, or put back
      // an own descriptor if one existed.
      if (scroll) Object.defineProperty(proto, "scrollWidth", scroll);
      else delete (proto as unknown as Record<string, unknown>).scrollWidth;
      if (client) Object.defineProperty(proto, "clientWidth", client);
      else delete (proto as unknown as Record<string, unknown>).clientWidth;
    }
  });

  it.each([
    { name: "no reference", refCount: 0, more: null },
    { name: "two references", refCount: 2, more: "+2" },
  ])("shortens the person part to its icon so the reason stays inside the line, $name", ({ refCount, more }) => {
    // jsdom lays nothing out: the line reports 100 px of reason, 60 px per
    // reference shown, 140 px for the full person label and 12 px for its icon
    // alone, in a 200 px box (the fr row at 1024 is 162 px wide).
    const proto = HTMLElement.prototype;
    const scroll = Object.getOwnPropertyDescriptor(proto, "scrollWidth");
    const client = Object.getOwnPropertyDescriptor(proto, "clientWidth");
    const isLine = (el: HTMLElement) => el.dataset.testid === "work-item-blocked-why";
    const contentWidth = (el: HTMLElement) => {
      const person = el.querySelector<HTMLElement>("[data-testid='work-item-blocked-person']");
      const personWidth = person ? (person.dataset.compact === "true" ? 12 : 140) : 0;
      return 100 + 60 * el.querySelectorAll("[data-blocked-ref]").length + personWidth;
    };
    Object.defineProperty(proto, "scrollWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return isLine(this) ? contentWidth(this) : 0;
      },
    });
    Object.defineProperty(proto, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return isLine(this) ? 200 : 0;
      },
    });
    try {
      const refs = [
        { kind: "pull_request" as const, repo: "xtm-one-platform/xtm-one", number: 8309, release_on: "merged" as const },
        { kind: "issue" as const, repo: "xtm-one-platform/xtm-one", number: 8279, release_on: "merged" as const },
      ].slice(0, refCount);
      renderWithProviders(<WorkItemBlockedWhy refs={refs} reason="Needs the go of the release owner" needsHuman nowrap />);
      const line = screen.getByTestId("work-item-blocked-why");
      // The line no longer overflows: the reason ends inside its box.
      expect(line.scrollWidth).toBeLessThanOrEqual(line.clientWidth);
      const reason = screen.getByTestId("work-item-blocked-reason");
      expect(line.contains(reason)).toBe(true);
      expect(reason.textContent).toBe("Needs the go of the release owner");
      const person = screen.getByTestId("work-item-blocked-person");
      expect(person.dataset.compact).toBe("true");
      expect(person.textContent).toBe("");
      expect(person.getAttribute("aria-label")).toBe("a person's decision");
      expect(person.getAttribute("title")).toBe("a person's decision");
      if (more) expect(screen.getByTestId("work-item-blocked-refs-more").textContent).toBe(more);
      else expect(screen.queryByTestId("work-item-blocked-refs-more")).toBeNull();
    } finally {
      if (scroll) Object.defineProperty(proto, "scrollWidth", scroll);
      else delete (proto as unknown as Record<string, unknown>).scrollWidth;
      if (client) Object.defineProperty(proto, "clientWidth", client);
      else delete (proto as unknown as Record<string, unknown>).clientWidth;
    }
  });

  it("keeps the full person label when the line fits", () => {
    renderWithProviders(<WorkItemBlockedWhy refs={[]} reason="Needs the go" needsHuman nowrap />);
    const person = screen.getByTestId("work-item-blocked-person");
    expect(person.dataset.compact).toBe("false");
    expect(person.textContent).toBe("a person's decision");
  });

  it("shows every reference and no +N when they fit", () => {
    const refs = [
      { kind: "pull_request" as const, repo: "xtm-one-platform/xtm-one", number: 8309, release_on: "merged" as const },
      { kind: "issue" as const, repo: "xtm-one-platform/xtm-one", number: 8279, release_on: "merged" as const },
    ];
    renderWithProviders(<WorkItemBlockedWhy refs={refs} nowrap />);
    expect(screen.getAllByRole("img").map((el) => el.textContent)).toEqual(["xtm-one#8309", "xtm-one#8279"]);
    expect(screen.queryByTestId("work-item-blocked-refs-more")).toBeNull();
  });

  it("shows every chip and no +N chip when the cap is not reached", () => {
    const prs = [7123, 7124].map((number) => ({ repo: "xtm-one-platform/xtm-one", number, url: null, state: "open" as const }));
    renderWithProviders(
      <MemoryRouter>
        <WorkItemPullRequestChips pullRequests={prs} short max={2} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("work-item-pull-requests").querySelectorAll("[data-state]")).toHaveLength(2);
    expect(screen.queryByTestId("work-item-pull-requests-more")).toBeNull();
  });

  it("shows only the pull requests the backend can bind, each once", () => {
    const prs = pullRequestsOf({
      pull_requests: [
        { repo: "Acme/Widget", number: 8, state: "open" },
        { repo: "acme/widget", number: "8", state: "open" },
        { repo: "not-a-repo", number: 3 },
        { repo: "a/b/c", number: 4 },
        { repo: "acme/widget", number: 0 },
        { repo: "acme/widget", number: 2.5 },
        { repo: "acme/widget", number: "12" },
      ],
    });
    expect(prs.map((pr) => `${pr.repo}#${pr.number}`)).toEqual(["acme/widget#8", "acme/widget#12"]);
    // The first entry wins, as the backend's waiting-for-CI list reads it.
    const closedFirst = pullRequestsOf({
      pull_requests: [
        { repo: "acme/widget", number: 8, state: "closed" },
        { repo: "acme/widget", number: 8, state: "open" },
      ],
    });
    expect(closedFirst.map((pr) => pr.state)).toEqual(["closed"]);
  });

  it("names a single pull request waiting for CI by its number alone", () => {
    const pr = { repo: "acme/widget", number: 8, url: null };
    openDialog(
      item({
        payload: { pull_requests: [{ ...pr, state: "open" }] },
        context: context({ pickup: { ...context().pickup, state: "waiting_for_ci", pull_request: pr, pull_requests: [pr] } }),
      }),
    );
    expect(screen.getByTestId("work-item-pickup").textContent).toContain("Waiting for CI on #8");
    expect(screen.getByTestId("work-item-pickup").textContent).not.toContain("more");
    expect(screen.getByTestId("work-item-detail").textContent).toContain("The CI of its pull request is running: when it completes");
    // An open item is not told it stays blocked.
    expect(screen.getByTestId("work-item-detail").textContent).not.toContain("It stays blocked");
  });

  it("never shows a raw status word for an ended holder it has no phrase for", () => {
    const runId = "f7db27b2-8d27-4090-9a9d-8abd5d90cba4";
    openDialog(
      item({
        status: "claimed",
        claimed_by_run_id: runId,
        claimed_by_run: { id: runId, kind: null, status: null, alive: false },
      }),
    );
    const holder = screen.getByTestId("work-item-holding-run");
    expect(holder.textContent).toContain("ended");
    expect(holder.textContent).not.toContain("null");
    expect(holder.textContent).not.toContain("(");
  });

  it("renders the description and the result as Markdown, after the facts", () => {
    openDialog(
      item({
        status: "done",
        description: "**Deployed:** production runs `cf04d0a`\n- a\n- b",
        result: "Merged in **#12**",
      }),
    );
    const detail = screen.getByTestId("work-item-detail");
    expect(detail.querySelector("strong")?.textContent).toBe("Deployed:");
    expect(detail.querySelector("code")?.textContent).toBe("cf04d0a");
    expect(detail.querySelectorAll("li")).toHaveLength(2);
    expect(detail.textContent).not.toContain("**");
    expect(screen.getByText("#12").tagName).toBe("STRONG");
    const facts = screen.getByTestId("work-item-facts");
    expect(facts.textContent).toContain("notion-marketing-graph");
    expect(facts.textContent).toContain("EMT Assistant");
    expect(facts.compareDocumentPosition(detail.querySelector("strong") as Node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows a remote image of the description as a link, never loads it", () => {
    openDialog(item({ description: "See ![the chart](https://tracker.example.test/pixel.png)" }));
    const detail = screen.getByTestId("work-item-detail");
    expect(detail.querySelector("img")).toBeNull();
    expect(screen.getByText("the chart").closest("a")?.getAttribute("href")).toBe(
      "https://tracker.example.test/pixel.png",
    );
  });

  it("treats a protocol-relative image address as remote too", () => {
    openDialog(item({ description: "![pixel](//tracker.example.test/p.png)" }));
    expect(screen.getByTestId("work-item-detail").querySelector("img")).toBeNull();
    expect(screen.getByText("pixel")).toBeTruthy();
  });

  it("shows stamped notes as an activity timeline over the brief, ids as chips (#776)", () => {
    const run = "8f240eba-1111-4222-8333-944445555666";
    openDialog(
      item({
        description: `2026-11-20 23:22Z (run ${run}): **Pushed** head b6ce1323be6d239a33100a2b6f20a1918298aad3 (run 8f240eba...)\n\nRead the 120 pages.`,
        note_runs: { [run]: { agent: { id: "a1", name: "Developer" }, accessible: true } },
      }),
    );
    const notes = screen.getAllByTestId("work-item-note");
    expect(notes).toHaveLength(1);
    expect(screen.getByTestId("work-item-note-author").textContent).toBe("Developer");
    // The run is a link named by its first 8 characters, never the uuid inline.
    expect(notes[0].querySelector(`a[href="/operations/executions/${run}"]`)?.textContent).toContain("8f240eba");
    const body = screen.getByTestId("work-item-note-body");
    expect(body.textContent).not.toContain(run);
    expect(body.textContent).not.toContain("(run");
    expect(body.querySelector("strong")?.textContent).toBe("Pushed");
    expect(screen.getByTestId("work-item-id-chip").textContent).toBe("b6ce1323");
    expect(screen.getByTestId("work-item-brief").textContent).toContain("Read the 120 pages.");
  });

  it("folds a long note by height, block Markdown included, and unfolds it (#776, UX B1)", () => {
    const para = "This sentence makes the note long enough to be folded in the activity timeline. ".repeat(12);
    openDialog(item({ description: `2026-11-20 23:22Z: ${para}\n\nA second paragraph.\n\nRead the brief.` }));
    const body = screen.getByTestId("work-item-note-body");
    // A height cap on the wrapper, never -webkit-line-clamp: the text sits in block children.
    expect(body.className).not.toContain("line-clamp");
    // overflow-clip, never overflow-hidden: a clip box cannot be scrolled by a focus (UX round 3 B1).
    expect(body.className).toContain("overflow-clip");
    expect(body.className).not.toContain("overflow-hidden");
    expect(body.getAttribute("data-folded")).toBe("true");
    expect(screen.getByTestId("work-item-note-fade")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(body.getAttribute("data-folded")).toBeNull();
    expect(body.className).not.toContain("overflow-clip");
    expect(screen.queryByTestId("work-item-note-fade")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show less" }));
    expect(body.getAttribute("data-folded")).toBe("true");
  });

  it("opens a folded note when keyboard focus enters it, so focus never lands in hidden text (#776, UX round 2 B1)", () => {
    const para = "This sentence makes the note long enough to be folded in the activity timeline. ".repeat(12);
    // The link sits in the note's own paragraph, past the fold: the oldest note keeps its
    // first paragraph only (splitDescription), a second paragraph would be the brief.
    openDialog(item({ description: `2026-11-20 23:22Z: ${para}See [the run log](https://example.com/log) below the fold.\n\nRead the brief.` }));
    const body = screen.getByTestId("work-item-note-body");
    expect(body.getAttribute("data-folded")).toBe("true");
    const link = within(body).getByRole("link", { name: /the run log/ });
    const intoView = vi.fn();
    link.scrollIntoView = intoView;
    const frames = vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 0;
    });
    // The browser's focus scroll of the folded box, which the handler undoes
    // (jsdom has no layout: an own writable scrollTop stands in for it).
    Object.defineProperty(body, "scrollTop", { configurable: true, writable: true, value: 405 });
    act(() => link.focus());
    frames.mockRestore();
    expect(body.getAttribute("data-folded")).toBeNull();
    expect(body.className).not.toContain("overflow-clip");
    expect(body.scrollTop).toBe(0);
    // The focused link is brought into the dialog's visible area once the note opens (UX round 3 B1).
    expect(intoView).toHaveBeenCalledWith({ block: "nearest" });
    expect(screen.getByRole("button", { name: "Show less" }).getAttribute("aria-expanded")).toBe("true");
    // Folding again by keyboard leaves the body at its first lines.
    body.scrollTop = 405;
    fireEvent.click(screen.getByRole("button", { name: "Show less" }));
    expect(body.getAttribute("data-folded")).toBe("true");
    expect(body.scrollTop).toBe(0);
  });

  it("renders no script or iframe a description carries", () => {
    openDialog(item({ description: 'x <script>window.__pwned = 1</script> <iframe src="https://e.test"></iframe> [a](javascript:alert(1))' }));
    const detail = screen.getByTestId("work-item-detail");
    expect(detail.querySelector("script")).toBeNull();
    expect(detail.querySelector("iframe")).toBeNull();
    expect(detail.querySelector('a[href^="javascript"]')).toBeNull();
  });

  it("offers no action to a reader who may not change the item", () => {
    openDialog(item());
    expect(screen.queryByText("Mark done")).toBeNull();
    expect(screen.queryByText("Delete")).toBeNull();
  });

  it("cuts a long origin inside its column, keeps the link, and shows it whole in a tooltip", async () => {
    const longName =
      "Fleet handover: a desk finished an item, apply its follow-through (inbox, event-driven) for every desk";
    expect(longName.length).toBeGreaterThanOrEqual(90);
    openDialog(
      item({
        context: context({
          origin: {
            kind: "run",
            source: "agent",
            conversation: null,
            conversation_hidden: false,
            run: { id: "run-9", name: longName, status: "running", accessible: true },
            flow_run: null,
          },
        }),
      }),
    );
    const values = screen.getAllByTestId("work-item-fact-value");
    // Every value of the header grid stays on one line inside its column.
    for (const value of values) {
      expect(value.className).toContain("min-w-0");
      expect(value.className).toContain("whitespace-nowrap");
      expect(value.className).not.toContain("break-words");
    }
    const name = screen.getByText(longName);
    expect(name.className).toContain("truncate");
    expect(name.closest("a")?.getAttribute("href")).toBe("/operations/executions/run-9");
    // The person stays in its own column, truncated too.
    const facts = screen.getByTestId("work-item-facts");
    expect(within(facts).getByText("you").className).toContain("truncate");
    fireEvent.focus(name.closest("dd") as HTMLElement);
    expect((await screen.findByRole("tooltip")).textContent).toContain(longName);
  });
});

describe("closing a work item", () => {
  it("records the result the person gave, with the status they chose", async () => {
    renderWithProviders(<WorkItemCloseDialog item={item()} mode="cancelled" onOpenChange={() => {}} />);
    expect(screen.getByText("Cancel this work item?")).toBeTruthy();
    fireEvent.change(screen.getByTestId("work-item-close-result"), { target: { value: "  Duplicate.  " } });
    fireEvent.click(screen.getByTestId("work-item-close-confirm"));
    await vi.waitFor(() =>
      expect(complete.mutateAsync).toHaveBeenCalledWith({ id: item().id, status: "cancelled", result: "Duplicate." }),
    );
  });

  // A list row opens the dialog through its `open` prop, with no DialogTrigger:
  // focus still goes back to the row's button on close.
  function Row() {
    const [closing, setClosing] = useState<WorkItem | null>(null);
    return (
      <>
        <button type="button" onClick={() => setClosing(item())}>
          Mark done
        </button>
        <WorkItemCloseDialog item={closing} mode="done" onOpenChange={(open) => !open && setClosing(null)} />
      </>
    );
  }

  it.each([
    ["Escape", () => fireEvent.keyDown(document.activeElement as Element, { key: "Escape" })],
    ["Keep it open", () => fireEvent.click(screen.getByRole("button", { name: "Keep it open" }))],
    ["the confirmation", () => fireEvent.click(screen.getByTestId("work-item-close-confirm"))],
  ])("gives focus back to the row's button after %s", async (_how, close) => {
    renderWithProviders(<Row />);
    const trigger = screen.getByRole("button", { name: "Mark done" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(await screen.findByText("Mark this work item done?")).toBeTruthy();
    close();
    await vi.waitFor(() => expect(screen.queryByText("Mark this work item done?")).toBeNull());
    await vi.waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  // The real row: once the item is done its Mark done button unmounts,
  // so focus goes to the target the list names, the row's actions button.
  function RowThatLosesItsButton() {
    const [closing, setClosing] = useState<WorkItem | null>(null);
    const [done, setDone] = useState(false);
    return (
      <>
        {!done && (
          <button type="button" onClick={() => setClosing(item())}>
            Mark done
          </button>
        )}
        <button type="button" aria-haspopup="menu">
          Work item actions
        </button>
        <WorkItemCloseDialog
          item={closing}
          mode="done"
          onOpenChange={(open) => {
            if (!open) {
              setClosing(null);
              setDone(true);
            }
          }}
          returnFocusTo={() => screen.queryByRole("button", { name: "Work item actions" })}
        />
      </>
    );
  }

  it("focuses the row's actions button when the confirmed action removed the opener", async () => {
    renderWithProviders(<RowThatLosesItsButton />);
    const trigger = screen.getByRole("button", { name: "Mark done" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(await screen.findByText("Mark this work item done?")).toBeTruthy();
    fireEvent.click(screen.getByTestId("work-item-close-confirm"));
    await vi.waitFor(() => expect(screen.queryByText("Mark this work item done?")).toBeNull());
    expect(screen.queryByRole("button", { name: "Mark done" })).toBeNull();
    await vi.waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Work item actions" })),
    );
  });
});

describe("times and dates in the app language", () => {
  const NOW = Date.parse("2026-11-03T06:20:00Z");

  afterEach(() => {
    vi.useRealTimers();
  });

  function openIn(locale: string) {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    const data = item({
      created_at: "2026-11-03T06:14:00Z",
      last_activity_at: "2026-11-03T06:14:00Z",
      updated_at: "2026-11-03T06:14:00Z",
      claimed_at: "2026-11-03T06:15:00Z",
    });
    workItemQuery.current = { data, isLoading: false, isError: false };
    render(
      <QueryClientProvider client={testQueryClient()}>
        <IntlProvider locale={locale} messages={{}} timeZone="UTC" onError={() => {}}>
          <ToastProvider>
            <MemoryRouter>
              <WorkItemDetailDialog itemId={data.id} onClose={() => {}} actionsFor={() => ({})} />
            </MemoryRouter>
          </ToastProvider>
        </IntlProvider>
      </QueryClientProvider>,
    );
    return screen.getByTestId("work-item-detail").textContent ?? "";
  }

  it("reads the last activity and the dates in French", () => {
    const text = openIn("fr-fr");
    expect(screen.getAllByTestId("work-item-fact-value").map((node) => node.textContent)).toContain("il y a 6 minutes");
    expect(text).toContain("3 nov., 06:14");
    expect(text).not.toContain("ago");
    expect(text).not.toMatch(/Nov \d/);
    expect(text).not.toMatch(/\b(AM|PM)\b/);
  });

  it("reads them in Spanish and in Japanese", () => {
    const es = openIn("es-es");
    expect(es).toContain("hace 6 minutos");
    expect(es).not.toContain("ago");
    expect(es).not.toMatch(/\b(AM|PM)\b/);
    cleanup();
    const ja = openIn("ja-jp");
    expect(ja).toContain("6 分前");
    expect(ja).toContain("11月3日");
    expect(ja).not.toContain("ago");
  });

  it("keeps English in English", () => {
    const text = openIn("en-us");
    expect(text).toContain("6 minutes ago");
    expect(text).toContain("Nov 3, 06:14 AM");
  });
});

describe("what a row may offer", () => {
  it("puts back in the queue only what is held, blocked or finished", () => {
    expect(canRequeue(item())).toBe(false);
    expect(canRequeue(item({ status: "blocked" }))).toBe(true);
    expect(canRequeue(item({ status: "done" }))).toBe(true);
    expect(canRequeue(item({ claimed_by_agent_id: "agent-1" }))).toBe(true);
    expect(canClose(item({ status: "done" }))).toBe(false);
    expect(canClose(item())).toBe(true);
  });

  it("flags the states nothing will act on", () => {
    expect(waitsForSomeone("manual")).toBe(true);
    expect(waitsForSomeone("filtered_out")).toBe(true);
    expect(waitsForSomeone("automatic")).toBe(false);
    expect(visiblePayload({ _dispatch_attempts: 2 })).toBeNull();
  });
});
