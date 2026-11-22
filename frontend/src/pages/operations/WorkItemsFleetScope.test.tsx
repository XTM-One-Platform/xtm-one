// The administrators' Fleet / Mine / All switch on a fleet's Work Items tab:
// a member reads the fleet's items only, an administrator may read
// its own items or every item it can see, and the list asks the API with the
// matching filters (no `fleet` outside the Fleet scope, `vis=mine` on Mine).

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";

let IS_ADMIN = true;
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "Europe/Paris" }, isAdmin: IS_ADMIN }),
}));
vi.mock("@/hooks/useCompactToolbar", () => ({ useCompactToolbar: () => false }));

const asked: Array<Record<string, unknown>> = [];
const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useWorkItemApi", () => ({
  useWorkItems: (filters: Record<string, unknown>) => {
    asked.push(filters);
    return { data: [], isLoading: false };
  },
  useWorkItemCount: () => ({ data: { count: 0 } }),
  useStaleWorkItemCount: () => ({ data: { count: 0 } }),
  useWorkItemOwners: () => ({ data: [] }),
  useWorkItemGraph: () => ({ data: undefined, isLoading: false }),
  useWorkItemStatusCounts: () => ({ data: { open: 3, claimed: 1, in_progress: 2, blocked: 4, done: 7, cancelled: 0 } }),
  useWorkItem: () => ({ data: undefined, isLoading: false, isError: false }),
  useDeleteWorkItem: () => idle,
  useBulkDeleteWorkItems: () => idle,
  useRequeueWorkItem: () => idle,
  useCompleteWorkItem: () => idle,
  useCancelScheduledWorkItem: () => idle,
  useRescheduleScheduledWorkItem: () => idle,
  useAskWorkItem: () => idle,
}));

const { default: WorkItemsTab, fleetListScope } = await import("./WorkItemsTab");

const FLEET_EMPTY = "Nothing yet. What the agents of this fleet create appears here.";
const GENERIC_EMPTY = /Agents create durable work items to track follow-ups/;

function renderFleetTab(query = "") {
  asked.length = 0;
  return renderWithProviders(
    <MemoryRouter initialEntries={[`/fleets/f1?ops=work-items${query}`]}>
      <WorkItemsTab fleet="f1" emptyDescription={FLEET_EMPTY} />
    </MemoryRouter>,
  );
}

/** The switch sits in the Filters pill of the one filter row (#10215 criterion 6). */
function openFilters() {
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
}

/** The filters of the main list request (the first one asked, not the blocked list). */
function listFilters(): Record<string, unknown> {
  const list = asked.find((f) => f.status !== "blocked");
  if (!list) throw new Error("no list request");
  return list;
}

describe("fleetListScope", () => {
  it("keeps a member on the fleet whatever the URL says", () => {
    expect(fleetListScope("all", false)).toBe("fleet");
    expect(fleetListScope("mine", false)).toBe("fleet");
  });

  it("lets an administrator read Mine or All, and falls back to Fleet", () => {
    expect(fleetListScope("mine", true)).toBe("mine");
    expect(fleetListScope("all", true)).toBe("all");
    expect(fleetListScope("other", true)).toBe("fleet");
    expect(fleetListScope(null, true)).toBe("fleet");
  });
});

describe("Fleet / Mine / All switch on a fleet's Work Items tab", () => {
  it("is offered to an administrator, on Fleet by default, asking the fleet's items", () => {
    IS_ADMIN = true;
    renderFleetTab();
    openFilters();
    const group = screen.getByRole("group", { name: "Scope" });
    expect(within(group).getByRole("button", { name: /Fleet/ })).toHaveAttribute("aria-pressed", "true");
    expect(listFilters().fleet).toBe("f1");
  });

  it("reads every item the administrator can see on All", () => {
    IS_ADMIN = true;
    renderFleetTab("&scope=all");
    // The scope narrows the list: the pill counts it.
    expect(screen.getByRole("button", { name: /^Filters/ }).textContent).toContain("1");
    openFilters();
    const group = screen.getByRole("group", { name: "Scope" });
    expect(within(group).getByRole("button", { name: /All/ })).toHaveAttribute("aria-pressed", "true");
    expect(listFilters().fleet).toBeUndefined();
  });

  it("reads the administrator's own items on Mine", () => {
    IS_ADMIN = true;
    renderFleetTab("&scope=mine");
    expect(listFilters().fleet).toBeUndefined();
    expect(listFilters().vis).toBe("mine");
  });

  it("is not offered to a member, who reads the fleet even with ?scope=all", () => {
    IS_ADMIN = false;
    renderFleetTab("&scope=all");
    openFilters();
    expect(screen.queryByRole("group", { name: "Scope" })).not.toBeInTheDocument();
    expect(listFilters().fleet).toBe("f1");
  });
});

describe("empty state on each switch position", () => {
  it("promises the fleet's items on Fleet only", () => {
    IS_ADMIN = true;
    renderFleetTab();
    expect(screen.getByText(FLEET_EMPTY)).toBeInTheDocument();
    expect(screen.queryByText(GENERIC_EMPTY)).not.toBeInTheDocument();
  });

  // Mine and All narrow the fleet's list from the Filters pill (slice 8d, the shared
  // DataTable): the table's no-results line with Clear all, never the fleet's promise.
  it.each(["mine", "all"])("shows the no-results line on %s", (scope) => {
    IS_ADMIN = true;
    renderFleetTab(`&scope=${scope}`);
    expect(screen.getByText("No work items match your filters.")).toBeInTheDocument();
    expect(screen.queryByText(FLEET_EMPTY)).not.toBeInTheDocument();
    expect(screen.queryByText(GENERIC_EMPTY)).not.toBeInTheDocument();
  });
});
