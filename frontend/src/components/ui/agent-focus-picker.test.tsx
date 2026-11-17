import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AgentFocusPicker } from "@/components/ui/agent-focus-picker";
import { renderWithProviders } from "@/testing/providers";

/**
 * The Focus picker of the Agentic Flow map: an input announced as a
 * combobox that controls a listbox of options. The focus ring token is
 * checked rendered by the UX gate, not by class names here (frontend/AGENTS.md).
 */
const agents = [
  { id: "a", name: "Triage" },
  { id: "b", name: "Writer" },
];

function renderPicker(onToggle: (id: string) => void = vi.fn()) {
  renderWithProviders(
    <AgentFocusPicker
      agents={agents}
      focusedIds={new Set<string>()}
      onToggle={onToggle}
      onClear={vi.fn()}
      nameOf={(id) => agents.find((a) => a.id === id)?.name}
    />,
  );
}

describe("AgentFocusPicker", () => {
  it("is a named combobox, collapsed until it has results", () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.getAttribute("aria-autocomplete")).toBe("list");
    expect(input.hasAttribute("aria-controls")).toBe(false);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("controls a listbox of options once open", async () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    const list = screen.getByRole("listbox");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    expect(input.getAttribute("aria-controls")).toBe(list.id);
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["Triage", "Writer"]);
  });
});

describe("AgentFocusPicker keyboard", () => {
  it("moves the active option with the arrow keys while the input keeps the focus", async () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);

    await userEvent.keyboard("{ArrowDown}");
    const [first, second] = screen.getAllByRole("option");
    expect(input.getAttribute("aria-activedescendant")).toBe(first.id);
    expect(first.getAttribute("aria-selected")).toBe("true");
    expect(second.getAttribute("aria-selected")).toBe("false");
    expect(document.activeElement).toBe(input);

    await userEvent.keyboard("{ArrowDown}{ArrowDown}");
    expect(input.getAttribute("aria-activedescendant")).toBe(second.id);

    await userEvent.keyboard("{ArrowUp}");
    expect(input.getAttribute("aria-activedescendant")).toBe(first.id);
  });

  it("picks the active option with Enter", async () => {
    const onToggle = vi.fn();
    renderPicker(onToggle);
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(onToggle).toHaveBeenCalledWith("b");
    expect(document.activeElement).toBe(input);
  });

  it("closes the list with Escape and drops the active option", async () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    await userEvent.keyboard("{ArrowDown}{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);
  });

  it("keeps the keyboard index when a row slides under a still pointer", async () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    const [first, second] = screen.getAllByRole("option");
    // scrollIntoView moving a row under a resting pointer fires mouseenter
    // with no pointer movement: the active option must not follow it.
    fireEvent.mouseEnter(second);
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);
    await userEvent.keyboard("{ArrowDown}");
    expect(input.getAttribute("aria-activedescendant")).toBe(first.id);
  });

  it("reopens on the first option with the first ArrowDown after Escape", async () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();

    await userEvent.keyboard("{ArrowDown}");
    const [first] = screen.getAllByRole("option");
    expect(input.getAttribute("aria-activedescendant")).toBe(first.id);
    expect(first.getAttribute("aria-selected")).toBe("true");
  });

  it("jumps to the last and first option with End and Home once one is active", async () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    await userEvent.keyboard("{ArrowDown}{End}");
    const [first, second] = screen.getAllByRole("option");
    expect(input.getAttribute("aria-activedescendant")).toBe(second.id);
    await userEvent.keyboard("{Home}");
    expect(input.getAttribute("aria-activedescendant")).toBe(first.id);
    expect(document.activeElement).toBe(input);
  });

  it("leaves Home and End to the caret while no option is active", async () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    await userEvent.keyboard("{End}");
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);
  });

  it("sets the active option on real pointer movement", async () => {
    renderPicker();
    const input = screen.getByRole("combobox", { name: "Focus on agents" });
    await userEvent.click(input);
    const [, second] = screen.getAllByRole("option");
    fireEvent.mouseMove(second);
    expect(input.getAttribute("aria-activedescendant")).toBe(second.id);
    expect(document.activeElement).toBe(input);
  });
});

describe("AgentFocusPicker Clear", () => {
  it("is a keyboard-reachable button that clears the focus", async () => {
    const onClear = vi.fn();
    renderWithProviders(
      <AgentFocusPicker
        agents={agents}
        focusedIds={new Set<string>(["a"])}
        onToggle={vi.fn()}
        onClear={onClear}
        nameOf={(id) => agents.find((a) => a.id === id)?.name}
      />,
    );
    const clear = screen.getByRole("button", { name: "Clear" });
    clear.focus();
    expect(document.activeElement).toBe(clear);
    await userEvent.keyboard("{Enter}");
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});
