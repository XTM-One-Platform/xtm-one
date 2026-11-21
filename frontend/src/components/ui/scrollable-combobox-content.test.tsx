import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { useState, type ReactNode } from "react";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { MultiSelect } from "@/components/ui/multi-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { renderWithProviders } from "@/testing/providers";

/**
 * Both Combobox pickers portal their list out of a modal dialog around them,
 * whose scroll lock cancels any wheel or touch move reaching the document from
 * outside it: the list never scrolled.
 */

/** Radix positions the panel with floating-ui, which happy-dom does not lay out. */
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

const OPTIONS = Array.from({ length: 30 }, (_, i) => ({ value: `p${i}`, label: `Provider ${i}` }));

function Single() {
  const [value, setValue] = useState("");
  return <SearchableSelect aria-label="Provider" searchable options={OPTIONS} value={value} onValueChange={setValue} />;
}

function Multiple() {
  const [selected, setSelected] = useState<string[]>([]);
  return (
    <MultiSelect
      options={OPTIONS}
      selected={selected}
      onToggle={(v) => setSelected((s) => (s.includes(v) ? s.filter((x) => x !== v) : [...s, v]))}
      placeholder="Provider"
      searchPlaceholder="Search providers"
      emptyOptionsLabel="No providers"
      noMatchLabel="No match"
    />
  );
}

const inDialog = (picker: ReactNode) => (
  <Dialog open>
    <DialogContent>
      <DialogTitle>Add Provider</DialogTitle>
      {picker}
    </DialogContent>
  </Dialog>
);

const pickers = [
  ["SearchableSelect", Single],
  ["MultiSelect", Multiple],
] as const;
const moves = ["wheel", "touchmove"] as const;
const cases = pickers.flatMap(([name, Picker]) => moves.map((move) => [name, move, Picker] as const));

let reachedDocument: string[] = [];
const record = (event: Event) => reachedDocument.push(event.type);
afterEach(() => {
  for (const move of moves) document.removeEventListener(move, record);
  reachedDocument = [];
});

async function moveOverList(move: string) {
  await userEvent.setup().click(screen.getByRole("combobox", { name: "Provider" }));
  for (const m of moves) document.addEventListener(m, record);
  const event = new Event(move, { bubbles: true, cancelable: true });
  screen.getByRole("listbox").dispatchEvent(event);
  return event;
}

describe("ScrollableComboboxContent", () => {
  it.each(cases)("%s: a %s over the list scrolls it inside a modal dialog", async (_, move, Picker) => {
    renderWithProviders(inDialog(<Picker />));

    const event = await moveOverList(move);

    expect(event.defaultPrevented).toBe(false);
    expect(reachedDocument).toEqual([]);
  });

  // Since @filigran/design-system 1.9.0 the Combobox list stops its wheel and
  // touch moves itself, dialog or not; outside a dialog the move is still never
  // cancelled, so the list and the page scroll as usual.
  it.each(cases)("%s: a %s over the list is not cancelled outside a dialog", async (_, move, Picker) => {
    renderWithProviders(<Picker />);

    const event = await moveOverList(move);

    expect(event.defaultPrevented).toBe(false);
  });
});
