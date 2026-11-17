// Scheduled work items: times in a zone, and the fleet timeline's merge.

import { describe, expect, it } from "vitest";
import { createIntl } from "react-intl";

import fr from "@/i18n/lang/fr.json";
import {
  buildTimelineLanes,
  canChangeSchedule,
  canReschedule,
  cutByRestart,
  formatCountdown,
  formatUtcTime,
  isUtcZone,
  isoToWallTime,
  timezoneWarningZone,
  wallTimeToOffsetIso,
  zoneOffsetMinutes,
  zonedNextRun,
} from "./scheduled-work-items";
import type { AssignmentSchedule, ScheduledWorkItem, WorkItemScheduleState } from "@/types";

const PARIS = "Europe/Paris";

describe("the countdown in the viewer's language", () => {
  const intl = createIntl({ locale: "fr-fr", messages: fr as Record<string, string>, onError: () => {} });
  const t = (id: string, values?: Record<string, string | number>) => intl.formatMessage({ id, defaultMessage: id }, values);
  const now = Date.parse("2026-11-03T06:00:00Z");
  const at = (minutes: number) => new Date(now + minutes * 60_000).toISOString();

  it("says when in a row", () => {
    expect(formatCountdown(at(23 * 60 + 59), now, t)).toBe("dans 23 h 59 min");
    expect(formatCountdown(at(11), now, t)).toBe("dans 11 min");
  });

  it("drops the prefix under a label that already says next (KPI card)", () => {
    expect(formatCountdown(at(23 * 60 + 59), now, t, { bare: true })).toBe("23 h 59 min");
    expect(formatCountdown(at(2 * 24 * 60 + 60), now, t, { bare: true })).toBe("2 j 1 h");
    expect(formatCountdown(at(11), now, t, { bare: true })).toBe("11 min");
    expect(formatCountdown(new Date(now + 20_000).toISOString(), now, t, { bare: true })).toBe("<1 min");
    // The passed form is a short word a 129 px card holds at 1280 (round 5 B1).
    expect(formatCountdown(at(-1), now, t, { bare: true })).toBe("imminent");
    expect(formatCountdown(at(-1), now, t)).toBe("maintenant");
  });
});

describe("a wall time picked in a zone", () => {
  // 25 Oct 2026: at 03:00 CEST (UTC+2) the clocks go back to 02:00 CET (UTC+1).
  it("is sent with the offset the zone has on that date", () => {
    expect(wallTimeToOffsetIso("2026-10-24T09:00", PARIS)).toBe("2026-10-24T09:00:00+02:00");
    expect(wallTimeToOffsetIso("2026-10-25T09:00", PARIS)).toBe("2026-10-25T09:00:00+01:00");
    expect(wallTimeToOffsetIso("2026-10-08T09:00", "UTC")).toBe("2026-10-08T09:00:00+00:00");
    expect(wallTimeToOffsetIso("2026-10-08T09:00", "America/New_York")).toBe("2026-10-08T09:00:00-04:00");
  });

  it("reads back as the same wall time across the change", () => {
    expect(isoToWallTime("2026-10-25T08:00:00Z", PARIS)).toBe("2026-10-25T09:00");
    expect(isoToWallTime("2026-10-24T07:00:00Z", PARIS)).toBe("2026-10-24T09:00");
    expect(zoneOffsetMinutes(Date.parse("2026-10-25T00:30:00Z"), PARIS)).toBe(120);
    expect(zoneOffsetMinutes(Date.parse("2026-10-25T01:30:00Z"), PARIS)).toBe(60);
  });

  it("refuses what is not a datetime-local value", () => {
    expect(wallTimeToOffsetIso("tomorrow at 9", PARIS)).toBeNull();
  });

  it("is said in UTC too", () => {
    expect(formatUtcTime("2026-10-25T09:00:00+01:00")).toBe("08:00 UTC");
  });

  it("knows UTC under its other names", () => {
    for (const zone of ["UTC", "Etc/UTC", "Etc/GMT", "GMT", "Zulu"]) expect(isUtcZone(zone)).toBe(true);
    for (const zone of ["Europe/Paris", "Europe/London", "Etc/GMT+1"]) expect(isUtcZone(zone)).toBe(false);
  });
});

