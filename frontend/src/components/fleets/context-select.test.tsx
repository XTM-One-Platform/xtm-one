// The context Select of a members row: the compact type scale must land on the
// value element (the design-system SelectValue sets its own base scale, so a
// class on the trigger never reaches the text), round 1 UX review of #9338, B1.
// The scale is read from the value's data-scale, set from the same prop as its classes.
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders as render } from "@/testing/providers";
import { ContextSelect } from "./FleetMembersView";

function contextValue(name: string) {
  return screen.getByRole("combobox", { name }).querySelector('[data-part="context-value"]');
}

describe("ContextSelect", () => {
  it("renders the value of a members row at the compact scale", () => {
    render(<ContextSelect value="summary" onChange={vi.fn()} scale="compact" label="Context for Analyst" />);
    expect(contextValue("Context for Analyst")).toHaveAttribute("data-scale", "compact");
    expect(screen.getByRole("combobox", { name: "Context for Analyst" })).toHaveTextContent("A summary");
  });

  it("keeps the base scale on the value by default (dialog, defaults bar)", () => {
    render(<ContextSelect value="full" onChange={vi.fn()} label="Fleet default" />);
    expect(contextValue("Fleet default")).toHaveAttribute("data-scale", "base");
  });
});
