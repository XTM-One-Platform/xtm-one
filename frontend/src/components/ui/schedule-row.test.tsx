// The rows of a schedule list: two lines of one height whatever they carry.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { humanizeCron } from "@/lib/cron";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import type { AssignmentSchedule, ProviderCatalogItem, ScheduledWorkItem } from "@/types";
import { OneOffRow, ScheduleRow } from "./schedule-row";

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { timezone: "Europe/Paris" } }) }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(() => new Promise(() => undefined)), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const NOW = Date.parse("2026-11-01T12:00:00Z");

function schedule(over: Partial<AssignmentSchedule> = {}): AssignmentSchedule {
  return {
    assignment_id: "s1",
    assignment_name: "Morning digest",
    agent_id: "a",
    agent_name: "Ops agent",
    agent_icon: "bot",
    trigger_type: "schedule",
    enabled: true,
    cron: "0 7 * * *",
    next_run_at: "2026-11-02T06:00:00+00:00",
    timezone: "Europe/Paris",
    next_run_local: "2026-11-02T07:00:00+01:00",
    delay_seconds: 0,
    last_triggered_at: null,
    has_active_run: false,
    pending_run_at: null,
    queued_run_count: 0,
    provider: null,
    event: null,
    poll_interval_seconds: null,
    webhook_url: null,
    ...over,
  };
}

function oneOff(over: Partial<ScheduledWorkItem> = {}): ScheduledWorkItem {
  return {
    id: "w1",
    title: "Post the weekly note",
    due_at: "2026-11-04T08:00:00+00:00",
    scheduled: {
      mode: "exact",
      tool: "send_message",
      target: "EMT channel",
      preview: "",
      state: "scheduled",
      run_id: null,
      conversation_id: null,
      outcome_code: null,
    },
    agent_name: "Assistant",
    agent_icon: null,
    ...over,
  } as ScheduledWorkItem;
}

function drawRow(s: AssignmentSchedule) {
  return renderWithProviders(
    <MemoryRouter>
      <ScheduleRow s={s} nowMs={NOW} zone="Europe/Paris" onClick={() => undefined} />
    </MemoryRouter>,
  );
}

