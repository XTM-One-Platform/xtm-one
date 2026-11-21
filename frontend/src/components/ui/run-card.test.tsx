// The dashboard Live Activity row: whatever started the run, the row
// keeps one title line and one status line, the trigger shown as a muted icon
// with a tooltip, never as a second chip that wraps the row.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "Europe/Paris" }, isAdmin: false }),
}));
vi.mock("@/hooks/api/useAssignmentApi", () => ({
  useDescendantRuns: () => ({ data: [], isLoading: false }),
}));

const { RunCard } = await import("./run-card");

const BASE = {
  id: "run-1",
  status: "running",
  result_summary: null,
  error: null,
  actions_taken: null,
  created_at: "2026-11-01T10:00:00Z",
  agent_name: "CEO Office Developer",
  agent_icon: "bot",
};

function renderRow(run: Parameters<typeof RunCard>[0]["run"], locale = "en") {
  return renderWithProviders(
    <MemoryRouter>
      <RunCard run={run} compact card={false} />
    </MemoryRouter>,
    { locale },
  );
}

// The short date and time of the feed rows (formatShortDateTimeIntl), in a language.
function shortDateTime(locale: string, at: Date): string {
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(at);
}

describe("a Live Activity row", () => {
  it("keeps a scheduled run on one title line, its trigger an icon with a tooltip", async () => {
    renderRow({
      ...BASE,
      trigger_context: { type: "scheduled_work_item", work_item_id: "item-1", title: "Re-check the main CI run" },
    });
    const titleLine = screen.getByTestId("run-card-title-line");
    expect(titleLine.className).toContain("whitespace-nowrap");
    expect(titleLine.className).not.toContain("flex-wrap");
    // The trigger is a link to the item, an icon only: no "Scheduled" chip text.
    const marker = within(titleLine).getByTestId("scheduled-run-badge");
    expect(marker.getAttribute("href")).toBe("/tasks/work-items/item-1");
    expect(marker.getAttribute("aria-label")).toBe("Scheduled");
    expect(marker.textContent).toBe("");
    expect(within(titleLine).getByText("Re-check the main CI run").className).toContain("truncate");
    // One status chip, on a status line that never wraps.
    const statusLine = screen.getByTestId("run-card-status-line");
    expect(statusLine.className).toContain("whitespace-nowrap");
    expect(statusLine.className).not.toContain("flex-wrap");
    // The chip keeps its size; a long preview is what truncates.
    expect(within(statusLine).getByText("Running").className).toContain("shrink-0");
    expect(screen.queryByText("Scheduled")).toBeNull();
    fireEvent.focus(marker);
    expect((await screen.findByRole("tooltip")).textContent).toContain("Run of a scheduled work item");
  });

  it("gives an assignment run the same two lines, without a trigger marker, its name whole in a tooltip", async () => {
    renderRow({ ...BASE, assignment_name: "Developer: harness self-improvement sweep" });
    fireEvent.focus(screen.getByText("Developer: harness self-improvement sweep"));
    expect((await screen.findByRole("tooltip")).textContent).toContain("Developer: harness self-improvement sweep");
    const titleLine = screen.getByTestId("run-card-title-line");
    expect(titleLine.className).toContain("whitespace-nowrap");
    expect(within(titleLine).getByText("Developer: harness self-improvement sweep").className).toContain("truncate");
    expect(within(titleLine).queryByTestId("scheduled-run-badge")).toBeNull();
    expect(within(titleLine).queryByTestId("run-trigger-marker")).toBeNull();
    expect(within(screen.getByTestId("run-card-status-line")).getByText("Running")).toBeTruthy();
  });

  it("truncates a long error after the status chip, never the chip", () => {
    renderRow({ ...BASE, status: "failed", assignment_name: "Nightly sync", error: "x".repeat(120) });
    const statusLine = screen.getByTestId("run-card-status-line");
    expect(within(statusLine).getByText("Failed").className).toContain("shrink-0");
    const error = within(statusLine).getByText(/^x+…$/);
    expect(error.className).toContain("min-w-0");
    expect(error.className).toContain("truncate");
  });

  it("shows a delegation as a marker, not a chip", () => {
    renderRow({ ...BASE, parent_run_id: "run-0", parent_agent_name: "Executive Assistant" });
    const titleLine = screen.getByTestId("run-card-title-line");
    expect(within(titleLine).getByTestId("run-trigger-marker").getAttribute("aria-label")).toBe("Delegation");
    expect(within(titleLine).getByText(/Executive Assistant/)).toBeTruthy();
    expect(screen.queryByText("Delegation")).toBeNull();
  });

  // #9594 A1: the row reads a relative time, the absolute one in a tooltip,
  // as the CTEM Live activity feed rows.
  it("reads a relative time with the absolute time in a tooltip", async () => {
    const at = new Date(Date.now() - 3 * 60_000);
    renderRow({ ...BASE, created_at: at.toISOString() });
    const time = screen.getByTestId("run-card-time");
    expect(time.textContent).toBe("3 minutes ago");
    fireEvent.focus(time);
    expect((await screen.findByRole("tooltip")).textContent).toContain(shortDateTime("en", at));
  });

  // #9623 UX round 1 B1: the absolute time is in the user's language and in the
  // format of the feed rows beside it, never the US English full date.
  it("shows the absolute time in the user's language, in the feed rows' format", async () => {
    const at = new Date(Date.now() - 3 * 60_000);
    renderRow({ ...BASE, created_at: at.toISOString() }, "fr");
    const time = screen.getByTestId("run-card-time");
    expect(time.textContent).toBe("il y a 3 minutes");
    fireEvent.focus(time);
    const tooltip = (await screen.findByRole("tooltip")).textContent ?? "";
    expect(tooltip).toContain(shortDateTime("fr", at));
    expect(tooltip).not.toMatch(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d/);
    expect(tooltip).not.toMatch(/AM|PM/);
  });

  it("keeps the chips of the full history card as they were", () => {
    renderWithProviders(
      <MemoryRouter>
        <RunCard
          run={{ ...BASE, trigger_context: { type: "scheduled_work_item", work_item_id: "item-1", title: "Re-check" } }}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("run-card-title-line")).toBeNull();
    expect(screen.getByText("Scheduled")).toBeTruthy();
  });
});
