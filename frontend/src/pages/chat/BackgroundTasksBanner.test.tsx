// The background tasks bar above the composer: one line, CSS ellipsis, full title on hover and focus (#12243).

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/testing/providers";
import type { SubAgentTask } from "@/types";
import { BackgroundTasksBanner } from "./BackgroundTasksBanner";
import { fitChipCount } from "./background-task-strip-fit";

const LONG = "Customer-grade quality audit of every assignment of the fleet, with a report per desk";

function task(n: number, status: SubAgentTask["status"] = "running"): SubAgentTask {
  return {
    task_id: `t-${n}`,
    user_id: "u-1",
    parent_agent_id: "a-1",
    target_agent_id: "a-2",
    target_agent_name: "Auditor",
    task_description: `${LONG} (${n})`,
    status,
    result: null,
    error: null,
    created_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
    completed_at: null,
  };
}

describe("fitChipCount", () => {
  it("shows every chip that fits, and folds the rest leaving room for the counter", () => {
    // A chip slot is 150 px, the counter keeps 72 px.
    expect(fitChipCount(600, 4)).toBe(4);
    expect(fitChipCount(2 * 150 + 72, 8)).toBe(2);
    expect(fitChipCount(2 * 150 + 71, 8)).toBe(1);
    expect(fitChipCount(40, 8)).toBe(1);
    expect(fitChipCount(0, 1)).toBe(1);
    expect(fitChipCount(0, 8)).toBe(3);
    expect(fitChipCount(500, 0)).toBe(0);
  });
});

describe("BackgroundTasksBanner", () => {
  it("keeps the full title of a chip in the DOM and as its accessible name, never cut in JS", () => {
    const tasks = [1, 2, 3, 4].map((n) => task(n));
    renderWithProviders(<BackgroundTasksBanner activeBgTasks={tasks} bgTasks={tasks} />);

    const chips = screen.getAllByTestId("background-task-chip");
    expect(chips).toHaveLength(3);
    chips.forEach((chip, i) => {
      expect(chip.getAttribute("aria-label")).toBe(`${LONG} (${i + 1})`);
      expect(chip.textContent).toBe(`${LONG} (${i + 1})`);
      expect(chip.textContent).not.toContain("...");
    });
    expect(screen.getByTestId("background-task-more").textContent).toBe("+1 more");
  });

  it("reads the short label on a narrow bar that mounts after the banner (0 then N running tasks)", () => {
    // The bar measures itself with a ResizeObserver; this one reports a 480 px box at once.
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(private readonly callback: (entries: { contentRect: { width: number } }[]) => void) {}
        observe() {
          this.callback([{ contentRect: { width: 480 } }]);
        }
        unobserve() {}
        disconnect() {}
      },
    );
    try {
      const { rerender } = renderWithProviders(<BackgroundTasksBanner activeBgTasks={[]} bgTasks={[]} />);
      expect(screen.queryByTestId("background-tasks-bar")).toBeNull();

      const tasks = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => task(n));
      rerender(<BackgroundTasksBanner activeBgTasks={tasks} bgTasks={tasks} />);
      const bar = screen.getByTestId("background-tasks-bar");
      expect(bar.textContent).toContain("8 tasks running");
      expect(bar.textContent).not.toContain("background tasks running");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("opens the full task on keyboard focus of a chip", async () => {
    const user = userEvent.setup();
    const tasks = [task(1)];
    renderWithProviders(<BackgroundTasksBanner activeBgTasks={tasks} bgTasks={tasks} />);

    const chip = screen.getByTestId("background-task-chip");
    await user.tab();
    expect(document.activeElement).toBe(chip);
    expect((await screen.findByRole("tooltip")).textContent).toContain(`${LONG} (1)`);
  });

  it("lists the titles of the folded tasks in the tooltip of +N more", async () => {
    const user = userEvent.setup();
    const tasks = [1, 2, 3, 4, 5].map((n) => task(n));
    renderWithProviders(<BackgroundTasksBanner activeBgTasks={tasks} bgTasks={tasks} />);

    const more = screen.getByTestId("background-task-more");
    expect(more.textContent).toBe("+2 more");
    for (let presses = 0; presses < 10 && document.activeElement !== more; presses++) await user.tab();
    expect(document.activeElement).toBe(more);
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip.textContent).toContain(`${LONG} (4)`);
    expect(tooltip.textContent).toContain(`${LONG} (5)`);
  });
});
