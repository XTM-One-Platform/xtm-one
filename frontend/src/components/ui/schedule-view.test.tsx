// What the schedule view shows for the agents it is scoped to.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { AssignmentSchedule } from "@/types";
import { ScheduleView, type ScheduleViewProps } from "./schedule-view";

function schedule(id: string, agentId: string): AssignmentSchedule {
  return {
    assignment_id: id,
    assignment_name: `Assignment ${id}`,
    agent_id: agentId,
    agent_name: `Agent ${agentId}`,
    agent_icon: "bot",
    trigger_type: "manual",
    enabled: true,
    cron: null,
    next_run_at: null,
    delay_seconds: 0,
    last_triggered_at: null,
    has_active_run: false,
    pending_run_at: null,
    queued_run_count: 0,
    provider: null,
    event: null,
    poll_interval_seconds: null,
    webhook_url: null,
  };
}

const SCHEDULES = [schedule("s1", "a"), schedule("s2", "b"), schedule("s3", "c")];

vi.mock("@/hooks/api/useAssignmentApi", () => ({
  useAssignmentSchedule: () => ({ data: SCHEDULES, isLoading: false }),
  useActiveAgentIds: () => ({ data: [] }),
}));
vi.mock("@/hooks/api/useWorkItemApi", () => ({
  useScheduledWorkItems: () => ({ data: [], isLoading: false }),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { timezone: "UTC" } }) }));

function draw(props: ScheduleViewProps = {}) {
  return renderWithProviders(
    <MemoryRouter>
      <ScheduleView {...props} />
    </MemoryRouter>,
  );
}

const shown = () =>
  ["s1", "s2", "s3"].filter((id) => screen.queryByText(`Assignment ${id}`) !== null);

describe("ScheduleView scope", () => {
  it("shows every visible agent's schedule without a scope", () => {
    draw();
    expect(shown()).toEqual(["s1", "s2", "s3"]);
  });

  it("shows only the agents it is scoped to", () => {
    draw({ agentIds: new Set(["a", "c"]) });
    expect(shown()).toEqual(["s1", "s3"]);
  });

  it("narrows to one agent and widens back on demand", () => {
    const onClearAgent = vi.fn();
    draw({ agentId: "b", onClearAgent });
    expect(shown()).toEqual(["s2"]);
    expect(screen.getByText("Agent: Agent b")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Show every agent/ }));
    expect(onClearAgent).toHaveBeenCalledOnce();
  });

  it("offers no widening without a handler", () => {
    draw({ agentId: "b" });
    expect(screen.queryByRole("button", { name: /Show every agent/ })).toBeNull();
  });

  it("reads its KPI strip and timeline window in the viewer's language", () => {
    renderWithProviders(
      <MemoryRouter>
        <ScheduleView />
      </MemoryRouter>,
      { locale: "fr-fr" },
    );
    expect(screen.getByText("Prochain déclenchement")).toBeTruthy();
    expect(screen.getByText("aucune planification")).toBeTruthy();
    expect(screen.getByText("Prochaine heure")).toBeTruthy();
    expect(screen.queryByText("Next trigger")).toBeNull();
    expect(screen.queryByText("Next hour")).toBeNull();
    // The whole fr label is rendered; its wrapping is verified in the UX lab.
    const labels = screen.getAllByTestId("stat-card-label");
    expect(labels.map((l) => l.textContent)).toContain("Prochain déclenchement");
  });

  it("says nothing is scheduled for a scope with no schedule", () => {
    draw({ agentIds: new Set(["nobody"]) });
    expect(shown()).toEqual([]);
    expect(screen.getByText("Nothing is scheduled")).toBeTruthy();
  });
});
