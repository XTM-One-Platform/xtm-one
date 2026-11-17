// The assignment page never says "Active" for a platform event trigger whose deadline passed:
// the status tile and the header badge read "Ended" in the warning tone, as the Trigger card does.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Assignment } from "@/types";
import { AssignmentStatusBadge, AssignmentStatusTile } from "./AssignmentStatus";
import { assignmentDisplayStatus } from "./assignment-status";

const PAST = "2020-01-01T09:00:00Z";
const AHEAD = "2099-01-01T09:00:00Z";

const assignment = (overrides: Partial<Assignment> = {}) =>
  ({
    enabled: true,
    paused_by_owner_deactivation: false,
    paused_reason: null,
    trigger_type: "platform_event",
    trigger_config: { event: "work_item.completed" },
    ...overrides,
  }) as Assignment;

const expiredWatch = assignment({ trigger_config: { event: "work_item.completed", once: true, expires_at: PAST } });
const endedStanding = assignment({ trigger_config: { event: "run.failed", expires_at: PAST } });

describe("assignment status with a platform event deadline", () => {
  it.each([
    ["an expired one-off watch", expiredWatch],
    ["a standing trigger past its deadline", endedStanding],
  ])("the tile reads %s as ended, deadline passed, never active", (_, a) => {
    renderWithProviders(<AssignmentStatusTile assignment={a} />);
    const value = screen.getByText("Ended");
    expect(value.className).toContain("text-feedback-warning-primary");
    expect(screen.getByText("deadline passed")).toBeTruthy();
    expect(screen.queryByText("Active")).toBeNull();
    expect(screen.queryByText("accepting triggers")).toBeNull();
  });

  it.each([
    ["an expired one-off watch", expiredWatch],
    ["a standing trigger past its deadline", endedStanding],
  ])("the header badge reads %s as ended", (_, a) => {
    renderWithProviders(<AssignmentStatusBadge assignment={a} />);
    expect(screen.getByText("Ended")).toBeTruthy();
    expect(screen.queryByText("Active")).toBeNull();
  });

  it("reads a one-off watch that fired with no deadline as ended, watch fired, on the tile and the badge", () => {
    const firedWatch = assignment({ trigger_config: { event: "work_item.completed", once: true, fired_event_id: "e1" } });
    expect(assignmentDisplayStatus(firedWatch)).toBe("ended");
    renderWithProviders(<AssignmentStatusTile assignment={firedWatch} />);
    expect(screen.getByText("Ended")).toBeTruthy();
    expect(screen.getByText("watch fired")).toBeTruthy();
    expect(screen.queryByText("deadline passed")).toBeNull();
    expect(screen.queryByText("Active")).toBeNull();
  });

  it("the header badge, also used by the agent Overview assignment list, reads a fired watch as ended", () => {
    const firedWatch = assignment({ trigger_config: { event: "work_item.completed", once: true, fired_event_id: "e1" } });
    renderWithProviders(<AssignmentStatusBadge assignment={firedWatch} />);
    expect(screen.getByText("Ended")).toBeTruthy();
    expect(screen.queryByText("Active")).toBeNull();
  });

  it("keeps an armed watch, a standing trigger and a deadline ahead active", () => {
    expect(assignmentDisplayStatus(assignment({ trigger_config: { once: true, expires_at: AHEAD } }))).toBe("active");
    expect(assignmentDisplayStatus(assignment({ trigger_config: { expires_at: AHEAD } }))).toBe("active");
    expect(assignmentDisplayStatus(assignment())).toBe("active");
    renderWithProviders(<AssignmentStatusTile assignment={assignment({ trigger_config: { expires_at: AHEAD } })} />);
    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.getByText("accepting triggers")).toBeTruthy();
  });

  it("a paused assignment reads paused, its deadline aside", () => {
    expect(assignmentDisplayStatus({ ...expiredWatch, enabled: false, paused_reason: "user_toggle" })).toBe("paused");
    renderWithProviders(<AssignmentStatusTile assignment={{ ...expiredWatch, enabled: false, paused_reason: "user_toggle" }} />);
    expect(screen.getByText("Paused")).toBeTruthy();
    expect(screen.getByText("paused by a user")).toBeTruthy();
  });

  it("another trigger type never reads a deadline", () => {
    expect(assignmentDisplayStatus(assignment({ trigger_type: "schedule", trigger_config: { expires_at: PAST } }))).toBe("active");
  });
});
