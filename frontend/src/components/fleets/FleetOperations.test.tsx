/**
 * The fleet's Operations tab opens on ONE control row (header density sweep,
 * #10215 slice b): the intro sentence is the "i" beside the list select, never
 * a visible paragraph, and the select is handed to the list as the first
 * control of its own filter row, never a row of its own.
 */
import type { ReactNode } from "react";
import { screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";

// Each list draws its filter row with the host's lead first: the stand-in does the same.
const { list } = vi.hoisted(() => ({
  list: (name: string) =>
    function List({ toolbarLead }: { toolbarLead?: ReactNode }) {
      return (
        <div data-testid={`list-${name}`}>
          <div data-testid="filter-row">
            {toolbarLead}
            <input aria-label="Search" />
          </div>
        </div>
      );
    },
}));

vi.mock("@/pages/TaskHistory", () => ({ default: list("runs") }));
vi.mock("@/pages/BackgroundTasks", () => ({ default: list("background-tasks") }));
vi.mock("@/pages/operations/WorkItemsTab", () => ({ default: list("work-items") }));
vi.mock("@/pages/operations/SharedStateTab", () => ({ default: list("state") }));
vi.mock("@/pages/operations/WorksetsTab", () => ({ default: list("worksets") }));

import { FleetOperations } from "./FleetOperations";

function renderAt(path: string, locale?: string) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/fleets/:fleetId/operations/:list" element={<FleetOperations fleetId="f1" fleetName="CEO Office" />} />
      </Routes>
    </MemoryRouter>,
    locale ? { locale } : undefined,
  );
}

// The select is 224 px wide (w-56) with a 16 px icon: at 12 px type a label of
// 24 Latin characters (es "Tareas en segundo plano" is 23, about 165 px) or
// 14 Japanese characters (about 12 px each) fits uncut (UX round 3 B1 on #10260).
const LABEL_BUDGET: Record<string, number> = { en: 24, fr: 24, es: 24, ja: 14 };
const LIST_NAMES = ["runs", "background-tasks", "work-items", "state", "worksets"];

describe("FleetOperations", () => {
  it.each(["runs", "background-tasks", "work-items", "state", "worksets"])(
    "puts the list select on the %s list's own filter row, with no row of its own",
    (name) => {
      renderAt(`/fleets/f1/operations/${name}`);
      const row = screen.getByTestId("filter-row");
      expect(row).toContainElement(screen.getByTestId("fleet-operations-list"));
      expect(screen.getByTestId(`list-${name}`)).toBeInTheDocument();
    },
  );

  it.each(Object.keys(LABEL_BUDGET))("keeps every %s list label within the select's width budget", (locale) => {
    for (const name of LIST_NAMES) {
      const { unmount } = renderAt(`/fleets/f1/operations/${name}`, locale);
      const label = (screen.getByTestId("fleet-operations-list").textContent ?? "").trim();
      expect(label.length).toBeGreaterThan(0);
      expect([...label].length, `${locale} "${label}"`).toBeLessThanOrEqual(LABEL_BUDGET[locale]);
      unmount();
    }
  });

  it("keeps the intro sentence in the info tip, never as a visible paragraph", () => {
    renderAt("/fleets/f1/operations/runs");
    expect(screen.queryByText(/run, delegate and produce/)).toBeNull();
    expect(screen.getByRole("img", { name: "About this tab" })).toBeInTheDocument();
    expect(screen.getByTestId("fleet-operations").querySelector("p")).toBeNull();
  });
});
