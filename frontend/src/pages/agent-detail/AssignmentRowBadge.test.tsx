// The agent page's assignment list agrees with the assignment page: a platform event trigger
// whose deadline passed reads "Ended" in its row, never a green "Active".

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Assignment } from "@/types";
import { assignmentStatusTint } from "../assignment-detail/assignment-status";
import { AssignmentRowBadge } from "./AssignmentRowBadge";

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

const row = (a: Assignment, running = false) =>
  renderWithProviders(<AssignmentRowBadge assignment={a} running={running} ownerPausedRemedy="" />);

describe("assignment list row badge", () => {
  it.each([
    ["an ended standing trigger", assignment({ trigger_config: { event: "run.failed", expires_at: PAST } })],
    ["an expired one-off watch", assignment({ trigger_config: { event: "work_item.completed", once: true, expires_at: PAST } })],
  ])("reads %s as Ended, never Active", (_, a) => {
    row(a);
    expect(screen.getByText("Ended")).toBeTruthy();
    expect(screen.queryByText("Active")).toBeNull();
  });

  it("keeps a trigger with its deadline ahead Active", () => {
    row(assignment({ trigger_config: { event: "run.failed", expires_at: AHEAD } }));
    expect(screen.getByText("Active")).toBeTruthy();
  });

  it("reads a paused assignment as Paused, its deadline aside", () => {
    row(assignment({ enabled: false, paused_reason: "user_toggle", trigger_config: { expires_at: PAST } }));
    expect(screen.getByText("Paused")).toBeTruthy();
    expect(screen.queryByText("Ended")).toBeNull();
  });

  it("says Running while a run is in flight", () => {
    row(assignment({ trigger_config: { expires_at: PAST } }), true);
    expect(screen.getByText("Running")).toBeTruthy();
  });

  it("tints an ended row in the warning tone, not as active", () => {
    expect(assignmentStatusTint("ended")).not.toEqual(assignmentStatusTint("active"));
    expect(assignmentStatusTint("ended").icon).not.toBe(assignmentStatusTint("active").icon);
  });
});
