// The title line of the command center widgets (#11660, slice 5): one
// heading, the description behind a named "i" (never a paragraph), the
// counter and the actions on the same line.

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CtemWidgetHeader } from "./CtemWidgetHeader";

describe("CtemWidgetHeader", () => {
  it("draws one heading, the hint behind a named icon, the counter and the actions, no paragraph", () => {
    renderWithProviders(
      <CtemWidgetHeader
        title="Recommended actions"
        hint="Mobilize - fix what matters first"
        hintLabel="About recommended actions"
        count="4"
        actions={<button type="button">All</button>}
      />,
    );
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
    expect(screen.getByRole("img", { name: "About recommended actions" })).toBeTruthy();
    expect(screen.getByTestId("ctem-widget-header-count").textContent).toBe("4");
    expect(screen.getByRole("button", { name: "All" })).toBeTruthy();
    expect(screen.getByTestId("ctem-widget-header").querySelector("p")).toBeNull();
  });

  it("uses the level asked and leaves out the counter and the hint when absent", () => {
    renderWithProviders(<CtemWidgetHeader title="Governance & Risk" level={3} />);
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(1);
    expect(screen.queryByTestId("ctem-widget-header-count")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });
});
