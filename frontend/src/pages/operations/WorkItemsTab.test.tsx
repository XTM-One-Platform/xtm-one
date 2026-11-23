// The Work Items list keeps one selection column for every row: a
// scheduled item that has not run is cancelled, never deleted, so its box is
// shown disabled with the reason instead of being left out, which shifted the
// row left of every other one. jsdom does no layout: these tests assert the
// box is there, first and disabled; the pixel alignment is checked rendered.

import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { capTipText } from "@/lib/tip-text";
import { renderWithProviders } from "@/testing/providers";
import { blockedReason, cancelledBlockers } from "@/lib/work-item-context";
import type { WorkItem } from "@/types";

// An administrator in the test that reads a fleet tab with `?scope=all`; a reader elsewhere.
let IS_ADMIN = false;
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "Europe/Paris" }, isAdmin: IS_ADMIN }),
}));

// The wide toolbar (pills): happy-dom's 1024 px window would select the
// compact one, which compact-toolbar.test.tsx covers.
vi.mock("@/hooks/useCompactToolbar", () => ({ useCompactToolbar: () => false }));

// The decision inbox read of "Waiting for me" (#842 S5): the read switch off by default, so
// the view keeps the per-engine work items list; a test sets the pending or failed read.
type WaitingRead = { data?: { enabled: boolean; reason: string | null; items: unknown[] }; isLoading: boolean; isError: boolean };
const SWITCH_OFF: WaitingRead = { data: { enabled: false, reason: "off", items: [] }, isLoading: false, isError: false };
let WAITING: WaitingRead = SWITCH_OFF;
const WAITING_REFETCH = vi.fn();
vi.mock("@/hooks/api/useDecisionApi", () => ({
  useWaitingDecisions: () => ({ ...WAITING, refetch: WAITING_REFETCH }),
  useAnswerDecision: () => ({ mutate: () => undefined, mutateAsync: async () => undefined, isPending: false }),
}));

const base = (overrides: Partial<WorkItem>): WorkItem =>
  ({
    id: "id",
    user_id: "u1",
    is_company_managed: false,
    owner_agent_id: "agent-1",
    created_by_agent_id: "agent-1",
    title: "title",
    description: "a description",
    payload: {},
    status: "open",
    priority: "medium",
    correlation_key: null,
    due_at: null,
    claimed_by_agent_id: null,
    claimed_at: null,
    source: "agent",
    result: null,
    is_stale: false,
    scheduled: null,
    last_activity_at: null,
    created_at: null,
    updated_at: null,
    ...overrides,
  }) as WorkItem;

const scheduledItem = (id: string, title: string, state: "scheduled" | "running", status: "open" | "in_progress") =>
  base({
    id,
    title,
    status,
    due_at: "2026-10-25T08:00:00Z",
    scheduled: {
      mode: "exact",
      tool: "send_teams_channel_message",
      target: "Testing",
      preview: "Test",
      state,
      run_id: null,
      conversation_id: null,
      outcome_code: null,
    },
  } as Partial<WorkItem>);

const MIXED: WorkItem[] = [
  base({
    id: "sched",
    title: "Post the reminder",
    due_at: "2026-10-25T08:00:00Z",
    scheduled: {
      mode: "exact",
      tool: "send_teams_channel_message",
      target: "Testing",
      preview: "Test",
      state: "scheduled",
      run_id: null,
      conversation_id: null,
      outcome_code: null,
    },
  } as Partial<WorkItem>),
  base({ id: "regular", title: "Review the pull request" }),
];
const ONLY_SCHEDULED: WorkItem[] = [
  scheduledItem("s1", "Post the reminder", "scheduled", "open"),
  scheduledItem("s2", "Send the digest", "running", "in_progress"),
];
let ITEMS: WorkItem[] = MIXED;

const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
// The filters of every list read, the last one last: the sort is read off it.
const LIST_READS: Array<{ sort?: string; limit?: number; status?: string; open_only?: boolean; vis?: string }> = [];
// The filters of every count read (#1499: the Waiting for me chip's own read is among them).
const COUNT_READS: Array<Record<string, unknown>> = [];
/** The last read of the list itself (the Blocked hint reads 100 blocked items beside it). */
const lastListRead = () => {
  const reads = LIST_READS.filter((f) => f.limit !== 100);
  return reads[reads.length - 1];
};
vi.mock("@/hooks/api/useWorkItemApi", () => ({
  useWorkItems: (filters: { sort?: string; limit?: number; status?: string; open_only?: boolean } = {}) => {
    LIST_READS.push(filters);
    return { data: ITEMS, isLoading: false };
  },
  useWorkItemCount: (filters: Record<string, unknown>, deletableOnly?: boolean) => {
    COUNT_READS.push(filters);
    return { data: { count: deletableOnly ? 1 : 2 } };
  },
  useStaleWorkItemCount: () => ({ data: { count: 0 } }),
  useWorkItemOwners: () => ({ data: [] }),
  useWorkItemGraph: () => ({ data: undefined, isLoading: false }),
  useWorkItemStatusCounts: () => ({ data: { open: 3, claimed: 1, in_progress: 2, blocked: 4, done: 7, cancelled: 0 } }),
  useWorkItem: () => ({ data: undefined, isLoading: false, isError: false }),
  useDeleteWorkItem: () => idle,
  useBulkDeleteWorkItems: () => idle,
  useRequeueWorkItem: () => idle,
  useCompleteWorkItem: () => idle,
  useCancelScheduledWorkItem: () => idle,
  useRescheduleScheduledWorkItem: () => idle,
  useAskWorkItem: () => idle,
}));

const { default: WorkItemsTab } = await import("./WorkItemsTab");

// The last count read asked with `waiting_for_me` (#1499: the chip's own read).
function lastWaitingCountRead(): Record<string, unknown> | undefined {
  return [...COUNT_READS].reverse().find((f) => f.waiting_for_me === true);
}

function renderTab(query = "") {
  // A named segment: the tab shows its rows at once, without waiting for the
  // per-segment counts it lands by when the URL names none.
  return renderWithProviders(
    <MemoryRouter initialEntries={[`/operations?vis=mine${query}`]}>
      <WorkItemsTab />
    </MemoryRouter>,
  );
}

function rowOf(title: string): HTMLElement {
  const row = screen.getAllByTestId("work-item-row").find((r) => within(r).queryByText(title));
  if (!row) throw new Error(`no row ${title}`);
  return row;
}

