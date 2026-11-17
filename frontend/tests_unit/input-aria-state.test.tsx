import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Input } from "@/components/ui/input";

/**
 * The `Input` adapter must keep the ARIA state its callers set.
 *
 * The design system's `Input` writes `aria-invalid` and `aria-required` after
 * spreading the caller's props, from its own `error` and `required` alone
 * (LIBRARY-FEEDBACK F26), so a field marked invalid read as valid and a field
 * marked required lost it. Main's end-to-end tests found both: the MCP preset
 * form and the provider dialog's embedding model.
 */
describe("Input ARIA state", () => {
  it("keeps the caller's aria-invalid, both ways", () => {
    const { rerender } = render(<Input aria-label="Window" aria-invalid />);
    const field = screen.getByRole("textbox", { name: "Window" });
    expect(field.getAttribute("aria-invalid")).toBe("true");

    rerender(<Input aria-label="Window" aria-invalid={false} />);
    expect(field.getAttribute("aria-invalid")).toBe("false");

    rerender(<Input aria-label="Window" />);
    expect(field.hasAttribute("aria-invalid")).toBe(false);
  });

  it("keeps the caller's aria-required, an explicit false included", () => {
    const { rerender } = render(<Input aria-label="Embedding Model" aria-required={false} />);
    const field = screen.getByRole("textbox", { name: "Embedding Model" });
    expect(field.getAttribute("aria-required")).toBe("false");

    rerender(<Input aria-label="Embedding Model" aria-required />);
    expect(field.getAttribute("aria-required")).toBe("true");
  });

  it("leaves the library's own props in charge when they are set", () => {
    render(<Input aria-label="Name" required error="Required" aria-required={false} aria-invalid={false} />);
    const field = screen.getByRole("textbox", { name: "Name" });
    expect(field.getAttribute("aria-required")).toBe("true");
    expect(field.getAttribute("aria-invalid")).toBe("true");
  });
});
