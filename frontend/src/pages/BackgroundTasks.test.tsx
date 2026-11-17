// The empty state of Background tasks: a segment
// other than All is a filter, so an empty Shared, Company or Mine reads "match
// your filters" and never "No background tasks yet.", which would claim the
// platform holds no task at all. Only an empty All with no search or status
// filter says "yet".

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { IntlProvider } from "react-intl";
import { MemoryRouter } from "react-router-dom";

import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import BackgroundTasks from "./BackgroundTasks";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ isAdmin: true, user: { id: "u-1" } }),
}));

const get = vi.mocked(api.get);

beforeEach(() => {
  get.mockReset();
  get.mockImplementation(async (url: string) => {
    // Shared is empty while All holds tasks.
    if (url.includes("/segment-counts")) return { mine: 3, shared: 0, company: 2, all: 5 };
    if (url.includes("/count")) return { count: 0 };
    return [];
  });
});

function renderAt(entry: string, page = <BackgroundTasks />) {
  renderWithProviders(<MemoryRouter initialEntries={[entry]}>{page}</MemoryRouter>);
}

describe("Background tasks empty state", () => {
  it.each(["shared", "company", "mine"])("an empty %s segment reads as a filter, not as no task at all", async (vis) => {
    renderAt(`/operations/background-tasks?vis=${vis}`);
    expect(await screen.findByText("No background tasks match your filters.")).toBeInTheDocument();
    expect(screen.queryByText("No background tasks yet.")).not.toBeInTheDocument();
  });

  it("an empty All with no search or status filter says there is no task yet", async () => {
    renderAt("/operations/background-tasks?vis=all");
    expect(await screen.findByText("No background tasks yet.")).toBeInTheDocument();
  });
});

const oneTask = (createdAt: string) => ({
  task_id: "t-1",
  user_id: "u-1",
  parent_agent_id: "a-1",
  target_agent_id: "a-2",
  parent_agent_name: "Parent",
  target_agent_name: "Target",
  task_description: "Summarise the report",
  status: "completed",
  result: null,
  error: null,
  created_at: createdAt,
  started_at: createdAt,
  completed_at: createdAt,
});

// The row time reads the viewer's language, like the execution line under it,
// and the order is the shared table's: one Sort by control in its toolbar.
describe("Background tasks row time and sort placement", () => {
  const norm = (s: string | null | undefined) => (s ?? "").replace(/[\u00a0\u202f]/g, " ");

  it.each([
    ["fr", fr, "il y a 8 minutes"],
    ["es", es, "hace 8 minutos"],
    ["ja", ja, "8 分前"],
  ] as const)("reads the row time in %s", async (locale, messages, expected) => {
    const createdAt = new Date(Date.now() - 8 * 60_000 - 10_000).toISOString();
    get.mockImplementation(async (url: string) => {
      if (url.includes("/segment-counts")) return { mine: 1, shared: 0, company: 0, all: 1 };
      if (url.includes("/status-counts")) return { counts: { completed: 1 } };
      if (url.includes("/count")) return { count: 1 };
      if (/\/chat\/sub-agent-tasks\?/.test(url)) return [oneTask(createdAt)];
      return [];
    });
    renderAt(
      "/operations/background-tasks?vis=all",
      <IntlProvider locale={locale} messages={messages as Record<string, string>} onError={() => {}}>
        <BackgroundTasks />
      </IntlProvider>,
    );
    await screen.findByText("Summarise the report");
    const times = screen.getAllByText((_, el) => el?.tagName === "SPAN" && norm(el.textContent) === expected);
    expect(times.length).toBeGreaterThan(0);
    expect(screen.queryByText(/8m ago/)).toBeNull();

    // The table's own toolbar holds the order; no hand-made list bar is left.
    expect(screen.getByTestId("data-table-toolbar")).toContainElement(screen.getByTestId("data-table-grid-sort"));
    expect(screen.queryByTestId("list-control-bar")).toBeNull();
  });
});