describe("a schedule row", () => {
  it("is two fixed lines whatever it carries", () => {
    const variants = [
      schedule(),
      schedule({ last_triggered_at: "2026-11-01T11:55:00+00:00", last_run_status: "failed", has_active_run: true }),
      schedule({ cron: "*/5 9-17 * * 1-5", queued_run_count: 3, timezone_warning: "timezone 'Mars/Olympus' not recognised, UTC used" }),
      schedule({ trigger_type: "webhook", cron: null, next_run_at: null, next_run_local: null }),
      schedule({ trigger_type: "manual", cron: null, next_run_at: null, next_run_local: null }),
    ];
    const heights = new Set<string>();
    for (const s of variants) {
      const { unmount } = drawRow(s);
      const row = screen.getByTestId("schedule-row");
      heights.add([...row.classList].filter((c) => /^h-\d+$/.test(c)).join(" "));
      // Exactly two lines in the text column, and two in the time column.
      const [text, when] = [row.children[1], row.children[2]];
      expect(text.children).toHaveLength(2);
      expect(when.children).toHaveLength(2);
      unmount();
    }
    expect([...heights]).toEqual(["h-14"]);
  });

  it("says the next run as a countdown, then in the viewer's zone with UTC muted", () => {
    drawRow(schedule());
    expect(screen.getByTestId("schedule-row-countdown").textContent).toBe("in 18h 0m");
    const absolute = screen.getByTestId("schedule-row-absolute").textContent ?? "";
    expect(absolute).toContain("07:00");
    expect(absolute).toContain("06:00 UTC");
  });

  it("does not repeat the time in UTC for a viewer in UTC", () => {
    renderWithProviders(
      <MemoryRouter>
        <ScheduleRow s={schedule()} nowMs={NOW} zone="UTC" onClick={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("schedule-row-absolute").textContent).not.toContain("UTC");
  });

  it("shows no outcome dot for a status it does not know", () => {
    drawRow(
      schedule({
        last_triggered_at: "2026-11-01T11:55:00+00:00",
        last_run_status: "paused",
        last_run_finished_at: "2026-11-01T11:56:00+00:00",
      }),
    );
    expect(screen.queryByTestId("schedule-row-outcome")).toBeNull();
  });

  it("gives the schedule, the owner and the last run as tokens", () => {
    drawRow(
      schedule({
        last_triggered_at: new Date(Date.now() - 40 * 60_000).toISOString(),
        last_run_status: "completed",
        last_run_finished_at: new Date(Date.now() - 5 * 60_000).toISOString(),
      }),
    );
    expect(screen.getByTestId("schedule-row-trigger").textContent).not.toBe("");
    expect(screen.getByTestId("schedule-row-owner").textContent).toBe("Ops agent");
    const last = screen.getByTestId("schedule-row-last-run");
    // The time beside the dot is the finished run's, not the last trigger's.
    expect(last.textContent).toContain("5m ago");
    expect(within(last).getByTestId("schedule-row-outcome").dataset.outcome).toBe("completed");
  });

  it("shows the last trigger without a dot when no finished run is readable", () => {
    drawRow(
      schedule({
        last_triggered_at: new Date(Date.now() - 5 * 60_000).toISOString(),
        last_run_status: null,
        last_run_finished_at: null,
        has_active_run: true,
      }),
    );
    expect(screen.getByTestId("schedule-row-last-run").textContent).toContain("5m ago");
    expect(screen.queryByTestId("schedule-row-outcome")).toBeNull();
  });

  it("says a trigger that never ran so, without an outcome dot", () => {
    drawRow(schedule({ last_run_status: "failed" }));
    expect(screen.getByTestId("schedule-row-last-run").textContent).toContain("No run yet");
    expect(screen.queryByTestId("schedule-row-outcome")).toBeNull();
  });

  it("words its status chips and trigger phrase through the catalog", () => {
    drawRow(
      schedule({
        trigger_type: "integration_event",
        cron: null,
        next_run_at: null,
        next_run_local: null,
        event: "new_slack_message",
        provider: "slack",
        poll_interval_seconds: 300,
        has_active_run: true,
        queued_run_count: 2,
      }),
    );
    expect(screen.getByText("In flight")).toBeTruthy();
    expect(screen.getByTestId("schedule-row-queued").textContent).toBe("2 queued");
    // No catalog read yet: the provider still reads by its display name.
    expect(screen.getByTestId("schedule-row-trigger").textContent).toBe(
      "On new slack message from Slack, polled every 5m",
    );
    expect(screen.getByText("Integration event")).toBeTruthy();
  });

  it("names the event by its catalog label and the provider by its display name", () => {
    const client = testQueryClient();
    client.setQueryData(["provider-catalog", "all", false], [
      { provider: "slack", name: "Slack", available_events: [{ event: "new_slack_message", label: "New message" }] },
    ] as unknown as ProviderCatalogItem[]);
    renderWithProviders(
      <MemoryRouter>
        <ScheduleRow
          s={schedule({
            trigger_type: "integration_event",
            cron: null,
            next_run_at: null,
            next_run_local: null,
            event: "new_slack_message",
            provider: "slack",
          })}
          nowMs={NOW}
          zone="Europe/Paris"
          onClick={() => undefined}
        />
      </MemoryRouter>,
      client,
    );
    expect(screen.getByTestId("schedule-row-trigger").textContent).toBe("On New message from Slack");
  });

  it("names a catalog event in the viewer's language, for every provider", () => {
    const client = testQueryClient();
    client.setQueryData(["provider-catalog", "all", false], [
      {
        provider: "microsoft",
        name: "Microsoft 365",
        available_events: [{ event: "new_teams_message", label: "New Teams message" }],
      },
    ] as unknown as ProviderCatalogItem[]);
    renderWithProviders(
      <MemoryRouter>
        <ScheduleRow
          s={schedule({
            trigger_type: "integration_event",
            cron: null,
            next_run_at: null,
            next_run_local: null,
            event: "new_teams_message",
            provider: "microsoft",
          })}
          nowMs={NOW}
          zone="Europe/Paris"
          onClick={() => undefined}
        />
      </MemoryRouter>,
      { client, locale: "fr-fr" },
    );
    const phrase = screen.getByTestId("schedule-row-trigger").textContent ?? "";
    expect(phrase).toContain("Nouveau message Teams");
    expect(phrase).not.toContain("New Teams message");
  });

  it("reads its countdown, last run, date and cron phrase in the viewer's language", () => {
    renderWithProviders(
      <MemoryRouter>
        <ScheduleRow
          s={schedule({
            last_triggered_at: new Date(Date.now() - 5 * 60_000).toISOString(),
            last_run_finished_at: null,
          })}
          nowMs={NOW}
          zone="Europe/Paris"
          onClick={() => undefined}
        />
      </MemoryRouter>,
      { locale: "fr-fr" },
    );
    expect(screen.getByTestId("schedule-row-countdown").textContent).toBe("dans 18 h 0 min");
    expect(screen.getByTestId("schedule-row-last-run").textContent).toContain("il y a 5 min");
    // The weekday and month in French (2 Nov 2026 is a Monday).
    expect(screen.getByTestId("schedule-row-absolute").textContent).toContain("lun.");
    const phrase = screen.getByTestId("schedule-row-trigger").textContent ?? "";
    expect(phrase).toBe(humanizeCron("0 7 * * *", "fr-fr"));
    expect(phrase).not.toBe(humanizeCron("0 7 * * *"));
  });

  it("says a manual trigger and a webhook in words of the catalog", () => {
    const { unmount } = drawRow(schedule({ trigger_type: "manual", cron: null, next_run_at: null, next_run_local: null }));
    expect(screen.getByTestId("schedule-row-trigger").textContent).toBe("Manual trigger");
    unmount();
    drawRow(schedule({ trigger_type: "webhook", cron: null, next_run_at: null, next_run_local: null }));
    expect(screen.getByTestId("schedule-row-trigger").textContent).toBe("On inbound webhook");
  });

  it("names its trigger kind where there is no next run", () => {
    drawRow(schedule({ trigger_type: "webhook", cron: null, next_run_at: null, next_run_local: null }));
    expect(screen.queryByTestId("schedule-row-countdown")).toBeNull();
    expect(screen.getByText("Webhook")).toBeTruthy();
  });
});

describe("a one-off row", () => {
  it("has the height of a schedule row, its actions on the same lines", () => {
    renderWithProviders(
      <MemoryRouter>
        <OneOffRow item={oneOff()} nowMs={NOW} zone="Europe/Paris" onOpen={() => undefined} />
      </MemoryRouter>,
    );
    const row = screen.getByTestId("one-off-row");
    expect(row.classList.contains("h-14")).toBe(true);
    expect(screen.getByTestId("schedule-row-countdown").textContent).toBe("in 2d 20h");
    expect(screen.getByTestId("schedule-row-trigger").textContent).toBe("send_message -> EMT channel");
    expect(screen.getByRole("button", { name: "Reschedule" })).toBeTruthy();
  });

  it("leads its action token with the server's preview", () => {
    renderWithProviders(
      <MemoryRouter>
        <OneOffRow
          item={oneOff({
            scheduled: { ...oneOff().scheduled, mode: "instruction", tool: null, target: null, preview: "Re-check CI" },
          })}
          nowMs={NOW}
          zone="Europe/Paris"
          onOpen={() => undefined}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("schedule-row-trigger").textContent).toBe("Re-check CI · Instruction");
  });

  it("does not open the item on Enter pressed on one of its actions", () => {
    const onOpen = vi.fn();
    renderWithProviders(
      <MemoryRouter>
        <OneOffRow item={oneOff()} nowMs={NOW} zone="Europe/Paris" onOpen={onOpen} />
      </MemoryRouter>,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Reschedule" }), { key: "Enter" });
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByTestId("one-off-row"), { key: "Enter" });
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
