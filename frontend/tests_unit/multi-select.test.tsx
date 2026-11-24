import { describe, it, expect, vi, beforeAll } from "vitest";
import { useState } from "react";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MultiSelect } from "@/components/ui/multi-select";
import { renderWithProviders as render } from "@/testing/providers";

/**
 * Contract test for the `Combobox` that replaced the hand-rolled multi-select.
 *
 * It exists because the end-to-end assertions on this control are expensive to
 * be wrong about: the suite runs only in CI, takes the better part of an hour,
 * and three of the things those assertions depend on are decisions the LIBRARY
 * makes, not this app —
 *
 *   1. the field is named by its label, and that name must not change when a
 *      selection is made (the placeholder switches to the search hint);
 *   2. a selection appears as a chip whose delete button is named
 *      `Remove <label>`, which is what replaced the old "N selected" trigger;
 *   3. `aria-selected` carries the selection state that the hand-rolled control
 *      drew as a check icon.
 *
 * Each is asserted below in the same shape the Playwright specs use, so a
 * library upgrade that changes any of them fails here, in seconds, instead of
 * in a CI run forty minutes later.
 *
 * One caveat, learned the hard way, about what this file can and cannot settle:
 * it exercises the component in isolation, and the CALL SITES wire it to the
 * URL. The library keeps the list open after a pick in multiple mode — asserted
 * below — but in the app the pick writes a query parameter and the re-render
 * that follows closes the popover. So a test here showing the list open is NOT
 * licence to drop the reopen step from the end-to-end specs; that inference was
 * made once and cost a CI run.
 */

const OPTIONS = [
  { value: "alpha", label: "Agent Alpha" },
  { value: "beta", label: "Agent Beta" },
];

/** Radix positions its popover with floating-ui, which jsdom does not implement. */
beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = vi.fn();
  }
});

/**
 * Local state, deliberately — NOT what the call sites do. They round-trip the
 * selection through the URL, and that difference is the caveat above: it is what
 * closes the popover in the app and not here.
 */
function Harness({ onToggle }: { onToggle?: (value: string) => void }) {
  const [selected, setSelected] = useState<string[]>([]);
  return (
    <MultiSelect
      options={OPTIONS}
      selected={selected}
      onToggle={(value) => {
        onToggle?.(value);
        setSelected((current) =>
          current.includes(value) ? current.filter((v) => v !== value) : [...current, value],
        );
      }}
      placeholder="Select agents"
      searchPlaceholder="Search agents..."
      emptyOptionsLabel="No agents available"
      noMatchLabel="No matching agents."
    />
  );
}

describe("MultiSelect", () => {
  it("names the field by its label, before and after a selection", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const field = screen.getByRole("combobox", { name: "Select agents" });
    await user.click(field);
    await user.click(await screen.findByRole("option", { name: "Agent Alpha" }));

    // The regression this guards: the placeholder becomes "Search agents..."
    // here, and if the placeholder is the only thing naming the input then the
    // field loses its identity exactly when it has content.
    expect(screen.getByRole("combobox", { name: "Select agents" })).toBeInTheDocument();
  });

  it("shows the selection as a chip whose delete button names it", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("combobox", { name: "Select agents" }));
    await user.click(await screen.findByRole("option", { name: "Agent Alpha" }));

    expect(screen.getByRole("button", { name: "Remove Agent Alpha" })).toBeInTheDocument();
  });

  it("keeps the list open after a pick — in isolation; the app's URL update closes it", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("combobox", { name: "Select agents" }));
    await user.click(await screen.findByRole("option", { name: "Agent Alpha" }));

    // Multiple mode does not close on pick. This is the LIBRARY's behaviour with
    // local state; the traces filters drive selection through the URL, and the
    // re-render that causes does close it, which is why those specs reopen the
    // field before asserting. Both facts are true and neither implies the other.
    const option = screen.getByRole("option", { name: "Agent Alpha" });
    expect(option).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("option", { name: "Agent Beta" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("reports each change once, in both directions", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(<Harness onToggle={onToggle} />);

    await user.click(screen.getByRole("combobox", { name: "Select agents" }));
    await user.click(await screen.findByRole("option", { name: "Agent Alpha" }));
    await user.click(screen.getByRole("option", { name: "Agent Beta" }));

    expect(onToggle.mock.calls).toEqual([["alpha"], ["beta"]]);

    // Deselecting reports the same value again — the call sites treat each
    // value as an independent filter toggle.
    await user.click(screen.getByRole("option", { name: "Agent Alpha" }));
    expect(onToggle.mock.calls).toEqual([["alpha"], ["beta"], ["alpha"]]);
  });

  it("read-only: no list opens, no chip or clear button acts, and the help line names the field", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(
      <>
        <MultiSelect
          options={OPTIONS}
          selected={["alpha"]}
          onToggle={onToggle}
          placeholder="Select agents"
          searchPlaceholder="Search agents..."
          emptyOptionsLabel="No agents available"
          noMatchLabel="No matching agents."
          aria-describedby="agents-help"
          disabled
        />
        <p id="agents-help">The agents this applies to.</p>
      </>,
    );

    const field = screen.getByRole("combobox", { name: "Select agents" });
    expect(field).toBeDisabled();
    expect(field).toHaveAttribute("aria-describedby", "agents-help");
    await user.click(field);
    expect(screen.queryByRole("option", { name: "Agent Beta" })).toBeNull();
    expect(screen.queryByRole("button", { name: /clear/i })).toBeNull();
    const remove = screen.queryByRole("button", { name: "Remove Agent Alpha" });
    if (remove) await user.click(remove);
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("held to one line: the chips past the cap fold into a +N chip that names them", () => {
    render(
      <MultiSelect
        options={OPTIONS}
        selected={["alpha", "beta"]}
        onToggle={() => {}}
        placeholder="Select agents"
        searchPlaceholder="Search agents..."
        emptyOptionsLabel="No agents available"
        noMatchLabel="No matching agents."
        maxVisibleChips={1}
      />,
    );

    // A field in a fixed-width cell (the primary assets Assets column) must not
    // wrap its chips past the cell: the rest goes in one translated +N chip.
    // jsdom measures 0 px, so every chip folds into it here ("+2").
    expect(screen.getByLabelText(/^\+\d more: .*Agent Beta$/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove Agent Beta" })).toBeNull();
  });
});