// Data tables #9378 slice 8d: the list is the shared DataTable. Search, visibility
// and the Status pill sit on the toolbar row; the view, the scope and the fleet and
// owner pickers sit in the Filters pill, whose badge counts those that narrow it.
describe("Work Items filter row", () => {
  it("puts the Status pill on the toolbar row, the view in the Filters pill", () => {
    renderTab();
    const row = screen.getByTestId("work-items-filter-row");
    expect(row).toContainElement(screen.getByTestId("work-items-status-pill"));
    expect(screen.queryByRole("group", { name: "View filter" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
    const panel = screen.getByRole("group", { name: "Filters" });
    expect(within(panel).getByRole("group", { name: "View filter" })).toBeTruthy();
    expect(within(panel).queryByTestId("work-items-status-pill")).toBeNull();
  });

  it("offers every status with its count, open work selected by default", () => {
    renderTab();
    fireEvent.click(screen.getByTestId("work-items-status-pill"));
    const panel = screen.getByRole("group", { name: "Status filter" });
    const pressed = (status: string) => within(panel).getByTestId(`work-items-status-${status}`).getAttribute("aria-pressed");
    for (const status of ["open", "claimed", "in_progress", "blocked"]) expect(pressed(status)).toBe("true");
    for (const status of ["done", "cancelled"]) expect(pressed(status)).toBe("false");
    expect(within(panel).getByTestId("work-items-status-done").textContent).toContain("7");
    expect(within(panel).getByTestId("work-items-status-blocked").textContent).toContain("4");
  });

  it("asks the server for the selected statuses, comma-separated", () => {
    renderTab("&status=blocked,done");
    expect(lastListRead()).toMatchObject({ status: "blocked,done", open_only: false });
    fireEvent.click(screen.getByTestId("work-items-status-pill"));
    expect(screen.getByTestId("work-items-status-pill").textContent).toContain("2");
  });

  it("reads every status on status=all and open work with no status key", () => {
    const { unmount } = renderTab("&status=all");
    expect(lastListRead()).toMatchObject({ status: undefined, open_only: false });
    unmount();
    renderTab();
    expect(lastListRead()).toMatchObject({ status: undefined, open_only: true });
  });

  it("reads a default page of 25 rows, one of the Rows per page sizes", () => {
    // UX round 1 on #11917, B2: a default of 20 is not in PAGE_SIZES (10, 25, 50, 100) and left the select empty.
    renderTab();
    expect(lastListRead()).toMatchObject({ limit: 25 });
  });

  it("keeps the visibility segments on the toolbar row", async () => {
    renderTab("&status=blocked");
    expect(await screen.findByRole("radiogroup", { name: "Visibility" })).toBeTruthy();
  });

  it("counts a narrowing view on the Filters pill and none for the status", () => {
    const { unmount } = renderTab("&status=blocked");
    expect(screen.getByRole("button", { name: /^Filters/ }).textContent).toBe("Filters");
    unmount();
    renderTab("&view=stale");
    expect(screen.getByRole("button", { name: /^Filters/ }).textContent).toBe("Filters1");
  });
});

describe("Work Items help text", () => {
  // Header density sweep #10215 slice b, UX round 1 B1 on #10260: what a work
  // item is sits in the "i" of the filter row, never as a visible paragraph.
  it("keeps the explanation in the info tip on the filter row, not a visible paragraph", () => {
    renderTab();
    expect(screen.queryByText(/Tasks agents hand to each other/)).toBeNull();
    // The "i" sits on the toolbar row of the table, beside the search (slice 8d).
    const tip = screen.getByRole("img", { name: "About work items" });
    const toolbar = screen.getByTestId("data-table-toolbar");
    expect(toolbar).toContainElement(tip);
    expect(toolbar).toContainElement(screen.getByRole("search", { name: "Search title or key..." }));
  });

  // UX round 2 B1 on #10260: the Tip cuts a string past 280 characters, so the
  // explanation must fit whole in every catalog.
  it("fits the info tip whole in en, fr, es and ja", () => {
    const key =
      "Tasks agents hand to each other, kept across runs. An item is picked up only when an inbox, an assignment or a flow drains its owner agent's queue; otherwise it waits. Open one to see why you see it and what will act on it.";
    for (const [lang, strings] of Object.entries({ en, fr, es, ja }) as [string, Record<string, string>][]) {
      const text = strings[key];
      expect(text, `${lang} has the key`).toBeTruthy();
      expect(capTipText(text), `${lang} is ${text.length} characters`).toBe(text);
    }
  });
});

describe("Work Items list selection column", () => {
  it("gives a scheduled row the same selection column as a regular row", () => {
    renderTab();
    for (const title of ["Post the reminder", "Review the pull request"]) {
      expect(within(rowOf(title)).getAllByRole("checkbox")).toHaveLength(1);
    }
    const scheduledBox = within(rowOf("Post the reminder")).getByRole("checkbox");
    expect((scheduledBox as HTMLButtonElement).disabled).toBe(true);
    expect(scheduledBox.getAttribute("aria-label")).toBe("Not selectable");
    expect(scheduledBox.parentElement?.getAttribute("aria-disabled")).toBe("true");
    const regularBox = within(rowOf("Review the pull request")).getByRole("checkbox");
    expect((regularBox as HTMLButtonElement).disabled).toBe(false);
  });

  it("puts the selection box before the title on every row", () => {
    renderTab();
    for (const title of ["Post the reminder", "Review the pull request"]) {
      const row = rowOf(title);
      const box = within(row).getByRole("checkbox");
      const titleButton = within(row).getByRole("button", { name: title });
      expect(box.compareDocumentPosition(titleButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });

  // Sam, 14 Nov 2026: no page-level select row above the grid's cards; the page box
  // moves into the DataTable toolbar row (item dev:data-table-grid-select-in-toolbar).
  it("draws no page-select row above the grid's cards", () => {
    renderTab();
    expect(screen.queryByTestId("work-items-page-select")).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Select all" })).toBeNull();
  });

  it("selects one row and shows the count in the selection bar", () => {
    renderTab();
    fireEvent.click(within(rowOf("Review the pull request")).getByRole("checkbox"));
    expect(within(rowOf("Review the pull request")).getByRole("checkbox").getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("1 selected")).toBeTruthy();
  });

  it("says Scheduled once on a waiting scheduled row", () => {
    renderTab();
    const row = rowOf("Post the reminder");
    expect(within(row).getAllByText("Scheduled")).toHaveLength(1);
    expect(within(row).queryByRole("img", { name: "Scheduled" })).toBeNull();
  });

  it("names a later state of a scheduled row in the same badge", () => {
    ITEMS = ONLY_SCHEDULED;
    try {
      renderTab();
      const row = rowOf("Send the digest");
      expect(within(row).getByText("Running")).toBeTruthy();
      // Shown once as the state; a screen reader still hears it is scheduled.
      const srOnly = within(row).getByText(/^Scheduled/);
      expect(srOnly.className).toContain("sr-only");
    } finally {
      ITEMS = MIXED;
    }
  });

  it("does not open the details when the disabled box is clicked", () => {
    renderTab();
    const box = within(rowOf("Post the reminder")).getByRole("checkbox");
    fireEvent.click(box.parentElement as HTMLElement);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

// One row height and one line of plain text per item: jsdom does no
// layout, so these tests pin the row's structure and the content that must
// leave the row; the pixel heights are measured rendered.
describe("Work Items list rows", () => {
  const ROWS: WorkItem[] = [
    base({ id: "md", title: "Markdown item", description: "**Deployed:** production runs `cf04d0a`\n- a\n- b" }),
    base({
      id: "done-sched",
      title: "Posted the digest",
      status: "done",
      result: "**Sent** to `#emt`",
      due_at: "2026-10-25T08:00:00Z",
      scheduled: {
        mode: "instruction",
        tool: null,
        target: "EMT channel",
        preview: "Post **the** digest",
        state: "done",
        run_id: "99999999-8888-7777-6666-555555555555",
        conversation_id: null,
        outcome_code: null,
      },
    } as Partial<WorkItem>),
    base({ id: "blocked", title: "Blocked item", status: "blocked", is_stale: true, correlation_key: "dev:a-long-key" }),
  ];

  // The rendered height itself is measured by the E2E spec (jsdom lays nothing
  // out); here every row, whatever its status or schedule, has the same parts
  // in the same columns: one line below the title, one meta column, one
  // updated cell, one actions cell.
  it("gives every row the same structure: title line, one line below, meta, updated, actions", () => {
    ITEMS = ROWS;
    try {
      renderTab();
      const rows = screen.getAllByTestId("work-item-row");
      expect(rows).toHaveLength(3);
      for (const row of rows) {
        expect(within(row).queryAllByTestId("work-item-summary").length + within(row).queryAllByTestId("work-item-waits").length).toBe(1);
        expect(within(row).getAllByTestId("work-item-provenance")).toHaveLength(1);
        expect(within(row).getAllByTestId("work-item-actions")).toHaveLength(1);
      }
    } finally {
      ITEMS = MIXED;
    }
  });

  it("shows a Markdown description as one line of plain text", () => {
    ITEMS = ROWS;
    try {
      renderTab();
      const summary = within(rowOf("Markdown item")).getByTestId("work-item-summary");
      expect(summary.textContent).toBe("Deployed: production runs cf04d0a · a · b");
      expect(summary.textContent).not.toMatch(/[*`#]/);
      expect(summary.querySelector("strong, code, ul, li, h1, h2, h3, p")).toBeNull();
    } finally {
      ITEMS = MIXED;
    }
  });

  it("folds a scheduled item into one line and keeps its run as an action", () => {
    ITEMS = ROWS;
    try {
      renderTab();
      const row = rowOf("Posted the digest");
      expect(within(row).getByTestId("scheduled-summary").textContent).toContain("EMT channel");
      // The preview and the outcome are in the dialog, not in the row.
      expect(row.textContent).not.toContain("Post the digest");
      expect(row.textContent).not.toContain("Sent");
      expect(within(row).getByLabelText("View the run").getAttribute("href")).toBe(
        "/operations/executions/99999999-8888-7777-6666-555555555555",
      );
    } finally {
      ITEMS = MIXED;
    }
  });

  it("keeps the metadata and the stale badge on the row", () => {
    ITEMS = ROWS;
    try {
      renderTab();
      const row = rowOf("Blocked item");
      // A server that names no reason keeps the generic badge; a named one is on the badge (UX #9890 B2).
      expect(within(row).getByText(/Stale|Past due|Never released|Reference quiet|Quiet for a day/)).toBeTruthy();
      expect(within(row).getByTestId("work-item-provenance").textContent).toContain("dev:a-long-key");
    } finally {
      ITEMS = MIXED;
    }
  });

  it("shows Impacts with the agents its completion is routed to, next to the owner, and nothing without", async () => {
    const owner = { id: "agent-1", name: "Developer", icon: "bot", accessible: true };
    const ea = { id: "agent-9", name: "Executive Assistant", icon: "bot", accessible: true };
    const context = (fleet_impact: unknown) => ({
      owner_agent: owner,
      created_by_agent: null,
      claimed_by_agent: null,
      person: null,
      visibility: { reason: "own", workspace: null },
      origin: { kind: "manual", source: "agent", conversation: null, conversation_hidden: false, run: null, flow_run: null },
      pickup: {
        state: "manual",
        inboxes: [],
        available_at: null,
        auto_cancel_at: null,
        purge_at: null,
        dispatch_attempts: 0,
        max_dispatch_attempts: 3,
      },
      fleet_impact,
    });
    ITEMS = [
      base({
        id: "impact",
        title: "Ship the switch",
        context: context({ agents: [ea], kind: "skill", instructions: null, inferred: false }),
      } as Partial<WorkItem>),
      base({ id: "plain", title: "Plain item", context: context(null) } as Partial<WorkItem>),
    ];
    try {
      renderTab();
      // Impacts lives in the hover card, after the owner; the row keeps the owner.
      const row = rowOf("Ship the switch");
      expect(within(row).getByTestId("work-item-owner").textContent).toContain("Developer");
      expect(within(row).getByTestId("work-item-more").textContent).toBe("+1");
      fireEvent.focus(within(row).getByTestId("work-item-more"));
      const card = await screen.findByTestId("ops-hover-card");
      const terms = within(card).getAllByRole("term").map((t) => t.textContent);
      expect(terms.indexOf("Owner")).toBeLessThan(terms.indexOf("Impacts"));
      const impacts = within(card).getByTestId("work-item-impacts");
      expect(within(impacts).getByText("Executive Assistant").closest("a")?.getAttribute("href")).toBe("/agents/agent-9");
      expect(within(rowOf("Plain item")).queryByTestId("work-item-more")).toBeNull();
    } finally {
      ITEMS = MIXED;
    }
  });
});

// A blocked row says whether it needs anyone: one waiting on open
// dependencies reopens by itself, one with none open needs an action.
describe("Work Items blocked rows", () => {
  const blockerRef = (id: string, title: string, status: WorkItem["status"]) => ({ id, title, status, accessible: true });
  const withContext = (id: string, title: string, blockedOn: ReturnType<typeof blockerRef>[], extra: Partial<WorkItem> = {}) =>
    base({
      id,
      title,
      status: "blocked",
      blocked_on: blockedOn.map((b) => b.id),
      context: {
        owner_agent: null,
        created_by_agent: null,
        claimed_by_agent: null,
        person: null,
        visibility: { reason: "own", workspace: null },
        origin: { kind: "manual", source: "agent", conversation: null, conversation_hidden: false, run: null, flow_run: null },
        pickup: {
          state: blockedOn.length ? "blocked_on_items" : "blocked",
          inboxes: [],
          available_at: null,
          auto_cancel_at: null,
          purge_at: null,
          dispatch_attempts: 0,
          max_dispatch_attempts: 3,
        },
        blocked_on: blockedOn,
      },
      ...extra,
    } as Partial<WorkItem>);

  const BLOCKED: WorkItem[] = [
    withContext("w", "Waits on the release", [blockerRef("b1", "Ship the release", "in_progress")]),
    withContext("l", "Scheduled leftover", [], { result: "The run was cut by a restart." }),
    withContext("d", "Dependencies all done", [blockerRef("b2", "Finished step", "done")]),
    withContext("c", "Dependency cancelled", [
      blockerRef("b3", "Open step", "open"),
      blockerRef("b4", "Dropped step", "cancelled"),
    ]),
  ];

  const statusOf = (title: string) => within(rowOf(title)).getByTestId("work-item-blocked-status");

  it("shows Waiting on a row whose dependencies are open, Blocked on the others", () => {
    ITEMS = BLOCKED;
    try {
      renderTab();
      expect(statusOf("Waits on the release").textContent).toBe("Waiting");
      expect(statusOf("Waits on the release").getAttribute("data-kind")).toBe("waiting");
      for (const title of ["Scheduled leftover", "Dependencies all done", "Dependency cancelled"]) {
        expect(statusOf(title).textContent).toBe("Blocked");
        expect(statusOf(title).getAttribute("data-kind")).toBe("action");
      }
    } finally {
      ITEMS = MIXED;
    }
  });

  it("hides Mark done on a row that waits for open dependencies only", () => {
    ITEMS = BLOCKED;
    try {
      renderTab();
      expect(within(rowOf("Waits on the release")).queryByRole("button", { name: "Mark done" })).toBeNull();
      expect(within(rowOf("Scheduled leftover")).getByRole("button", { name: "Mark done" })).toBeTruthy();
      expect(within(rowOf("Dependency cancelled")).getByRole("button", { name: "Mark done" })).toBeTruthy();
      // One actions track on every row, with or without Mark done, so the
      // metadata column starts at the same x in every language.
      const track = (title: string) => within(rowOf(title)).getByTestId("work-item-actions").className;
      expect(track("Waits on the release")).toBe(track("Scheduled leftover"));
    } finally {
      ITEMS = MIXED;
    }
  });

  it("lists on a waiting row only the dependencies its badge counts", () => {
    ITEMS = [
      withContext("p", "Waits on one of two", [
        blockerRef("b5", "Ship the migration", "in_progress"),
        blockerRef("b6", "Write the notes", "done"),
      ]),
    ];
    try {
      renderTab();
      const row = rowOf("Waits on one of two");
      expect(within(row).getByTestId("work-item-pickup").getAttribute("aria-label")).toBe("Waits for 1 item");
      const blockers = within(row).getByTestId("work-item-blockers");
      expect(blockers.querySelectorAll("a")).toHaveLength(1);
      expect(blockers.textContent).toContain("Ship the migration");
      expect(blockers.textContent).not.toContain("Write the notes");
    } finally {
      ITEMS = MIXED;
    }
  });

  it("lists the blocker items of a row that also waits for a person's decision", () => {
    const item = withContext("h", "Waits on a person and an item", [blockerRef("b8", "Ship the migration", "in_progress")], {
      needs_human: true,
    });
    item.context = { ...item.context!, pickup: { ...item.context!.pickup, state: "waiting_for_person" } };
    ITEMS = [item];
    try {
      renderTab();
      const blockers = within(rowOf("Waits on a person and an item")).getByTestId("work-item-blockers");
      expect(blockers.querySelectorAll("a")).toHaveLength(1);
      expect(blockers.textContent).toContain("Ship the migration");
    } finally {
      ITEMS = MIXED;
    }
  });

  // Every fact is in the hover card: one term per fact, what a blocked row
  // waits for first, names whole. The row shows what it waits for on its
  // second line and the owner in the meta column; the updated cell is its own
  // column, outside the meta.
  it("lists every fact in the hover card, Waits for first and full names", async () => {
    const agent = (id: string, name: string) => ({ id, name, icon: null, accessible: true });
    ITEMS = [
      withContext("m", "Waits with owners", [blockerRef("b7", "Fleet handover: a desk finished an item, apply it", "in_progress")], {
        last_activity_at: new Date().toISOString(),
        context: {
          ...withContext("m", "x", [blockerRef("b7", "Fleet handover: a desk finished an item, apply it", "in_progress")]).context!,
          owner_agent: agent("a1", "[CEO Office] Developer"),
          created_by_agent: agent("a2", "[CEO Office] Executive Assistant"),
        },
      } as Partial<WorkItem>),
    ];
    try {
      renderTab();
      const row = rowOf("Waits with owners");
      expect(within(row).getByTestId("work-item-waits").textContent).toContain("Fleet handover: a desk finished an item, apply it");
      expect(within(row).getByTestId("work-item-owner").textContent).toContain("[CEO Office] Developer");
      // The priority sits next to the state chip, outside the strip that drops chips.
      expect(within(row).getByTestId("work-item-priority").textContent).toBe("Medium");
      expect(within(row).getByTestId("work-item-chips").contains(within(row).getByTestId("work-item-priority"))).toBe(false);
      // The title opens the hover card from the keyboard.
      fireEvent.focus(within(row).getByRole("button", { name: "Waits with owners" }));
      const card = await screen.findByTestId("ops-hover-card");
      // A <dl> has no implicit ARIA role; its terms and definitions do.
      const terms = within(card).getAllByRole("term").map((t) => t.textContent);
      expect(terms.slice(0, 3)).toEqual(["Waits for", "Owner", "Created by"]);
      expect(terms).toContain("Last activity");
      // The facts a narrow row's chips can hide are in the card too.
      expect(within(card).getByTestId("work-item-fact-priority").textContent).toContain("Medium");
      expect(within(card).getByTestId("work-item-fact-pickup").textContent).toContain("Waits for 1 item");
      expect(terms.indexOf("Priority")).toBeLessThan(terms.indexOf("Last activity"));
      const values = within(card).getAllByRole("definition").map((d) => d.textContent);
      expect(values[0]).toContain("Fleet handover: a desk finished an item, apply it");
      expect(values[1]).toContain("[CEO Office] Developer");
      expect(values[2]).toContain("[CEO Office] Executive Assistant");
      // The updated cell is its own column: the relative time alone, outside
      // the meta column and the actions.
      const updated = within(row).getByTestId("work-item-updated");
      expect(updated.textContent).toMatch(/ago|now/);
      expect(within(row).getByTestId("work-item-provenance").contains(updated)).toBe(false);
      expect(within(row).getByTestId("work-item-actions").contains(updated)).toBe(false);
      // The warning term of the card is marked by its tone, read by the UX gate's contrast check.
      expect(within(card).getAllByRole("term")[0].getAttribute("data-tone")).toBe("warning");
    } finally {
      ITEMS = MIXED;
    }
  });

  // Done and Cancelled read newest completion first and the time cell says
  // when the item ended; the hover card lists Claimed and Completed (#8558).
  it("sorts the Done filter by completion and shows the completion time", async () => {
    const ended = new Date(Date.now() - 5 * 60_000).toISOString();
    ITEMS = [
      base({
        id: "d1",
        title: "Ended item",
        status: "done",
        claimed_at: new Date(Date.now() - 60 * 60_000).toISOString(),
        completed_at: ended,
        last_activity_at: new Date(Date.now() - 2 * 24 * 60 * 60_000).toISOString(),
      }),
    ];
    LIST_READS.length = 0;
    try {
      renderTab("&status=done");
      expect(LIST_READS.some((f) => f.sort === "completed_desc" && f.limit !== 100)).toBe(true);
      const row = rowOf("Ended item");
      const cell = within(row).getByTestId("work-item-updated");
      expect(cell.textContent).toMatch(/5 minutes ago|5 min/);
      fireEvent.focus(cell);
      expect((await screen.findByRole("tooltip")).textContent).toMatch(/^Completed /);
      fireEvent.focus(within(row).getByRole("button", { name: "Ended item" }));
      const card = await screen.findByTestId("ops-hover-card");
      const terms = within(card).getAllByRole("term").map((t) => t.textContent);
      expect(terms).toEqual(expect.arrayContaining(["Claimed", "Completed"]));
      expect(terms.indexOf("Completed")).toBeLessThan(terms.indexOf("Last activity"));
    } finally {
      ITEMS = MIXED;
    }
  });

  it("keeps newest created first on the open filter", () => {
    LIST_READS.length = 0;
    renderTab();
    expect(LIST_READS.length).toBeGreaterThan(0);
    expect(LIST_READS.every((f) => f.sort === undefined)).toBe(true);
  });

  // A row with pull requests and no key shows the first one as
  // one token, and the others join the single "+N", never a second counter.
  it("shows the first pull request as one token and counts the rest in the single +N", () => {
    ITEMS = [
      base({
        id: "prs",
        title: "Ships three pull requests",
        payload: {
          pull_requests: [
            { repo: "XTM-One-Platform/xtm-one", number: 8407 },
            { repo: "OpenCTI-Platform/opencti", number: 19581 },
            { repo: "XTM-One-Platform/xtm-one", number: 8408 },
          ],
        },
      } as Partial<WorkItem>),
    ];
    try {
      renderTab();
      const row = rowOf("Ships three pull requests");
      expect(within(row).getByTestId("work-item-pull-request").textContent).toBe("xtm-one#8407");
      const more = within(row).getAllByTestId("work-item-more");
      expect(more).toHaveLength(1);
      expect(within(row).getAllByText(/^\+\d+$/)).toHaveLength(1);
      expect(more[0].getAttribute("aria-label")).toMatch(/more details/);
    } finally {
      ITEMS = MIXED;
    }
  });

  // A blocked row says what it waits for, whatever it is: pull
  // requests and issues, a reason, a person; one that says nothing carries a
  // red "No reason" chip and the toolbar counts it.
  it("shows Waits for on a row blocked on a pull request, an issue or a reason, and No reason on a silent one", () => {
    ITEMS = [
      withContext("r", "Waits on a pull request", [], {
        blocked_on_refs: [
          { repo: "acme/app", kind: "pull_request", number: 12, release_on: "merged" },
          { repo: "acme/app", kind: "issue", number: 7, release_on: "closed" },
          { repo: "acme/app", kind: "pull_request", number: 9, release_on: "merged", state: "merged" },
        ],
        blocked_why: { text: "Waits for: acme/app#12 (merged)", missing: false },
      }),
      withContext("t", "Waits on words", [], {
        blocked_reason: "the UX verdict on the head",
        blocked_why: { text: "Waits for: the UX verdict on the head", missing: false },
      }),
      withContext("n", "Says nothing", [], { blocked_why: { text: "No reason given", missing: true } }),
    ];
    try {
      renderTab();
      const why = within(rowOf("Waits on a pull request")).getByTestId("work-item-blocked-why");
      expect(within(why).getByRole("img", { name: "Pull request: acme/app#12" })).toBeTruthy();
      // The kind travels by icon and by the localised prefix, never as an
      // English word in the label.
      const issue = within(why).getByRole("img", { name: "Issue: acme/app#7" });
      // In the one-line row each reference reads `name#N`, the number always in
      // the visible text, never only in the title.
      expect(issue.textContent).toBe("app#7");
      expect(within(why).getByRole("img", { name: "Pull request: acme/app#12" }).textContent).toBe("app#12");
      expect(why.textContent).not.toContain("issue");
      // Each reference names itself in full when its own ellipsis cuts it.
      expect(issue.getAttribute("title")).toBe("acme/app#7");
      expect(issue.getAttribute("data-blocked-ref")).toBe("acme/app#7");
      // A reference the code host reported merged is not waited for any more.
      expect(why.textContent).not.toContain("#9");
      expect(within(rowOf("Waits on a pull request")).getByTestId("work-item-waits").textContent).toMatch(/^Waits for/);
      expect(within(rowOf("Waits on words")).getByTestId("work-item-blocked-why").textContent).toContain("the UX verdict on the head");
      // The reason is its own element, named in full for when it truncates.
      const reason = within(rowOf("Waits on words")).getByTestId("work-item-blocked-reason");
      expect(reason.textContent).toBe("the UX verdict on the head");
      expect(within(rowOf("Says nothing")).getByTestId("work-item-no-reason").textContent).toBe("No reason");
      expect(within(rowOf("Waits on words")).queryByTestId("work-item-no-reason")).toBeNull();
      expect(within(rowOf("Says nothing")).queryByTestId("work-item-blocked-why")).toBeNull();
      // The toolbar counts the blocked items with no reason, next to the status filter.
      expect(screen.getByRole("button", { name: "1 blocked with no reason" })).toBe(screen.getByTestId("work-items-no-reason-count"));
    } finally {
      ITEMS = MIXED;
    }
  });

  it("names the pickup of a waiting row Waits for, the others Blocked on", () => {
    ITEMS = BLOCKED;
    try {
      renderTab();
      const pickupOf = (title: string) => within(rowOf(title)).getByTestId("work-item-pickup").getAttribute("aria-label");
      expect(pickupOf("Waits on the release")).toBe("Waits for 1 item");
      expect(pickupOf("Dependency cancelled")).toBe("Blocked on 2 items");
    } finally {
      ITEMS = MIXED;
    }
  });

  it("makes the compact pickup badge of a row a tab stop that announces its label", () => {
    ITEMS = BLOCKED;
    try {
      renderTab();
      const badge = within(rowOf("Waits on the release")).getByTestId("work-item-pickup");
      expect(badge.getAttribute("tabindex")).toBe("0");
      expect(badge.getAttribute("aria-label")).toBe("Waits for 1 item");
      badge.focus();
      expect(document.activeElement).toBe(badge);
    } finally {
      ITEMS = MIXED;
    }
  });

  it("draws a neutral row icon on a waiting row and the error icon on the others", () => {
    ITEMS = BLOCKED;
    try {
      renderTab();
      const iconOf = (title: string) => within(rowOf(title)).getByTestId("work-item-status-icon").getAttribute("data-tone");
      expect(iconOf("Waits on the release")).toBe("waiting");
      for (const title of ["Scheduled leftover", "Dependencies all done", "Dependency cancelled"]) {
        expect(iconOf(title)).toBe("blocked");
      }
    } finally {
      ITEMS = MIXED;
    }
  });

  it("never shows a platform claim note as the reason of a cancelled dependency", () => {
    const note = "still blocked: ed7f69be-3c50-468c-8301-74c76971752a cancelled; put it back in the queue or change blocked_on";
    ITEMS = [
      withContext("n", "Cancelled with a note", [blockerRef("b5", "Dropped release", "cancelled")], {
        claim_notes: [{ at: "2026-11-02T22:00:00Z", text: note }],
      }),
    ];
    try {
      const { container } = renderTab();
      expect(container.textContent).not.toContain("ed7f69be");
      expect(blockedReason({ result: null, claim_notes: [{ at: "2026-11-02T22:00:00Z", text: note }] } as WorkItem)).toBeNull();
      expect(cancelledBlockers(ITEMS[0]).map((b) => b.title)).toEqual(["Dropped release"]);
    } finally {
      ITEMS = MIXED;
    }
  });

  it("says on the Blocked filter how many wait and how many need an action", () => {
    ITEMS = BLOCKED;
    try {
      renderTab();
      fireEvent.click(screen.getByTestId("work-items-status-pill"));
      const blocked = screen.getByTestId("work-items-status-blocked");
      expect(blocked?.getAttribute("title")).toBe("1 waiting on dependencies, 3 needing an action");
    } finally {
      ITEMS = MIXED;
    }
  });
});

// Focus after a confirmed Mark done: the confirm clears the
// closing item before the dialog's close focus event, so the target is
// recorded when the dialog opens. A row that stays (All) gets focus on its actions menu; a row
// that leaves on the refetch (Open) hands focus to the next row's menu.
describe("Work Items focus after a confirmed Mark done", () => {
  const TWO: WorkItem[] = [base({ id: "f1", title: "First item" }), base({ id: "f2", title: "Second item" })];
  const actionsOf = (title: string) => within(rowOf(title)).getByRole("button", { name: "Work item actions" });

  async function confirmMarkDone(title: string) {
    fireEvent.click(within(rowOf(title)).getByText("Mark done"));
    expect(await screen.findByText("Mark this work item done?")).toBeTruthy();
    fireEvent.click(screen.getByTestId("work-item-close-confirm"));
    await vi.waitFor(() => expect(screen.queryByText("Mark this work item done?")).toBeNull());
  }

  it("focuses the row's actions menu when the row stays in the list (All)", async () => {
    ITEMS = TWO;
    try {
      renderTab();
      await confirmMarkDone("First item");
      await vi.waitFor(() => expect(document.activeElement).toBe(actionsOf("First item")));
    } finally {
      ITEMS = MIXED;
    }
  });

  it("focuses the next row's actions menu when the row leaves the list (Open)", async () => {
    ITEMS = TWO;
    try {
      const { rerender } = renderTab();
      await confirmMarkDone("First item");
      await vi.waitFor(() => expect(document.activeElement).toBe(actionsOf("First item")));
      ITEMS = [TWO[1]];
      rerender(
        <MemoryRouter initialEntries={["/operations?vis=mine"]}>
          <WorkItemsTab />
        </MemoryRouter>,
      );
      await vi.waitFor(() => expect(document.activeElement).toBe(actionsOf("Second item")));
    } finally {
      ITEMS = MIXED;
    }
  });

  // The real mutation awaits its refetch (onSuccess returns invalidateQueries),
  // so on the Open filter the row has left the DOM BEFORE the dialog closes:
  // the target recorded at open must still land.
  async function confirmWithRefetch(title: string, remaining: WorkItem[]) {
    const { rerender } = renderTab();
    idle.mutateAsync.mockImplementationOnce(async () => {
      // Leave the click's act() first, so the refetch renders on its own.
      await new Promise((resolve) => setTimeout(resolve, 0));
      ITEMS = remaining;
      act(() => {
        rerender(
          <MemoryRouter initialEntries={["/operations?vis=mine"]}>
            <WorkItemsTab />
          </MemoryRouter>,
        );
      });
      // Observed, not asserted here: a throw would reject the mutation.
      rowGoneWhileOpen = {
        rows: screen.queryAllByTestId("work-item-row").map((r) => r.dataset.workItemId ?? ""),
        dialogOpen: screen.queryAllByText("Mark this work item done?").length > 0,
      };
    });
    await confirmMarkDone(title);
    expect(rowGoneWhileOpen).toEqual({ rows: remaining.map((i) => i.id), dialogOpen: true });
  }
  let rowGoneWhileOpen: { rows: string[]; dialogOpen: boolean } | null = null;

  it("focuses the next row's actions menu when the refetch drops the row before the dialog closes (Open)", async () => {
    ITEMS = TWO;
    try {
      await confirmWithRefetch("First item", [TWO[1]]);
      await vi.waitFor(() => expect(document.activeElement).toBe(actionsOf("Second item")));
    } finally {
      ITEMS = MIXED;
    }
  });

  it("focuses the previous row's actions menu when the last row is dropped before the dialog closes (Open)", async () => {
    ITEMS = TWO;
    try {
      await confirmWithRefetch("Second item", [TWO[0]]);
      await vi.waitFor(() => expect(document.activeElement).toBe(actionsOf("First item")));
    } finally {
      ITEMS = MIXED;
    }
  });

  // The only row of the view leaves: no neighbour, so focus goes to the
  // Status pill of the toolbar (slice 8d), never the page body.
  it("focuses the Status pill when the only row is dropped before the dialog closes (Open)", async () => {
    ITEMS = [TWO[0]];
    try {
      await confirmWithRefetch("First item", []);
      const pill = screen.getByTestId("work-items-status-pill");
      await vi.waitFor(() => expect(document.activeElement).toBe(pill));
      expect(document.activeElement).not.toBe(document.body);
    } finally {
      ITEMS = MIXED;
    }
  });
});

// A cleared "Waiting for me" queue says so and what lands there, never the
// agent setup hint of the default empty list.
describe("Work Items Waiting for me view, empty", () => {
  it("says nothing is waiting and what lands here", () => {
    ITEMS = [];
    try {
      // The view narrows the list from the Filters pill (slice 8d): the table's no-results
      // line says it in the view's words, with its Clear all.
      const { container } = renderTab("&view=waiting");
      const text = container.textContent ?? "";
      expect(text).toContain("Nothing is waiting for you");
      expect(text).toContain("Clear all filters");
      expect(text).not.toContain("No open work items");
      expect(text).not.toContain("Enable the Work Items built-in tool");
    } finally {
      ITEMS = MIXED;
    }
  });
});

// With the read switch on, "Waiting for me" is the decision inbox. Only a server answer
// `enabled: false` falls back to the work items list: a pending read shows the inbox
// skeleton and a failed one its error line with Retry, never the work items rows
// (UX r2 B1 on #1081).
describe("Work Items Waiting for me view, inbox read states", () => {
  it("shows the inbox skeleton while the read is pending", () => {
    WAITING = { data: undefined, isLoading: true, isError: false };
    try {
      renderTab("&view=waiting");
      expect(screen.getByTestId("waiting-for-you")).toBeTruthy();
      expect(screen.getAllByTestId("data-table-skeleton-row").length).toBeGreaterThan(0);
      expect(screen.queryAllByTestId("work-item-row")).toHaveLength(0);
    } finally {
      WAITING = SWITCH_OFF;
    }
  });

  it("shows the inbox error line with Retry when the read fails", () => {
    WAITING = { data: undefined, isLoading: false, isError: true };
    WAITING_REFETCH.mockClear();
    try {
      renderTab("&view=waiting");
      const inbox = screen.getByTestId("waiting-for-you");
      expect(inbox.textContent).toContain("Could not load what is waiting for you.");
      expect(screen.queryAllByTestId("work-item-row")).toHaveLength(0);
      fireEvent.click(within(inbox).getByRole("button", { name: "Retry" }));
      expect(WAITING_REFETCH).toHaveBeenCalledTimes(1);
    } finally {
      WAITING = SWITCH_OFF;
    }
  });

  it("keeps the work items list when the server says the read switch is off", () => {
    renderTab("&view=waiting");
    expect(screen.queryByTestId("waiting-for-you")).toBeNull();
  });
});

// The list in the app language: filters, the row's relative time and
// its due date come from the catalog and the locale formatter, never English.
describe("Work Items list in French", () => {
  it("shows no English filter, status or time string", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-03T10:00:00Z"));
    ITEMS = [
      base({ id: "fr1", title: "Relire la demande", due_at: "2026-11-04T08:30:00Z", last_activity_at: "2026-11-03T09:54:00Z" }),
    ];
    try {
      const { container } = renderWithProviders(
        <MemoryRouter initialEntries={["/operations?vis=mine"]}>
          <WorkItemsTab />
        </MemoryRouter>,
        { locale: "fr-fr" },
      );
      // The view filter sits in the Filters pill (#10215 criterion 6), the statuses in the Status pill (8d).
      fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${fr.Filters}`) }));
      fireEvent.click(screen.getByTestId("work-items-status-pill"));
      const text = container.textContent ?? "";
      for (const english of ["In Progress", "In progress", "All items", "Claimed", "Cancelled", "updated", " ago", "due "]) {
        expect(text).not.toContain(english);
      }
      expect(text).toContain(fr["In progress"]);
      expect(text).toContain(fr["All items"]);
      expect(text).toContain("il y a 6 minutes");
    } finally {
      ITEMS = MIXED;
      vi.useRealTimers();
    }
  });
});

// The URL the tab last wrote, read by the probe beside it (a mutated holder, never a reassigned variable).
const LOCATION = { search: "" };
function LocationProbe() {
  const { search } = useLocation();
  LOCATION.search = search;
  return null;
}

// Clear all filters clears every filter of the page, not only the table's own keys (#1001):
// the view, owner, fleet, status and search leave the URL in one write; the open item, the
// segment and the sort stay.
describe("Work Items Clear all filters", () => {
  const renderAt = (query: string) =>
    renderWithProviders(
      <MemoryRouter initialEntries={[`/operations?vis=mine${query}`]}>
        <WorkItemsTab />
        <LocationProbe />
      </MemoryRouter>,
    );

  it("drops view, owner, fleet, status and search, keeps item, vis and sort", () => {
    // The fleet key waits on the reader's fleet access here; clearFilterParams' own test covers it.
    renderAt("&view=waiting&agent=agent-1&status=blocked&wi_q=deploy&q=old&stale=stale&wi_sort=due:asc&item=regular&wi_page=2");
    // `item` opens the detail dialog, which hides the page behind it from the role query.
    fireEvent.click(screen.getAllByRole("button", { name: "Clear all filters", hidden: true })[0]);
    const params = new URLSearchParams(LOCATION.search);
    for (const key of ["view", "agent", "status", "wi_q", "q", "stale", "wi_page"]) expect(params.has(key), key).toBe(false);
    expect(params.get("vis")).toBe("mine");
    expect(params.get("item")).toBe("regular");
    expect(params.get("wi_sort")).toBe("due:asc");
    expect(lastListRead()).toMatchObject({ open_only: true });
  });

  // UX round 1 on #1004, B1: a Status or a search alone narrows the list, so Clear all shows.
  it("offers Clear all for a Status alone and clears it", () => {
    renderAt("&status=blocked");
    fireEvent.click(screen.getAllByRole("button", { name: "Clear all filters" })[0]);
    expect(new URLSearchParams(LOCATION.search).has("status")).toBe(false);
  });

  it("offers Clear all for a search alone and clears it", () => {
    renderAt("&wi_q=deploy");
    fireEvent.click(screen.getAllByRole("button", { name: "Clear all filters" })[0]);
    expect(new URLSearchParams(LOCATION.search).has("wi_q")).toBe(false);
  });

  it("shows no Clear all on the default open view", () => {
    renderAt("");
    expect(screen.queryAllByRole("button", { name: "Clear all filters" })).toHaveLength(0);
  });

  it("clears from the Waiting for me empty state too", () => {
    ITEMS = [];
    try {
      renderAt("&view=waiting&status=blocked");
      // The view narrows the list, so the empty list is the table's no-results line and its Clear all.
      fireEvent.click(within(screen.getByTestId("data-table-no-results")).getByRole("button", { name: "Clear all filters" }));
      const params = new URLSearchParams(LOCATION.search);
      expect(params.has("view")).toBe(false);
      expect(params.has("status")).toBe(false);
    } finally {
      ITEMS = MIXED;
    }
  });
});

// The quick filters on the row (#1001): Waiting for me and Stale with the server's count,
// the same `view` key as the Filters pill, a second click clears it.
describe("Work Items quick filters", () => {
  it("toggles the view from the row, synced with the Filters pill", () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/operations?vis=mine"]}>
        <WorkItemsTab />
        <LocationProbe />
      </MemoryRouter>,
    );
    const quick = screen.getByRole("group", { name: "Quick filters" });
    expect(screen.getByTestId("work-items-filter-row")).toContainElement(quick);
    const waiting = within(quick).getByRole("button", { name: /Waiting for me/ });
    expect(waiting.textContent).toContain("2");
    fireEvent.click(waiting);
    expect(new URLSearchParams(LOCATION.search).get("view")).toBe("waiting");
    expect(lastListRead()).toMatchObject({ waiting_for_me: true });
    expect(within(screen.getByRole("group", { name: "Quick filters" })).getByRole("button", { name: /Waiting for me/ }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
    const view = within(screen.getByRole("group", { name: "Filters" })).getByRole("group", { name: "View filter" });
    expect(within(view).getByRole("button", { name: "Waiting for me" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(within(screen.getByRole("group", { name: "Quick filters" })).getByRole("button", { name: /Waiting for me/ }));
    expect(new URLSearchParams(LOCATION.search).has("view")).toBe(false);
  });

  it("counts every open item waiting for the reader, whatever the segment and filters (#1499)", () => {
    COUNT_READS.length = 0;
    renderWithProviders(
      <MemoryRouter initialEntries={["/operations?vis=company&status=blocked&agent=a1&wi_q=grafana"]}>
        <WorkItemsTab />
        <LocationProbe />
      </MemoryRouter>,
    );
    const read = lastWaitingCountRead();
    expect(read).toMatchObject({ open_only: true, waiting_for_me: true });
    expect(read?.vis).toBeUndefined();
    expect(read?.status).toBeUndefined();
    expect(read?.owner_agent_id).toBeUndefined();
    expect(read?.search).toBeUndefined();
  });

  it("counts inside a locked fleet when an administrator reads scope=all (#1499)", () => {
    IS_ADMIN = true;
    try {
      COUNT_READS.length = 0;
      renderWithProviders(
        <MemoryRouter initialEntries={["/operations?scope=all"]}>
          <WorkItemsTab fleet="fleet-1" />
        </MemoryRouter>,
      );
      expect(lastWaitingCountRead()).toMatchObject({ open_only: true, waiting_for_me: true, fleet: "fleet-1" });
    } finally {
      IS_ADMIN = false;
    }
  });

  it("resets every other filter and lands on All when turned on (#1499)", () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/operations?vis=company&status=blocked&agent=a1&wi_q=grafana"]}>
        <WorkItemsTab />
        <LocationProbe />
      </MemoryRouter>,
    );
    fireEvent.click(within(screen.getByRole("group", { name: "Quick filters" })).getByRole("button", { name: /Waiting for me/ }));
    const params = new URLSearchParams(LOCATION.search);
    expect(params.get("view")).toBe("waiting");
    expect(params.get("vis")).toBe("all");
    expect(params.has("status")).toBe(false);
    expect(params.has("agent")).toBe(false);
    expect(params.has("wi_q")).toBe(false);
    expect(lastListRead()).toMatchObject({ waiting_for_me: true, open_only: true });
    expect(lastListRead()?.vis).toBeUndefined();
  });

  it("marks the rows that wait for the reader", () => {
    ITEMS = [
      base({ id: "ask", title: "Approve the plan", status: "blocked", needs_human: true, user_id: "u1" }),
      base({ id: "other", title: "Someone else's", status: "blocked", needs_human: true, user_id: "u1", waits_for_user_id: "u2" }),
    ];
    try {
      renderTab();
      expect(within(rowOf("Approve the plan")).queryByTestId("work-item-waiting-for-you")).not.toBeNull();
      expect(within(rowOf("Someone else's")).queryByTestId("work-item-waiting-for-you")).toBeNull();
    } finally {
      ITEMS = MIXED;
    }
  });
});
