// The icon-only company-managed mark of the entity cards: a keyboard user reaches it with Tab, it shows the
// focus ring and its tooltip opens on focus.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CompanyManagedIcon } from "@/components/ui/company-managed-badge";
import { renderWithProviders } from "@/testing/providers";

describe("CompanyManagedIcon", () => {
  it("takes keyboard focus and opens its tooltip on focus", async () => {
    const user = userEvent.setup();
    renderWithProviders(<CompanyManagedIcon />);

    await user.tab();
    const mark = screen.getByRole("img", { name: "Company-managed" });
    expect(document.activeElement).toBe(mark);
    expect((await screen.findByRole("tooltip")).textContent).toContain("Company-managed");
  });

  it("is in the tab order and keeps the svg out of the accessibility tree", () => {
    renderWithProviders(<CompanyManagedIcon label="Company-managed agent" />);
    const mark = screen.getByRole("img", { name: "Company-managed agent" });
    expect(mark.getAttribute("tabindex")).toBe("0");
    expect(mark.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("draws its trigger through the shared IconMark", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/ui/company-managed-badge.tsx"),
      "utf8",
    );
    expect(source).toContain("<IconMark");
  });
});

describe("CompanyManagedBadge", () => {
  it("never wraps its label onto a second line", () => {
    // A wrapped badge is 42 px tall and clips the 56 px row it sits in (the home dashboard
    // Agents widget, measured by the UX Director at 1728).
    const source = readFileSync(
      resolve(process.cwd(), "src/components/ui/company-managed-badge.tsx"),
      "utf8",
    );
    expect(source).toMatch(/<Badge variant="cyan" className="[^"]*\bwhitespace-nowrap\b[^"]*">/);
  });
});
