// The scheduled marker: what a scheduled work item says wherever it is
// listed, and the chat card a [[WORK_ITEM:id]] marker renders.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { IntlProvider } from "react-intl";
import { MemoryRouter } from "react-router-dom";

import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { STATUS_LABELS } from "@/lib/work-item-context";

import { renderWithProviders } from "@/testing/providers";
import { answerParts, answerProse, markerRefs } from "@/pages/chat/answerMarkers";
import type { WorkItem, WorkItemScheduled } from "@/types";

const viewer = vi.hoisted(() => ({ timezone: "Europe/Paris" }));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: viewer.timezone }, isAdmin: false }),
}));

const item = (scheduled: Partial<WorkItemScheduled> = {}, overrides: Partial<WorkItem> = {}) =>
  ({
    id: "11111111-2222-3333-4444-555555555555",
    user_id: "u1",
    is_company_managed: false,
    owner_agent_id: "agent-1",
    created_by_agent_id: "agent-1",
    title: "Post the EMT reminder",
    description: null,
    payload: {},
    status: "open",
    priority: "medium",
    correlation_key: null,
    due_at: "2026-10-25T08:00:00Z",
    claimed_by_agent_id: null,
    claimed_at: null,
    source: "agent",
    result: null,
    is_stale: false,
    last_activity_at: null,
    created_at: null,
    updated_at: null,
    ...overrides,
    scheduled: {
      mode: "exact",
      tool: "send_teams_channel_message",
      target: "Testing XTM One",
      preview: "Test",
      state: "scheduled",
      run_id: null,
      conversation_id: null,
      outcome_code: null,
      ...scheduled,
    },
  }) as WorkItem & { scheduled: WorkItemScheduled };

const workItemQuery = vi.hoisted(() => ({ current: { data: undefined as unknown, isLoading: false, isError: false } }));
vi.mock("@/hooks/api/useWorkItemApi", () => ({
  useWorkItem: () => workItemQuery.current,
  useCancelScheduledWorkItem: () => ({ mutate: vi.fn(), isPending: false }),
  useRescheduleScheduledWorkItem: () => ({ mutate: vi.fn(), isPending: false }),
}));

const { ScheduledDetails, ScheduledSummary, ScheduleStateBadge } = await import("./ScheduledDetails");
const { ScheduledWorkItemCard } = await import("./ScheduledWorkItemCard");
const { ScheduleActions } = await import("./ScheduleActions");

function inRouter(ui: React.ReactElement) {
  return renderWithProviders(<MemoryRouter>{ui}</MemoryRouter>);
}

describe("the scheduled marker", () => {
  it("shows the time in the viewer's zone and in UTC, the tool, the target and the preview", () => {
    inRouter(<ScheduledDetails item={item()} />);
    const details = screen.getByTestId("scheduled-details");
    // 25 Oct 2026 08:00 UTC is 09:00 in Paris, after the daylight saving change.
    expect(details.textContent).toMatch(/09:00/);
    expect(details.textContent).toMatch(/\(08:00 UTC\)/);
    expect(screen.getByText("Exact: send_teams_channel_message")).toBeTruthy();
    expect(screen.getByText("Testing XTM One")).toBeTruthy();
    expect(screen.getByText("Test")).toBeTruthy();
    expect(screen.queryByText("View the run")).toBeNull();
  });

  it("shows the time once when the viewer's zone is UTC", () => {
    viewer.timezone = "UTC";
    try {
      inRouter(
        <>
          <ScheduledSummary item={item()} />
          <ScheduledDetails item={item()} />
        </>,
      );
      for (const id of ["scheduled-summary", "scheduled-details"]) {
        const text = screen.getByTestId(id).textContent ?? "";
        expect(text).toMatch(/08:00/);
        expect(text).not.toMatch(/\(08:00 UTC\)/);
        expect(text.match(/08:00/g)).toHaveLength(1);
      }
    } finally {
      viewer.timezone = "Europe/Paris";
    }
  });

  it("keeps the UTC time in parentheses in the list row for a viewer outside UTC", () => {
    inRouter(<ScheduledSummary item={item()} />);
    const text = screen.getByTestId("scheduled-summary").textContent ?? "";
    expect(text).toMatch(/09:00/);
    expect(text).toMatch(/\(08:00 UTC\)/);
  });

  it("shows the outcome and links the run once it ran", () => {
    inRouter(
      <ScheduledDetails
        item={item(
          { state: "done", run_id: "99999999-8888-7777-6666-555555555555" },
          { status: "done", result: '{"message_id": "1700000000000"}' },
        )}
      />,
    );
    expect(screen.getByText('{"message_id": "1700000000000"}')).toBeTruthy();
    const link = screen.getByText("View the run").closest("a");
    expect(link?.getAttribute("href")).toBe("/operations/executions/99999999-8888-7777-6666-555555555555");
  });

  it("keeps the link token on a link in the toned outcome", () => {
    inRouter(
      <ScheduledDetails
        full
        item={item(
          { state: "done", run_id: null },
          { status: "done", result: "Posted, see [the message](https://example.com/m/1)" },
        )}
      />,
    );
    const link = screen.getByRole("link", { name: "the message" });
    expect(link.className).toContain("text-accent");
    // The outcome's success or warning tone is not on the link itself.
    expect(link.className.split(" ").some((c) => c.includes("feedback"))).toBe(false);
  });

  it("says missed, not stale, for an item nobody could run in time", () => {
    inRouter(<ScheduleStateBadge scheduled={item({ state: "missed", outcome_code: "missed" }).scheduled} />);
    expect(screen.getByText("Missed")).toBeTruthy();
    expect(screen.queryByText(/stale/i)).toBeNull();
  });
});