// The Status pill on the toolbar (#9378 slice 8e): multi-select, kept in
// `?status=a,b`, its counts from /status-counts, one click to Running.
describe("Background tasks status filter", () => {
  it("sends the URL's statuses to the list and the count, never to the status counts", async () => {
    renderAt("/operations/background-tasks?vis=all&status=running,pending");
    await screen.findByTestId("background-tasks-status-pill");
    await waitFor(() => {
      const urls = get.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => /\/chat\/sub-agent-tasks\?/.test(u) && u.includes("status=pending%2Crunning"))).toBe(true);
      expect(urls.some((u) => u.includes("/count?") && u.includes("status=pending%2Crunning"))).toBe(true);
      const counts = urls.filter((u) => u.includes("/status-counts"));
      expect(counts.length).toBeGreaterThan(0);
      for (const u of counts) expect(u).not.toContain("status=");
    });
  });

  it("shows the count of each status and narrows to Running in one click", async () => {
    get.mockImplementation(async (url: string) => {
      if (url.includes("/segment-counts")) return { mine: 5, shared: 0, company: 0, all: 5 };
      if (url.includes("/status-counts")) return { counts: { running: 3, failed: 2 } };
      if (url.includes("/count")) return { count: 5 };
      return [];
    });
    renderAt("/operations/background-tasks?vis=all");
    const chip = await screen.findByTestId("background-tasks-running-chip");
    await waitFor(() => expect(chip.textContent).toContain("3"));
    fireEvent.click(chip);
    expect(chip).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("background-tasks-status-pill"));
    expect(screen.getByTestId("background-tasks-status-failed").textContent).toContain("2");
    expect(screen.getByTestId("background-tasks-status-pending").textContent).toContain("0");
    expect(screen.getByTestId("background-tasks-status-running")).toHaveAttribute("aria-pressed", "true");
  });

  it("drops an unknown status from the URL instead of asking the server for it", async () => {
    renderAt("/operations/background-tasks?vis=all&status=zombie");
    await screen.findByTestId("background-tasks-status-pill");
    const urls = get.mock.calls.map(([url]) => String(url));
    expect(urls.some((u) => u.includes("status=zombie"))).toBe(false);
  });
});

// The server sorts each column both ways; an old `?sort=last_progress` link
// reads as the Updated column, newest move first.
describe("Background tasks server sort", () => {
  it.each([
    ["/operations/background-tasks?vis=all", ""],
    ["/operations/background-tasks?vis=all&sort=last_progress", "sort=last_progress"],
    ["/operations/background-tasks?vis=all&sort=updated&dir=asc", "sort=last_progress_asc"],
    ["/operations/background-tasks?vis=all&sort=created&dir=asc", "sort=created_asc"],
    ["/operations/background-tasks?vis=all&sort=status", "sort=status"],
    ["/operations/background-tasks?vis=all&sort=from&dir=desc", "sort=from_desc"],
    ["/operations/background-tasks?vis=all&sort=to", "sort=to"],
  ])("%s asks the list with %s", async (entry, expected) => {
    renderAt(entry);
    await screen.findByTestId("background-tasks-status-pill");
    await waitFor(() => {
      const list = get.mock.calls.map(([url]) => String(url)).filter((u) => /\/chat\/sub-agent-tasks\?/.test(u));
      expect(list.length).toBeGreaterThan(0);
      for (const u of list) {
        if (expected) expect(new URLSearchParams(u.split("?")[1]).get("sort")).toBe(expected.split("=")[1]);
        else expect(u).not.toContain("sort=");
      }
    });
  });

  it("pages the list with the table's page size", async () => {
    renderAt("/operations/background-tasks?vis=all&page=2&size=50");
    await screen.findByTestId("background-tasks-status-pill");
    await waitFor(() => {
      const list = get.mock.calls.map(([url]) => String(url)).filter((u) => /\/chat\/sub-agent-tasks\?/.test(u));
      expect(list.some((u) => u.includes("limit=50") && u.includes("offset=100"))).toBe(true);
    });
  });
});

