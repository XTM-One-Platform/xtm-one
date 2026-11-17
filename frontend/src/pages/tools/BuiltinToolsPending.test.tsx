// The skeleton of a built-in tool list takes the form of the list it stands
// in for: cards for a picker grid, list rows for the Profile override, chips
// of the right shape for a badge row, so nothing changes form when the
// registry lands (A1 and A2).

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { BuiltinToolsPending } from "./BuiltinToolsPending";

describe("BuiltinToolsPending", () => {
  it("renders the picker cards by default", () => {
    renderWithProviders(<BuiltinToolsPending isError={false} testId="t" />);
    const loading = screen.getByTestId("t-loading");
    expect(loading.getAttribute("aria-busy")).toBe("true");
    expect(loading.getAttribute("data-variant")).toBe("cards");
  });

  it("renders one list row per expected tool in the rows variant", () => {
    renderWithProviders(<BuiltinToolsPending isError={false} variant="rows" count={3} testId="t" />);
    expect(screen.getByTestId("t-loading").getAttribute("data-variant")).toBe("rows");
    expect(screen.getAllByTestId("t-loading-row")).toHaveLength(3);
  });

  it("keeps the composer chip shape unless told otherwise", () => {
    renderWithProviders(<BuiltinToolsPending isError={false} variant="chips" testId="t" />);
    const loading = screen.getByTestId("t-loading");
    expect(loading.getAttribute("data-variant")).toBe("chips");
    expect(loading.getAttribute("data-chip-shape")).toBe("square");
  });

  it("takes the pill shape of the agent page badges when asked", () => {
    renderWithProviders(<BuiltinToolsPending isError={false} variant="chips" chipShape="pill" testId="t" />);
    expect(screen.getByTestId("t-loading").getAttribute("data-chip-shape")).toBe("pill");
  });

  it("varies the pill widths in a fixed cycle so the row wraps like real badges", () => {
    renderWithProviders(<BuiltinToolsPending isError={false} variant="chips" chipShape="pill" testId="t" />);
    const widths = screen.getAllByTestId("t-loading-chip").map((chip) => chip.getAttribute("data-width"));
    expect(widths).toEqual(["96", "144", "120", "96", "144", "120"]);
  });

  it("keeps one width for the square composer chips", () => {
    renderWithProviders(<BuiltinToolsPending isError={false} variant="chips" count={3} testId="t" />);
    const widths = screen.getAllByTestId("t-loading-chip").map((chip) => chip.getAttribute("data-width"));
    expect(widths).toEqual(["112", "112", "112"]);
  });

  it("shows the inline error line instead of any skeleton when the read failed", () => {
    renderWithProviders(<BuiltinToolsPending isError variant="rows" testId="t" />);
    expect(screen.getByRole("alert").textContent).toContain("Built-in tools could not be loaded");
    expect(screen.queryByTestId("t-loading")).toBeNull();
  });
});
