// The Operations lists on the one visibility convention (dev:visibility-filters-consistency):
// Work Items, Shared State and Worksets draw the shared VisibilityFilter
// (Mine | Company | Shared | All, a radio group), land on the first segment
// holding a row when the URL names none, keep the segment in ?vis=
// and send it to the API as vis=. The [DEPRECATED]
// ?ownership= is still read once as an alias.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import BackgroundTasks from "@/pages/BackgroundTasks";
import TaskHistory from "@/pages/TaskHistory";
import SharedStateTab from "./SharedStateTab";
import WorkItemsTab from "./WorkItemsTab";
import WorksetsTab from "./WorksetsTab";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ isAdmin: false, user: { id: "u-1" } }),
}));
vi.mock("@/hooks/useFeatureFlag", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useFeatureFlag")>()),
  useFeatureFlagResolved: () => ({ enabled: false, isResolved: true, isError: false }),
}));

const get = vi.mocked(api.get);

beforeEach(() => {
  get.mockReset();
  get.mockImplementation(async (url: string) => {
    if (url.includes("/segment-counts")) return { mine: 2, shared: 1, company: 3, all: 6 };
    if (url.includes("/count") || url.includes("stale-count")) return { count: 0 };
    return [];
  });
});

function listCalls(prefix: string): string[] {
  return get.mock.calls
    .map(([url]) => String(url))
    .filter(
      (url) =>
        url.startsWith(prefix) &&
        !url.includes("/count") &&
        !url.includes("/segment-counts") &&
        !url.includes("/owners") &&
        !url.includes("stale-count"),
    )
    // The one-row probe an empty Mine asks over every segment is not the list.
    .filter((url) => !url.includes("limit=1&") && !url.endsWith("limit=1"));
}

const TABS = [
  { name: "Work items", Tab: WorkItemsTab, prefix: "/work-items" },
  { name: "Shared state", Tab: SharedStateTab, prefix: "/shared-state" },
  { name: "Worksets", Tab: WorksetsTab, prefix: "/worksets" },
] as const;

