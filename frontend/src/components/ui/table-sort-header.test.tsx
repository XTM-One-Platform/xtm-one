/**
 * TableSortHeader: a fixed-width label cut in a narrow column is named whole by the
 * design-system tooltip, never a native title (UX r1 B4 on #10853, "Personnes" in a 96 px
 * column), sortable or not; a required column carries the required marker (UX r1 B2).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";
import { TableSortHeader } from "@/components/ui/table-sort-header";
import { renderWithProviders } from "@/testing/providers";

function renderHeader(props: Partial<Parameters<typeof TableSortHeader>[0]> = {}) {
  return renderWithProviders(
    <table>
      <thead>
        <tr>
          <TableSortHeader label="People" style={{ width: 96 }} {...props} />
        </tr>
      </thead>
    </table>,
  );
}

async function nextFrame() {
  await act(async () => {
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
  });
}

describe("TableSortHeader", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it("names a cut sortable label in the tooltip when the header button takes the focus", async () => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(61);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(56);
    renderHeader({ sortKey: "people", onSort: () => undefined });
    const button = screen.getByRole("button", { name: /^People/ });
    expect(button.getAttribute("title")).toBeNull();
    act(() => button.focus());
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips[0].textContent).toContain("People");
  });

  it("draws no tooltip when the label fits", () => {
    renderHeader({ sortKey: "people", onSort: () => undefined });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("keeps the hint in the label's own row, so a fixed width cuts the label and never the icon", () => {
    renderHeader({ label: "People", sortKey: "people", onSort: () => undefined, hint: "Members of the department" });
    const header = screen.getByRole("columnheader");
    const row = screen.getByRole("button", { name: /^People/ }).parentElement as HTMLElement;
    // The row is its own box inside the cell (the cell's ellipsis used to clip the hint, retest of #10853).
    expect(row).not.toBe(header);
    expect(header).toContainElement(row);
    expect(row).toContainElement(screen.getByRole("img", { name: "People" }));
  });

  it("marks a required column, sortable or not", () => {
    renderHeader({ label: "Name", required: true });
    expect(screen.getByRole("columnheader")).toContainElement(screen.getByLabelText("Required"));
  });
});
