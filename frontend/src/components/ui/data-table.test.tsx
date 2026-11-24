/**
 * The one records table: search, column filters with their count and clear
 * all, sort with aria-sort, the no-results state distinct from the empty one,
 * the error state with Retry, the state in the URL, the selection and the
 * hidden columns.
 */
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { BadgeFocusTrigger } from "@/components/ui/badge";
import { HeldBadge } from "@/components/ui/held-badge";
import { IconMark } from "@/components/ui/icon-mark";
import { InfoLabel, InfoTip } from "@/components/ui/info-tip";
import { TagList } from "@/components/ui/tag-list";
import { DataTable, tableSkeletonRowCount, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { readTableState, storedPageSize } from "@/lib/data-table-state";
import { renderWithProviders } from "@/testing/providers";

interface Row {
  id: string;
  name: string;
  status: string;
  runs: number;
}

const ROWS: Row[] = [
  { id: "a", name: "Alpha mailbox", status: "active", runs: 3 },
  { id: "b", name: "Bravo triage", status: "paused", runs: 12 },
  { id: "c", name: "Charlie report", status: "active", runs: 7 },
];

const COLUMNS: DataTableColumn<Row>[] = [
  { key: "name", header: "Name", cell: (row) => row.name, sortValue: (row) => row.name },
  { key: "status", header: "Status", cell: (row) => row.status, sortValue: (row) => row.status, hideable: true },
  { key: "runs", header: "Runs", cell: (row) => row.runs, sortValue: (row) => row.runs, align: "right" },
];

const FILTERS: DataTableFilter<Row>[] = [{ key: "status", label: "Status", values: (row) => [row.status] }];

function Location() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

function renderTable(props: Partial<Parameters<typeof DataTable<Row>>[0]> = {}, url = "/") {
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
                searchFields={(row) => [row.name]}
                searchPlaceholder="Search agents..."
                filters={FILTERS}
                emptyLabel="No agent yet."
                urlKey=""
                {...props}
              />
              <Location />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

const names = () =>
  screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent);

describe("DataTable", () => {
  // #1176: grouping keeps the sort inside each group and names each group with its count.
  it("groups rows under a header row with their count, the sort kept inside each group", () => {
    renderTable({
      defaultSort: { key: "runs", direction: "desc" },
      groupBy: { value: (row) => row.status, label: (value) => `Group ${value}` },
    });
    const groups = screen.getAllByTestId("data-table-group-row");
    expect(groups.map((row) => row.textContent)).toEqual(["Group active2 rows", "Group paused1 row"]);
    expect(names()).toEqual(["Group active2 rows", "Charlie report", "Alpha mailbox", "Group paused1 row", "Bravo triage"]);
  });

  it("filters by a column value, counts the filter and clears it", () => {
    renderTable();
    const pill = screen.getAllByRole("button", { name: /Status/ }).find((button) => button.hasAttribute("aria-expanded"));
    fireEvent.click(pill as HTMLElement);
    fireEvent.click(screen.getByRole("button", { name: "paused" }));
    expect(names()).toEqual(["Bravo triage"]);
    expect(screen.getByTestId("location").textContent).toContain("f_status=paused");
    fireEvent.click(screen.getAllByRole("button", { name: "Clear all filters" })[0]);
    expect(names()).toHaveLength(3);
    expect(screen.getByTestId("location").textContent).not.toContain("f_status");
  });

  // UX round 1 on xtm-one #1004, B1: a toolbar filter (a Status pill) counted on its own
  // pill still offers Clear all, which runs the page's clear.
  it("offers Clear all for a toolbar filter outside the Filters badge", () => {
    const onClearAll = vi.fn();
    renderTable({ toolbarFilterActive: true, onClearAll });
    fireEvent.click(screen.getAllByRole("button", { name: "Clear all filters" })[0]);
    expect(onClearAll).toHaveBeenCalledTimes(1);
  });

  describe("lists every filter's options alphabetically (#12289)", () => {
    const openPill = (name: string) => {
      const pill = screen.getAllByRole("button", { name: new RegExp(name) }).find((button) => button.hasAttribute("aria-expanded"));
      fireEvent.click(pill as HTMLElement);
      return within(screen.getByRole("group", { name }))
        .getAllByRole("button")
        .map((button) => button.textContent);
    };
    const TYPE = {
      key: "type",
      label: "Type",
      options: [
        { value: "agent", label: "Agents" },
        { value: "knowledge_base", label: "Knowledge Bases" },
        { value: "prompt", label: "prompts" },
        { value: "mcp_server", label: "MCP Servers" },
        { value: "integration", label: "États" },
      ],
    };

    it("sorts given options on their label, case and accents ignored", () => {
      renderTable({ filters: [TYPE] });
      expect(openPill("Type")).toEqual(["Agents", "États", "Knowledge Bases", "MCP Servers", "prompts"]);
    });

    it("keeps the pinned values first, then the rest alphabetically", () => {
      renderTable({ filters: [{ ...TYPE, pinned: ["prompt"] }] });
      expect(openPill("Type")).toEqual(["prompts", "Agents", "États", "Knowledge Bases", "MCP Servers"]);
    });

    it("keeps the order of an ordinal scale", () => {
      const priority = {
        key: "priority",
        label: "Priority",
        ordered: true,
        options: [
          { value: "critical", label: "Critical" },
          { value: "high", label: "High" },
          { value: "medium", label: "Medium" },
          { value: "low", label: "Low" },
        ],
      };
      renderTable({ filters: [priority] });
      expect(openPill("Priority")).toEqual(["Critical", "High", "Medium", "Low"]);
    });
  });

  it("says the total once, in the toolbar, and draws no footer on a one-page list", () => {
    const { unmount } = renderTable();
    expect(screen.getByTestId("data-table-total").textContent).toBe("3");
    expect(screen.queryByTestId("pagination")).not.toBeInTheDocument();
    expect(screen.queryByText(/\b\d+ results?\b/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
    unmount();
    renderTable({ rows: ROWS.slice(0, 1) });
    expect(screen.getByTestId("data-table-total").textContent).toBe("1");
    expect(screen.queryByText(/\b\d+ results?\b/)).not.toBeInTheDocument();
  });

  it("sorts on a header click and says so with aria-sort", () => {
    renderTable();
    const runs = screen.getByRole("columnheader", { name: "Runs" });
    expect(runs.getAttribute("aria-sort")).toBe("none");
    fireEvent.click(within(runs).getByRole("button"));
    expect(runs.getAttribute("aria-sort")).toBe("descending");
    expect(names()).toEqual(["Bravo triage", "Charlie report", "Alpha mailbox"]);
    expect(screen.getByTestId("location").textContent).toContain("sort=runs%3Adesc");
  });

  it("restores the view from the URL", () => {
    renderTable({}, "/?q=report&sort=runs:asc");
    expect(names()).toEqual(["Charlie report"]);
  });

  it("keeps a search typed then a sort clicked within the pause", () => {
    vi.useFakeTimers();
    renderTable();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "a" } });
    fireEvent.click(within(screen.getByRole("columnheader", { name: "Runs" })).getByRole("button"));
    act(() => vi.advanceTimersByTime(300));
    vi.useRealTimers();
    const search = screen.getByTestId("location").textContent ?? "";
    expect(search).toContain("q=a");
    expect(search).toContain("sort=runs%3Adesc");
    expect(names()).toEqual(["Bravo triage", "Charlie report", "Alpha mailbox"]);
  });

  it("keeps both tables' keys when two tables of one page write in the same act", () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route
            path="/"
            element={
              <>
                {["aa_", "bb_"].map((prefix) => (
                  <DataTable
                    key={prefix}
                    rows={ROWS}
                    columns={COLUMNS}
                    rowKey={(row) => row.id}
                    filters={FILTERS}
                    emptyLabel="No agent yet."
                    urlKey={prefix}
                  />
                ))}
                <Location />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    );
    const headers = screen.getAllByRole("columnheader", { name: "Runs" });
    act(() => {
      fireEvent.click(within(headers[0]).getByRole("button"));
      fireEvent.click(within(headers[1]).getByRole("button"));
    });
    const search = screen.getByTestId("location").textContent ?? "";
    expect(search).toContain("aa_sort=runs%3Adesc");
    expect(search).toContain("bb_sort=runs%3Adesc");
  });

  it("tells no results apart from empty", () => {
    vi.useFakeTimers();
    renderTable();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "zulu" } });
    act(() => vi.advanceTimersByTime(300));
    vi.useRealTimers();
    expect(screen.getByText("No row matches the search and filters.")).toBeInTheDocument();
    expect(screen.queryByText("No agent yet.")).toBeNull();
  });

  it("keeps the no-results row on the visible part of a table wider than its card (UX round 3 on #10601, A1)", () => {
    const original = globalThis.ResizeObserver;
    // The card is 800 px; its scroll area's client box leaves out the 1 px border and a 6 px vertical scrollbar.
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.testid === "data-table-scroll" ? 792 : 800;
    });
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    vi.useFakeTimers();
    try {
      renderTable();
      fireEvent.change(screen.getByRole("searchbox"), { target: { value: "zulu" } });
      act(() => vi.advanceTimersByTime(300));
      const content = screen.getByTestId("data-table-no-results");
      // As wide as the scroll area's client box, whatever the table's own width: a full-width row past it
      // overflowed by the scrollbar's width (UX round 1 on #1356, B1).
      expect(content.style.width).toBe("792px");
      expect(within(content).getByRole("button", { name: "Clear all filters" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
      width.mockRestore();
      globalThis.ResizeObserver = original;
    }
  });

  it("names the search with the full wording and shows a placeholder that never clips", () => {
    renderTable();
    const search = screen.getByRole("searchbox", { name: "Search agents..." });
    expect(search.getAttribute("placeholder")).toBe("Search...");
  });

  it("shows the empty state when nothing exists", () => {
    renderTable({ rows: [] });
    expect(screen.getByText("No agent yet.")).toBeInTheDocument();
  });

  it("offers Retry on an error", () => {
    const onRetry = vi.fn();
    renderTable({ isError: true, onRetry });
    expect(screen.getByRole("alert").textContent).toContain("Could not load these rows.");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("loads on its own table: the real head and rows at the loaded row height, one cell per column (#11345)", () => {
    const { unmount } = renderTable({ rows: [], server: { total: 0 }, isLoading: true, selection: { selected: [], onChange: vi.fn() } });
    // The head is the loaded head: the same column names, the select-all box's cell.
    const head = screen.getByTestId("data-table-head");
    expect(within(head).getByText("Name")).toBeTruthy();
    expect(within(head).getByText("Runs")).toBeTruthy();
    const skeletonRows = screen.getAllByTestId("data-table-skeleton-row");
    expect(skeletonRows.length).toBeGreaterThan(0);
    // One cell per drawn column, plus the selection cell, as a loaded row.
    expect(skeletonRows[0].children).toHaveLength(COLUMNS.length + 1);
    expect(screen.queryByText("No agent yet.")).not.toBeInTheDocument();
    unmount();

    renderTable();
    expect(screen.queryAllByTestId("data-table-skeleton-row")).toHaveLength(0);
    expect(screen.getByText("Alpha mailbox")).toBeTruthy();
  });

  it("loads a card grid on asset-card skeletons in its own grid, never one block (#11345)", () => {
    const { unmount } = renderTable({
      rows: [],
      isLoading: true,
      gridClassName: "grid gap-3 grid-cols-2",
      renderGrid: (rows) => <div className="grid gap-3 grid-cols-2">{rows.map((row) => <article key={row.id}>{row.name}</article>)}</div>,
    });
    const grid = screen.getByRole("status", { name: "Loading" });
    const cards = within(grid).getAllByTestId("entity-card-skeleton");
    expect(cards.length).toBeGreaterThan(0);
    // Each placeholder carries the loaded card's slots: head, tag row, metadata footer.
    for (const slot of ["card-head", "card-tags", "card-meta"]) {
      expect(cards[0].querySelector(`[data-slot="${slot}"]`)).not.toBeNull();
    }
    expect(screen.queryByText("No agent yet.")).not.toBeInTheDocument();
    unmount();

    renderTable({ renderGrid: (rows) => <div>{rows.map((row) => <article key={row.id}>{row.name}</article>)}</div> });
    expect(screen.queryAllByTestId("entity-card-skeleton")).toHaveLength(0);
    expect(screen.getByText("Alpha mailbox")).toBeTruthy();
  });

  it("makes the card grid a fit-to-window scroll area only with fitGrid (One scrollbar, #11917 B3)", () => {
    const grid = (rows: Row[]) => <div>{rows.map((row) => <article key={row.id}>{row.name}</article>)}</div>;
    const { unmount } = renderTable({ renderGrid: grid });
    // An asset card grid keeps the page scroll: no fit attribute, no bound.
    expect(screen.getByTestId("data-table-grid").hasAttribute("data-scroll-fit")).toBe(false);
    unmount();
    renderTable({ renderGrid: grid, fitGrid: true });
    const area = screen.getByTestId("data-table-grid");
    // Outside the app's page (no #app-content) the area is not measured: no bound yet.
    expect(area.getAttribute("data-scroll-fit")).toBe("capped");
    expect(area.style.maxHeight).toBe("");
    expect(screen.queryByTestId("data-table-scroll")).not.toBeInTheDocument();
  });

  it("loads a row-card grid on the page's own row skeleton when it gives one (#11345)", () => {
    const pageSizes: number[] = [];
    renderTable({
      rows: [],
      isLoading: true,
      renderGrid: (rows) => <div>{rows.map((row) => <article key={row.id}>{row.name}</article>)}</div>,
      renderGridSkeleton: (pageSize) => {
        pageSizes.push(pageSize);
        return <div role="status" aria-label="Rows loading" data-testid="page-row-skeleton" />;
      },
    });
    expect(screen.getByTestId("page-row-skeleton")).toBeTruthy();
    expect(screen.queryAllByTestId("entity-card-skeleton")).toHaveLength(0);
    expect(pageSizes.length).toBeGreaterThan(0);
    expect(pageSizes.every((size) => size > 0)).toBe(true);
    expect(screen.queryByText("No agent yet.")).not.toBeInTheDocument();
  });

  it("draws a page's own card anatomy: no tag row, a one-line footer (#11345, UX round 1 B3)", () => {
    renderWithProviders(<CardGridSkeleton count={2} label="Cards loading" tags={false} meta="caption" />);
    const cards = screen.getAllByTestId("entity-card-skeleton");
    expect(cards).toHaveLength(2);
    expect(cards[0].querySelector('[data-slot="card-tags"]')).toBeNull();
    // The footer row takes the caption line's 15 px height, not the 24 px control row.
    expect(cards[0].querySelector('[data-slot="card-meta"] > [data-meta="caption"]')).not.toBeNull();
    // A one-line footer has no 24 px control placeholder.
    expect(cards[0].querySelector('[data-slot="card-meta-control"]')).toBeNull();
  });

  it("keeps the Tags pill in the toolbar during the first read, so the toolbar does not wrap later (#11345, UX round 2 B5)", () => {
    const { unmount } = renderTable({ rows: [], isLoading: true, tags: { all: [], selected: [], onChange: () => {} } });
    const pending = screen.getByRole("button", { name: /Tags/ });
    expect(pending).toBeDisabled();
    unmount();

    renderTable({ tags: { all: [], selected: [], onChange: () => {} } });
    expect(screen.queryByRole("button", { name: /Tags/ })).toBeNull();
  });

  it("fills the window with loading cards, never more than a page", () => {
    expect(cardSkeletonCount(25, 700)).toBe(15);
    expect(cardSkeletonCount(10, 2000)).toBe(10);
    expect(cardSkeletonCount(25, 0)).toBe(25);
  });

  it("fills the window with loading rows, never more than a page", () => {
    expect(tableSkeletonRowCount(25, 1, 800)).toBe(20);
    expect(tableSkeletonRowCount(10, 1, 800)).toBe(10);
    expect(tableSkeletonRowCount(25, 2, 560)).toBe(10);
    expect(tableSkeletonRowCount(25, 1, 0)).toBe(25);
  });

  it("never reads a total of 0 before a server page answered (UX r1 A1 on #11012)", () => {
    // Loading: a placeholder holds the chip's place, no figure.
    const { unmount } = renderTable({ rows: [], server: { total: 0 }, isLoading: true });
    const loading = screen.getByTestId("data-table-total");
    expect(loading.getAttribute("aria-busy")).toBe("true");
    expect(loading.textContent).toBe("");
    expect(loading.querySelector("[data-reserved-width]")?.getAttribute("data-reserved-width")).toBe("count");
    unmount();
    // Read error: no total at all, the error row says what happened.
    renderTable({ rows: [], server: { total: 0 }, isError: true });
    expect(screen.queryByTestId("data-table-total")).toBeNull();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("holds the width of an 'n of N' total while a narrowed list loads (UX r3 B5 on #11349)", () => {
    renderTable({ rows: [], server: { total: 0 }, isLoading: true, scope: { value: "mine", onChange: () => {}, counts: { mine: 0, all: 0 } } });
    const placeholder = screen.getByTestId("data-table-total").querySelector("[data-reserved-width]");
    expect(placeholder?.getAttribute("data-reserved-width")).toBe("narrowed");
    expect(screen.getByRole("radio", { name: "Mine" }).textContent).toBe("Mine");
  });

  it("reserves widths while loading only: the loaded toolbar fits its text (UX r5 B8 on #11349)", () => {
    const scope = { value: "mine" as const, onChange: () => {}, counts: { mine: 1, all: 1 } };
    const { unmount } = renderTable({ rows: [], server: { total: 0 }, isLoading: true, scope });
    expect(screen.getByTestId("data-table-total").querySelector("[data-reserved-width]")?.getAttribute("data-reserved-width")).toBe("narrowed");
    unmount();
    renderTable({ rows: ROWS.slice(0, 1), server: { total: 1 }, scope });
    // Loaded, the chip is its text alone and every segment count is its figure alone: a
    // min width there drew a 68 px "1" and wrapped Prompts, Skills and Flows library at 1080.
    const chip = screen.getByTestId("data-table-total");
    expect(chip.querySelector("[data-reserved-width]")).toBeNull();
    expect(chip.textContent).toBe("1");
    const slots = document.querySelectorAll('[data-count-slot="true"]');
    expect(slots.length).toBeGreaterThan(0);
    for (const slot of slots) {
      expect(slot.textContent).toBe("1");
      expect(slot.querySelector("[data-reserved-width]")).toBeNull();
    }
  });

  it("reads 'n of N' when a filter narrows a server list given its unfiltered count (#12039)", () => {
    // The page asked the API with the filter: 289 rows match, 419 exist (the audit log retest of #11939).
    renderTable({ rows: ROWS, server: { total: 289 }, totalAll: 419 }, "/?f_status=active");
    expect(screen.getByTestId("data-table-total").textContent).toBe("289 of 419");
  });

  it("shows the server total once the page answered", () => {
    renderTable({ rows: ROWS, server: { total: 42 } });
    const chip = screen.getByTestId("data-table-total");
    expect(chip.textContent).toBe("42");
    expect(chip.hasAttribute("aria-busy")).toBe(false);
  });

  it("draws the page box of a selectable card grid first in the toolbar, no row above the cards (#9378)", () => {
    const onChange = vi.fn();
    const grid = (rows: typeof ROWS) => <div>{rows.map((row) => <article key={row.id}>{row.name}</article>)}</div>;
    const { unmount } = renderTable({ renderGrid: grid, selection: { selected: [], onChange } });
    const toolbar = screen.getByTestId("data-table-toolbar");
    const slot = within(toolbar).getByTestId("data-table-grid-page-select");
    expect(toolbar.firstElementChild).toBe(slot);
    const box = within(slot).getByRole("checkbox", { name: "Select every row on this page" });
    // The grid itself starts with the first card: nothing drawn between the toolbar and it.
    expect(screen.getByTestId("data-table-grid").firstElementChild?.firstElementChild?.tagName).toBe("ARTICLE");
    expect(screen.getAllByRole("checkbox", { name: "Select every row on this page" })).toHaveLength(1);
    fireEvent.click(box);
    expect(onChange).toHaveBeenCalledWith(["a", "b", "c"]);
    unmount();

    // The first read keeps the slot, so the toolbar does not move when the rows land.
    const loading = renderTable({ rows: [], isLoading: true, renderGrid: grid, selection: { selected: [], onChange } });
    expect(screen.getByTestId("data-table-grid-page-select")).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "Select every row on this page" })).toBeNull();
    loading.unmount();

    // No selectable row on the page, or no selection: no box.
    renderTable({ renderGrid: grid, selection: { selected: [], onChange, disabledReason: () => "Locked" } });
    expect(screen.queryByTestId("data-table-grid-page-select")).toBeNull();
  });

  it("selects the rows of the page and shows the bulk actions", () => {
    const onChange = vi.fn();
    const { rerender } = renderTable({ selection: { selected: [], onChange } });
    fireEvent.click(screen.getByRole("checkbox", { name: "Select every row on this page" }));
    expect(onChange).toHaveBeenCalledWith(["a", "b", "c"]);
    rerender(
      <MemoryRouter>
        <DataTable
          rows={ROWS}
          columns={COLUMNS}
          rowKey={(row) => row.id}
          emptyLabel="No agent yet."
          selection={{ selected: ["a"], onChange, actions: <button type="button">Pause</button> }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it("lets a one-row toolbar wrap once rows are selected (UX r2 B6 on #11666)", () => {
    const onChange = vi.fn();
    const { rerender } = renderTable({ filtersInPill: true, selection: { selected: [], onChange } });
    expect(screen.getByTestId("data-table-toolbar").getAttribute("data-wraps")).toBeNull();
    rerender(
      <MemoryRouter>
        <DataTable
          rows={ROWS}
          columns={COLUMNS}
          rowKey={(row) => row.id}
          emptyLabel="No agent yet."
          filtersInPill
          selection={{ selected: ["a", "b"], onChange, actions: <button type="button">Merge</button> }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("data-table-toolbar").getAttribute("data-wraps")).toBe("true");
    expect(within(screen.getByTestId("data-table-selection")).getByText("2 selected")).toBeInTheDocument();
  });

  it("hides a column the user switches off", () => {
    renderTable();
    fireEvent.click(screen.getByRole("button", { name: /Columns/ }));
    fireEvent.click(within(screen.getByRole("group", { name: "Shown columns" })).getByRole("button", { name: "Status" }));
    expect(screen.queryByRole("columnheader", { name: "Status" })).toBeNull();
  });

  it("never lists a headerless column in the picker and always draws it (#11289)", () => {
    const columns: DataTableColumn<Row>[] = [
      ...COLUMNS,
      { key: "actions", header: "", interactive: true, hideable: true, cell: (row) => <button type="button">{`Menu ${row.id}`}</button> },
    ];
    renderTable({ columns });
    fireEvent.click(screen.getByRole("button", { name: /Columns/ }));
    const toggles = within(screen.getByRole("group", { name: "Shown columns" })).getAllByRole("button");
    expect(toggles.map((toggle) => toggle.textContent?.trim())).toEqual(["Status"]);
    expect(screen.getByRole("button", { name: "Menu a" })).toBeInTheDocument();
  });

  it("keeps a default-hidden column out until the picker brings it back (UX r5 B1 on #10581)", () => {
    const columns: DataTableColumn<Row>[] = COLUMNS.map((column) =>
      column.key === "status" ? { ...column, defaultHidden: true } : column,
    );
    renderTable({ columns });
    expect(screen.queryByRole("columnheader", { name: "Status" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Columns/ }));
    const toggle = within(screen.getByRole("group", { name: "Shown columns" })).getByRole("button", { name: "Status" });
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);
    expect(screen.getByRole("columnheader", { name: "Status" })).toBeInTheDocument();
  });

  it("draws a remainder row through the drawn columns, a hidden one included (UX round 1 on #10020, B1)", () => {
    renderTable({
      remainderRows: [{ key: "other", row: { id: "z", name: "unused", status: "mixed", runs: 40 }, label: "Other agents", testId: "remainder" }],
    });
    const header = screen.getAllByRole("row")[0];
    const cells = () => within(screen.getByTestId("remainder")).getAllByRole("cell");
    expect(cells()).toHaveLength(within(header).getAllByRole("columnheader").length);
    expect(cells().map((cell) => cell.textContent)).toEqual(["Other agents", "mixed", "40"]);
    fireEvent.click(screen.getByRole("button", { name: /Columns/ }));
    fireEvent.click(within(screen.getByRole("group", { name: "Shown columns" })).getByRole("button", { name: "Status" }));
    expect(within(screen.getAllByRole("row")[0]).getAllByRole("columnheader")).toHaveLength(2);
    expect(cells().map((cell) => cell.textContent)).toEqual(["Other agents", "40"]);
  });

  it("lets the picker bring back a column hidden by the table width, and says it is hidden (UX round 1 on #10020, B1)", () => {
    const original = globalThis.ResizeObserver;
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(800);
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    try {
      const columns: DataTableColumn<Row>[] = COLUMNS.map((column) =>
        column.key === "status" ? { ...column, hideBelow: "4xl" } : column,
      );
      renderTable({ columns });
      // 800 px is under 4xl (896): the column is not drawn and the picker says so.
      expect(screen.queryByRole("columnheader", { name: "Status" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: /Columns/ }));
      const toggle = within(screen.getByRole("group", { name: "Shown columns" })).getByRole("button", { name: "Status" });
      expect(toggle.getAttribute("aria-pressed")).toBe("false");
      fireEvent.click(toggle);
      expect(screen.getByRole("columnheader", { name: "Status" })).toBeInTheDocument();
      expect(toggle.getAttribute("aria-pressed")).toBe("true");
    } finally {
      width.mockRestore();
      globalThis.ResizeObserver = original;
    }
  });

  it("draws Columns, page size and Export CSV compact under 896 px so the toolbar keeps one row at 1024 (UX r2 B1 on #10336)", () => {
    const original = globalThis.ResizeObserver;
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(730);
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    try {
      renderTable({ exportName: "agents" });
      const exportButton = screen.getByRole("button", { name: "Export CSV" });
      expect(exportButton.textContent).toBe("");
      const columnsButton = screen.getByRole("button", { name: /Columns/ });
      expect(columnsButton.textContent).toBe("Columns");
      const pageSize = screen.getByRole("combobox", { name: "Rows per page" });
      expect(pageSize.getAttribute("data-compact")).toBe("true");
      expect(pageSize.textContent).toBe("25");
    } finally {
      width.mockRestore();
      globalThis.ResizeObserver = original;
    }
  });

  it("wraps a compact one-row toolbar whose page controls still run past its box (UX r3 B1 on #1963)", () => {
    const original = globalThis.ResizeObserver;
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(846);
    // The ja Cases toolbar at 1080: compact controls plus the page's own switch need 867 px.
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.testid === "data-table-toolbar" ? 867 : 0;
    });
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    try {
      renderTable({ filtersInPill: true, exportName: "cases", toolbar: <button type="button">Page control</button> });
      expect(screen.getByRole("combobox", { name: "Rows per page" }).getAttribute("data-compact")).toBe("true");
      expect(screen.getByTestId("data-table-toolbar").getAttribute("data-wraps")).toBe("true");
    } finally {
      scroll.mockRestore();
      width.mockRestore();
      globalThis.ResizeObserver = original;
    }
  });

  it("keeps a compact one-row toolbar on one row when it fits its box", () => {
    const original = globalThis.ResizeObserver;
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(846);
    const scroll = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.dataset.testid === "data-table-toolbar" ? 846 : 0;
    });
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    try {
      renderTable({ filtersInPill: true, exportName: "cases", toolbar: <button type="button">Page control</button> });
      expect(screen.getByRole("combobox", { name: "Rows per page" }).getAttribute("data-compact")).toBe("true");
      expect(screen.getByTestId("data-table-toolbar").getAttribute("data-wraps")).toBeNull();
    } finally {
      scroll.mockRestore();
      width.mockRestore();
      globalThis.ResizeObserver = original;
    }
  });

  it("keeps the full toolbar labels on a wide table", () => {
    const original = globalThis.ResizeObserver;
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1200);
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    try {
      renderTable({ exportName: "agents" });
      expect(screen.getByRole("button", { name: "Export CSV" }).textContent).toBe("Export CSV");
      expect(screen.getByRole("combobox", { name: "Rows per page" }).getAttribute("data-compact")).toBeNull();
    } finally {
      width.mockRestore();
      globalThis.ResizeObserver = original;
    }
  });

  it("sizes the row-actions column under the fixed layout", () => {
    renderTable({ layout: "fixed", rowActions: () => <button type="button">Edit</button> });
    expect(screen.getByTestId("data-table-actions-header").style.width).toBe("96px");
  });

  it("lets the caller size the row-actions column", () => {
    renderTable({ layout: "fixed", rowActionsWidth: 64, rowActions: () => <button type="button">Edit</button> });
    expect(screen.getByTestId("data-table-actions-header").style.width).toBe("64px");
  });

  it("opens a row in place from the keyboard and says whether it is open", () => {
    function Opening({ toggleInCell = false }: { toggleInCell?: boolean }) {
      const [open, setOpen] = useState<string[]>([]);
      return (
        <DataTable
          rows={ROWS}
          columns={COLUMNS}
          rowKey={(row) => row.id}
          emptyLabel="No agent yet."
          onRowClick={(row) => setOpen((ids) => (ids.includes(row.id) ? ids.filter((id) => id !== row.id) : [...ids, row.id]))}
          expandedRow={(row) => (open.includes(row.id) ? <p>Detail of {row.name}</p> : null)}
          toggleInCell={toggleInCell}
          rowTestId="opening-row"
        />
      );
    }
    const { unmount } = renderWithProviders(
      <MemoryRouter>
        <Opening />
      </MemoryRouter>,
    );
    const first = screen.getAllByTestId("opening-row")[0];
    expect(first.getAttribute("tabindex")).toBe("0");
    expect(first.getAttribute("aria-expanded")).toBe("false");
    fireEvent.keyDown(first, { key: "Enter" });
    expect(screen.getByText("Detail of Alpha mailbox")).toBeInTheDocument();
    expect(screen.getAllByTestId("opening-row")[0].getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(screen.getAllByTestId("opening-row")[0], { key: " " });
    expect(screen.queryByText("Detail of Alpha mailbox")).toBeNull();
    unmount();

    renderWithProviders(
      <MemoryRouter>
        <Opening toggleInCell />
      </MemoryRouter>,
    );
    const row = screen.getAllByTestId("opening-row")[0];
    expect(row.hasAttribute("tabindex")).toBe(false);
    expect(row.hasAttribute("aria-expanded")).toBe(false);
  });

  it("opens a row from the keyboard without a link in its cells, and leaves a row that opens nothing alone (#12070)", () => {
    const opened: string[] = [];
    renderWithProviders(
      <MemoryRouter>
        <DataTable
          rows={ROWS}
          columns={COLUMNS}
          rowKey={(row) => row.id}
          emptyLabel="No agent yet."
          onRowClick={(row) => opened.push(row.id)}
          rowOpens={(row) => row.id !== ROWS[1].id}
          rowTestId="page-row"
        />
      </MemoryRouter>,
    );
    const [first, second] = screen.getAllByTestId("page-row");
    expect(first.getAttribute("tabindex")).toBe("0");
    expect(first.hasAttribute("aria-expanded")).toBe(false);
    expect(screen.queryAllByRole("link")).toEqual([]);
    fireEvent.keyDown(first, { key: "Enter" });
    expect(opened).toEqual([ROWS[0].id]);
    expect(second.hasAttribute("tabindex")).toBe(false);
    fireEvent.click(second);
    expect(opened).toEqual([ROWS[0].id]);
  });

  it("keeps a routed row to ONE tab stop: a date cell adds none there, and one elsewhere (#12070, D2)", () => {
    const created = "2026-11-15T10:00:00Z";
    const columns: DataTableColumn<Row>[] = [
      ...COLUMNS,
      { key: "created", header: "Created", cell: () => <DateTimeCell value={created} testId="created-cell" /> },
    ];
    renderWithProviders(
      <MemoryRouter>
        <DataTable
          rows={ROWS}
          columns={columns}
          rowKey={(row) => row.id}
          emptyLabel="No agent yet."
          onRowClick={() => undefined}
          rowOpens={(row) => row.id !== ROWS[1].id}
          rowTestId="page-row"
        />
      </MemoryRouter>,
    );
    const [first, second] = screen.getAllByTestId("page-row");
    const stops = (row: HTMLElement) => [row, ...Array.from(row.querySelectorAll("[tabindex]"))].filter((el) => el.getAttribute("tabindex") === "0");
    expect(stops(first)).toEqual([first]);
    // A row that opens nothing keeps the date focusable: its tooltip still opens from the keyboard.
    expect(within(second).getByTestId("created-cell").getAttribute("tabindex")).toBe("0");
  });

  it("keeps a routed row to ONE tab stop with every shared tooltip trigger in its cells (#12070, UX round 1 B1, B2)", () => {
    const columns: DataTableColumn<Row>[] = [
      ...COLUMNS,
      {
        key: "marks",
        header: "Marks",
        cell: () => (
          <span>
            <IconMark label="Company-managed" testId="mark">
              <span aria-hidden="true">M</span>
            </IconMark>
            <InfoTip label="Why" content="Because" />
            <InfoLabel content="Term">Term</InfoLabel>
            <BadgeFocusTrigger>Badge</BadgeFocusTrigger>
            <HeldBadge reason="ai_disabled" />
            <TagList tags={["a", "b", "c", "d"]} max={1} />
          </span>
        ),
      },
    ];
    renderWithProviders(
      <MemoryRouter>
        <DataTable
          rows={ROWS}
          columns={columns}
          rowKey={(row) => row.id}
          emptyLabel="No agent yet."
          onRowClick={() => undefined}
          rowOpens={(row) => row.id !== ROWS[1].id}
          rowTestId="page-row"
        />
      </MemoryRouter>,
    );
    const [first, second] = screen.getAllByTestId("page-row");
    const stops = (row: HTMLElement) => [row, ...Array.from(row.querySelectorAll("[tabindex]"))].filter((el) => el.getAttribute("tabindex") === "0");
    expect(stops(first)).toEqual([first]);
    // Outside a routed row each trigger stays a tab stop, so its tooltip opens from the keyboard.
    expect(within(second).getByTestId("mark").getAttribute("tabindex")).toBe("0");
    expect(stops(second).length).toBeGreaterThanOrEqual(5);
  });

  function OpeningTable({ columns = COLUMNS }: { columns?: DataTableColumn<Row>[] }) {
    const [open, setOpen] = useState<string[]>([]);
    return (
      <DataTable
        rows={ROWS}
        columns={columns}
        rowKey={(row) => row.id}
        emptyLabel="No agent yet."
        layout="fixed"
        onRowClick={(row) => setOpen((ids) => (ids.includes(row.id) ? ids.filter((id) => id !== row.id) : [...ids, row.id]))}
        expandedRow={(row) => (open.includes(row.id) ? <p>Detail of {row.name}</p> : null)}
        rowActions={() => <button type="button">Edit</button>}
        rowTestId="opening-row"
      />
    );
  }

  it("keeps the keyboard focus on a row it opens and closes (UX round 2 on #9975, B1)", () => {
    renderWithProviders(
      <MemoryRouter>
        <OpeningTable />
      </MemoryRouter>,
    );
    const first = screen.getAllByTestId("opening-row")[0];
    first.focus();
    fireEvent.keyDown(first, { key: "Enter" });
    expect(screen.getByText("Detail of Alpha mailbox")).toBeInTheDocument();
    // Same element, still focused: the row was not remounted by the toggle.
    expect(screen.getAllByTestId("opening-row")[0]).toBe(first);
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "Enter" });
    expect(screen.queryByText("Detail of Alpha mailbox")).toBeNull();
    expect(document.activeElement).toBe(first);
  });

  it("asks a server page with the page size the user kept (UX round 3 on #10980, B1)", () => {
    const many: Row[] = Array.from({ length: 30 }, (_, index) => ({ id: `r${index}`, name: `Row ${index}`, status: "active", runs: index }));
    const limits: number[] = [];
    // A server-mode page as MemoryTab and FeedbackTab write it: the fetch limit comes from the URL
    // state, its default from the stored preferences, and the rows are the page the API answers.
    function ServerPage() {
      const location = useLocation();
      const state = readTableState(new URLSearchParams(location.search), "g_", { sort: null, pageSize: storedPageSize("guard-server", 25) });
      limits.push(state.pageSize);
      return (
        <DataTable
          rows={many.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize)}
          columns={COLUMNS}
          rowKey={(row) => row.id}
          server={{ total: many.length }}
          filters={FILTERS}
          emptyLabel="No agent yet."
          urlKey="g_"
          storageKey="guard-server"
          defaultPageSize={25}
        />
      );
    }
    localStorage.setItem("copilot-table:guard-server", JSON.stringify({ pageSize: 10 }));
    try {
      renderWithProviders(
        <MemoryRouter>
          <ServerPage />
        </MemoryRouter>,
      );
      expect(limits[limits.length - 1]).toBe(10);
      expect(screen.getAllByRole("row")).toHaveLength(11);
      expect(screen.getByText("10 per page")).toBeInTheDocument();
    } finally {
      localStorage.removeItem("copilot-table:guard-server");
    }
  });

  it("spans an opened row over the columns drawn, not the hidden ones (UX round 2 on #9975, B2)", () => {
    const style = document.createElement("style");
    style.textContent = ".hidden { display: none; }";
    document.head.appendChild(style);
    try {
      const columns: DataTableColumn<Row>[] = COLUMNS.map((column) =>
        column.key === "status" ? { ...column, hideBelow: "4xl" } : column,
      );
      renderWithProviders(
        <MemoryRouter>
          <OpeningTable columns={columns} />
        </MemoryRouter>,
      );
      fireEvent.click(screen.getAllByTestId("opening-row")[0]);
      const cell = within(screen.getByTestId("data-table-expanded-row")).getByText("Detail of Alpha mailbox").closest("td");
      // Name, Runs and the actions column: the Status column is hidden at this width.
      expect(cell?.getAttribute("colspan")).toBe("3");
    } finally {
      style.remove();
    }
  });

  it("keeps every plain cell inside its column under layout fixed, never the caller (Sam, 15 Nov 2026)", () => {
    const { unmount } = renderTable({ layout: "fixed" });
    // A plain string or number cell is drawn by the table as the shared cut text (the
    // ellipsis and the tooltip when it is cut); the caller passes the bare value.
    const name = screen.getByText("Alpha mailbox");
    expect(name.tagName).toBe("SPAN");
    expect(name.closest("td")).not.toBe(name);
    expect(screen.getByText("12").tagName).toBe("SPAN");
    unmount();

    // Auto layout draws the value as before.
    renderTable();
    expect(screen.getByText("Alpha mailbox").tagName).toBe("TD");
  });
});
