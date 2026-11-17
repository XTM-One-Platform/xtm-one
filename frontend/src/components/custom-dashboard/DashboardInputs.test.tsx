// Inputs and tokens as a person meets them: a source using a token waits
// for it, an input sets it and the source is read again with it; with the
// Submit button nothing moves until it is pressed; a button sets tokens and
// switches tab; a title shows a token and another source's result.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

const api = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<object>()), api }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));
vi.mock("react-grid-layout", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-grid-layout")>()),
  useContainerWidth: () => ({ width: 1280, mounted: true, containerRef: { current: null } }),
}));

import { CustomDashboardView } from "./CustomDashboardView";
import { normalizeDashboard } from "./spec";

const ID = "11111111-1111-4111-8111-111111111111";

function definition(layoutOptions: Record<string, unknown> = {}) {
  return {
    version: 2,
    title: "Board",
    dataSources: {
      base: { type: "static", options: { shape: "rows", data: { columns: [{ key: "sev" }], rows: [{ sev: "high" }] } } },
      picked: {
        type: "derived",
        options: { extends: "base", steps: [{ op: "where", field: "sev", operator: "eq", value: "$sev$" }, { op: "stats", agg: "count" }] },
      },
      total: { type: "static", options: { shape: "number", data: { value: 12, change: null } } },
    },
    visualizations: {
      count: { type: "xtm.singlevalue", title: "Findings: $sev$ of $total:result.value$", dataSources: { primary: "picked" } },
      other: { type: "xtm.text", title: "Elsewhere", options: { body: "Second tab for $env:user$" } },
    },
    inputs: {
      sev: { type: "input.text", token: "sev", title: "Severity" },
      reset: {
        type: "input.button",
        options: { label: "Show low", actions: [{ type: "setToken", token: "sev", value: "low" }, { type: "switchTab", layoutId: "two" }] },
      },
    },
    layout: {
      options: layoutOptions,
      tabs: { items: [{ layoutId: "main", label: "Main" }, { layoutId: "two", label: "Two" }] },
      layoutDefinitions: {
        main: { structure: [{ item: "count", position: { x: 0, y: 0, w: 6, h: 6 } }] },
        two: { structure: [{ item: "other", position: { x: 0, y: 0, w: 6, h: 4 } }] },
      },
      globalInputs: ["sev", "reset"],
    },
  };
}

function draw(layoutOptions: Record<string, unknown> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderWithProviders(
    <MemoryRouter>
      <CustomDashboardView dashboard={normalizeDashboard(definition(layoutOptions))} dashboardId={ID} revision={1} />
    </MemoryRouter>,
    client,
  );
}

const last = <T,>(items: T[]): T | undefined => items[items.length - 1];

const reads = () =>
  api.post.mock.calls.filter(([url]) => url.endsWith("/widgets/count/data")).map(([, body]) => body as Record<string, unknown>);

describe("dashboard inputs and tokens", () => {
  beforeEach(() => {
    api.post.mockReset();
    api.get.mockReset();
    api.get.mockResolvedValue({ id: "u1", email: "marcus@example.com", full_name: "Marcus", timezone: "Europe/Paris" });
    api.post.mockImplementation(async (url: string, body: { tokens?: Record<string, string> }) => {
      if (url.endsWith("/data-sources/total/data")) {
        return { status: "ok", shape: "number", data: { value: 12, change: null }, fetched_at: "2026-10-25T10:00:00Z", open_url: null };
      }
      if (!body.tokens?.sev) return { status: "error", code: "waiting", open_url: null };
      return { status: "ok", shape: "number", data: { value: body.tokens.sev === "high" ? 1 : 0, change: null }, fetched_at: "2026-10-25T10:00:00Z", open_url: null };
    });
  });

  it("waits for its input, then reads its source with the token", async () => {
    draw();
    expect(await screen.findByText("Waiting for input.", {}, { timeout: 15_000 })).toBeTruthy();
    expect(reads()[0]).toEqual({});
    const box = screen.getByRole("textbox", { name: "Severity" });
    fireEvent.change(box, { target: { value: "high" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(last(reads())).toEqual({ tokens: { sev: "high" } }));
    expect(await screen.findByText("Findings: high of 12")).toBeTruthy();
    expect(api.post.mock.calls.filter(([url]) => url.endsWith("/data-sources/total/data")).length).toBe(1);
  }, 30_000);

  it("with a Submit button, reads nothing until it is pressed", async () => {
    draw({ submitButton: true, submitOnDashboardLoad: false });
    expect(await screen.findByText("Waiting for input.", {}, { timeout: 15_000 })).toBeTruthy();
    const box = screen.getByRole("textbox", { name: "Severity" });
    fireEvent.change(box, { target: { value: "high" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(reads()).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(reads()).toEqual([{ tokens: { sev: "high" } }]));
  }, 30_000);

  it("runs a button's actions: sets a token and switches tab", async () => {
    draw();
    fireEvent.click(await screen.findByRole("button", { name: "Show low" }, { timeout: 15_000 }));
    expect(await screen.findByText("Second tab for marcus@example.com")).toBeTruthy();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Main" }));
    await waitFor(() => expect(last(reads())).toEqual({ tokens: { sev: "low" } }));
  }, 30_000);
});

// A button's link is written by whoever edits the dashboard: only a page of
// the web app (followed in place) or an http(s) address (a new tab, without
// opener) is ever opened; anything else is dropped before any navigation.
describe("a button's link", () => {
  function Where() {
    return <output aria-label="Location">{useLocation().pathname}</output>;
  }

  function press(url: string, newTab = false) {
    const board = {
      version: 2,
      title: "Board",
      dataSources: {},
      visualizations: {},
      inputs: { go: { type: "input.button", options: { label: "Go", actions: [{ type: "link", url, newTab }] } } },
      layout: { tabs: { items: [{ layoutId: "main", label: "Main" }] }, layoutDefinitions: { main: { structure: [] } }, globalInputs: ["go"] },
    };
    const view = renderWithProviders(
      <MemoryRouter initialEntries={["/dashboards/here"]}>
        <CustomDashboardView dashboard={normalizeDashboard(board)} dashboardId={ID} revision={1} />
        <Where />
      </MemoryRouter>,
      new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    const where = screen.getByRole("status", { name: "Location" }).textContent;
    view.unmount();
    return where;
  }

  beforeEach(() => {
    api.get.mockResolvedValue({ id: "u1", email: "marcus@example.com", full_name: "Marcus", timezone: "Europe/Paris" });
  });

  it.each([
    "javascript:alert(document.cookie)",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "//evil.example/x",
    "/\\evil.example",
    "/api/v1/users",
    "https://user:pass@evil.example/",
    "file:///etc/passwd",
  ])("drops %s: no navigation, no window", (url) => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    expect(press(url)).toBe("/dashboards/here");
    expect(press(url, true)).toBe("/dashboards/here");
    expect(open.mock.calls).toEqual([]);
    open.mockRestore();
  });

  it("follows a page of the web app in place", () => {
    expect(press("/agents")).toBe("/agents");
  });

  it("opens an http(s) address in a new tab without opener, even when not asked for one", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    expect(press("https://cti.example/dashboard")).toBe("/dashboards/here");
    expect(open.mock.calls).toEqual([["https://cti.example/dashboard", "_blank", "noopener,noreferrer"]]);
    open.mockRestore();
  });
});
