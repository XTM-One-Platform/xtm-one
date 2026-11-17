// Settings > Platform branding colour fields (#9375, UX round 1 B1 on #11072):
// the design-system ColorPicker carries the label, the help line and the
// error itself, so an invalid hex marks the field aria-invalid and names the
// message as its description, and the label focuses the field.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { ColorField } from "./ColorField";

const MESSAGE = "Must be a hex color (#rgb, #rrggbb, or #rrggbbaa).";

function mount(value: string) {
  return renderWithProviders(
    <ColorField label="Accent Color" help="Buttons, links, active states, and highlights." info="Used across the app." value={value} onChange={vi.fn()} />,
  );
}

describe("ColorField", () => {
  it("marks an invalid hex aria-invalid with the message as description", () => {
    mount("#zz12");
    const field = screen.getByLabelText("Accent Color");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription(expect.stringContaining(MESSAGE));
  });

  it("keeps a valid hex unflagged with the help line as description", () => {
    mount("#00aaff");
    const field = screen.getByLabelText("Accent Color");
    expect(field).not.toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByText(MESSAGE)).toBeNull();
    expect(field).toHaveAccessibleDescription(expect.stringContaining("Buttons, links"));
  });
});
