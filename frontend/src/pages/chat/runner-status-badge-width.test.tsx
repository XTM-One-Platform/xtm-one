/**
 * The header of a chat machine card keeps its status readable - guarded on the RENDERED card through the data attributes
 * the component sets, never on its source text nor on its class
 * names.
 *
 * The header holds the kind icon, the title, the command or path, the machine
 * chip, the status badge and Stop. The badge never shrinks nor wraps (a short
 * status and its duration always read in full); only its label truncates,
 * bounded and with the full label in its title (a long stopper name). The
 * command takes the free space; the machine chip collapses to its icon on a
 * narrow card, its name kept for screen readers.
 *
 * Each sizing role is declared on the element as `data-fit`: `fixed` (never
 * shrinks), `truncate` (the one that gives way, full text in a title) or
 * `fill` (takes the free space). happy-dom computes no layout, so the widths
 * at 1024 and 1440 are measured by the UX gate.
 */
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { readRunnerActivity } from "@/lib/runner-activity";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u-reader" }, isAdmin: false }),
}));

const { RunnerActivityBlock } = await import("./RunnerActivity");

const LONG_NAME = "Maximilian Alexander Featherstonehaugh-Montgomery";

function renderCard(overrides: Record<string, unknown>) {
  const records = [
    {
      command_id: "cmd-width",
      kind: "run_shell",
      status: "completed",
      runner_id: "r-1",
      runner_name: "LAB-MACHINE",
      platform: "linux",
      local: true,
      started_at: "2026-11-03T05:00:00+00:00",
      duration_ms: 1100,
      input: { command: "python -m pytest -q tests/test_a_very_long_module_name.py", cwd: "/tmp" },
      output: { exit_code: 137, stdout: "", stderr: "" },
      error: null,
      ...overrides,
    },
  ].map((raw) => readRunnerActivity(raw)!);
  const { container } = renderWithProviders(<RunnerActivityBlock records={records} />);
  return container;
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const found = container.querySelectorAll<HTMLElement>(selector);
  expect(found.length, `${selector} rendered once`).toBe(1);
  return found[0];
}

const fit = (el: Element) => el.getAttribute("data-fit");

describe("runner card status badge", () => {
  it("never shrinks, so a short status and its duration read in full", () => {
    const container = renderCard({});
    const badge = one(container, "[data-runner-status-badge]");
    expect(fit(badge)).toBe("fixed");
    const duration = screen.getByText("1.1 s");
    expect(badge.contains(duration)).toBe(true);
    expect(duration.hasAttribute("data-runner-status-duration")).toBe(true);
    expect(fit(duration)).toBe("fixed");
  });

  it("truncates only the label, with the full label in its title", () => {
    const container = renderCard({
      status: "stopped",
      output: { exit_code: null, stdout: "", stderr: "", stopped: "user" },
      stopped_by: { user_id: "u-other", name: LONG_NAME },
    });
    const badge = one(container, "[data-runner-status-badge]");
    const label = one(container, "[data-runner-status-label]");
    expect(badge.contains(label)).toBe(true);
    expect(label.getAttribute("title")).toBe(`Stopped by ${LONG_NAME}`);
    expect(label.textContent).toBe(`Stopped by ${LONG_NAME}`);
    // The label is the only element of the badge that gives way.
    const truncating = Array.from(badge.querySelectorAll('[data-fit="truncate"]'));
    expect(truncating).toEqual([label]);
  });
});

describe("runner card header", () => {
  it("lets the command take the free space, with its full value in a title", () => {
    const container = renderCard({});
    const detail = one(container, "[data-runner-card-detail]");
    expect(detail.getAttribute("title")).toBe("python -m pytest -q tests/test_a_very_long_module_name.py");
    expect(fit(detail)).toBe("fill");
  });

  it("collapses the machine chip to its icon on a narrow card, never to an empty pill", () => {
    const container = renderCard({});
    const chip = one(container, "[data-runner-machine-chip]");
    expect(fit(chip)).toBe("fixed");
    const name = one(container, "[data-runner-machine-name]");
    expect(chip.contains(name)).toBe(true);
    // The visible name gives way and shows only from the medium card width.
    expect(fit(name)).toBe("truncate");
    expect(name.getAttribute("data-shown-from")).toBe("md");
    // The name stays for screen readers when the visible label is hidden.
    expect(name.getAttribute("aria-hidden")).toBe("true");
    expect(chip.textContent).toContain("LAB-MACHINE");
  });
});
