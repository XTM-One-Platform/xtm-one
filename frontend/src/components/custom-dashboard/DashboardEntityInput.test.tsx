// An entity input as a viewer meets it (an OpenCTI custom view's "current
// entity"): the source waits for it; what is typed is searched among the
// entities of the input's types, in its platform; the entity chosen is the
// token the source is read with, and is named, not shown as an id.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));
vi.mock("react-grid-layout", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-grid-layout")>()),
  useContainerWidth: () => ({ width: 1280, mounted: true, containerRef: { current: null } }),
}));

import { DashboardAccessContext } from "@/hooks/api/dashboard-access";

import { CustomDashboardView } from "./CustomDashboardView";
import { normalizeDashboard } from "./spec";

const ID = "11111111-1111-4111-8111-111111111111";
const CTI = "22222222-2222-4222-8222-222222222222";

const DEFINITION = {
  version: 2,
  title: "Country view",
  dataSources: {
    base: { type: "static", options: { shape: "rows", data: { columns: [{ key: "country" }], rows: [{ country: "c1" }, { country: "c2" }] } } },
    about: { type: "derived", options: { extends: "base", steps: [{ op: "where", field: "country", operator: "eq", value: "$entity$" }, { op: "stats", agg: "count" }] } },
  },
  visualizations: { count: { type: "xtm.singlevalue", title: "Reports about it", dataSources: { primary: "about" } } },
  inputs: { country: { type: "input.entity", token: "entity", title: "Country", options: { source: "opencti", integration_id: CTI, entityTypes: ["Country"] } } },
  layout: {
    tabs: { items: [{ layoutId: "main", label: "Main" }] },
    layoutDefinitions: { main: { structure: [{ item: "count", position: { x: 0, y: 0, w: 6, h: 6 } }] } },
    globalInputs: ["country"],
  },
};

const FRANCE = { value: "c1", label: "France", group: "Country" };

describe("an entity input", () => {
  beforeEach(() => {
    api.post.mockReset();
    api.get.mockReset();
    api.get.mockResolvedValue({ id: "u1", email: "marcus@example.com", full_name: "Marcus", timezone: "Europe/Paris" });
    api.post.mockImplementation(async (url: string, body: Record<string, unknown>) => {
      if (url.endsWith("/platform-schema")) {
        const ids = (body.ids as string[] | undefined) ?? [];
        const search = String(body.search ?? "").toLowerCase();
        const items = ids.length ? (ids.includes("c1") ? [FRANCE] : []) : "france".startsWith(search) && search ? [FRANCE] : [];
        return { status: "ok", items, code: null };
      }
      const tokens = (body.tokens as Record<string, string> | undefined) ?? {};
      if (!tokens.entity) return { status: "error", code: "waiting", open_url: null };
      return { status: "ok", shape: "number", data: { value: tokens.entity === "c1" ? 1 : 0, change: null }, fetched_at: "2026-10-26T10:00:00Z", open_url: null };
    });
  });

  it("searches its platform's entities of its types and reads its source with the one chosen", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderWithProviders(
      <MemoryRouter>
        <CustomDashboardView dashboard={normalizeDashboard(DEFINITION)} dashboardId={ID} revision={1} />
      </MemoryRouter>,
      client,
    );
    expect(await screen.findByText("Waiting for input.", {}, { timeout: 15_000 })).toBeTruthy();
    const box = screen.getByRole("combobox", { name: "Country" });
    fireEvent.change(box, { target: { value: "fra" } });
    fireEvent.click(await screen.findByText("France", {}, { timeout: 5_000 }));
    await waitFor(() => {
      const reads = api.post.mock.calls.filter(([url]) => url.endsWith("/widgets/count/data"));
      expect(reads[reads.length - 1]?.[1]).toEqual({ tokens: { entity: "c1" } });
    });
    // The search went to the input's platform and integration, for its types only.
    const searched = api.post.mock.calls.filter(([url]) => url.endsWith("/platform-schema")).map(([, body]) => body as Record<string, unknown>);
    expect(searched.some((q) => q.kind === "entities" && q.search === "fra")).toBe(true);
    expect(searched.every((q) => q.source === "opencti" && q.integration_id === CTI && JSON.stringify(q.types) === '["Country"]')).toBe(true);
    // The chosen entity is named, never shown as its id.
    await waitFor(() => expect((screen.getByRole("combobox", { name: "Country" }) as HTMLInputElement).value).toBe("France"));
  }, 30_000);

  it("searches nothing where there is no session (an export's page), and shows the entity by the name it was given", async () => {
    // A refused search sent the export's page to the sign-in page, and the export never finished.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const withDefault = { ...DEFINITION, inputs: { country: { ...DEFINITION.inputs.country, defaultValue: "c1" } } };
    renderWithProviders(
      <MemoryRouter>
        <DashboardAccessContext.Provider value={{ kind: "render", token: "t", entityNames: { c1: "France" } }}>
          <CustomDashboardView dashboard={normalizeDashboard(withDefault)} dashboardId={ID} revision={1} />
        </DashboardAccessContext.Provider>
      </MemoryRouter>,
      client,
    );
    expect(await screen.findByRole("combobox", { name: "Country" }, { timeout: 15_000 })).toBeTruthy();
    // ... and names the entity it starts on by the name the render route gave, not its id.
    await waitFor(() => expect((screen.getByRole("combobox", { name: "Country" }) as HTMLInputElement).value).toBe("France"));
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(api.post.mock.calls.filter(([url]) => url.endsWith("/platform-schema"))).toEqual([]);
  }, 30_000);
});
