import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AgentFocusPicker } from "@/components/ui/agent-focus-picker";
import { renderWithProviders } from "@/testing/providers";

/**
 * A Focus list row keeps one line however long the agent's name, and the
 * full name stays reachable on hover.
 */
const longName = "[Customer Success] Weekly renewal risk digest for the enterprise accounts";
const agents = [
  { id: "a", name: longName },
  { id: "b", name: "Writer" },
];

describe("AgentFocusPicker rows", () => {
  it("names every row in full on hover and still picks it", async () => {
    const onToggle = vi.fn();
    renderWithProviders(
      <AgentFocusPicker
        agents={agents}
        focusedIds={new Set<string>()}
        onToggle={onToggle}
        onClear={vi.fn()}
        nameOf={(id) => agents.find((a) => a.id === id)?.name}
      />,
    );
    await userEvent.click(screen.getByPlaceholderText("All agents"));
    for (const a of agents) {
      const label = screen.getByTitle(a.name);
      expect(label.textContent).toBe(a.name);
    }
    await userEvent.click(screen.getByTitle(longName));
    expect(onToggle).toHaveBeenCalledWith("a");
  });
});

describe("AgentFocusPicker chips", () => {
  it("names a picked chip in full on hover and still removes it", async () => {
    const onToggle = vi.fn();
    renderWithProviders(
      <AgentFocusPicker
        agents={agents}
        focusedIds={new Set<string>(["a", "gone"])}
        onToggle={onToggle}
        onClear={vi.fn()}
        nameOf={(id) => agents.find((a) => a.id === id)?.name}
      />,
    );
    const chip = screen.getByTitle(longName);
    expect(chip.textContent).toBe(longName);
    // An agent the filters no longer offer falls back to its id.
    expect(screen.getByTitle("gone").textContent).toBe("gone");
    await userEvent.click(chip);
    expect(onToggle).toHaveBeenCalledWith("a");
  });
});