describe.each(TABS)("$name tab", ({ Tab, prefix }) => {
  it("opens on a Mine that holds rows and asks the API for vis=mine", async () => {
    renderWithProviders(
      <MemoryRouter>
        <Tab />
      </MemoryRouter>,
    );
    await waitFor(() => expect(listCalls(prefix).length).toBeGreaterThan(0));
    expect(listCalls(prefix).every((url) => url.includes("vis=mine"))).toBe(true);
    const group = await screen.findByRole("radiogroup", { name: "Visibility" });
    expect(within(group).getByRole("radio", { name: /Mine/ })).toHaveAttribute("aria-checked", "true");
    // Each segment carries its count, read from <list>/segment-counts without the segment.
    await waitFor(() =>
      expect(within(group).getAllByRole("radio").map((r) => r.textContent?.trim())).toEqual([
        "Mine2",
        "Company3",
        "Shared1",
        "All6",
      ]),
    );
    const counted = get.mock.calls.map(([url]) => String(url)).filter((url) => url.startsWith(`${prefix}/segment-counts`));
    expect(counted.length).toBeGreaterThan(0);
    expect(counted.every((url) => !url.includes("vis="))).toBe(true);
  });

  it("moves to Company and to All from the same control", async () => {
    renderWithProviders(
      <MemoryRouter>
        <Tab />
      </MemoryRouter>,
    );
    const group = await screen.findByRole("radiogroup", { name: "Visibility" });
    fireEvent.click(within(group).getByRole("radio", { name: /Company/ }));
    await waitFor(() => expect(listCalls(prefix).some((url) => url.includes("vis=company"))).toBe(true));
    fireEvent.click(within(await screen.findByRole("radiogroup", { name: "Visibility" })).getByRole("radio", { name: /All/ }));
    await waitFor(() => {
      const calls = listCalls(prefix);
      expect(calls[calls.length - 1]).not.toContain("vis=");
    });
  });

  it("reads ?vis= and the [DEPRECATED] ?ownership= alias", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/operations?vis=shared"]}>
        <Tab />
      </MemoryRouter>,
    );
    await waitFor(() => expect(listCalls(prefix).length).toBeGreaterThan(0));
    expect(listCalls(prefix).every((url) => url.includes("vis=shared"))).toBe(true);
  });

  it("lands on the first segment holding a row when the URL names none", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.includes("/segment-counts")) return { mine: 0, shared: 1, company: 3, all: 4 };
      if (url.includes("/count") || url.includes("stale-count")) return { count: 0 };
      return [];
    });
    renderWithProviders(
      <MemoryRouter>
        <Tab />
      </MemoryRouter>,
    );
    await waitFor(() => expect(listCalls(prefix).length).toBeGreaterThan(0));
    // No list request on an empty Mine first: the counts decide before the list is asked.
    expect(listCalls(prefix).every((url) => url.includes("vis=company"))).toBe(true);
    const group = await screen.findByRole("radiogroup", { name: "Visibility" });
    expect(within(group).getByRole("radio", { name: /Company/ })).toHaveAttribute("aria-checked", "true");
  });

  it("stays on a named empty Mine", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.includes("/segment-counts")) return { mine: 0, shared: 1, company: 3, all: 4 };
      if (url.includes("/count") || url.includes("stale-count")) return { count: 0 };
      return [];
    });
    renderWithProviders(
      <MemoryRouter initialEntries={["/operations?vis=mine"]}>
        <Tab />
      </MemoryRouter>,
    );
    await waitFor(() => expect(listCalls(prefix).length).toBeGreaterThan(0));
    expect(listCalls(prefix).every((url) => url.includes("vis=mine"))).toBe(true);
  });

  it("opens an old ?ownership=company_managed link (shared + company) on All", async () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/operations?ownership=company_managed"]}>
        <Tab />
      </MemoryRouter>,
    );
    await waitFor(() => expect(listCalls(prefix).length).toBeGreaterThan(0));
    expect(listCalls(prefix).every((url) => !url.includes("vis="))).toBe(true);
    const group = await screen.findByRole("radiogroup", { name: "Visibility" });
    expect(within(group).getByRole("radio", { name: /All/ })).toHaveAttribute("aria-checked", "true");
  });
});

describe("Shared state tab while its list loads", () => {
  it("keeps the search box and the visibility control on Mine", async () => {
    // The list never answers: the tab stays in its loading state.
    get.mockImplementation(async (url: string) => {
      if (url.includes("/segment-counts")) return { mine: 1, shared: 0, company: 0, all: 1 };
      if (url.includes("/count") || url.includes("stale-count")) return { count: 0 };
      if (url.startsWith("/shared-state")) return new Promise(() => {});
      return [];
    });
    renderWithProviders(
      <MemoryRouter initialEntries={["/operations/shared-state"]}>
        <SharedStateTab />
      </MemoryRouter>,
    );
    const group = await screen.findByRole("radiogroup", { name: "Visibility" });
    expect(within(group).getByRole("radio", { name: /Mine/ })).toHaveAttribute("aria-checked", "true");
    // By its accessible name: a compact toolbar search shows the short placeholder.
    expect(screen.getByRole("search", { name: "Search key or label..." })).toBeInTheDocument();
  });
});

// Every Operations tab keeps its toolbar mounted while its list loads, the
// skeleton in the list slot only, with no fake page title.
const LOADING_TABS = [
  { name: "Shared state", Tab: SharedStateTab, list: "/shared-state", search: "Search key or label..." },
  { name: "Work items", Tab: WorkItemsTab, list: "/work-items", search: "Search title or key..." },
  { name: "Worksets", Tab: WorksetsTab, list: "/worksets", search: "Search worksets..." },
  { name: "Background tasks", Tab: BackgroundTasks, list: "/chat/sub-agent-tasks", search: "Search tasks..." },
  { name: "Executions", Tab: TaskHistory, list: "/assignments/runs/all", search: "Search runs..." },
] as const;

