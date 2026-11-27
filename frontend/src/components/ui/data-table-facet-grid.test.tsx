// SPDX-License-Identifier: Apache-2.0
/**
 * The DataTable's faceted Filters panel (#2556): counts per value, active filters
 * as removable chips, the state in the URL.
 */
import { describe, expect, it } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { renderWithProviders } from "@/testing/providers";

interface Row {
  id: string;
  name: string;
  status: string;
  kind: string;
}

const ROWS: Row[] = [
  { id: "a", name: "Alpha", status: "active", kind: "mail" },
  { id: "b", name: "Bravo", status: "paused", kind: "mail" },
  { id: "c", name: "Charlie", status: "active", kind: "report" },
];
const COLUMNS: DataTableColumn<Row>[] = [{ key: "name", header: "Name", cell: (row) => row.name }];
const FILTERS: DataTableFilter<Row>[] = [
  { key: "status", label: "Status", values: (row) => [row.status] },
  { key: "kind", label: "Kind", values: (row) => [row.kind] },
];

function Location() {
  return <output data-testid="location">{useLocation().search}</output>;
}

function renderTable(url = "/", facetGrid = true) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/"
          element={
            <>
              <DataTable
                rows={ROWS}
                columns={COLUMNS}
                rowKey={(row) => row.id}
                filters={FILTERS}
                filtersInPill
                facetGrid={facetGrid}
                emptyLabel="None"
                urlKey=""
              />
              <Location />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function openPanel() {
  fireEvent.click(within(screen.getByTestId("data-table-filters")).getByRole("button", { name: /Filters/ }));
  return screen.getByRole("group", { name: "Filters" });
}

function option(panel: HTMLElement, label: string) {
  return within(panel).getAllByRole("button").find((button) => button.textContent?.startsWith(label)) as HTMLElement;
}

describe("DataTable facetGrid (#2556)", () => {
  it("lays the groups out in columns and counts each value", () => {
    renderTable();
    const panel = openPanel();
    expect(panel.querySelectorAll("[data-facet]")).toHaveLength(2);
    expect(option(panel, "active").textContent).toContain("2");
    expect(option(panel, "mail").textContent).toContain("2");
    expect(option(panel, "report").textContent).toContain("1");
  });

  it("counts a facet over the rows the other filters keep", () => {
    renderTable("/?f_status=paused");
    const panel = openPanel();
    expect(option(panel, "mail").textContent).toContain("1");
    expect(option(panel, "report").textContent).toContain("0");
    // Its own selection does not narrow its counts.
    expect(option(panel, "active").textContent).toContain("2");
  });

  it("shows the active filters as chips that remove themselves and the URL value", () => {
    renderTable("/?f_status=active&f_kind=report");
    const chips = screen.getByTestId("data-table-filter-chips");
    expect(within(chips).getAllByRole("listitem")).toHaveLength(2);
    expect(chips.textContent).toContain("Status: active");
    fireEvent.click(within(chips).getByRole("button", { name: "Remove filter Status" }));
    expect(screen.getByTestId("location").textContent).not.toContain("f_status");
    expect(screen.getByTestId("location").textContent).toContain("f_kind=report");
    expect(within(screen.getByTestId("data-table-filter-chips")).getAllByRole("listitem")).toHaveLength(1);
  });

  it("writes a chip as one catalog string with the locale's list join", () => {
    renderTable("/?f_status=active&f_status=paused");
    const chip = within(screen.getByTestId("data-table-filter-chips")).getByRole("listitem");
    // One string, no separator outside the catalog (UX r1 B1 on #2559).
    expect(chip.textContent).toBe("Status: active and paused");
    expect(chip.querySelector("[title]")?.getAttribute("title")).toBe("Status: active and paused");
  });

  it("draws no chip and no count without facetGrid", () => {
    renderTable("/?f_status=active", false);
    expect(screen.queryByTestId("data-table-filter-chips")).toBeNull();
    openPanel();
    expect(screen.queryAllByTestId("filter-option-count")).toHaveLength(0);
  });
});