describe("the chat card", () => {
  it("renders the item with Cancel and Reschedule while it waits", () => {
    workItemQuery.current = { data: item(), isLoading: false, isError: false };
    inRouter(<ScheduledWorkItemCard workItemId={item().id} />);
    expect(screen.getByTestId("scheduled-work-item-card")).toBeTruthy();
    expect(screen.getByText("Post the EMT reminder")).toBeTruthy();
    expect(screen.getByText("Scheduled")).toBeTruthy();
    expect(screen.getByText("Reschedule")).toBeTruthy();
    expect(screen.getByText("Cancel")).toBeTruthy();
  });

  it("offers both as labelled icon buttons in a list row", () => {
    inRouter(<ScheduleActions item={item()} iconOnly />);
    expect(screen.getByRole("button", { name: "Reschedule" }).textContent).toBe("");
    expect(screen.getByRole("button", { name: "Cancel schedule" }).textContent).toBe("");
  });

  it("opens the reschedule editor in a dialog, not in the row", () => {
    const { container } = inRouter(<ScheduleActions item={item()} iconOnly />);
    fireEvent.click(screen.getByRole("button", { name: "Reschedule" }));
    const dialog = screen.getByTestId("schedule-reschedule-dialog");
    expect(dialog.querySelector('input[type="datetime-local"]')).toBeTruthy();
    expect(container.querySelector('input[type="datetime-local"]')).toBeNull();
    // A visible label names the field and its zone; Keep current time then Save close the form.
    expect(screen.getByLabelText("New time (Europe/Paris)")).toBeTruthy();
    const actions = Array.from(dialog.querySelectorAll("button")).map((b) => b.textContent?.trim()).filter(Boolean);
    expect(actions.slice(-2)).toEqual(["Keep current time", "Save"]);
    // The footer button and the corner X each have a name of their own.
    expect(screen.getAllByRole("button", { name: "Keep current time" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Close" })).toHaveLength(1);
    // Close it: the dialog portals to document.body, which the next test shares.
    fireEvent.click(screen.getByRole("button", { name: "Keep current time" }));
    expect(screen.queryByTestId("schedule-reschedule-dialog")).toBeNull();
  });

  it.each([
    ["Escape", () => fireEvent.keyDown(document.activeElement as Element, { key: "Escape" })],
    ["Keep current time", () => fireEvent.click(screen.getByRole("button", { name: "Keep current time" }))],
    ["the corner X", () => fireEvent.click(screen.getByRole("button", { name: "Close" }))],
  ])("gives focus back to the row's Reschedule button after %s", async (_how, close) => {
    inRouter(<ScheduleActions item={item()} iconOnly />);
    const trigger = screen.getByRole("button", { name: "Reschedule" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByTestId("schedule-reschedule-dialog")).toBeTruthy();
    close();
    await vi.waitFor(() => expect(screen.queryByTestId("schedule-reschedule-dialog")).toBeNull());
    await vi.waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("offers neither once it ran", () => {
    workItemQuery.current = {
      data: item({ state: "done" }, { status: "done", result: "ok" }),
      isLoading: false,
      isError: false,
    };
    inRouter(<ScheduledWorkItemCard workItemId={item().id} />);
    expect(screen.queryByText("Reschedule")).toBeNull();
    expect(screen.queryByText("Cancel")).toBeNull();
  });

  it.each(["blocked", "missed"] as const)("offers Cancel on a %s leftover", (state) => {
    inRouter(
      <ScheduleActions item={item({ state, outcome_code: state === "missed" ? "missed" : "binding" }, { status: "blocked" })} iconOnly />,
    );
    expect(screen.getByRole("button", { name: "Cancel schedule" })).toBeTruthy();
  });

  // A scheduled action never shows Reschedule after a rollout: the
  // platform resumed it or ran it again for its agent; nothing is asked.
  it.each([
    ["interrupted", { resumed: 1, requeued: 0 }],
    ["resume_exhausted", undefined],
    ["execution_error", { resumed: 2, requeued: 0 }],
    [null, { resumed: 0, requeued: 1 }],
  ] as const)("offers neither after a rollout (outcome %s)", (outcome_code, restarts) => {
    const cut = item({ state: "blocked", outcome_code, restarts }, { status: "blocked" });
    workItemQuery.current = { data: cut, isLoading: false, isError: false };
    inRouter(<ScheduledWorkItemCard workItemId={cut.id} />);
    expect(screen.queryByText("Reschedule")).toBeNull();
    expect(screen.queryByText("Cancel")).toBeNull();
    inRouter(<ScheduleActions item={cut} iconOnly />);
    expect(screen.queryByRole("button", { name: "Reschedule" })).toBeNull();
  });

  it("keeps Cancel on an item an operator stopped, which is not a restart", () => {
    inRouter(<ScheduleActions item={item({ state: "blocked", outcome_code: "interrupted" }, { status: "blocked" })} iconOnly />);
    expect(screen.getByRole("button", { name: "Cancel schedule" })).toBeTruthy();
  });

  it("says so when the item is gone", () => {
    workItemQuery.current = { data: undefined, isLoading: false, isError: true };
    inRouter(<ScheduledWorkItemCard workItemId={item().id} />);
    expect(screen.getByText("This scheduled work item is no longer available.")).toBeTruthy();
  });
});

describe("scheduled dates and labels in the app language", () => {
  const ENGLISH_DATE = /\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b/;

  function inJapanese(ui: React.ReactElement) {
    return render(
      <IntlProvider locale="ja" messages={ja} onError={() => {}}>
        <MemoryRouter>{ui}</MemoryRouter>
      </IntlProvider>,
    );
  }

  it("writes the row date and the details date in Japanese, not in the browser's locale", () => {
    inJapanese(<ScheduledSummary item={item()} />);
    const summary = screen.getByTestId("scheduled-summary").textContent ?? "";
    expect(summary).toMatch(/09:00/);
    expect(summary).toMatch(/2026年/);
    expect(summary).not.toMatch(ENGLISH_DATE);
  });

  it("writes the details date in Japanese too", () => {
    inJapanese(<ScheduledDetails item={item()} />);
    const details = screen.getByTestId("scheduled-details").textContent ?? "";
    expect(details).toMatch(/2026年/);
    expect(details).not.toMatch(ENGLISH_DATE);
  });

  it("puts no space before に in the ja \"updated {when}\"", () => {
    expect((ja as Record<string, string>)["updated {when}"]).toBe("{when}に更新");
  });

  it("names the Open status with an adjective, never the verb Open", () => {
    expect(STATUS_LABELS.open).not.toBe("Open");
    expect((fr as Record<string, string>)[STATUS_LABELS.open]).toBe("Ouvert");
    expect((es as Record<string, string>)[STATUS_LABELS.open]).toBe("Abierto");
    expect((ja as Record<string, string>)[STATUS_LABELS.open]).toBeTruthy();
  });
});

describe("the chat marker", () => {
  it("is found once and stripped from the text", () => {
    const id = item().id;
    const content = `Scheduled.\n\n[[WORK_ITEM:${id}]]\n[[WORK_ITEM:${id.toUpperCase()}]]`;
    expect(markerRefs(answerParts(content), "WORK_ITEM").map((ref) => ref.id)).toEqual([id]);
    expect(answerProse(content)).toBe("Scheduled.");
  });
});
