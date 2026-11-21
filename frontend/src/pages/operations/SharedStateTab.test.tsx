// The Shared State list keeps one selection column for every row: a
// company-managed entry a non-admin cannot manage shows its box disabled with
// the reason instead of leaving it out, which shifted the row 28 px left of
// every other one. jsdom does no layout: these tests assert the box is there,
// first and disabled; the pixel alignment is checked rendered.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { SharedState } from "@/types";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1" }, isAdmin: false }),
}));

const entry = (overrides: Partial<SharedState>): SharedState => ({
  id: "id",
  user_id: "u1",
  is_company_managed: false,
  key: "key",
  label: null,
  owner_agent_id: null,
  status: "active",
  is_stale: false,
  value: {},
  notes: [],
  resolved_at: null,
  created_at: null,
  updated_at: null,
  ...overrides,
});

const MIXED: SharedState[] = [
  entry({ id: "company", key: "fleet:register", is_company_managed: true, user_id: "admin" }),
  entry({ id: "own", key: "dev:open-prs" }),
];
const ONLY_COMPANY: SharedState[] = [
  entry({ id: "c1", key: "fleet:register", is_company_managed: true, user_id: "admin" }),
  entry({ id: "c2", key: "fleet:runbook", is_company_managed: true, user_id: "admin" }),
];
let STATES: SharedState[] = MIXED;
// The owners `GET /shared-state/owners` answers, and the filters each hook was asked with.
let OWNERS: { agent: { id: string | null; name: string | null; icon: null; accessible: boolean } | null; count: number }[] = [];
const asked: { list: Record<string, unknown>[]; owners: Record<string, unknown>[] } = { list: [], owners: [] };
const last = (calls: Record<string, unknown>[]) => calls[calls.length - 1];

const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useSharedStateApi", () => ({
  useSharedStates: (filters: Record<string, unknown>) => {
    asked.list.push(filters);
    return { data: STATES, isLoading: false };
  },
  useSharedStateOwners: (filters: Record<string, unknown>) => {
    asked.owners.push(filters);
    return { data: OWNERS };
  },
  useSharedStateCount: (_filters: unknown, deletableOnly?: boolean) => ({
    data: { count: deletableOnly ? STATES.filter((s) => !s.is_company_managed).length : STATES.length },
  }),
  useDeleteSharedState: () => idle,
  useBulkDeleteSharedState: () => idle,
  useResolveSharedState: () => idle,
}));
vi.mock("@/hooks/api/useAgentApi", () => ({
  useAgents: () => ({ data: [] }),
}));

const { default: SharedStateTab } = await import("./SharedStateTab");

function renderTab(url = "/operations/shared-state") {
  // A named segment: the tab shows its rows at once, without waiting for the
  // per-segment counts it lands by when the URL names none. The grid view (the
  // Operations row) is the default view of the table.
  const named = url.includes("vis=") ? url : `${url}${url.includes("?") ? "&" : "?"}vis=mine`;
  return renderWithProviders(
    <MemoryRouter initialEntries={[named]}>
      <SharedStateTab />
    </MemoryRouter>,
  );
}

/** Opens the Filters pill of the toolbar and answers its panel. */
function openFilters(): HTMLElement {
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
  return screen.getByRole("group", { name: "Filters" });
}

function rowOf(key: string): HTMLElement {
  const row = screen.getAllByTestId("shared-state-row").find((r) => within(r).queryAllByText(key).length > 0);
  if (!row) throw new Error(`no row ${key}`);
  return row;
}