describe.each(LOADING_TABS)("$name tab while its list loads", ({ Tab, list, search }) => {
  it("keeps the search box and the visibility control, the skeleton in the list slot", async () => {
    // The list never answers: the tab stays in its loading state.
    get.mockImplementation(async (url: string) => {
      if (url.includes("/segment-counts")) return { mine: 1, shared: 0, company: 0, all: 1 };
      // A count that lands before the list: the bar must still wait.
      if (url.includes("/count") || url.includes("stale-count")) return { count: 7 };
      if (url.includes("/pending-by-tool")) return { tools: [], mixed_run_count: 0 };
      if (url.startsWith(list) && !url.includes("limit=1&") && !url.endsWith("limit=1")) {
        return new Promise(() => {});
      }
      return [];
    });
    renderWithProviders(
      <MemoryRouter initialEntries={["/operations?vis=mine"]}>
        <Tab />
      </MemoryRouter>,
    );
    const group = await screen.findByRole("radiogroup", { name: "Visibility" });
    expect(within(group).getByRole("radio", { name: /Mine/ })).toHaveAttribute("aria-checked", "true");
    // By its accessible name: a compact toolbar search shows the short placeholder.
    expect(screen.getByRole("search", { name: search })).toBeInTheDocument();
    expect(await screen.findByTestId("list-skeleton")).toBeInTheDocument();
    // The count bar never contradicts the skeleton: no range, no pager.
    expect(screen.queryByText("No results")).not.toBeInTheDocument();
    expect(screen.queryByText(/^Showing /)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "First" })).not.toBeInTheDocument();
  });
});

// Background tasks on the shared data table (#9378 slice 8e, as Executions and
// Work items): search, visibility and the Status pill on the table's one toolbar
// row; execution and fleet in its Filters pill, counted when they narrow the list.
describe("Background tasks filter row", () => {
  function renderTasks(url: string) {
    renderWithProviders(
      <MemoryRouter initialEntries={[url]}>
        <BackgroundTasks hideHeader />
      </MemoryRouter>,
    );
  }

  it("draws search, visibility and the Status pill on the toolbar, execution in the Filters pill", async () => {
    renderTasks("/operations/tasks?vis=mine");
    const toolbar = await screen.findByTestId("data-table-toolbar");
    expect(toolbar).toContainElement(await screen.findByRole("radiogroup", { name: "Visibility" }));
    expect(toolbar).toContainElement(screen.getByRole("search", { name: "Search tasks..." }));
    expect(toolbar).toContainElement(screen.getByTestId("background-tasks-status-pill"));
    expect(toolbar).toContainElement(screen.getByTestId("background-tasks-running-chip"));
    const pill = screen.getByTestId("data-table-filters");
    expect(toolbar).toContainElement(pill);
    expect(screen.queryByRole("group", { name: "Execution filter" })).toBeNull();
    fireEvent.click(within(pill).getByRole("button", { name: /^Filters/ }));
    expect(within(pill).getByRole("group", { name: "Execution filter" })).toBeTruthy();
    // Status is on the toolbar, never a second copy in the pill.
    expect(within(pill).queryByRole("group", { name: "Status filter" })).toBeNull();
  });

  it("counts the Filters pill's own filters only, the Status pill its selection", async () => {
    renderTasks("/operations/tasks?vis=mine&status=running,pending&liveness=no_live_execution");
    const pill = await screen.findByTestId("data-table-filters");
    expect(within(pill).getByRole("button", { name: /^Filters/ }).textContent).toBe("Filters1");
    expect(screen.getByTestId("background-tasks-status-pill").textContent).toContain("2");
  });

  it("shows no count with no narrowing filter", async () => {
    renderTasks("/operations/tasks?vis=mine");
    const pill = await screen.findByTestId("data-table-filters");
    expect(within(pill).getByRole("button", { name: /^Filters/ }).textContent).toBe("Filters");
  });
});

