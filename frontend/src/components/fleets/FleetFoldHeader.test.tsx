// The shared fleet section header (UX round 1 of #1393, B1): "Show" / "Hide" with the
// section in the accessible name, aria-expanded and aria-controls, a disabled fold kept
// open, and an action in the toggle's place on a section with no fold.
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders as render } from "@/testing/providers";

import { FleetFoldHeader } from "./FleetFoldHeader";

describe("FleetFoldHeader", () => {
  it("names the section in the toggle and reports its state", () => {
    const onToggle = vi.fn();
    const { rerender } = render(<FleetFoldHeader icon="users" title="Members" fold={{ open: false, onToggle, controls: "body" }} />);
    const show = screen.getByRole("button", { name: "Show Members" });
    expect(show).toHaveTextContent("Show");
    expect(show).toHaveAttribute("aria-expanded", "false");
    expect(show).toHaveAttribute("aria-controls", "body");
    fireEvent.click(show);
    expect(onToggle).toHaveBeenCalledTimes(1);
    rerender(<FleetFoldHeader icon="users" title="Members" fold={{ open: true, onToggle, controls: "body" }} />);
    expect(screen.getByRole("button", { name: "Hide Members" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("heading", { level: 3, name: "Members" })).toBeInTheDocument();
  });

  it("refuses a fold while it is pinned open", () => {
    render(<FleetFoldHeader icon="users" title="Members" fold={{ open: true, onToggle: vi.fn(), controls: "body", disabled: true }} />);
    expect(screen.getByRole("button", { name: "Hide Members" })).toBeDisabled();
  });

  it("draws the action in the toggle's place when the section has no fold", () => {
    render(<FleetFoldHeader icon="sliders-horizontal" title="Fleet defaults" action={<button type="button">Apply a preset</button>} />);
    expect(screen.getByRole("button", { name: "Apply a preset" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show|Hide/ })).toBeNull();
  });
});
