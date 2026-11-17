import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CountryFlag } from "./country-flag";
import { SettingsNotSet, SettingsReadField, SettingsReadGrid } from "./settings-read-view";

describe("SettingsReadField (#10855)", () => {
  it("draws the label and the value as text, never as a form control", () => {
    renderWithProviders(
      <SettingsReadGrid>
        <SettingsReadField label="Sector" value="Finance" data-testid="tile" />
      </SettingsReadGrid>,
    );
    const tile = screen.getByTestId("tile");
    expect(tile.textContent).toContain("Sector");
    expect(tile.textContent).toContain("Finance");
    expect(tile.querySelector("input, select, textarea, button[role=combobox]")).toBeNull();
  });

  it("reads an empty value as Not set", () => {
    renderWithProviders(<SettingsReadField label="Sector" value="  " data-testid="tile" />);
    expect(screen.getByTestId("tile").textContent).toContain("Not set");
  });

  it("keeps the info bubble at rest", () => {
    renderWithProviders(<SettingsReadField label="Sector" value="Finance" info="What the sector is for" />);
    expect(screen.getByRole("img", { name: "About this field" })).toBeTruthy();
  });

  it("reads null, undefined and an empty list as Not set, and keeps a zero", () => {
    renderWithProviders(
      <>
        <SettingsReadField label="A" value={null} data-testid="a" />
        <SettingsReadField label="B" value={[]} data-testid="b" />
        <SettingsReadField label="C" value={0} data-testid="c" />
      </>,
    );
    expect(screen.getByTestId("a").textContent).toContain("Not set");
    expect(screen.getByTestId("b").textContent).toContain("Not set");
    expect(screen.getByTestId("c").textContent).toBe("C0");
  });

  it("draws a fallback such as Product default as the muted empty value, never as a set one", () => {
    renderWithProviders(
      <>
        <SettingsReadField label="Logo" value={<SettingsNotSet label="Product default" />} data-testid="logo" />
        <SettingsReadField label="Name" value="Acme" data-testid="name" />
        <SettingsReadField label="Sector" value="" data-testid="sector" />
      </>,
    );
    const empty = screen.getByTestId("logo").querySelector("[data-slot=settings-read-empty]");
    expect(empty?.textContent).toBe("Product default");
    expect(screen.getByTestId("sector").querySelector("[data-slot=settings-read-empty]")?.textContent).toBe("Not set");
    expect(screen.getByTestId("name").querySelector("[data-slot=settings-read-empty]")).toBeNull();
  });
});

describe("CountryFlag (#10855)", () => {
  it("draws a bundled SVG named after the country, never an emoji", () => {
    render(<CountryFlag code="fr" name="France" />);
    const flag = screen.getByRole("img", { name: "France" });
    expect(flag.tagName.toLowerCase()).toBe("svg");
    expect(document.body.textContent ?? "").not.toMatch(/\p{Regional_Indicator}/u);
  });

  it("draws nothing for a code with no flag", () => {
    const { container } = render(<CountryFlag code="ZZ" name="Nowhere" />);
    expect(container.firstChild).toBeNull();
    render(<CountryFlag code="gb" name="United Kingdom" />);
    expect(screen.getByRole("img", { name: "United Kingdom" })).toBeTruthy();
  });
});
