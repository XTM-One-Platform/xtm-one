/**
 * Catalog card names: a name too long for its card is cut with an ellipsis and named
 * whole by the design-system tooltip (design standards s.3). Issue #11096.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { ProviderCatalogItem } from "@/types";
import { IntegrationCatalogBrowser } from "./integration-catalog-browser";

const LONG_NAME = "Google Threat Intelligence";

const provider = {
  provider: "google_threat_intelligence",
  name: LONG_NAME,
  description: "Threat intelligence",
  integration_category: "threat_intelligence",
  integration_types: [],
  available_events: [],
  auth_type: "api_key",
  is_custom: false,
  logo: null,
} as unknown as ProviderCatalogItem;

async function nextFrame() {
  await act(async () => {
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
  });
}

describe("IntegrationCatalogBrowser card name", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it("draws no tooltip when the name fits its card", () => {
    renderWithProviders(<IntegrationCatalogBrowser catalog={[provider]} onSelect={() => {}} variant="admin" />);
    expect(screen.getByText(LONG_NAME)).toBeTruthy();
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("names a cut card name whole in the tooltip when its card takes the focus", async () => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(100);
    renderWithProviders(<IntegrationCatalogBrowser catalog={[provider]} onSelect={() => {}} variant="admin" />);
    const name = screen.getByText(LONG_NAME);
    const host = name.closest<HTMLElement>("a[href],button,[tabindex]:not([tabindex='-1'])") ?? name;
    act(() => host.focus());
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips[0].textContent).toContain(LONG_NAME);
  });
});

/**
 * Filter facets: one line per option, as in OpenCTI and OpenAEV. A label or a group name too
 * long for the aside is cut with an ellipsis, and the tooltip names it whole on keyboard focus.
 */
describe("IntegrationCatalogBrowser filter facets", () => {
  beforeEach(() => vi.restoreAllMocks());
  afterEach(() => vi.restoreAllMocks());

  function facetLabel(text: string): HTMLElement {
    const aside = document.querySelector("aside");
    const match = Array.from(aside?.querySelectorAll<HTMLElement>("span") ?? []).find(
      (node) => node.textContent === text && node.children.length === 0,
    );
    if (!match) throw new Error(`no facet label ${text}`);
    return match;
  }

  it("draws no tooltip and no extra tab stop when a facet label fits", () => {
    renderWithProviders(<IntegrationCatalogBrowser catalog={[provider]} onSelect={() => {}} variant="admin" />);
    expect(facetLabel("Threat Intelligence").getAttribute("tabindex")).toBeNull();
    expect(facetLabel("Cybersecurity").getAttribute("tabindex")).toBeNull();
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("names a cut group name whole in the tooltip on keyboard focus", async () => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(100);
    renderWithProviders(<IntegrationCatalogBrowser catalog={[provider]} onSelect={() => {}} variant="admin" />);
    const group = facetLabel("Cybersecurity");
    expect(group.getAttribute("tabindex")).toBe("0");
    act(() => group.focus());
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips[0].textContent).toContain("Cybersecurity");
  });

  it("names a cut facet label whole in the tooltip on keyboard focus", async () => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(100);
    renderWithProviders(<IntegrationCatalogBrowser catalog={[provider]} onSelect={() => {}} variant="admin" />);
    const label = facetLabel("Threat Intelligence");
    expect(label.getAttribute("tabindex")).toBe("0");
    act(() => label.focus());
    await nextFrame();
    const tips = await screen.findAllByRole("tooltip");
    expect(tips[0].textContent).toContain("Threat Intelligence");
  });
});
