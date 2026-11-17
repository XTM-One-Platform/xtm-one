// The feedback criteria of Settings > Quality are drawn by the shared DataTable
// (dev-docs/reference/data-tables.md, slice 9 of #9378): search on name, key and
// description, Type and Status filters, sort, Export CSV, the state kept in the
// URL (sfc_), the Enabled switch and the edit / delete row actions.

import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { FeedbackCriterion } from "@/types";
import { CRITERIA_COLUMN_WIDTHS, FeedbackCriteriaSection } from "./FeedbackCriteriaSection";

const criterion = (overrides: Partial<FeedbackCriterion>): FeedbackCriterion => ({
  id: "c",
  key: "c",
  name: "criterion",
  description: null,
  type: "scale",
  weight: 1,
  options: null,
  min_value: 1,
  max_value: 5,
  sort_order: 0,
  enabled: true,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:00:00Z",
  ...overrides,
});

const CRITERIA = [
  criterion({ id: "c-acc", key: "accuracy", name: "Accuracy", description: "Are the facts right?", weight: 2 }),
  criterion({
    id: "c-tone",
    key: "tone",
    name: "Tone",
    type: "categorical",
    min_value: null,
    max_value: null,
    options: [
      { value: "good", label: "Good", score: 1 },
      { value: "bad", label: "Bad", score: 0 },
    ],
    enabled: false,
  }),
  criterion({ id: "c-time", key: "time_saved", name: "Time saved", type: "numeric", min_value: 0, max_value: 120 }),
];

const query = { data: CRITERIA, isLoading: false, isError: false, refetch: vi.fn() };
const mutation = { mutate: vi.fn(), isPending: false };

vi.mock("@/hooks/api/useFeedbackCriteriaApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useFeedbackCriteriaApi")>()),
  useFeedbackCriteria: () => query,
  useCreateFeedbackCriterion: () => mutation,
  useUpdateFeedbackCriterion: () => mutation,
  useDeleteFeedbackCriterion: () => mutation,
}));

function renderSection() {
  return renderWithProviders(
    <MemoryRouter>
      <FeedbackCriteriaSection />
    </MemoryRouter>,
  );
}

function nameCells(): string[] {
  return within(screen.getByRole("table"))
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent ?? "");
}

describe("Settings feedback criteria table", () => {
  it("lists the criteria in the shared data table, by name, with Export CSV", () => {
    renderSection();
    expect(nameCells()).toEqual(["AccuracyAre the facts right?", "Time saved", "Tone"]);
    expect(screen.getAllByRole("button", { name: /export csv/i }).length).toBeGreaterThan(0);
  });

  it("narrows the rows through the table search, description included", () => {
    vi.useFakeTimers();
    try {
      renderSection();
      fireEvent.change(screen.getByRole("searchbox"), { target: { value: "facts" } });
      act(() => {
        vi.runAllTimers();
      });
      expect(nameCells()).toEqual(["AccuracyAre the facts right?"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers the Type and Status filters", () => {
    renderSection();
    // The filter pills open a panel (aria-expanded); the column headers sort.
    const pills = screen
      .getAllByRole("button", { expanded: false })
      .map((button) => button.textContent ?? "")
      .filter((text) => /^(Type|Status)/.test(text));
    expect(pills).toEqual(["Type", "Status"]);
  });

  it("lays out fixed with a width on every column but the criterion", () => {
    renderSection();
    expect(screen.getByRole("table").getAttribute("data-layout")).toBe("fixed");
    const widths = screen.getAllByRole("columnheader").slice(0, 5).map((th) => th.style.width);
    expect(widths).toEqual([
      "",
      ...[CRITERIA_COLUMN_WIDTHS.type, CRITERIA_COLUMN_WIDTHS.values, CRITERIA_COLUMN_WIDTHS.weight, CRITERIA_COLUMN_WIDTHS.enabled].map(
        (px) => `${px}px`,
      ),
    ]);
  });

  it("keeps the Enabled switch and the edit and delete actions on every row", () => {
    renderSection();
    expect(screen.getAllByRole("switch", { name: "Enabled" })).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(3);
    const tone = screen.getAllByTestId("feedback-criterion-row").find((row) => row.textContent?.includes("Tone"));
    expect(within(tone as HTMLElement).getByText("Good, Bad")).toBeInTheDocument();
    expect(within(tone as HTMLElement).getByRole("switch", { name: "Enabled" })).not.toBeChecked();
  });
});
