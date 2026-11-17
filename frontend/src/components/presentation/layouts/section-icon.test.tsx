// The section slide's icon: drawn on every composition, the brand
// `design.section: "outline"` one included and absent
// when the slide stores no icon.

import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { SectionSlide, TemplateDesign } from "@/types";

import { DeckDesignContext } from "../design-context";
import { SectionLayout } from "./opening";

// The icon catalog, loaded: the lazy chunk the deck fetches is ~1,700 ESM
// modules vitest would transform one by one, while the package's own entry
// exports the same map as `icons`.
vi.mock("../deck-icons", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../deck-icons")>();
  const { icons } = await import("lucide-react");
  return { ...actual, getDeckIconCatalog: () => icons, loadDeckIconCatalog: () => Promise.resolve(icons) };
});

/** The stored spec shape of the light example deck's section slide. */
function section(extra: Partial<SectionSlide> = {}): SectionSlide {
  return {
    layout: "section",
    number: 1,
    label: "Part 1",
    icon: "compass",
    title: "Traction and financials",
    subtitle: "Where we stand after three quarters, and the plan for the next four",
    ...extra,
  } as SectionSlide;
}

function renderSection(slide: SectionSlide, design: TemplateDesign | null) {
  return renderWithProviders(
    <div className="deck-root">
      <DeckDesignContext.Provider value={design}>
        <SectionLayout slide={slide} />
      </DeckDesignContext.Provider>
    </div>,
  );
}

/** The drawn icons of the slide's main column (the title group). */
function mainIcons(container: HTMLElement): SVGElement[] {
  const title = container.querySelector("h1");
  const main = title?.parentElement;
  return main ? Array.from(main.querySelectorAll("svg")) : [];
}

describe("section slide icon", () => {
  const brand = { section: "outline" } as TemplateDesign;

  it("draws the icon on the brand outline section, above the title", async () => {
    const { container, findByText } = renderSection(section(), brand);
    await findByText("Traction and financials");
    await vi.waitFor(() => expect(mainIcons(container)).toHaveLength(1));
    const icon = mainIcons(container)[0];
    const title = container.querySelector("h1");
    expect(title).not.toBeNull();
    // The icon's holder precedes the title in the title group.
    const holder = icon.parentElement as Element;
    expect(holder.compareDocumentPosition(title as Node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(icon.getAttribute("aria-hidden")).toBe("true");
  });

  it("draws the icon on a section with no number", async () => {
    const { container, findByText } = renderSection(section({ number: undefined }), brand);
    await findByText("Traction and financials");
    await vi.waitFor(() => expect(mainIcons(container)).toHaveLength(1));
    expect(container.textContent).not.toContain("01");
  });

  it("draws the icon on a template without compositions, as before", async () => {
    const { container, findByText } = renderSection(section(), null);
    await findByText("Traction and financials");
    await vi.waitFor(() => expect(mainIcons(container)).toHaveLength(1));
  });

  it("draws no icon when the slide stores none", async () => {
    const { container, findByText } = renderSection(section({ icon: undefined }), brand);
    await findByText("Traction and financials");
    expect(mainIcons(container)).toHaveLength(0);
  });
});
