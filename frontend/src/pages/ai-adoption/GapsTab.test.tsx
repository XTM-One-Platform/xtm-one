// The inactive users of the Gaps tab are drawn by the shared DataTable
// (dev-docs/reference/data-tables.md, slice 6i): search on name and email,
// sort, page size, Export CSV, and the state kept in the URL. Agentic AI only:
// one section, no section heading (#11380).

import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import GapsTab from "./GapsTab";

const SUMMARY = { active_count: 1, inactive_count: 2, low_activity_count: 0, avg_active_days: 1 };
const INACTIVE = [
  { user_id: "u-2", name: "Zoe Martin", email: "zoe@example.com", score: 0 },
  { user_id: "u-1", name: "Ada Lovelace", email: "ada@example.com", score: 0 },
];

vi.mock("@/hooks/api/useAiAdoptionApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useAiAdoptionApi")>()),
  useAiAdoptionGaps: () => ({
    isLoading: false,
    data: {
      summary: SUMMARY,
      prev_summary: null,
      total_users: 3,
      inactive: INACTIVE,
      low_activity: [],
      trend: [],
      volume: [],
      activity_threshold: 5,
    },
  }),
}));

function renderGaps(locale?: string) {
  return renderWithProviders(
    <MemoryRouter>
      <GapsTab />
    </MemoryRouter>,
    locale ? { locale } : undefined,
  );
}

describe("GapsTab inactive users", () => {
  it("lists the inactive users in the shared data table, name A to Z first", () => {
    renderGaps();
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0].textContent)).toEqual(["Ada Lovelace", "Zoe Martin"]);
    expect(screen.getAllByRole("button", { name: /export csv/i }).length).toBeGreaterThan(0);
  });

  it("narrows the rows by email through the table search", () => {
    vi.useFakeTimers();
    renderGaps();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "zoe@" } });
    act(() => vi.advanceTimersByTime(300));
    vi.useRealTimers();
    const table = screen.getByRole("table");
    expect(within(table).queryByText("Ada Lovelace")).toBeNull();
    expect(within(table).getByText("Zoe Martin")).toBeTruthy();
  });

  it("translates the inactive users card title", () => {
    renderGaps("fr");
    expect(screen.getAllByText("Utilisateurs inactifs").length).toBeGreaterThan(0);
  });

  it("puts every card straight in its widget row, so the row gives each one its height (#11470)", () => {
    const { container } = renderGaps();
    const rows = Array.from(container.querySelectorAll("[data-widget-row]"));
    expect(rows.length).toBe(3);
    for (const row of rows) {
      const children = Array.from(row.children);
      expect(children.length).toBeGreaterThan(0);
      // A wrapper around a card would stretch while the card inside it ends short.
      expect(children.every((child) => child.matches('[data-slot="card"]'))).toBe(true);
    }
    const bottom = screen.getByTestId("gaps-bottom-row");
    expect(within(bottom).getByTestId("gaps-inactive-users-cell").parentElement).toBe(bottom);
  });

  it("draws one agentic section, without a Development or Agentic AI heading", () => {
    renderGaps();
    const cells = screen.getAllByTestId("gaps-inactive-users-cell");
    expect(cells.length).toBe(1);
    expect(within(cells[0]).getByRole("table")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: /development|agentic ai/i })).toBeNull();
  });
});