// Worksets on the shared data table (slice 8b): search and visibility on the
// table's one toolbar row, Status and Scope in its Filters pill (#10215
// criterion 6, as the other Operations lists); the server pages the list.
describe("Worksets records table", () => {
  function renderWorksets(url = "/operations/worksets?vis=mine") {
    renderWithProviders(
      <MemoryRouter initialEntries={[url]}>
        <WorksetsTab />
      </MemoryRouter>,
    );
  }

  it("draws search and visibility on the toolbar, Status and Scope in its Filters pill", async () => {
    renderWorksets();
    const row = await screen.findByTestId("data-table-toolbar");
    expect(row).toContainElement(await screen.findByRole("radiogroup", { name: "Visibility" }));
    expect(row).toContainElement(screen.getByRole("search", { name: "Search worksets..." }));
    expect(screen.queryByRole("button", { name: /^Status/ })).toBeNull();
    const pill = screen.getByTestId("data-table-filters");
    expect(row).toContainElement(pill);
    fireEvent.click(within(pill).getByRole("button", { name: /^Filters/ }));
    expect(within(pill).getByRole("group", { name: "Status" })).toBeInTheDocument();
    expect(within(pill).getByRole("group", { name: "Scope" })).toBeInTheDocument();
  });

  it("counts the URL's status filter on the Filters pill", async () => {
    renderWorksets("/operations/worksets?vis=all&wks_f_status=resolved");
    const pill = await screen.findByTestId("data-table-filters");
    expect(within(pill).getByRole("button", { name: /^Filters/ }).textContent).toBe("Filters1");
  });

  // UX rounds 1 and 2 (B1, B2, #12171): the chip strip drops chips when a long
  // implicit key fills the title, so the collection state never sits in it: it
  // rides with the status chip, which never shrinks.
  it("keeps the collection chip out of the chip strip, beside the status", async () => {
    const row = {
      id: "w-1", key: `agent-${"a".repeat(36)}:workspace-${"b".repeat(36)}:conversation-${"c".repeat(36)}`, implicit: true,
      label: null, scope: "run", scope_ref: null, status: "active", processing: "auto", workers: 1, item_count: 3,
      evicted_count: 0, content_chars: 10, max_items: 100, max_content_chars: 1000, max_auto_calls: 10, auto_calls_used: 0,
      declared_total: null, declared_total_source: null, sealed_at: null, watermark_at: null, last_progress_at: null,
      expires_at: null, resolved_at: null, is_company_managed: false, owner_agent_id: null,
      created_at: "2026-11-16T00:00:00Z", updated_at: "2026-11-16T00:00:00Z",
    };
    get.mockImplementation(async (url: string) => {
      if (url.includes("/segment-counts")) return { mine: 1, shared: 0, company: 0, all: 1 };
      if (url.includes("/count") || url.includes("stale-count")) return { count: 1 };
      if (url.startsWith("/worksets")) return [row];
      return [];
    });
    renderWorksets();
    const state = await screen.findByTestId("workset-state");
    expect(within(state).getByTestId("workset-status").textContent).toBe("Active");
    expect(within(state).getByTestId("workset-collection").textContent).toBe("Rolling");
    const strip = screen.getByTestId("workset-chips");
    expect(within(strip).queryByTestId("workset-collection")).toBeNull();
    expect(within(strip).getByText("This run")).toBeInTheDocument();
  });

  it("sends the URL's status and scope filters to the server", async () => {
    renderWorksets("/operations/worksets?vis=all&wks_f_status=resolved&wks_f_scope=run");
    await screen.findByTestId("data-table-toolbar");
    await waitFor(() => {
      const urls = get.mock.calls.map(([url]) => String(url)).filter((url) => url.startsWith("/worksets?"));
      expect(urls.some((url) => url.includes("status_filter=resolved") && url.includes("scope=run"))).toBe(true);
    });
  });
});
