// The public catalog's loading states are its loaded pages with their content
// greyed out (#11345): the list page draws a page of catalog cards on the
// loaded grid, the detail page draws DetailShell's back link, actions and
// header card, and no catalog page goes back to the generic PageSkeleton.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { CatalogDetailSkeleton, CatalogListSkeleton } from "./components";

describe("catalog skeletons", () => {
  it("draws the requested number of catalog cards, each with the loaded card's head, description and footer slots", () => {
    const { container } = renderWithProviders(
      <MemoryRouter>
        <CatalogListSkeleton count={6} />
      </MemoryRouter>,
    );
    const cards = screen.getAllByTestId("catalog-card-skeleton");
    expect(cards).toHaveLength(6);
    for (const card of cards) {
      expect(card.querySelector('[data-slot="card-head"]')).not.toBeNull();
      expect(card.querySelector('[data-slot="card-description"]')).not.toBeNull();
      expect(card.querySelector('[data-slot="card-footer"]')).not.toBeNull();
    }
    expect(container.querySelector('[data-slot="header"]')).not.toBeNull();
    expect(screen.getByTestId("catalog-list-skeleton").getAttribute("aria-busy")).toBe("true");
  });

  it("keeps the detail page's real back link and its header, meta and section blocks", () => {
    renderWithProviders(
      <MemoryRouter>
        <CatalogDetailSkeleton backTo="/catalog/agents" backLabel="Back to Agents" />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: /Back to Agents/ })).toBeTruthy();
    const root = screen.getByTestId("catalog-detail-skeleton");
    for (const slot of ["actions", "header", "meta", "section"]) {
      expect(root.querySelector(`[data-slot="${slot}"]`)).not.toBeNull();
    }
  });

  it("leaves no public catalog page on the generic PageSkeleton", () => {
    // vitest runs from frontend/, as detail-page-skeleton.test.tsx.
    const dir = join(process.cwd(), "src", "pages", "public");
    const offenders = readdirSync(dir)
      .filter((name) => name.endsWith(".tsx") && !name.endsWith(".test.tsx"))
      .filter((name) => readFileSync(join(dir, name), "utf8").includes("<PageSkeleton"));
    expect(offenders).toEqual([]);
  });
});
