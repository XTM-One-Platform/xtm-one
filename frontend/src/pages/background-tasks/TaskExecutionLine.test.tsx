import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { TaskExecutionLine } from "@/pages/background-tasks/TaskExecutionLine";
import { hasNoLiveExecution } from "@/pages/background-tasks/liveness";
import { renderWithProviders } from "@/testing/providers";
import type { SubAgentTask } from "@/types";

const NOW = Date.parse("2026-11-03T07:00:00Z");

function task(overrides: Partial<SubAgentTask>): SubAgentTask {
  return {
    task_id: "t-1",
    user_id: "u-1",
    parent_agent_id: "a-1",
    target_agent_id: "a-2",
    task_description: "Summarise the report",
    status: "running",
    result: null,
    error: null,
    created_at: "2026-11-03T06:00:00Z",
    started_at: "2026-11-03T06:00:01Z",
    completed_at: null,
    ...overrides,
  };
}

describe("TaskExecutionLine", () => {
  it("names the run, the execution and the last progress of a running task", () => {
    const { container } = renderWithProviders(
      <MemoryRouter>
        <TaskExecutionLine
          nowMs={NOW}
          task={task({
            assignment_run_id: "1234567890abcdef",
            execution_state: "running",
            execution_id: "abcdef0123456789",
            last_progress_at: "2026-11-03T06:55:00Z",
          })}
        />
      </MemoryRouter>,
    );
    const line = container.querySelector('[data-testid="task-execution-line"]');
    expect(line).not.toBeNull();
    expect(line?.getAttribute("data-no-live-execution")).toBeNull();
    expect(screen.getByText("Execution running")).toBeTruthy();
    expect(screen.getByRole("link", { name: "12345678" }).getAttribute("href")).toBe("/operations/executions/1234567890abcdef");
    expect(screen.getByText("abcdef01")).toBeTruthy();
    expect(screen.getByText(/Last progress 5 minutes ago/)).toBeTruthy();
    expect(screen.queryByText("No live execution")).toBeNull();
  });

  it("orders the parts so the ids are the first to go on a narrow row", () => {
    const { container } = renderWithProviders(
      <MemoryRouter>
        <TaskExecutionLine
          nowMs={NOW}
          task={task({
            assignment_run_id: "1234567890abcdef",
            execution_state: "running",
            execution_id: "abcdef0123456789",
            last_progress_at: "2026-11-03T06:55:00Z",
          })}
        />
      </MemoryRouter>,
    );
    const text = container.querySelector('[data-testid="task-execution-line"]')?.textContent ?? "";
    const at = (s: string) => text.indexOf(s);
    expect(at("Execution running")).toBeGreaterThanOrEqual(0);
    expect(at("Execution running")).toBeLessThan(at("Last progress"));
    expect(at("Last progress")).toBeLessThan(at("12345678"));
    expect(at("12345678")).toBeLessThan(at("abcdef01"));
  });

  it("flags a running task whose execution is gone", () => {
    const { container } = renderWithProviders(
      <TaskExecutionLine nowMs={NOW} task={task({ execution_state: "none", assignment_run_id: null })} />,
    );
    expect(screen.getByText("No live execution")).toBeTruthy();
    // The flag says it; the state "No execution" is not repeated beside it.
    expect(screen.queryByText("No execution")).toBeNull();
    expect(screen.getByText("none")).toBeTruthy();
    expect(
      container.querySelector('[data-testid="task-execution-line"]')?.getAttribute("data-no-live-execution"),
    ).toBe("true");
  });

  it("flags a pending task whose execution ended", () => {
    renderWithProviders(<TaskExecutionLine nowMs={NOW} task={task({ status: "pending", execution_state: "dead_letter" })} />);
    expect(screen.getByText("No live execution")).toBeTruthy();
    expect(screen.getByText("Execution dead-lettered")).toBeTruthy();
  });

  it("draws nothing for a finished task or one listed without the fields", () => {
    const { container: done } = renderWithProviders(
      <TaskExecutionLine nowMs={NOW} task={task({ status: "completed", execution_state: "completed" })} />,
    );
    expect(done.querySelector('[data-testid="task-execution-line"]')).toBeNull();
    const { container: bare } = renderWithProviders(<TaskExecutionLine nowMs={NOW} task={task({})} />);
    expect(bare.querySelector('[data-testid="task-execution-line"]')).toBeNull();
  });

  it.each(["en", "fr", "es", "ja"] as const)("labels the run id and the execution id differently in %s", (locale) => {
    const { container } = renderWithProviders(
      <MemoryRouter>
        <TaskExecutionLine
          nowMs={NOW}
          task={task({
            assignment_run_id: "1234567890abcdef",
            execution_state: "running",
            execution_id: "abcdef0123456789",
          })}
        />
      </MemoryRouter>,
      { locale },
    );
    const runLabel = screen.getByRole("link", { name: "12345678" }).previousElementSibling?.textContent;
    const executionLabel = container.querySelector('[title="abcdef0123456789"]')?.previousElementSibling?.textContent;
    expect(runLabel).toBeTruthy();
    expect(executionLabel).toBeTruthy();
    expect(executionLabel).not.toBe(runLabel);
    if (locale === "fr") {
      expect(runLabel).toBe("Exécution");
      expect(executionLabel).toBe("Orchestration");
    }
  });

  it.each([
    ["fr", "État : en cours"],
    ["es", "Estado: en curso"],
    ["ja", "実行中"],
  ] as const)("reads the state apart from the run label in %s", (locale, expected) => {
    renderWithProviders(
      <MemoryRouter>
        <TaskExecutionLine
          nowMs={NOW}
          task={task({ assignment_run_id: "1234567890abcdef", execution_state: "running" })}
        />
      </MemoryRouter>,
      { locale },
    );
    expect(screen.getByText(expected)).toBeTruthy();
    const runLabel = screen.getByRole("link", { name: "12345678" }).previousElementSibling?.textContent ?? "";
    expect(runLabel).toBeTruthy();
    // fr and es translate "Run" with the state's former word; the state now leads with its own.
    if (locale !== "ja") expect(expected.startsWith(runLabel)).toBe(false);
  });

  it("applies the tool line's rule", () => {
    expect(hasNoLiveExecution({ status: "running", execution_state: "running" })).toBe(false);
    expect(hasNoLiveExecution({ status: "running", execution_state: "pending" })).toBe(false);
    expect(hasNoLiveExecution({ status: "running", execution_state: "completed" })).toBe(true);
    expect(hasNoLiveExecution({ status: "pending", execution_state: "none" })).toBe(true);
    expect(hasNoLiveExecution({ status: "failed", execution_state: "none" })).toBe(false);
    expect(hasNoLiveExecution({ status: "running", execution_state: undefined })).toBe(false);
  });
});
