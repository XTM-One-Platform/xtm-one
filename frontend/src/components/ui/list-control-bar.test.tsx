/**
 * The shared list control bar's pager - `yarn test:dom`.
 *
 * An empty list has nothing to select and no page to move to, so the bar draws
 * nothing: the list's own empty state speaks alone, never under a "No results"
 * row or a disabled "First 1/1 Last" pager. A list of one or more items keeps it.
 */
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { Check } from "lucide-react";
import { ListControlBar, SelectionBox } from "@/components/ui/list-control-bar";

type Selection = NonNullable<ComponentProps<typeof ListControlBar>["selection"]>;

const selection = (overrides: Partial<Selection> = {}): Selection => ({
  hasVisibleRows: false,
  allVisibleSelected: false,
  selectedCount: 0,
  allMatchingSelected: false,
  totalMatching: 0,
  canSelectAllMatching: false,
  onToggleVisible: vi.fn(),
  onSelectAllMatching: vi.fn(),
  onClear: vi.fn(),
  onBulkDelete: vi.fn(),
  bulkDeleting: false,
  ...overrides,
});

describe("ListControlBar pager", () => {
  it("renders nothing on an empty list", () => {
    const { container } = renderWithProviders(
      <ListControlBar page={0} pageSize={20} total={0} onPageChange={vi.fn()} />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("No results")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
    expect(screen.queryByText("1/1")).not.toBeInTheDocument();
  });

  it("renders nothing on an empty list with nothing selected", () => {
    const { container } = renderWithProviders(
      <ListControlBar page={0} pageSize={20} total={0} onPageChange={vi.fn()} selection={selection()} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the selection actions when a selection outlives an emptied list", () => {
    renderWithProviders(
      <ListControlBar page={0} pageSize={20} total={0} onPageChange={vi.fn()} selection={selection({ selectedCount: 2 })} />,
    );

    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
  });

  // With an approvable selection the bulk actions outgrow the row at 1024
  // and at 1440; the selection group wraps instead of painting under the pager. Asserted on data-part and
  // data-wrap, never on a class name (frontend/AGENTS.md).
  it("lets the selection actions wrap when an approvable selection adds the bulk approval buttons", () => {
    const { container } = renderWithProviders(
      <ListControlBar
        page={0}
        pageSize={20}
        total={22}
        onPageChange={vi.fn()}
        selection={selection({
          hasVisibleRows: true,
          selectedCount: 2,
          approvableCount: 2,
          approvableRunCount: 2,
          onBulkApprove: vi.fn(),
        })}
      />,
    );

    const group = container.querySelector('[data-part="selection-actions"]');
    expect(group).not.toBeNull();
    expect(group).toHaveAttribute("data-wrap", "true");
    expect(group).toContainElement(screen.getByRole("button", { name: "Clear" }));
    expect(group).toContainElement(screen.getByRole("button", { name: "Approve 2 selected" }));
    expect(group).toContainElement(screen.getByRole("button", { name: "Delete" }));
    expect(group).not.toContainElement(screen.getByRole("button", { name: "First" }));
  });

  it("draws no pager when the list says it has none, and keeps the range", () => {
    renderWithProviders(<ListControlBar page={0} pageSize={7} total={7} onPageChange={vi.fn()} pager={false} />);

    expect(screen.queryByRole("button", { name: "First" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Next page" })).toBeNull();
    expect(screen.queryByText("1/1")).toBeNull();
    expect(screen.getByText(/of 7/)).toBeInTheDocument();
  });

  it("keeps the pager on a list of one item", () => {
    renderWithProviders(<ListControlBar page={0} pageSize={20} total={1} onPageChange={vi.fn()} />);

    expect(screen.getByRole("button", { name: "First" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous page" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Last" })).toBeInTheDocument();
    expect(screen.getByText("1/1")).toBeInTheDocument();
  });

  it("draws no range and no pager while the list loads, whatever the count", () => {
    const { container } = renderWithProviders(
      <ListControlBar page={0} pageSize={20} total={22} onPageChange={vi.fn()} loading />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/^Showing /)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
  });

  it("keeps a selectable list's header row while it loads: a select-all and a total placeholder, no pager (#11345)", () => {
    renderWithProviders(
      <ListControlBar paging="footer" showTotal page={0} pageSize={20} total={0} onPageChange={vi.fn()} selection={selection()} loading />,
    );

    expect(screen.getByTestId("list-control-bar")).toBeInTheDocument();
    expect(screen.getByTestId("list-control-bar-select-skeleton")).toBeInTheDocument();
    expect(screen.getByTestId("list-control-bar-total").getAttribute("aria-busy")).toBe("true");
    expect(screen.queryByRole("checkbox", { name: "Select all" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
  });

  it("keeps a selection but no pager while the list loads", () => {
    renderWithProviders(
      <ListControlBar page={0} pageSize={20} total={22} onPageChange={vi.fn()} selection={selection({ selectedCount: 2 })} loading />,
    );

    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
  });

  it("keeps the pager enabled across pages of a longer list", () => {
    renderWithProviders(<ListControlBar page={0} pageSize={20} total={22} onPageChange={vi.fn()} />);

    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Last" })).toBeEnabled();
  });

  it("hands the count and pager to the footer, keeping the selection", () => {
    renderWithProviders(
      <ListControlBar paging="footer" page={0} pageSize={20} total={22} onPageChange={vi.fn()} selection={selection({ hasVisibleRows: true })} />,
    );

    expect(screen.queryByText(/^Showing /)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
    expect(screen.queryByText("1/2")).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select all" })).toBeInTheDocument();
  });

  it("draws nothing with the pager in the footer and nothing to select or control", () => {
    const { container } = renderWithProviders(
      <ListControlBar paging="footer" page={0} pageSize={20} total={22} onPageChange={vi.fn()} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("says the total once at the top with showTotal, the pager left to the footer", () => {
    renderWithProviders(<ListControlBar paging="footer" showTotal page={0} pageSize={20} total={22} onPageChange={vi.fn()} />);

    expect(screen.getByTestId("list-control-bar-total").textContent).toBe("22");
    expect(screen.queryByText(/^Showing /)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
  });
});

// A control of the list (Background Tasks' sort) rides on the bar's own row, before the pager.
describe("ListControlBar extra slot", () => {
  it("renders the extra control inside the bar, before the pager", () => {
    renderWithProviders(
      <ListControlBar
        page={0}
        pageSize={20}
        total={22}
        onPageChange={vi.fn()}
        extra={<div role="radiogroup" aria-label="Sort by" />}
      />,
    );

    const bar = screen.getByTestId("list-control-bar");
    const slot = screen.getByTestId("list-control-bar-extra");
    const sort = screen.getByRole("radiogroup", { name: "Sort by" });
    const first = screen.getByRole("button", { name: "First" });

    expect(bar).toContainElement(slot);
    expect(slot).toContainElement(sort);
    expect(sort.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps the bar and its extra control mounted while the list loads, with no range and no pager", () => {
    renderWithProviders(
      <ListControlBar
        page={0}
        pageSize={20}
        total={22}
        onPageChange={vi.fn()}
        extra={<div role="radiogroup" aria-label="Sort by" />}
        loading
      />,
    );

    expect(screen.getByTestId("list-control-bar")).toContainElement(screen.getByTestId("list-control-bar-extra"));
    expect(screen.getByRole("radiogroup", { name: "Sort by" })).toBeInTheDocument();
    expect(screen.queryByText(/^Showing /)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
  });

  it("hides the bar and its extra control on an empty list that has loaded", () => {
    const { container } = renderWithProviders(
      <ListControlBar
        page={0}
        pageSize={20}
        total={0}
        onPageChange={vi.fn()}
        extra={<div role="radiogroup" aria-label="Sort by" />}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("draws no slot wrapper without an extra control", () => {
    renderWithProviders(<ListControlBar page={0} pageSize={20} total={22} onPageChange={vi.fn()} />);

    expect(screen.getByTestId("list-control-bar")).toBeInTheDocument();
    expect(screen.queryByTestId("list-control-bar-extra")).not.toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
  });
});

// The box every selectable list row and the bar share. Its contrast (3:1 or more on every surface,
// both themes) is proven in the PR body and the component's JSDoc; these tests assert roles and state only
// (frontend/AGENTS.md: never on class names).
describe("SelectionBox", () => {
  it("is an enabled checkbox that toggles with its accessible name", () => {
    const onToggle = vi.fn();
    renderWithProviders(<SelectionBox checked={false} onToggle={onToggle} label="Select row" />);

    const box = screen.getByRole("checkbox", { name: "Select row" });
    expect(box).toBeEnabled();
    expect(box).toHaveAttribute("aria-checked", "false");
    fireEvent.click(box);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("reports the checked and mixed states", () => {
    renderWithProviders(
      <>
        <SelectionBox checked onToggle={vi.fn()} label="Deselect row" />
        <SelectionBox checked="mixed" onToggle={vi.fn()} label="Select all" />
      </>,
    );

    expect(screen.getByRole("checkbox", { name: "Deselect row" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("checkbox", { name: "Select all" })).toHaveAttribute("aria-checked", "mixed");
  });

  it("keeps a disabled box that cannot be toggled", () => {
    const onToggle = vi.fn();
    renderWithProviders(
      <SelectionBox checked={false} onToggle={onToggle} label="Select row" disabled reason="Running" />,
    );

    const box = screen.getByRole("checkbox", { name: "Select row" });
    expect(box).toBeDisabled();
    expect(box).toHaveAttribute("aria-checked", "false");
    fireEvent.click(box);
    expect(onToggle).not.toHaveBeenCalled();
  });
});

describe("ListControlBar bulk actions slot", () => {
  it("draws a list's own bulk actions and no Delete when the list has none", () => {
    const onSelect = vi.fn();
    renderWithProviders(
      <ListControlBar
        page={0}
        pageSize={20}
        total={3}
        onPageChange={vi.fn()}
        selection={selection({
          hasVisibleRows: true,
          selectedCount: 2,
          onBulkDelete: undefined,
          bulkActions: [{ id: "reset", label: "Reset to fleet defaults", icon: Check, onSelect }],
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reset to fleet defaults" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  // An icon-only action keeps its label as the accessible name and draws no text, so a long translated
  // label cannot move it to a second row (UX round 2 of #9422, B1).
  it("draws an icon-only bulk action without its label text, named by it", () => {
    const onSelect = vi.fn();
    renderWithProviders(
      <ListControlBar
        page={0}
        pageSize={20}
        total={3}
        onPageChange={vi.fn()}
        selection={selection({
          hasVisibleRows: true,
          selectedCount: 2,
          onBulkDelete: undefined,
          bulkActions: [{ id: "reset", label: "Restablecer todo", icon: Check, onSelect, iconOnly: true }],
        })}
      />,
    );

    const reset = screen.getByRole("button", { name: "Restablecer todo" });
    expect(reset).toHaveTextContent("");
    expect(screen.queryByText("Restablecer todo")).not.toBeInTheDocument();
    fireEvent.click(reset);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("keeps a disabled bulk action disabled", () => {
    const onSelect = vi.fn();
    renderWithProviders(
      <ListControlBar
        page={0}
        pageSize={20}
        total={3}
        onPageChange={vi.fn()}
        selection={selection({
          hasVisibleRows: true,
          selectedCount: 1,
          bulkActions: [{ id: "reset", label: "Reset", icon: Check, onSelect, disabled: true, disabledReason: "Nothing to reset" }],
        })}
      />,
    );

    expect(screen.getByTestId("list-bulk-action-reset")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("clears the selection on Escape, not while a field has the focus", () => {
    const onClear = vi.fn();
    renderWithProviders(
      <>
        <input aria-label="field" />
        <ListControlBar page={0} pageSize={20} total={3} onPageChange={vi.fn()} selection={selection({ hasVisibleRows: true, selectedCount: 2, onClear })} />
      </>,
    );

    fireEvent.keyDown(screen.getByLabelText("field"), { key: "Escape" });
    expect(onClear).not.toHaveBeenCalled();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});