describe("Shared State list selection column", () => {
  it("gives a row the reader cannot manage the same selection column as an own row", () => {
    renderTab();
    for (const key of ["fleet:register", "dev:open-prs"]) {
      expect(within(rowOf(key)).getAllByRole("checkbox")).toHaveLength(1);
    }
    const companyBox = within(rowOf("fleet:register")).getByRole("checkbox") as HTMLButtonElement;
    expect(companyBox.disabled).toBe(true);
    expect(companyBox.getAttribute("aria-label")).toBe("Not selectable");
    const ownBox = within(rowOf("dev:open-prs")).getByRole("checkbox", { name: "Select entry" }) as HTMLButtonElement;
    expect(ownBox.disabled).toBe(false);
  });

  it("puts the selection box before the expand control on every row", () => {
    renderTab();
    for (const key of ["fleet:register", "dev:open-prs"]) {
      const row = rowOf(key);
      const box = within(row).getByRole("checkbox");
      const expand = within(row).getByRole("button", { name: "Expand" });
      expect(box.compareDocumentPosition(expand) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });

  it("selects only the manageable rows from the page box, and offers the bulk delete", () => {
    renderTab();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select every row on this page" }));
    expect(within(rowOf("dev:open-prs")).getByRole("checkbox").getAttribute("aria-checked")).toBe("true");
    expect(within(rowOf("fleet:register")).getByRole("checkbox").getAttribute("aria-checked")).toBe("false");
    expect(within(screen.getByTestId("data-table-selection")).getByTestId("shared-state-bulk-delete")).toBeTruthy();
  });

  it("draws no page box when no row of the page can be managed", () => {
    STATES = ONLY_COMPANY;
    try {
      renderTab();
      expect(screen.queryByRole("checkbox", { name: "Select every row on this page" })).toBeNull();
      for (const box of screen.getAllByRole("checkbox", { name: "Not selectable" })) {
        expect((box as HTMLButtonElement).disabled).toBe(true);
      }
    } finally {
      STATES = MIXED;
    }
  });
});

// The row is the shared Operations row: one line of chips, one line
// below, the key and the owner as tokens, every fact in the hover card.
// Geometry (same height, same x) is measured by tests_e2e/operations-list-grid.spec.ts.
describe("Shared State row on the Operations grid", () => {
  const NOTED = entry({
    id: "noted",
    key: "dev:harness-gaps",
    label: "Harness gaps with a label long enough to be cut in a narrow row",
    is_stale: true,
    value: { gaps: [], updated: "x" },
    notes: [
      { at: "2026-11-04T10:00:00Z", note: "first note" },
      { at: "2026-11-04T11:00:00Z", note: "the latest note, shown on line 2" },
    ],
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-04T11:00:00Z",
  });

  it("shows the latest note on line 2 and the field count when there is none", () => {
    STATES = [NOTED, entry({ id: "bare", key: "dev:bare", value: { a: 1 } })];
    try {
      renderTab();
      expect(within(rowOf("dev:harness-gaps")).getByTestId("shared-state-summary").textContent).toBe("the latest note, shown on line 2");
      expect(within(rowOf("dev:bare")).getByTestId("shared-state-summary").textContent).toBe("1 field");
    } finally {
      STATES = MIXED;
    }
  });

  it("names the status in words and gives the key as a copyable token", () => {
    STATES = [NOTED];
    try {
      renderTab();
      const row = rowOf("dev:harness-gaps");
      expect(within(row).getByTestId("shared-state-status").textContent).toBe("Active");
      expect(within(row).getByText("Stale")).toBeTruthy();
      expect(within(row).getByRole("button", { name: "Copy the key" })).toBeTruthy();
      expect(within(row).getByTestId("shared-state-updated").textContent).not.toBe("");
    } finally {
      STATES = MIXED;
    }
  });

  it("opens the hover card on focus of the title with every fact whole", async () => {
    STATES = [NOTED];
    try {
      renderTab();
      fireEvent.focus(within(rowOf("dev:harness-gaps")).getByTestId("shared-state-title"));
      const card = await screen.findByTestId("ops-hover-card");
      expect(card.textContent).toContain("Harness gaps with a label long enough to be cut in a narrow row");
      expect(within(card).getAllByRole("term").map((t) => t.textContent)).toEqual([
        "Status", "Stale", "Key", "Contents", "Latest note", "Created", "Updated",
      ]);
      expect(within(card).getByTestId("shared-state-fact-note").textContent).toContain("the latest note, shown on line 2");
    } finally {
      STATES = MIXED;
    }
  });

  it("flags an entry over the soft limit and gives its size in the hover card", async () => {
    STATES = [
      entry({ ...NOTED, is_stale: false, size_chars: 70000, largest_list: 1, over_soft_limit: true }),
      entry({ id: "small", key: "dev:small", value: { a: 1 }, size_chars: 1, largest_list: 0, over_soft_limit: false }),
    ];
    try {
      renderTab();
      expect(within(rowOf("dev:harness-gaps")).getByTestId("shared-state-oversized").textContent).toContain("Oversized");
      expect(within(rowOf("dev:small")).queryByTestId("shared-state-oversized")).toBeNull();
      fireEvent.focus(within(rowOf("dev:harness-gaps")).getByTestId("shared-state-title"));
      const card = await screen.findByTestId("ops-hover-card");
      expect(within(card).getAllByRole("term").map((t) => t.textContent)).toEqual([
        "Status", "Oversized", "Key", "Contents", "Size", "Latest note", "Created", "Updated",
      ]);
      expect(within(card).getByTestId("shared-state-fact-size").textContent).toContain("longest list, 1 item");
    } finally {
      STATES = MIXED;
    }
  });

  it("unfolds the value and the notes under the row on a click, and folds them back", () => {
    STATES = [NOTED];
    try {
      renderTab();
      const row = rowOf("dev:harness-gaps");
      expect(within(row).queryByTestId("shared-state-detail")).toBeNull();
      fireEvent.click(within(row).getByTestId("shared-state-summary"));
      expect(within(rowOf("dev:harness-gaps")).getByTestId("shared-state-detail").textContent).toContain("first note");
      expect(within(rowOf("dev:harness-gaps")).getByRole("button", { name: "Collapse" }).getAttribute("aria-expanded")).toBe("true");
      fireEvent.click(within(rowOf("dev:harness-gaps")).getByRole("button", { name: "Collapse" }));
      expect(within(rowOf("dev:harness-gaps")).queryByTestId("shared-state-detail")).toBeNull();
    } finally {
      STATES = MIXED;
    }
  });

  it("keeps a click on the selection box from unfolding the row", () => {
    renderTab();
    fireEvent.click(within(rowOf("dev:open-prs")).getByRole("checkbox", { name: "Select entry" }));
    expect(within(rowOf("dev:open-prs")).queryByTestId("shared-state-detail")).toBeNull();
  });
});

// UX round 1 on #11896, B1: at 1728 the browser sized the list view's columns from a
// long key and the table ran 1794 px wide in its 1498 px card. jsdom does no layout:
// this pins the fixed layout and the header widths that keep Key and Label the rest.
describe("Shared State list view width", () => {
  it("draws the list on fixed column widths that leave Key and Label most of a 1498 px card", () => {
    localStorage.setItem("copilot-view:operations-shared-state", "list");
    try {
      renderTab();
      const table = screen.getByRole("table");
      expect(table.getAttribute("data-layout")).toBe("fixed");
      const fixed = ["Status", "Owner", "Size", "Updated"].map((name) => {
        const header = screen.queryByRole("columnheader", { name });
        return header ? Number.parseInt(header.style.width, 10) || 0 : 0;
      });
      for (const width of fixed) expect(width).toBeLessThanOrEqual(200);
      // Selection box (32) + actions (64) + the fixed columns: Key and Label share at least 700 px.
      const taken = 32 + 64 + fixed.reduce((sum, w) => sum + w, 0);
      expect(1498 - taken).toBeGreaterThanOrEqual(700);
      expect(screen.getByTestId("data-table-actions-header").style.width).toBe("64px");
    } finally {
      localStorage.removeItem("copilot-view:operations-shared-state");
    }
  });
});

// The shared data table's one toolbar row (slice 8c of #9378): search,
// visibility and the Status filter ON the row (Sam: the status is the filter
// people reach for, never behind a pill), the owner and fleet in the Filters pill.
describe("Shared State toolbar", () => {
  it("draws the Status filter on the toolbar, outside the Filters pill", () => {
    renderTab();
    const row = screen.getByTestId("data-table-toolbar");
    expect(row).toContainElement(screen.getByRole("radiogroup", { name: "Visibility" }));
    const status = screen.getByRole("radiogroup", { name: "Status filter" });
    expect(row).toContainElement(status);
    expect(within(status).getAllByRole("radio").map((r) => r.textContent?.replace(/[\d\s]+$/, ""))).toEqual([
      "Active", "Stale", "Resolved", "All",
    ]);
    const panel = openFilters();
    expect(within(panel).queryByRole("radiogroup", { name: "Status filter" })).toBeNull();
  });

  it("shows the count of each status value", () => {
    renderTab();
    const active = within(screen.getByRole("radiogroup", { name: "Status filter" })).getByRole("radio", { name: /^Active/ });
    expect(active.textContent).toContain(String(MIXED.length));
  });

  it.each([
    ["/operations/shared-state", { status: "active", stale_only: false }, "Active"],
    ["/operations/shared-state?sst_status=stale", { status: "active", stale_only: true }, "Stale"],
    ["/operations/shared-state?sst_status=resolved", { status: "resolved", stale_only: false }, "Resolved"],
    ["/operations/shared-state?sst_status=all", { status: undefined, stale_only: false }, "All"],
    // The keys of the former list still land on the same view.
    ["/operations/shared-state?stale=stale", { status: "active", stale_only: true }, "Stale"],
    ["/operations/shared-state?status=resolved", { status: "resolved", stale_only: false }, "Resolved"],
  ])("on %s asks %o and checks %s", (url, query, label) => {
    asked.list.length = 0;
    renderTab(url);
    expect(last(asked.list)?.status).toBe(query.status);
    expect(last(asked.list)?.stale_only).toBe(query.stale_only);
    const checked = within(screen.getByRole("radiogroup", { name: "Status filter" })).getByRole("radio", { checked: true });
    expect(checked.textContent).toMatch(new RegExp(`^${label}`));
  });

  it("sends the table's sort to the server, and none for the default order", () => {
    asked.list.length = 0;
    renderTab("/operations/shared-state?sst_sort=key:asc");
    expect(last(asked.list)?.sort).toBe("key");
    expect(last(asked.list)?.direction).toBe("asc");
    asked.list.length = 0;
    renderTab();
    expect(last(asked.list)?.sort).toBeUndefined();
  });
});

// The empty state blames filters only for what the person narrowed:
// the landing view (Active) and All say what is empty, as the Work Items tab.
describe("Shared State empty state", () => {
  it.each([
    ["/operations/shared-state", "No active shared state"],
    ["/operations/shared-state?sst_status=all", "No shared state"],
    ["/operations/shared-state?sst_status=resolved", "No shared state matches your filters."],
    ["/operations/shared-state?sst_status=stale", "No shared state matches your filters."],
    ["/operations/shared-state?sst_status=all&sst_q=dev", "No shared state matches your filters."],
    // An owner agent picked with no entry is a filter the person set.
    ["/operations/shared-state?sst_f_owner=gone", "No shared state matches your filters."],
    ["/operations/shared-state?sst_status=all&sst_f_owner=gone", "No shared state matches your filters."],
  ])("on %s reads %s", async (url, title) => {
    STATES = [];
    try {
      renderTab(url);
      expect(await screen.findByText(title)).toBeTruthy();
    } finally {
      STATES = MIXED;
    }
  });
});

// The owner-agent filter: offered from `GET /shared-state/owners`,
// held in `?sst_f_owner=`, sent as `owner_agent_id` to the list.
describe("Shared State owner-agent filter", () => {
  const ANALYST = { id: "a-1", name: "Analyst", icon: null, accessible: true };
  const HIDDEN = { id: null, name: null, icon: null, accessible: false };

  it("offers no owner choice when no entry has an owner the reader may name", () => {
    OWNERS = [{ agent: null, count: 2 }, { agent: HIDDEN, count: 1 }];
    try {
      renderTab();
      expect(within(openFilters()).queryByRole("group", { name: "Owner" })).toBeNull();
    } finally {
      OWNERS = [];
    }
  });

  it("offers the named owners and sends the chosen one to the list", () => {
    OWNERS = [{ agent: ANALYST, count: 3 }, { agent: HIDDEN, count: 1 }];
    asked.list.length = 0;
    asked.owners.length = 0;
    try {
      renderTab("/operations/shared-state?sst_f_owner=a-1");
      expect(within(within(openFilters()).getByRole("group", { name: "Owner" })).getByRole("button", { name: /^Analyst/ })).toBeTruthy();
      expect(last(asked.list)?.owner_agent_id).toBe("a-1");
      expect(last(asked.owners)?.owner_agent_id).toBe("a-1");
    } finally {
      OWNERS = [];
    }
  });

  it("keeps the choice for an owner named in the URL that holds no entry", () => {
    OWNERS = [];
    renderTab("/operations/shared-state?sst_f_owner=gone");
    expect(within(openFilters()).getByRole("group", { name: "Owner" })).toBeTruthy();
  });

  it("sends no owner when the URL names none", () => {
    asked.list.length = 0;
    renderTab();
    expect(last(asked.list)?.owner_agent_id).toBeUndefined();
  });
});