describe("what can still be cancelled or rescheduled", () => {
  it("is what has not run", () => {
    const movable = (["scheduled", "due", "blocked", "missed"] as WorkItemScheduleState[]).map(canChangeSchedule);
    const settled = (["running", "done", "cancelled"] as WorkItemScheduleState[]).map(canChangeSchedule);
    expect(movable).toEqual([true, true, true, true]);
    expect(settled).toEqual([false, false, false]);
  });

  // A scheduled action never shows Reschedule after a rollout.
  it("is never an action a platform restart cut", () => {
    const states: WorkItemScheduleState[] = ["scheduled", "due", "running", "done", "blocked", "missed", "cancelled"];
    for (const state of states) {
      expect(canReschedule({ state, outcome_code: "interrupted", restarts: { resumed: 1, requeued: 0 } })).toBe(false);
      expect(canReschedule({ state, outcome_code: "resume_exhausted" })).toBe(false);
      expect(canReschedule({ state, outcome_code: null, restarts: { resumed: 1, requeued: 0 } })).toBe(false);
      expect(canReschedule({ state, outcome_code: "execution_error", restarts: { resumed: 0, requeued: 2 } })).toBe(false);
    }
    expect(cutByRestart({ outcome_code: "binding", restarts: { resumed: 0, requeued: 0 } })).toBe(false);
    expect(canReschedule({ state: "missed", outcome_code: "missed" })).toBe(true);
    expect(canReschedule({ state: "blocked", outcome_code: "binding" })).toBe(true);
    // An operator stop (emergency stop, cancelled run) is not a restart: it keeps its controls.
    expect(canReschedule({ state: "blocked", outcome_code: "interrupted" })).toBe(true);
    expect(canReschedule({ state: "blocked", outcome_code: "interrupted", restarts: { resumed: 0, requeued: 0 } })).toBe(
      true,
    );
  });
});

const NOW = Date.parse("2026-10-07T10:00:00Z");

function schedule(overrides: Partial<AssignmentSchedule> = {}): AssignmentSchedule {
  return {
    assignment_id: "a1",
    assignment_name: "Daily threat brief",
    agent_id: "agent-1",
    agent_name: "Analyst",
    agent_icon: "Bot",
    trigger_type: "schedule",
    enabled: true,
    cron: "0 * * * *",
    next_run_at: "2026-10-07T11:00:00Z",
    delay_seconds: 0,
    last_triggered_at: null,
    has_active_run: false,
    pending_run_at: null,
    queued_run_count: 0,
    provider: null,
    event: null,
    poll_interval_seconds: null,
    ...overrides,
  } as AssignmentSchedule;
}

function oneOff(id: string, dueAt: string, state: WorkItemScheduleState = "scheduled"): ScheduledWorkItem {
  return {
    id,
    user_id: "u1",
    is_company_managed: false,
    owner_agent_id: "agent-2",
    created_by_agent_id: "agent-2",
    title: `One-off ${id}`,
    description: null,
    payload: {},
    status: state === "done" ? "done" : "open",
    priority: "medium",
    correlation_key: null,
    due_at: dueAt,
    claimed_by_agent_id: null,
    claimed_at: null,
    source: "agent",
    result: null,
    is_stale: false,
    scheduled: {
      mode: "exact",
      tool: "send_teams_channel_message",
      target: "EMT",
      preview: "Test",
      state,
      run_id: null,
      conversation_id: null,
      outcome_code: null,
    },
    agent_name: "EMT Assistant",
    agent_icon: "Bot",
    last_activity_at: null,
    created_at: null,
    updated_at: null,
  };
}

