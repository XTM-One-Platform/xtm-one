import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { InfoTip } from "./info-tip";

describe("InfoTip on the design system Icon (#10533)", () => {
  it("is one focusable trigger named for assistive tech, the glyph inside it decorative", () => {
    renderWithProviders(<InfoTip label="About this field" content="Explains the field" />);
    const trigger = screen.getByRole("img", { name: "About this field" });
    expect(trigger.tagName).toBe("SPAN");
    expect(trigger.getAttribute("tabindex")).toBe("0");
    const glyph = trigger.querySelector("svg");
    expect(glyph).toBeTruthy();
    expect(glyph?.getAttribute("aria-hidden")).toBe("true");
    expect(screen.getAllByRole("img", { name: "About this field" })).toHaveLength(1);
  });

  it("renders the glyph at 16 px, the smallest library size", () => {
    renderWithProviders(<InfoTip label="About this field" content="Explains the field" />);
    const glyph = screen.getByRole("img", { name: "About this field" }).querySelector("svg");
    expect(glyph?.getAttribute("width")).toBe("16");
  });
});
