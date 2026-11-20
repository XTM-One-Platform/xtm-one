import { beforeAll, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  SearchableSelect,
  SEARCH_THRESHOLD,
  SHRINKING_WRAPPER_CLASS,
  FILLING_WRAPPER_CLASS,
  TWO_LINE_LIST_CLASS,
  type SelectOption,
} from "@/components/ui/searchable-select";
import { renderWithProviders } from "@/testing/providers";

/**
 * The picker every long list goes through: what a person sees when
 * the list opens, what typing narrows it to, and what a pick reports.
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

const PROVIDERS: SelectOption[] = [
  { value: "openai", label: "OpenAI" },
  { value: "tei", label: "Embedding Server (/embed)" },
  { value: "anthropic", label: "Anthropic" },
  { value: "elevenlabs", label: "ElevenLabs" },
];

function Harness({
  options = PROVIDERS,
  pinned,
  sort,
  searchable,
  initial = "",
  onChange,
}: {
  options?: SelectOption[];
  pinned?: SelectOption[];
  sort?: boolean;
  searchable?: boolean | "auto";
  initial?: string;
  onChange?: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SearchableSelect
      aria-label="Provider"
      options={options}
      pinned={pinned}
      sort={sort}
      searchable={searchable}
      value={value}
      onValueChange={(next) => {
        onChange?.(next);
        setValue(next);
      }}
    />
  );
}

const optionNames = () => screen.getAllByRole("option").map((option) => option.textContent);

describe("SearchableSelect", () => {
  it("lists the options alphabetically, pinned entries first in their own order", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <Harness pinned={[{ value: "__none__", label: "None" }, { value: "__auto__", label: "Auto (provider default)" }]} />,
    );

    await user.click(screen.getByRole("combobox", { name: "Provider" }));

    expect(optionNames()).toEqual(["None", "Auto (provider default)", "Anthropic", "ElevenLabs", "Embedding Server (/embed)", "OpenAI"]);
  });

  it("keeps the given order when the order means something", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness sort={false} />);

    await user.click(screen.getByRole("combobox", { name: "Provider" }));

    expect(optionNames()).toEqual(["OpenAI", "Embedding Server (/embed)", "Anthropic", "ElevenLabs"]);
  });

  it("shows an option's meaning on its own line at 12 px that wraps, never cut (B3 of #9443)", async () => {
    const user = userEvent.setup();
    const options: SelectOption[] = [
      { value: "provider", label: "Provider", description: "Supplies a product or service your organization depends on, a long meaning" },
    ];
    renderWithProviders(<Harness options={options} />);
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    // Its own element under the label, never sharing the label's line (the 12 px wrap is seen rendered at the UX gate).
    const meaning = await screen.findByText(/Supplies a product or service/);
    expect(meaning.getAttribute("data-slot")).toBe("option-description");
    expect(meaning.parentElement?.firstElementChild?.textContent).toBe("Provider");
    expect(meaning.getAttribute("title")).toBeNull();
  });

  it("gives a plain-select row with a meaning the two-line floor and leaves a one-line row at its floor (#11593)", async () => {
    const user = userEvent.setup();
    const options: SelectOption[] = [
      { value: "provider", label: "Provider", description: "Supplies a product or service your organization depends on" },
      { value: "other", label: "Other" },
    ];
    // The plain Select path (the Tracked organizations pickers); the search path draws the
    // DS two-line row through isOptionTwoLine.
    renderWithProviders(<Harness options={options} searchable={false} />);
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    const plainRow = screen.getByRole("option", { name: /^Other/ });
    const meaningRow = screen.getByRole("option", { name: /^Provider/ });
    expect(meaningRow.getAttribute("data-lines")).toBe("two");
    expect(plainRow.getAttribute("data-lines")).toBe("one");
    // Label and meaning stay two separate lines inside the row.
    expect(within(meaningRow).getByText(/Supplies a product/).getAttribute("data-slot")).toBe("option-description");
  });

  it("lets a list of rows with a meaning grow past the one-line cap and shows its scrollbar (B1 of #11596)", async () => {
    const user = userEvent.setup();
    const described: SelectOption[] = [
      { value: "provider", label: "Provider", description: "Supplies a product or service your organization depends on" },
      { value: "customer", label: "Customer", description: "Buys from your organization or is served by it" },
    ];
    const { unmount } = renderWithProviders(<Harness options={described} searchable={false} />);
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    // The class sits on the DS content, the viewport's parent, and reaches the viewport.
    const content = () => document.querySelector("[data-radix-select-viewport]")?.parentElement;
    const classes = TWO_LINE_LIST_CLASS.split(" ");
    expect(classes.some((name) => name.includes("max-h-[min(18rem"))).toBe(true);
    expect(classes.some((name) => name.includes("[scrollbar-width:thin]"))).toBe(true);
    for (const name of classes) expect(content()?.classList.contains(name)).toBe(true);
    unmount();

    // A list without meanings keeps the DS cap untouched.
    renderWithProviders(<Harness options={[{ value: "a", label: "Alpha" }, { value: "b", label: "Beta" }]} searchable={false} />);
    await user.click(screen.getByRole("combobox", { name: "Provider" }));
    for (const name of classes) expect(content()?.classList.contains(name)).toBe(false);
  });

  it("narrows the list word by word, on the label and on the secondary text", async () => {
    const user = userEvent.setup();
    const people: SelectOption[] = [
      { value: "u1", label: "Ada Lovelace", keywords: ["ada@example.com"] },
      { value: "u2", label: "Grace Hopper", description: "Admin", keywords: ["grace@example.com"] },
      { value: "u3", label: "Équipe rouge" },
    ];
    renderWithProviders(<Harness options={people} />);
    const field = screen.getByRole("combobox", { name: "Provider" });

    await user.type(field, "hopper grace");
    expect(optionNames()).toEqual(["Grace HopperAdmin"]);

    await user.clear(field);
    await user.type(field, "ada@");
    expect(optionNames()).toEqual(["Ada Lovelace"]);

    await user.clear(field);
    await user.type(field, "admin");
    expect(optionNames()).toEqual(["Grace HopperAdmin"]);

    await user.clear(field);
    await user.type(field, "equipe");
    expect(optionNames()).toEqual(["Équipe rouge"]);

    await user.clear(field);
    await user.type(field, "nobody");
    expect(screen.queryAllByRole("option")).toEqual([]);
    expect(screen.getByText("No results")).toBeInTheDocument();
  });

  it("reports the picked value and shows its field label", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const models: SelectOption[] = [
      { value: "p1::gpt-4o", label: "gpt-4o", selectedLabel: "gpt-4o (OpenAI)", group: "OpenAI" },
      { value: "p2::claude", label: "claude", selectedLabel: "claude (Anthropic)", group: "Anthropic" },
    ];
    renderWithProviders(<Harness options={models} onChange={onChange} />);
    const field = screen.getByRole("combobox", { name: "Provider" });

    await user.click(field);
    await user.click(screen.getByRole("option", { name: "gpt-4o" }));

    expect(onChange).toHaveBeenCalledWith("p1::gpt-4o");
    expect(field).toHaveValue("gpt-4o (OpenAI)");
  });

  it("sorts the groups and the rows inside them, and heads the pinned entries", async () => {
    const user = userEvent.setup();
    const models: SelectOption[] = [
      { value: "p1::gpt-4o", label: "gpt-4o", group: "OpenAI" },
      { value: "p2::sonnet", label: "claude-sonnet", group: "Anthropic" },
      { value: "p1::gpt-4.1", label: "gpt-4.1", group: "OpenAI" },
      { value: "p2::haiku", label: "claude-haiku", group: "Anthropic" },
    ];
    renderWithProviders(<Harness options={models} pinned={[{ value: "__default__", label: "Platform default" }]} />);

    await user.click(screen.getByRole("combobox", { name: "Provider" }));

    const groups = screen.getAllByRole("group");
    expect(groups.map((group) => group.getAttribute("aria-label"))).toEqual(["Other", "Anthropic", "OpenAI"]);
    expect(within(groups[1]).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "claude-haiku",
      "claude-sonnet",
    ]);
  });

  it("does not empty a picker that is not clearable", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(<Harness initial="openai" onChange={onChange} />);
    const field = screen.getByRole("combobox", { name: "Provider" });

    await user.click(field);
    await user.keyboard("{Backspace}{Backspace}");
    await user.tab();

    expect(onChange).not.toHaveBeenCalled();
    expect(field).toHaveValue("OpenAI");
  });

  it("selects the picked label when the list opens again, so typing searches", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);
    const field = screen.getByRole("combobox", { name: "Provider" }) as HTMLInputElement;

    await user.click(field);
    await user.click(screen.getByRole("option", { name: "ElevenLabs" }));
    expect(field).toHaveFocus();
    await user.click(field);
    await new Promise((resolve) => requestAnimationFrame(resolve));

    expect([field.selectionStart, field.selectionEnd]).toEqual([0, "ElevenLabs".length]);
  });

  it("follows a disabled fieldset: the field does not open its list", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <fieldset disabled>
        <Harness initial="openai" />
      </fieldset>,
    );
    const field = screen.getByRole("combobox", { name: "Provider" });

    await user.click(field.closest("[data-combobox-field]") as HTMLElement);

    expect(field).toBeDisabled();
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  it("stays a plain select for a short list of unknown length, and searches a long one", () => {
    const long = Array.from({ length: SEARCH_THRESHOLD + 1 }, (_, i) => ({ value: `v${i}`, label: `Value ${i}` }));
    const { unmount } = renderWithProviders(<Harness searchable="auto" initial="openai" />);
    const short = screen.getByRole("combobox", { name: "Provider" });
    expect(short.tagName).toBe("BUTTON");
    expect(short).toHaveTextContent("OpenAI");
    unmount();

    renderWithProviders(<Harness searchable="auto" options={long} />);
    expect(screen.getByRole("combobox", { name: "Provider" }).tagName).toBe("INPUT");
  });

  it("offers the whole of a long picked label on hover, in the search field and in the plain select", () => {
    const name = "Chief of Staff and Executive Assistant to the Executive Management Team";
    const options = [...PROVIDERS, { value: "long", label: name }];
    const { unmount } = renderWithProviders(<Harness options={options} initial="long" />);
    const field = screen.getByRole("combobox", { name: "Provider" });
    expect(field).toHaveValue(name);
    expect(field).toHaveAttribute("title", name);
    unmount();

    renderWithProviders(<Harness options={options} searchable="auto" initial="long" />);
    const trigger = screen.getByRole("combobox", { name: "Provider" });
    expect(trigger.tagName).toBe("BUTTON");
    expect(within(trigger).getByTitle(name)).toHaveTextContent(name);
  });

  it("lets the plain select shrink under a long one-word label instead of overflowing its cell (#611)", () => {
    const name = "production-dmz-frankfurt";
    renderWithProviders(<Harness options={[...PROVIDERS, { value: "long", label: name }]} searchable="auto" initial="long" />);
    const trigger = screen.getByRole("combobox", { name: "Provider" });
    expect(trigger.tagName).toBe("BUTTON");
    // happy-dom has no layout and the class-name gate forbids pinning `min-w-0`: the lab render
    // proves the width. Asserted here: the plain select keeps the whole label on hover.
    expect(within(trigger).getByTitle(name)).toHaveTextContent(name);
  });

  it("reaches the DS wrapper, the flex item of a cell, so it shrinks and fills (UX r1 B1 and A1, #611)", () => {
    // The DS SelectTrigger wraps the trigger in a span with no prop: a display-contents box carries
    // the rules that reach it. Asserted on the exported rules and the rendered nesting (no layout in happy-dom).
    expect(SHRINKING_WRAPPER_CLASS.split(" ")).toEqual(["contents", "[&>span]:min-w-0", "[&>span]:max-w-full"]);
    expect(FILLING_WRAPPER_CLASS).toBe("[&>span]:flex-1");
    const { container } = renderWithProviders(<Harness options={PROVIDERS} searchable="auto" initial={PROVIDERS[0].value} />);
    const trigger = screen.getByRole("combobox", { name: "Provider" });
    const dsWrapper = trigger.parentElement;
    expect(dsWrapper?.tagName).toBe("SPAN");
    expect(dsWrapper?.parentElement?.matches("span.contents")).toBe(true);
    expect(container.contains(dsWrapper)).toBe(true);
  });

  it("puts no title on an empty search field", () => {
    renderWithProviders(<Harness />);
    expect(screen.getByRole("combobox", { name: "Provider" })).not.toHaveAttribute("title");
  });
});
