import { beforeAll, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { screen } from "@testing-library/react";

import { BOUNDED_CHIP_ROW, MultiSelect } from "@/components/ui/multi-select";
import { renderWithProviders } from "@/testing/providers";

/**
 * A locked option (the always-on "In XTM One only" of the Notification picker, #1373) is the
 * field's value and nothing can remove it, so the field draws no remove or clear control for it
 * (UX r1 B1 of #1376).
 */

beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = vi.fn();
});

const OPTIONS = [
  { value: "builtin:in_app", label: "In XTM One only", locked: true },
  { value: "i1", label: "Microsoft Teams" },
  { value: "i2", label: "Slack" },
];

function Picker({ initial }: { initial: string[] }) {
  const [selected, setSelected] = useState<string[]>(initial);
  return (
    <MultiSelect
      aria-label="Notification"
      options={OPTIONS}
      sort={false}
      selected={selected}
      onToggle={(v) => setSelected((s) => (s.includes(v) ? s.filter((x) => x !== v) : [...s, v]))}
      placeholder="Choose an integration"
      searchPlaceholder="Search integrations..."
      emptyOptionsLabel="No integration connected"
      noMatchLabel="No match"
      maxVisibleChips="fit"
    />
  );
}

describe("MultiSelect with a locked option", () => {
  it("reads the locked value whole as the field's value, with no remove and no clear control", () => {
    renderWithProviders(<Picker initial={["builtin:in_app"]} />);
    expect(screen.getByRole("combobox", { name: "Notification" })).toHaveAttribute("placeholder", "In XTM One only");
    expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();
  });

  it("cuts a long locked value with an ellipsis, its whole label in the shared tooltip (UX r1 B1 of #2428)", () => {
    renderWithProviders(<Picker initial={["builtin:in_app"]} />);
    const input = screen.getByRole("combobox", { name: "Notification" });
    expect(input).toHaveClass("truncate");
    // The shared Tip is the input's trigger: Radix marks it with its closed state.
    expect(input).toHaveAttribute("data-state", "closed");
  });

  it("bounds every chip of a locked row to the field (UX r1 B2 of #2428)", () => {
    renderWithProviders(<Picker initial={["builtin:in_app", "i1"]} />);
    const row = screen.getByRole("list", { name: "Notification" });
    for (const name of BOUNDED_CHIP_ROW.split(" ")) expect(row).toHaveClass(name);
  });

  it("keeps a remove control on the other values, never on the locked one", () => {
    renderWithProviders(<Picker initial={["builtin:in_app", "i1"]} />);
    expect(screen.getByRole("button", { name: "Remove Microsoft Teams" })).toBeInTheDocument();
    // The locked chip is the first item of the row, the one the field hides (display: none).
    const lockedRemove = screen.queryByRole("button", { name: "Remove In XTM One only" });
    if (lockedRemove) expect(lockedRemove.closest("li")).toBe(lockedRemove.closest("ul")?.querySelector("li"));
    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
  });
});
