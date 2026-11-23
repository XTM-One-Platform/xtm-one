// The shared source badge and module pivot (#1357): a module source shows its
// emblem and name; a brand-less source its name alone; the pivot opens the
// entity in a new tab, or is not drawn at all.
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { ModulePivotLink } from "./module-pivot-link";
import { SourceBadge } from "./source-badge";

describe("SourceBadge", () => {
  it("draws the module's emblem next to its name", () => {
    renderWithProviders(<SourceBadge source="opencti" />);
    const badge = screen.getByTestId("source-badge");
    expect(badge.textContent).toBe("OpenCTI");
    expect(badge.querySelector("img")).not.toBeNull();
  });

  it("reads a pre-rename identifier as its module", () => {
    renderWithProviders(<SourceBadge source="openbas" />);
    expect(screen.getByTestId("source-badge").textContent).toBe("OpenAEV");
  });

  it("keeps a brand-less source's name with no glyph", () => {
    renderWithProviders(<SourceBadge source="signal" label="Signal" />);
    const badge = screen.getByTestId("source-badge");
    expect(badge.textContent).toBe("Signal");
    expect(badge.querySelector("img, svg")).toBeNull();
  });

  it("draws its name through TruncatedText, with no native title (UX r1 B1 on #1358)", () => {
    renderWithProviders(<SourceBadge source="a-very-long-brand-less-source-name" />);
    const name = screen.getByText("a-very-long-brand-less-source-name");
    // TruncatedText names a cut text with the design-system tooltip, never a title attribute.
    expect(name.closest("[title]")).toBeNull();
    expect(name.hasAttribute("data-state")).toBe(true);
  });
});

describe("ModulePivotLink", () => {
  it("opens the entity in the module, in a new tab, named by its action", () => {
    renderWithProviders(<ModulePivotLink module="opencti" baseUrl="https://cti.example" id="v-1" />);
    const link = screen.getByRole("link", { name: "Open in OpenCTI" });
    expect(link.getAttribute("href")).toBe("https://cti.example/dashboard/id/v-1");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("draws nothing without a base URL or an id", () => {
    renderWithProviders(
      <>
        <ModulePivotLink module="opencti" baseUrl={null} id="v-1" />
        <ModulePivotLink module="openaev" baseUrl="https://aev.example" id={null} kind="finding" />
      </>,
    );
    expect(screen.queryByTestId("module-pivot-link")).toBeNull();
  });
});
