/**
 * The wizard canvas must follow the app's light/dark mode.
 *
 * React Flow stamps its `colorMode` onto the `.react-flow` container as a
 * `light` / `dark` class, and the design system keys its elevation tokens on
 * that class — a canvas stuck on `light` renders every node white in dark mode.
 * This pins the container class to `useColorMode()`, which reads `data-mode` on
 * `<html>`. The same wiring guards FlowCanvas (its `<ReactFlow colorMode>`).
 */
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { waitFor } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { WizardCanvas, type WizardDraftSummary } from "./WizardCanvas";

const draft: WizardDraftSummary = {
  name: "Triage",
  icon: "Bot",
  persona: "A helpful analyst",
  toolNames: [],
  integrations: [],
  knowledgeBaseNames: [],
  skillNames: [],
  handoverNames: [],
  memoryTierLabels: [],
  hasGuardrails: false,
  modelLabel: null,
  hasBudget: false,
};

beforeAll(() => {
  // React Flow measures its container through ResizeObserver, which happy-dom
  // does not implement. A no-op is enough: the class we assert on is derived
  // from the `colorMode` prop, not from any measurement.
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
});

afterEach(() => {
  document.documentElement.removeAttribute("data-mode");
});

describe("WizardCanvas color mode", () => {
  it("renders the React Flow container in the app's dark mode by default", async () => {
    document.documentElement.removeAttribute("data-mode");
    const { container } = renderWithProviders(
      <WizardCanvas draft={draft} highlight={[]} />,
    );
    await waitFor(() => {
      expect(container.querySelector(".react-flow")).not.toBeNull();
    });
    const canvas = container.querySelector(".react-flow")!;
    expect(canvas.classList.contains("dark")).toBe(true);
    expect(canvas.classList.contains("light")).toBe(false);
  });

  it("follows a switch to light mode on <html>", async () => {
    const { container } = renderWithProviders(
      <WizardCanvas draft={draft} highlight={[]} />,
    );
    await waitFor(() => {
      expect(container.querySelector(".react-flow")).not.toBeNull();
    });

    document.documentElement.setAttribute("data-mode", "light");

    await waitFor(() => {
      const canvas = container.querySelector(".react-flow")!;
      expect(canvas.classList.contains("light")).toBe(true);
      expect(canvas.classList.contains("dark")).toBe(false);
    });
  });
});
