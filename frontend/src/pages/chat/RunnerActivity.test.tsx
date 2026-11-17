// A stopped runner card names who stopped it without breaking the card:
// a long stopper name is bounded and its full label stays
// reachable on the badge, the duration still shows.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { readRunnerActivity } from "@/lib/runner-activity";
import { resetRunnerCardOpen } from "@/lib/runner-card-open";
import type { RunnerActivity } from "@/types";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u-reader" }, isAdmin: false }),
}));

const stopCalls: string[] = [];
vi.mock("@/hooks/api/useRunnerApi", () => ({
  useStopRunnerCommand: () => ({
    mutate: (id: string, opts?: { onSuccess?: (a: { ok: boolean; stopping: boolean; status: string }) => void }) => {
      stopCalls.push(id);
      opts?.onSuccess?.({ ok: true, stopping: true, status: "stopping" });
    },
    isPending: false,
  }),
}));

beforeEach(() => {
  stopCalls.length = 0;
  resetRunnerCardOpen();
});

const { RunnerActivityBlock } = await import("./RunnerActivity");

const LONG_NAME = "Maximilian Alexander Featherstonehaugh-Montgomery";

const stoppedRecord = (stoppedBy: Record<string, unknown> | undefined): RunnerActivity[] => [
  readRunnerActivity({
    command_id: "cmd-1",
    kind: "run_shell",
    status: "stopped",
    runner_id: "r-1",
    runner_name: "LAB-MACHINE",
    platform: "linux",
    local: true,
    started_at: "2026-11-03T05:00:00+00:00",
    duration_ms: 42000,
    input: { command: "sleep 600", cwd: "/tmp" },
    output: { exit_code: null, stdout: "", stderr: "", stopped: "user" },
    error: null,
    ...(stoppedBy ? { stopped_by: stoppedBy } : {}),
  })!,
];

describe("RunnerActivity stopped badge", () => {
  it("bounds a long stopper name and keeps the full label in its title and the duration", () => {
    renderWithProviders(<RunnerActivityBlock records={stoppedRecord({ user_id: "u-other", name: LONG_NAME })} />);
    const label = screen.getByText(`Stopped by ${LONG_NAME}`);
    // The full label stays reachable when the visible text is cut.
    expect(label.getAttribute("title")).toBe(`Stopped by ${LONG_NAME}`);
    // The duration is a sibling of the label, not swallowed by it.
    expect(label.textContent).toBe(`Stopped by ${LONG_NAME}`);
    expect(screen.getByText("42 s")).toBeTruthy();
  });

  it("reads Stopped by you for the reader who stopped it", () => {
    renderWithProviders(<RunnerActivityBlock records={stoppedRecord({ user_id: "u-reader", name: "Lab User" })} />);
    expect(screen.getByText("Stopped by you")).toBeTruthy();
  });
});

// The exit label of a failed command reads at full error contrast:
// at 90% opacity it painted 4.02:1 on the dark card at 11 px, under 4.5:1.
describe("RunnerActivity exit label", () => {
  it("paints a non-zero exit with the full error token, never a faded one", () => {
    const records = [
      {
        command_id: "cmd-exit-1",
        kind: "run_shell",
        status: "completed",
        runner_id: "r-1",
        runner_name: "LAB-MACHINE",
        platform: "linux",
        local: true,
        started_at: "2026-11-03T05:00:00+00:00",
        duration_ms: 1000,
        input: { command: "false", cwd: "/tmp" },
        output: { exit_code: 1, stdout: "", stderr: "" },
        error: null,
      },
    ].map((raw) => readRunnerActivity(raw)!);
    renderWithProviders(<RunnerActivityBlock records={records} />);
    const label = screen.getByText("exit 1");
    // The tone is the contract: the error tone maps to the full
    // error token in the component; the contrast is measured by the UX gate.
    expect(label.hasAttribute("data-runner-status-label")).toBe(true);
    expect(label.getAttribute("data-tone")).toBe("error");
    // The duration beside it is localised with the reader's locale.
    expect(screen.getByText("1.0 s")).toBeTruthy();
  });
});

const backgroundJob = (overrides: Record<string, unknown> = {}) =>
  [
    {
      command_id: "job-1",
      kind: "run_shell",
      status: "completed",
      runner_id: "r-1",
      runner_name: "LAB-MACHINE",
      platform: "linux",
      local: false,
      started_at: "2026-11-03T05:00:00+00:00",
      duration_ms: 300,
      input: { command: "python -m http.server 8000 --bind 127.0.0.1 --directory /srv/www", cwd: "/tmp" },
      output: { background: true, job_id: "c0ffee01-aaaa-bbbb-cccc-0123456789ab", pid: 4242, job_status: "running", stdout: "" },
      error: null,
      ...overrides,
    },
  ].map((raw) => readRunnerActivity(raw)!);

// A background job card shows its pid, names Stop by its
// command, and stops the way the Machines page does (confirm, then settle).
describe("RunnerActivity background job", () => {
  it("names Stop by the command, clipped, never by the job id", () => {
    renderWithProviders(<RunnerActivityBlock records={backgroundJob()} />);
    const stop = screen.getByRole("button", { name: /^Stop python -m http\.server/ });
    const name = stop.getAttribute("aria-label") ?? "";
    expect(name).not.toContain("c0ffee01");
    expect(name.endsWith("...")).toBe(true);
    expect(name.length).toBeLessThanOrEqual("Stop ".length + 40);
  });

  it("shows the process id in the opened card", () => {
    renderWithProviders(<RunnerActivityBlock records={backgroundJob()} />);
    fireEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("Process 4242")).toBeTruthy();
  });

  it("asks before stopping, then settles as Stopped by you without Stop", async () => {
    renderWithProviders(<RunnerActivityBlock records={backgroundJob()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Stop python/ }));
    // Nothing is sent before the confirm.
    expect(stopCalls).toEqual([]);
    expect(await screen.findByText("Stop this job?")).toBeTruthy();
    const dialog = screen.getByRole("dialog");
    const confirmButton = Array.from(dialog.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Stop");
    expect(confirmButton).toBeTruthy();
    fireEvent.click(confirmButton!);
    await waitFor(() => expect(stopCalls).toEqual(["job-1"]));
    expect(await screen.findByText("Stopped by you")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Stop python/ })).toBeNull();
    // One label, one tone: muted like a job the machine reports stopped.
    const label = screen.getByText("Stopped by you");
    expect(label.getAttribute("data-tone")).toBe("muted");
    // Focus returns to the card header, not to <body>, and that header is the
    // card's disclosure button (round 2 B1: it draws the inset ring-focus ring).
    const header = screen.getByRole("button", { expanded: false });
    await waitFor(() => expect(document.activeElement).toBe(header));
  });
});
