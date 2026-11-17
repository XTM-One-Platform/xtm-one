// The one visibility control of every list page: a radio group, Mine | Company | Shared | All.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { VisibilityEmptyHint, VisibilityFilter } from "./visibility-filter";

describe("VisibilityFilter", () => {
  it("draws four radios in one order, the value checked and the only tab stop", () => {
    renderWithProviders(<VisibilityFilter value="mine" onChange={() => {}} />);

    const group = screen.getByRole("radiogroup", { name: "Visibility" });
    const radios = screen.getAllByRole("radio");
    expect(group).toBeInTheDocument();
    expect(radios.map((radio) => radio.textContent)).toEqual(["Mine", "Company", "Shared", "All"]);
    expect(radios.map((radio) => radio.getAttribute("aria-checked"))).toEqual(["true", "false", "false", "false"]);
    expect(radios.map((radio) => radio.tabIndex)).toEqual([0, -1, -1, -1]);
  });

  it("selects on click and keeps the selection when the active segment is clicked again", () => {
    const onChange = vi.fn();
    renderWithProviders(<VisibilityFilter value="mine" onChange={onChange} />);

    fireEvent.click(screen.getByRole("radio", { name: "Company" }));
    fireEvent.click(screen.getByRole("radio", { name: "Mine" }));
    expect(onChange.mock.calls).toEqual([["company"], ["mine"]]);
  });

  it("moves with the arrow keys, wraps, and jumps with Home and End", () => {
    const onChange = vi.fn();
    renderWithProviders(<VisibilityFilter value="mine" onChange={onChange} />);
    const mine = screen.getByRole("radio", { name: "Mine" });

    fireEvent.keyDown(mine, { key: "ArrowRight" });
    fireEvent.keyDown(mine, { key: "ArrowLeft" });
    fireEvent.keyDown(mine, { key: "End" });
    fireEvent.keyDown(mine, { key: "Home" });
    fireEvent.keyDown(mine, { key: "a" });
    expect(onChange.mock.calls).toEqual([["company"], ["all"], ["all"], ["mine"]]);
  });

  it("hides a segment the page cannot hold, never reorders the others", () => {
    renderWithProviders(<VisibilityFilter value="all" onChange={() => {}} hidden={["company"]} />);

    expect(screen.getAllByRole("radio").map((radio) => radio.textContent)).toEqual(["Mine", "Shared", "All"]);
  });

  it("shows the counts it is given", () => {
    renderWithProviders(
      <VisibilityFilter value="mine" onChange={() => {}} counts={{ mine: 0, shared: 3, company: 2, all: 5 }} />,
    );

    expect(screen.getByRole("radio", { name: "Shared 3" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "All 5" })).toBeInTheDocument();
  });

  it("draws a placeholder for each count during the first read, never 0 (UX r3 B5 on #11349)", () => {
    const { container } = renderWithProviders(
      <VisibilityFilter value="mine" onChange={() => {}} counts={{ mine: 0, shared: 0, company: 0, all: 0 }} pending />,
    );

    expect(screen.getAllByRole("radio").map((radio) => radio.textContent)).toEqual(["Mine", "Company", "Shared", "All"]);
    expect(container.querySelectorAll('[data-count-pending="true"]')).toHaveLength(4);
  });
});

describe("VisibilityEmptyHint", () => {
  it("on an empty Mine with items elsewhere, says so and links to All", () => {
    const onShowAll = vi.fn();
    renderWithProviders(
      <VisibilityEmptyHint value="mine" counts={{ mine: 0, all: 4 }} onShowAll={onShowAll}>
        <p>No match</p>
      </VisibilityEmptyHint>,
    );

    expect(screen.queryByText("No match")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(onShowAll).toHaveBeenCalledOnce();
  });

  it("leaves a search that matches nothing to the page's own line", () => {
    // Counts are taken under the page's other filters: a search matching
    // nothing empties All too, so the page's own "no match" line shows.
    renderWithProviders(
      <VisibilityEmptyHint value="mine" counts={{ mine: 0, shared: 0, company: 0, all: 0 }} onShowAll={() => {}}>
        <p>No match for the search</p>
      </VisibilityEmptyHint>,
    );

    expect(screen.getByText("No match for the search")).toBeInTheDocument();
    expect(screen.queryByTestId("visibility-empty-hint")).toBeNull();
  });

  it("otherwise renders the page's own line", () => {
    renderWithProviders(
      <>
        <VisibilityEmptyHint value="shared" counts={{ mine: 0, all: 4 }} onShowAll={() => {}}>
          <p>Shared empty</p>
        </VisibilityEmptyHint>
        <VisibilityEmptyHint value="mine" counts={{ mine: 0, all: 0 }} onShowAll={() => {}}>
          <p>Nothing anywhere</p>
        </VisibilityEmptyHint>
      </>,
    );

    expect(screen.getByText("Shared empty")).toBeInTheDocument();
    expect(screen.getByText("Nothing anywhere")).toBeInTheDocument();
    expect(screen.queryByTestId("visibility-empty-hint")).toBeNull();
  });
});
