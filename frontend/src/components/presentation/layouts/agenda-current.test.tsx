// The plain agenda draws every item alike: a stored `current` marks
// no item under a template with `design.agenda: "plain"` (the Filigran Brand
// Deck), and still marks it under any other template.

import { describe, expect, it } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import type { AgendaSlide, TemplateDesign } from "@/types";

import { DeckDesignContext } from "../design-context";
import { slideFieldOffered } from "../design-tokens";
import { AgendaLayout } from "./body";

function agenda(): AgendaSlide {
  return {
    layout: "agenda",
    title: "Agenda",
    current: 2,
    items: [
      { title: "Context", description: "Where the example starts" },
      { title: "Approach", description: "How the example proceeds" },
      { title: "Results", description: "What the example shows" },
    ],
  } as AgendaSlide;
}

function renderAgenda(design: TemplateDesign | null) {
  return renderWithProviders(
    <div className="deck-root">
      <DeckDesignContext.Provider value={design}>
        <AgendaLayout slide={agenda()} />
      </DeckDesignContext.Provider>
    </div>,
  );
}

describe("agenda current under the plain agenda", () => {
  it("marks no item current or dimmed under the plain agenda", () => {
    const { container, getAllByRole } = renderAgenda({ agenda: "plain" } as TemplateDesign);
    expect(getAllByRole("listitem")).toHaveLength(3);
    expect(container.querySelectorAll('[data-current="true"]')).toHaveLength(0);
    expect(container.querySelectorAll('[data-dimmed="true"]')).toHaveLength(0);
  });

  it("still highlights the current item under a template without the plain agenda", () => {
    const { container, getByText } = renderAgenda(null);
    const current = container.querySelectorAll('[data-current="true"]');
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain("Approach");
    expect(container.querySelectorAll('[data-dimmed="true"]')).toHaveLength(2);
    expect(getByText("Context")).toBeTruthy();
  });

  it("offers the current field only outside the plain agenda", () => {
    expect(slideFieldOffered("agenda", "current", { agenda: "plain" } as TemplateDesign)).toBe(false);
    expect(slideFieldOffered("agenda", "current", null)).toBe(true);
    expect(slideFieldOffered("agenda", "columns", { agenda: "plain" } as TemplateDesign)).toBe(true);
    expect(slideFieldOffered("content", "current", { agenda: "plain" } as TemplateDesign)).toBe(true);
  });
});
