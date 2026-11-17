// The presentation library comes a page at a time: the server pages,
// searches and filters, so every deck stays reachable; the page keeps its
// search, filters and page in the URL, with the page bar above and below the
// grid, as the Videos tab does.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { QueryClient } from "@tanstack/react-query";

import { ErrorBoundary } from "@/components/ui/error-boundary";
import { api } from "@/lib/api";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import Presentations from "./Presentations";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
// The dialogs have their own data.
vi.mock("@/components/presentation/NewPresentationDialog", () => ({ NewPresentationDialog: () => null }));
vi.mock("@/components/presentation/PresentationShareDialog", () => ({ PresentationShareDialog: () => null }));

let decks = 30;
/** What `GET /presentations/segment-counts` answers; `null`: every deck is the user's. */
let segments: Record<string, number> | null = null;

function answer(path: string): unknown {
  if (path.startsWith("/presentations/segment-counts"))
    return segments ?? { mine: decks, company: 0, shared: 0, all: decks };
  if (!path.startsWith("/presentations?")) return {};
  const params = new URLSearchParams(path.split("?")[1]);
  const offset = Number(params.get("offset") ?? 0);
  const limit = Number(params.get("limit") ?? 0);
  const count = Math.max(0, Math.min(limit, decks - offset));
  const items = Array.from({ length: count }, (_, i) => ({
    id: `p-${offset + i}`,
    user_id: "u-1",
    owner_name: null,
    title: `Quarterly review ${offset + i}`,
    theme: "dark",
    aspect_ratio: "16:9",
    kind: "deck",
    slide_count: 6,
    revision: 1,
    template_name: null,
    open_comment_count: 0,
    has_public_link: false,
    is_company_managed: false,
    shared_group_ids: null,
    shared_user_ids: null,
    can_manage: true,
    updated_by_user_id: null,
    updated_by_name: null,
    updated_at: "2026-10-20T10:00:00Z",
    created_at: "2026-10-20T10:00:00Z",
  }));
  return { items, total: decks };
}

const requested = (path: string) => vi.mocked(api.get).mock.calls.some(([called]) => called === path);

function open(url: string, client: QueryClient = testQueryClient()) {
  return renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <ErrorBoundary>
        <Routes>
          <Route path="/presentations" element={<Presentations />} />
        </Routes>
      </ErrorBoundary>
    </MemoryRouter>,
    client,
  );
}

/** Opens the table's Kind filter pill and picks one kind. */
function pickKind(label: string) {
  fireEvent.click(screen.getByRole("button", { name: /^Kind/ }));
  fireEvent.click(within(screen.getByRole("group", { name: "Kind" })).getByRole("button", { name: label }));
}

const countsRequests = () =>
  vi.mocked(api.get).mock.calls.filter(([called]) => String(called).startsWith("/presentations/segment-counts")).length;

beforeEach(() => {
  decks = 30;
  segments = null;
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => Promise.resolve(answer(path))) as typeof api.get);
});