// The execution filter and the last-progress sort together:
// the page renders, the list carries both, the counts carry the filter only
// (sort is a list-only query param).
describe("Background tasks execution filter with the last-progress sort", () => {
  it.each([
    "/operations/background-tasks?liveness=no_live_execution&sort=last_progress",
    "/operations/background-tasks?sort=last_progress&liveness=no_live_execution&vis=all",
  ])("renders %s", async (entry) => {
    renderAt(entry);
    expect(await screen.findByText("No background tasks match your filters.")).toBeInTheDocument();

    const urls = get.mock.calls.map(([url]) => String(url));
    const list = urls.filter((u) => /\/chat\/sub-agent-tasks\?/.test(u));
    expect(list.length).toBeGreaterThan(0);
    for (const url of list) {
      expect(url).toContain("liveness=no_live_execution");
      expect(url).toContain("sort=last_progress");
    }
    const counts = urls.filter((u) => u.includes("/count") || u.includes("/segment-counts"));
    expect(counts.length).toBeGreaterThan(0);
    for (const url of counts) {
      expect(url).toContain("liveness=no_live_execution");
      expect(url).not.toContain("sort=");
    }
  });
});

// The Operations row contract (components/ui/operations-list.tsx): one row of
// fixed height on OpsRowGrid, the essentials in the row, every fact whole in
// the hover card, the actions one OpsRowAction each.
describe("Background tasks row on the Operations grid", () => {
  const LONG = "Summarise every open pull request of the week with its reviewers, its checks and what it waits for";
  function serve(tasks: Record<string, unknown>[]) {
    get.mockImplementation(async (url: string) => {
      if (url.includes("/segment-counts")) return { mine: tasks.length, shared: 0, company: 0, all: tasks.length };
      if (url.includes("/count")) return { count: tasks.length };
      if (/\/chat\/sub-agent-tasks\?/.test(url)) return tasks;
      return [];
    });
  }
  const base = {
    user_id: "u-1",
    parent_agent_id: "a-1",
    target_agent_id: "a-2",
    parent_agent_name: "CEO Office Assistant",
    target_agent_name: "CEO Office Developer",
    result: null,
    error: null,
    created_at: "2026-11-04T10:00:00Z",
    started_at: "2026-11-04T10:00:01Z",
    completed_at: null,
  };

  it("draws every row on the one grid line, the run as a copyable token and the error on line 2", async () => {
    serve([
      { ...base, task_id: "t-1", task_description: LONG, status: "running", assignment_run_id: "1234567890abcdef", execution_state: "running", execution_id: "abcdef0123456789" },
      { ...base, task_id: "t-2", task_description: "A failed task", status: "failed", error: "The target agent refused the task", completed_at: "2026-11-04T10:02:01Z" },
    ]);
    renderAt("/operations/background-tasks?vis=all");
    await screen.findByText(LONG);
    const lines = screen.getAllByTestId("background-task-line");
    expect(lines).toHaveLength(2);
    // Each line holds one grid: lead, main, meta, updated and actions.
    for (const line of lines) expect(line.querySelectorAll('[data-testid="ops-meta"]')).toHaveLength(1);
    expect(screen.getByTestId("background-task-run")).toHaveTextContent("12345678");
    expect(screen.getByRole("button", { name: "Copy run ID 12345678" })).toBeInTheDocument();
    expect(screen.getByTestId("background-task-summary")).toHaveTextContent("The target agent refused the task");
    expect(screen.getAllByTestId("background-task-updated")).toHaveLength(2);
  });

  it("names its actions with one word each, kill only while in flight", async () => {
    serve([
      { ...base, task_id: "t-1", task_description: "An in-flight task", status: "running" },
      { ...base, task_id: "t-2", task_description: "A cancelled task", status: "cancelled", completed_at: "2026-11-04T10:02:01Z" },
    ]);
    renderAt("/operations/background-tasks?vis=all");
    await screen.findByText("An in-flight task");
    expect(screen.getAllByRole("button", { name: "Kill task" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(2);
  });

  it("opens the hover card on focus of the title with every fact whole", async () => {
    serve([
      { ...base, task_id: "t-1", task_description: LONG, status: "completed", result: "Done.", assignment_run_id: "1234567890abcdef", completed_at: "2026-11-04T10:02:01Z" },
    ]);
    renderAt("/operations/background-tasks?vis=all");
    const title = await screen.findByTestId("background-task-title");
    fireEvent.focus(title);
    const card = await screen.findByTestId("ops-hover-card");
    expect(card).toHaveTextContent(LONG);
    expect(card).toHaveTextContent("CEO Office Assistant");
    expect(card).toHaveTextContent("CEO Office Developer");
    expect(card).toHaveTextContent("1234567890abcdef");
    expect(card).toHaveTextContent("2m");
  });
});
