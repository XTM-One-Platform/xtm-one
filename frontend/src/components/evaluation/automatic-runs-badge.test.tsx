// `enabled` pauses the schedule only: the badge says so when one is configured,
// and nothing otherwise, since a run started by hand always runs. On agent change starts
// no run yet, so it has nothing to pause.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { AutomaticRunsPausedBadge } from "./automatic-runs-badge";

const schedule = { schedule: "0 0 * * 1", on_agent_change: false };
const onAgentChange = { schedule: null, on_agent_change: true };
const none = { schedule: null, on_agent_change: false };

describe("AutomaticRunsPausedBadge", () => {
  it("marks a paused schedule", () => {
    renderWithProviders(<AutomaticRunsPausedBadge evaluation={{ enabled: false, trigger_config: schedule }} />);
    expect(screen.getByText("Automatic runs paused")).toBeTruthy();
  });

  it.each([
    { enabled: true, trigger_config: schedule },
    { enabled: false, trigger_config: none },
    { enabled: false, trigger_config: onAgentChange },
  ])("renders nothing when no automatic run is paused", (evaluation) => {
    const { container } = renderWithProviders(<AutomaticRunsPausedBadge evaluation={evaluation} />);
    expect(container.textContent).toBe("");
  });
});
