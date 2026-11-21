// The Platform Health's crossed signals draw through the shared data table (#9378, slice 6a):
// search, filters, sort, Export and the error row with Retry.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { HealthSignalsList } from "./HealthSignalsList";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const SIGNALS = [
  {
    target_type: "agent",
    target_id: "a-1",
    target_name: "Triage analyst",
    kind: "feedback_drop",
    active: true,
    last_value: { value: 0.4, baseline: 0.8 },
    cycle_id: "c-1",
    refusal: null,
    fired_at: "2026-11-08T10:00:00Z",
  },
  {
    target_type: "flow",
    target_id: "f-1",
    target_name: "Weekly digest",
    kind: "failure_spike",
    active: true,
    last_value: { value: 12 },
    cycle_id: "c-2",
    refusal: null,
    fired_at: "2026-11-08T11:00:00Z",
  },
];

function answer(signals: unknown) {
  vi.mocked(api.get).mockImplementation((url: string) =>
    url.startsWith("/health/signals") ? (signals instanceof Error ? Promise.reject(signals) : Promise.resolve(signals)) : Promise.resolve({}),
  );
}

const renderList = () =>
  renderWithProviders(
    <MemoryRouter>
      <HealthSignalsList />
    </MemoryRouter>,
  );

describe("the Platform Health signals list", () => {
  it("draws the signals in the shared data table with search, filters and Export", async () => {
    answer(SIGNALS);
    renderList();

    expect(await screen.findByText("Feedback drop")).toBeTruthy();
    expect(screen.getByText("Failure spike")).toBeTruthy();
    expect(screen.getByText("0.8 → 0.4")).toBeTruthy();
    expect(screen.getByRole("searchbox")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Export/ })).toBeTruthy();
    // The target is plain text; the row opens it (#12070).
    const target = screen.getByText("Weekly digest");
    expect(target.closest("a")).toBeNull();
    expect(within(target.closest("tr") as HTMLElement).getByRole("link").getAttribute("href")).toBe("/flows/library/f-1");
    const agentRow = screen.getByText("Triage analyst").closest("tr") as HTMLElement;
    expect(within(agentRow).getByRole("link").getAttribute("href")).toContain("a-1");

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "feedback" } });
    await waitFor(() => expect(screen.queryByText("Failure spike")).toBeNull());
    expect(screen.getByText("Feedback drop")).toBeTruthy();
  });

  it("finds a signal by its target's name", async () => {
    answer(SIGNALS);
    renderList();
    await screen.findByText("Feedback drop");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "weekly" } });
    await waitFor(() => expect(screen.queryByText("Feedback drop")).toBeNull());
    expect(screen.getByText("Failure spike")).toBeTruthy();
  });

  it("names a target that no longer exists, without a link or its id", async () => {
    answer([{ ...SIGNALS[0], target_name: null }]);
    renderList();
    expect(await screen.findByText("Agent no longer exists")).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByText(/a-1/)).toBeNull();
    // The named state already says the type: no "Agent" label repeated next to it (A1, round 2).
    expect(screen.queryByText("Agent", { selector: "td span" })).toBeNull();
  });

  it("keeps a long target name on one line in a fixed-width grid, cut with its tooltip", async () => {
    const longName = "Threat intelligence analyst for the European ransomware campaigns and initial access brokers";
    answer([{ ...SIGNALS[0], target_name: longName }]);
    renderList();
    const name = await screen.findByTitle(longName);
    // UX round 2 on #10066, B1: the fixed layout gives Target what is left, so it never
    // widens the table past its box; the name is cut and whole in its title.
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
    const widths = screen.getAllByRole("columnheader").map((header) => header.style.width);
    expect(widths).toEqual(["144px", "", "112px", "176px", "152px"]);
    expect(name.textContent).toBe(longName);
    expect(name.closest("a")).toBeNull();
  });

  it("sorts the newest fired signal first", async () => {
    answer(SIGNALS);
    renderList();
    await screen.findByText("Feedback drop");
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows[0].textContent).toContain("Failure spike");
    expect(rows[1].textContent).toContain("Feedback drop");
  });

  it("shows the empty state when no signal is crossed", async () => {
    answer([]);
    renderList();
    expect(await screen.findByText("No signal is crossed")).toBeTruthy();
  });

  it("shows the error row with Retry when the signals cannot be read", async () => {
    answer(new Error("boom"));
    renderList();
    expect(await screen.findByRole("button", { name: /Retry/ }, { timeout: 4000 })).toBeTruthy();
  });
});