describe("a recurring trigger's next run in its time zone", () => {
  it("says a Europe/Paris daily 07:00 in that zone and in UTC, in winter", () => {
    const paris = schedule({
      cron: "0 7 * * *",
      next_run_at: "2026-11-02T06:00:00+00:00",
      next_run_local: "2026-11-02T07:00:00+01:00",
      timezone: "Europe/Paris",
    });
    expect(zonedNextRun(paris)).toBe("07:00 Europe/Paris, 06:00 UTC");
  });

  it("says nothing more for a trigger read in UTC, configured or not", () => {
    expect(zonedNextRun(schedule())).toBeNull();
    expect(
      zonedNextRun(schedule({ timezone: "UTC", next_run_local: "2026-10-07T11:00:00+00:00" })),
    ).toBeNull();
  });

  it("warns on an unknown zone instead of naming it as the zone used", () => {
    const unknown = schedule({
      timezone: "UTC",
      next_run_local: "2026-10-07T11:00:00+00:00",
      timezone_warning: "timezone 'Mars/Olympus' not recognised, UTC used",
    });
    expect(zonedNextRun(unknown)).toBeNull();
    expect(timezoneWarningZone(unknown.timezone_warning)).toBe("Mars/Olympus");
    expect(timezoneWarningZone(null)).toBeNull();
    expect(timezoneWarningZone("timezone 5 not recognised, UTC used")).toBe("5");
    expect(timezoneWarningZone("something else")).toBe("?");
  });

  it("steps aside while a delayed run queued before the fire is next", () => {
    const paris = {
      next_run_at: "2026-11-02T06:00:00+00:00",
      next_run_local: "2026-11-02T07:00:00+01:00",
      timezone: "Europe/Paris",
    };
    expect(zonedNextRun(schedule({ ...paris, pending_run_at: "2026-11-01T22:00:00+00:00" }))).toBeNull();
    expect(zonedNextRun(schedule({ ...paris, pending_run_at: "2026-11-02T09:00:00+00:00" }))).toBe(
      "07:00 Europe/Paris, 06:00 UTC",
    );
  });

  it("is only for schedule triggers with a next fire", () => {
    const zoned = { timezone: "Europe/Paris", next_run_local: "2026-11-02T07:00:00+01:00" };
    expect(zonedNextRun(schedule({ ...zoned, trigger_type: "webhook" }))).toBeNull();
    expect(zonedNextRun(schedule({ ...zoned, next_run_at: null }))).toBeNull();
  });
});

describe("the upcoming timeline", () => {
  it("merges one-off items and recurring triggers, typed, soonest first", () => {
    const lanes = buildTimelineLanes(
      [schedule()],
      [oneOff("w1", "2026-10-07T10:30:00Z"), oneOff("w2", "2026-10-07T13:00:00Z")],
      24,
      NOW,
    );
    expect(lanes.map((l) => [l.kind, l.label])).toEqual([
      ["one-off", "One-off w1"],
      ["recurring", "Daily threat brief"],
      ["one-off", "One-off w2"],
    ]);
    const recurring = lanes.find((l) => l.kind === "recurring");
    expect(recurring?.occurrences.length).toBeGreaterThan(1);
    const single = lanes.filter((l) => l.kind === "one-off").map((l) => l.occurrences.length);
    expect(single).toEqual([1, 1]);
  });

  it("puts a due one-off on the now-line and leaves settled or later ones out", () => {
    const lanes = buildTimelineLanes(
      [],
      [
        oneOff("due", "2026-10-07T09:58:00Z", "due"),
        oneOff("ran", "2026-10-07T10:20:00Z", "done"),
        oneOff("later", "2026-10-09T10:00:00Z"),
      ],
      24,
      NOW,
    );
    expect(lanes.map((l) => l.key)).toEqual(["work-item:due"]);
    expect(lanes[0].occurrences[0].getTime()).toBe(NOW);
  });
});
