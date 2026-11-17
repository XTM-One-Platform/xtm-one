// CardActionButton: one small card action with its accessible name,
// the design-system focus ring and an optional visible word.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CardActionButton } from "./card-action-button";

describe("CardActionButton", () => {
  it("is named by its label and runs its action", () => {
    const onClick = vi.fn();
    renderWithProviders(<CardActionButton icon={<span aria-hidden>x</span>} label="Stop the command" text="Stop" onClick={onClick} />);
    const button = screen.getByRole("button", { name: "Stop the command" });
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Stop")).toBeTruthy();
  });

  it("keeps its name when the visible word is hidden on a narrow card", () => {
    renderWithProviders(
      <CardActionButton icon={<span aria-hidden>x</span>} label="Stop the command" text="Stop" textClassName="hidden @xl:inline" onClick={vi.fn()} />,
    );
    // The word may be hidden by a container query; the button is still named by its label.
    expect(screen.getByRole("button", { name: "Stop the command" })).toBeTruthy();
  });

  it("does not run while disabled", () => {
    const onClick = vi.fn();
    renderWithProviders(<CardActionButton icon={<span aria-hidden>x</span>} label="Larger view" onClick={onClick} disabled />);
    fireEvent.click(screen.getByRole("button", { name: "Larger view" }));
    expect(onClick).not.toHaveBeenCalled();
  });
});
