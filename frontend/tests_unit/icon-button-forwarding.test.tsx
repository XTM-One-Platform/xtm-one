import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { IconButton } from "@filigran/design-system";

/**
 * `IconButton` must forward the ARIA state attributes this app sets on it.
 *
 * Raised in review: the assistant toggle in `Header` passes `aria-pressed`, and
 * `IconButton` has an `active` prop of its own that sets the same attribute. If
 * the component stripped unknown props, or if its own `active` handling won over
 * an explicitly passed value, the toggle state would be lost to assistive
 * technology — silently, because the button still looks and behaves correctly
 * for everyone else.
 *
 * It does forward them: props are spread last, after its own `active` handling,
 * so an explicit value wins. That ordering is the library's to change, which is
 * exactly why it is pinned here rather than trusted.
 */
describe("IconButton prop forwarding", () => {
  it("forwards aria-pressed, the attribute the assistant toggle depends on", () => {
    render(<IconButton aria-label="Ask" aria-pressed icon={<span />} />);
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAttribute("aria-pressed", "true");
  });

  it("lets an explicit aria-pressed win over its own active prop", () => {
    // Both set the same attribute. If the library ever reorders its spread, the
    // app's value would be silently overwritten by the variant styling prop.
    render(<IconButton aria-label="Ask" active={false} aria-pressed icon={<span />} />);
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps the icon out of the accessible name", () => {
    // The name must come from aria-label alone: the icon is decorative, and a
    // library that exposed it would make every one of these names ambiguous.
    render(<IconButton aria-label="Ask" icon={<svg data-testid="glyph" />} />);
    expect(screen.getByRole("button", { name: "Ask" })).toBeInTheDocument();
    expect(screen.getByTestId("glyph").closest("[aria-hidden='true']")).not.toBeNull();
  });
});
