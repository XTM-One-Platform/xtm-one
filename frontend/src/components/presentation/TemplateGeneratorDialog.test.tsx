// "Generate with AI" on the New Template page: the page passes no
// assets, and the candidate preview must stay on screen after a generation
// instead of looping into "Maximum update depth exceeded" (React error #185).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import type { RevealApi } from "reveal.js";

import { renderWithProviders } from "@/testing/providers";
import type { GenerateTemplateResponse, Presentation } from "@/types";

import type { DeckController } from "./deck-controller";
import { TemplateGeneratorDialog } from "./TemplateGeneratorDialog";

const MAX_SYNCS = 20;

const { response, previews, deck } = vi.hoisted(() => {
  const handlers = new Map<string, Set<() => void>>();
  return {
    response: {
      candidate: {
        name: "Quarterly navy",
        description: "A calm corporate deck",
        kind: "presentation",
        theme: null,
        aspect_ratio: "16:9",
        tags: [],
        theme_config: { background: "#0b1020", accent: "#d4af37" },
        layouts: [],
        extra_metadata: {},
      },
      warnings: [],
      palette_from_logo: [],
    } as GenerateTemplateResponse,
    previews: [] as Presentation[],
    deck: {
      api: {
        isReady: () => true,
        availableRoutes: () => ({ left: false, right: true, up: false, down: false }),
        getSlidePastCount: () => 0,
        getTotalSlides: () => 2,
        isOverview: () => false,
        isPaused: () => false,
        on: (type: string, handler: () => void) => {
          if (!handlers.has(type)) handlers.set(type, new Set());
          handlers.get(type)!.add(handler);
        },
        off: (type: string, handler: () => void) => handlers.get(type)?.delete(handler),
      } as unknown as RevealApi,
      emit: (type: string) => handlers.get(type)?.forEach((handler) => handler()),
    },
  };
});

vi.mock("@/hooks/api/useTemplateContainerApi", () => ({
  useGenerateTemplate: () => ({
    isPending: false,
    mutate: (_vars: unknown, options: { onSuccess: (data: GenerateTemplateResponse) => void }) =>
      options.onSuccess(response),
  }),
}));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAiEnabled: () => true,
}));
// Stands in for reveal the way `PresentationDeck` drives it: the deck reports
// to the host's controller once ready, and re-syncs whenever it is handed a
// new presentation (a `sync` event, read back by the controller).
vi.mock("./PreviewDeck", async () => {
  const { useEffect } = await import("react");
  return {
    PreviewDeck: ({ presentation, controller }: { presentation: Presentation; controller: DeckController }) => {
      const { attach } = controller;
      useEffect(() => {
        attach(deck.api);
        return () => attach(null);
      }, [attach]);
      useEffect(() => {
        previews.push(presentation);
        // Bounded so a regression fails on the assertion below instead of
        // spinning the test runner forever.
        if (previews.length <= MAX_SYNCS) deck.emit("sync");
      }, [presentation]);
      return <div data-testid="candidate-preview">{presentation.spec.title}</div>;
    },
  };
});

describe("TemplateGeneratorDialog", () => {
  it("keeps the candidate preview on screen after a generation with no assets", () => {
    renderWithProviders(
      <TemplateGeneratorDialog open onOpenChange={() => {}} kind="presentation" onApply={() => {}} />,
    );

    fireEvent.change(screen.getByPlaceholderText(/quarterly business review/), {
      target: { value: "A light corporate deck, navy and gold" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate" }));

    expect(screen.getByTestId("candidate-preview").textContent).toBe("Quarterly navy");
    expect(screen.getByRole("button", { name: "Apply to form" })).toBeEnabled();

    // A re-render of the dialog (typing a refinement) keeps the same preview:
    // the deck is not re-synced on every keystroke.
    fireEvent.change(screen.getByPlaceholderText(/Warmer accent/), { target: { value: "warmer" } });
    expect(new Set(previews).size).toBe(1);
  });
});
