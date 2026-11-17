// The Layout check panel at the 12 px type floor and with a footer that never
// runs past its edge (issue #10270, UX round 1 on #10271).
//
// jsdom does no layout, and frontend/AGENTS.md forbids asserting class names
// on the rendered DOM, so the panel is checked two ways: rendered in a 341 px
// box (the panel's width at 1024 x 768) with the fr catalog, for what the user
// reads, and its SOURCE scanned for the size and wrap utilities that decide
// what the browser lays out (the design-system guard form of
// no-class-name-assertions.test.ts).

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Presentation, PresentationLayoutCheck } from "@/types";
import { PresentationLayoutPanel } from "./PresentationLayoutPanel";
import type { LayoutCheckRun } from "./useLayoutCheckRun";

const SOURCE = readFileSync(resolve(__dirname, "PresentationLayoutPanel.tsx"), "utf8");

const DECK = {
  id: "p-1",
  kind: "deck",
  revision: 3,
  spec: { slides: [{ id: "s-1" }, { id: "s-2" }] },
} as unknown as Presentation;

const REPORT: PresentationLayoutCheck = {
  slide_count: 2,
  skipped: null,
  unit: "slide",
  revision: 3,
  findings: [
    {
      slide_index: 0,
      slide_number: 1,
      slide_id: "s-1",
      layout: "content",
      title: "Quarterly exposure review across every business unit",
      kind: "fit",
      severity: "overloaded",
      message: "The body was shrunk to fit.",
      scale: 0.72,
      amount: 39,
    },
    {
      slide_index: 1,
      slide_number: 2,
      slide_id: "s-2",
      layout: "content",
      title: "Next steps",
      kind: "sparse",
      severity: "advisory",
      message: "The bottom of the slide is empty.",
      scale: null,
      amount: 40,
    },
  ],
};

function check(): LayoutCheckRun {
  return {
    report: REPORT,
    isPending: false,
    isError: false,
    error: null,
    checkedRevision: 3,
    stale: false,
    run: vi.fn(),
  };
}

describe("PresentationLayoutPanel", () => {
  it("lets the footer actions wrap instead of running past a narrow panel", () => {
    const actions = /<div\s+className="([^"]*)"\s+data-testid="presentation\.layoutPanel\.actions"/.exec(SOURCE);
    expect(actions).not.toBeNull();
    const tokens = actions![1].split(/\s+/);
    expect(tokens).toEqual(expect.arrayContaining(["flex-wrap", "max-w-full", "min-w-0"]));
    expect(tokens).not.toContain("shrink-0");
  });

  it("shows the whole revision label and both actions in a 341 px panel in fr", () => {
    renderWithProviders(
      <div style={{ width: 341 }}>
        <PresentationLayoutPanel
          presentation={DECK}
          check={check()}
          activeIndex={0}
          onFixWithAssistant={() => {}}
          onClose={() => {}}
        />
      </div>,
      { locale: "fr" },
    );
    expect(screen.getByTestId("presentation.layoutPanel.revision")).toHaveTextContent("Révision 3 vérifiée");
    expect(screen.getByRole("button", { name: /Corriger avec mon assistant/ })).toBeInTheDocument();
    expect(screen.getByTestId("presentation.layoutPanel.actions")).toContainElement(
      screen.getByTestId("presentation.layoutPanel.fix"),
    );
    expect(screen.getByText("72%")).toBeInTheDocument();
  });
});