describe("The presentation library", () => {
  it("shows a page of decks and asks the server for the next one, from the one pager under the grid", async () => {
    open("/presentations");

    expect(await screen.findByText("Quarterly review 0")).toBeInTheDocument();
    // Mine when it holds a deck and the URL names no segment.
    expect(requested("/presentations?vis=mine&limit=25&offset=0")).toBe(true);
    expect(screen.queryByText("Quarterly review 25")).toBeNull();
    // One pager, under the grid (#10215 criterion 6: no pager row above the list); the
    // total once, in the toolbar, never repeated under the grid (Sam, 11 Nov 2026).
    expect(screen.getAllByText("Page 1 of 2")).toHaveLength(1);
    expect(screen.getByTestId("data-table-total").textContent).toBe("30");
    expect(screen.queryByText(/Showing \d/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));

    expect(await screen.findByText("Quarterly review 25")).toBeInTheDocument();
    expect(requested("/presentations?vis=mine&limit=25&offset=25")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(await screen.findByText("Quarterly review 0")).toBeInTheDocument();
  });

  it("lands on the first segment holding a deck when the user owns none, never asking for an empty Mine", async () => {
    segments = { mine: 0, company: 30, shared: 0, all: 30 };
    open("/presentations");

    expect(await screen.findByText("Quarterly review 0")).toBeInTheDocument();
    expect(requested("/presentations?vis=company&limit=25&offset=0")).toBe(true);
    expect(vi.mocked(api.get).mock.calls.some(([called]) => String(called).includes("vis=mine"))).toBe(false);
    expect(screen.getByRole("radio", { name: /Company/ })).toHaveAttribute("aria-checked", "true");

    // A kind filter does not move the landed segment.
    pickKind("Pages");
    await waitFor(() => expect(requested("/presentations?vis=company&kind=page&limit=25&offset=0")).toBe(true));
  });

  it("lands on Mine with no numbers and no error when the counts fail, as the app's strict query policy runs", async () => {
    // The app throws every query error to the page's boundary (`main.tsx`); the counts opt out.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, throwOnError: true } } });
    vi.mocked(api.get).mockImplementation(((path: string) =>
      path.startsWith("/presentations/segment-counts")
        ? Promise.reject(new Error("Internal Server Error: relation does not exist"))
        : Promise.resolve(answer(path))) as typeof api.get);
    open("/presentations", client);

    expect(await screen.findByText("Quarterly review 0")).toBeInTheDocument();
    expect(requested("/presentations?vis=mine&limit=25&offset=0")).toBe(true);
    expect(screen.queryByText("Failed to load this section")).toBeNull();
    expect(screen.queryByText(/relation does not exist/)).toBeNull();
    const mine = screen.getByRole("radio", { name: /Mine/ });
    expect(mine).toHaveAttribute("aria-checked", "true");
    expect(mine.textContent).not.toMatch(/\d/);
  });

  it("reads the counts again on mount when the URL names no segment, never landing from cached ones", async () => {
    const client = testQueryClient();
    // Counts cached by an earlier visit: Mine held every deck then.
    client.setQueryData(["presentations", "list", "segment-counts", "/presentations", ""], {
      mine: 30,
      company: 0,
      shared: 0,
      all: 30,
    });
    segments = { mine: 0, company: 30, shared: 0, all: 30 };
    open("/presentations", client);

    expect(await screen.findByText("Quarterly review 0")).toBeInTheDocument();
    expect(countsRequests()).toBeGreaterThanOrEqual(1);
    expect(requested("/presentations?vis=company&limit=25&offset=0")).toBe(true);
    expect(vi.mocked(api.get).mock.calls.some(([called]) => String(called).includes("vis=mine"))).toBe(false);
  });

  it("stays on a named empty Mine", async () => {
    segments = { mine: 0, company: 30, shared: 0, all: 30 };
    open("/presentations?vis=mine");

    await waitFor(() => expect(requested("/presentations?vis=mine&limit=25&offset=0")).toBe(true));
    expect(requested("/presentations?vis=company&limit=25&offset=0")).toBe(false);
  });

  it("sends a search to the server and starts again from the first page", async () => {
    open("/presentations?page=1");
    expect(await screen.findByText("Quarterly review 25")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search presentations" }), { target: { value: "board" } });

    await waitFor(() => expect(requested("/presentations?search=board&vis=mine&limit=25&offset=0")).toBe(true));
  });

  it("names the search in full but shows the short placeholder, which fits the field's 128 px floor in every locale", async () => {
    open("/presentations?page=1");
    expect(await screen.findByText("Quarterly review 25")).toBeInTheDocument();

    const search = screen.getByRole("searchbox", { name: "Search presentations" });
    expect(search).toHaveAttribute("placeholder", "Search...");
    expect(screen.queryByPlaceholderText(/Search presentations/)).toBeNull();
  });

  it("draws the scope in the table's toolbar, each segment named by its label with its count", async () => {
    // The table draws the segments icon-only in a box under 896 px (its own width, not the
    // viewport): measured in the UX round, jsdom has no layout.
    open("/presentations");
    expect(await screen.findByText("Quarterly review 0")).toBeInTheDocument();

    const mine = within(screen.getByTestId("data-table-toolbar")).getByRole("radio", { name: /^Mine/ });
    expect(mine.textContent).toContain("30");
  });

  it("sorts on the server and keeps the order in the URL", async () => {
    open("/presentations?sort=title&dir=asc");

    await waitFor(() =>
      expect(requested("/presentations?vis=mine&limit=25&offset=0&sort=title&direction=asc")).toBe(true),
    );
  });

  it("filters by kind and by visibility on the server, from the first page", async () => {
    open("/presentations?page=1");
    expect(await screen.findByText("Quarterly review 25")).toBeInTheDocument();

    pickKind("Pages");
    await waitFor(() => expect(requested("/presentations?vis=mine&kind=page&limit=25&offset=0")).toBe(true));

    fireEvent.click(screen.getByRole("radio", { name: /Shared/ }));
    await waitFor(() => expect(requested("/presentations?vis=shared&kind=page&limit=25&offset=0")).toBe(true));

    fireEvent.click(screen.getByRole("radio", { name: /All/ }));
    await waitFor(() => expect(requested("/presentations?kind=page&limit=25&offset=0")).toBe(true));
  });

  it("reads its filters from the URL, so a link or a reload keeps them", async () => {
    open("/presentations?q=board&vis=shared&kind=deck&page=1");

    expect(await screen.findByRole("searchbox", { name: "Search presentations" })).toHaveValue("board");
    await waitFor(() =>
      expect(requested("/presentations?search=board&vis=shared&kind=deck&limit=25&offset=25")).toBe(true),
    );
  });

  it("reads the [DEPRECATED] ?visibility= alias of an old link", async () => {
    open("/presentations?visibility=company");

    await waitFor(() => expect(requested("/presentations?vis=company&limit=25&offset=0")).toBe(true));
    expect(await screen.findByRole("radio", { name: /Company/ })).toHaveAttribute("aria-checked", "true");
  });

  it("goes back to the last page when the one shown has emptied, never saying no deck matches meanwhile", async () => {
    // The last page answers only when the test says so: the emptied page stays
    // on screen for the whole clamp, as on a slow network.
    let release: () => void = () => {};
    const lastPage = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.mocked(api.get).mockImplementation(((path: string) =>
      path === "/presentations?vis=mine&limit=25&offset=25"
        ? lastPage.then(() => answer(path))
        : Promise.resolve(answer(path))) as typeof api.get);
    open("/presentations?page=4");

    await waitFor(() => expect(requested("/presentations?vis=mine&limit=25&offset=25")).toBe(true));
    expect(requested("/presentations?vis=mine&limit=25&offset=100")).toBe(true);
    expect(screen.queryByText("No presentations matching your filters")).toBeNull();
    expect(screen.queryByText("No presentations yet")).toBeNull();

    release();
    expect(await screen.findByText("Quarterly review 25")).toBeInTheDocument();
    expect(screen.queryByText("No presentations matching your filters")).toBeNull();
  });

  it("says no deck matches only when the filtered library is empty", async () => {
    decks = 0;
    open("/presentations?q=board");

    expect(await screen.findByText("No presentations matching your filters")).toBeInTheDocument();
  });

  it("invites the user to create a deck when the library is empty", async () => {
    decks = 0;
    open("/presentations");

    expect(await screen.findByText("No presentations yet")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next page" })).toBeNull();
  });

  it("keeps the header and toolbar when the list read fails, with the table's error line and Retry", async () => {
    // The app throws every query error to the page's boundary (`main.tsx`); the list opts out (UX r1 B2 on #11075).
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, throwOnError: true } } });
    let failing = true;
    vi.mocked(api.get).mockImplementation(((path: string) =>
      failing && path.startsWith("/presentations?")
        ? Promise.reject(new Error("boom"))
        : Promise.resolve(answer(path))) as typeof api.get);
    open("/presentations", client);

    expect(await screen.findByText("The presentations could not be loaded.")).toBeInTheDocument();
    expect(screen.queryByText("boom")).toBeNull();
    expect(screen.queryByText("Failed to load this section")).toBeNull();
    expect(screen.getByPlaceholderText(/Search/)).toBeInTheDocument();

    failing = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Quarterly review 0")).toBeInTheDocument();
  });

  it("lays the list out on fixed column widths, so a long title is cut and the row actions stay on screen", async () => {
    open("/presentations");

    expect(await screen.findByText("Quarterly review 0")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "List view" }));
    expect(await screen.findByRole("table")).toHaveAttribute("data-layout", "fixed");
    localStorage.clear();
  });
});
