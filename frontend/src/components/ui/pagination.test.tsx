/**
 * The shared bottom pagination bar - `yarn test:dom`.
 *
 * At total 0 it renders nothing: the list's empty state already says there is
 * nothing. A single page renders nothing either: no pager with no page to move
 * to and no bottom "N results" line. Past one page it draws the pager alone,
 * never a range or a total: every list says its total once, at the top, with the
 * shared TotalChip (Sam, 11 Nov 2026, #11191; UX r1 B1 and B2 on #11192).
 */
import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { Pagination } from "@/components/ui/pagination";

describe("Pagination", () => {
  it("renders nothing on an empty list", () => {
    const { container } = renderWithProviders(
      <Pagination page={0} pageSize={20} total={0} onChange={vi.fn()} />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("No results")).not.toBeInTheDocument();
    expect(screen.queryByText("Page 1 of 1")).not.toBeInTheDocument();
  });

  it("renders nothing on a single page: no pager and no bottom count", () => {
    for (const total of [1, 3, 20]) {
      const { container, unmount } = renderWithProviders(<Pagination page={0} pageSize={20} total={total} onChange={vi.fn()} />);
      expect(container).toBeEmptyDOMElement();
      expect(screen.queryByText(/\bresults?\b/)).not.toBeInTheDocument();
      unmount();
    }
  });

  it("enables the next page across a longer list", () => {
    renderWithProviders(<Pagination page={0} pageSize={20} total={45} onChange={vi.fn()} />);

    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Last" })).toBeEnabled();
  });

  it("draws the pager alone, never the range or the total", () => {
    renderWithProviders(<Pagination page={1} pageSize={20} total={45} onChange={vi.fn()} />);

    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
    expect(screen.queryByTestId("pagination-range")).not.toBeInTheDocument();
    expect(screen.queryByText(/of 45/)).not.toBeInTheDocument();
  });
});
