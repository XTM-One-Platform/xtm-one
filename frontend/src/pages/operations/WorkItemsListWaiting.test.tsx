// The list view of Tasks > Work Items marks an item waiting for the reader with the
// card's Waiting for you badge, beside its title (#2200, clause 3 of #1001).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { WorkItem } from "@/types";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "Europe/Paris" }, isAdmin: false }),
}));
vi.mock("@/hooks/useCompactToolbar", () => ({ useCompactToolbar: () => false }));

function item(id: string, title: string, overrides: Partial<WorkItem>): WorkItem {
  return {
    id,
    title,
    description: "",
    status: "blocked",
    priority: "medium",
    user_id: "u1",
    owner_agent_id: "a1",
    needs_human: false,
    waits_for_user_id: null,
    due_at: null,
    scheduled: null,
    is_stale: false,
    correlation_key: null,
    created_at: "2026-11-25T10:00:00Z",
    updated_at: "2026-11-25T10:00:00Z",
    ...overrides,
  } as unknown as WorkItem;
}

const ITEMS = [
  item("w1", "Approve the release", { needs_human: true, waits_for_user_id: "u1" }),
  item("w2", "Rebuild the index", {}),
];

const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useWorkItemApi", () => ({
  useWorkItems: (filters: Record<string, unknown>) => ({ data: filters.status === "blocked" ? [] : ITEMS, isLoading: false }),
  useWorkItemCount: () => ({ data: { count: ITEMS.length } }),
  useStaleWorkItemCount: () => ({ data: { count: 0 } }),
  useWorkItemOwners: () => ({ data: [] }),
  useWorkItemGraph: () => ({ data: undefined, isLoading: false }),
  useWorkItemStatusCounts: () => ({ data: { open: 0, claimed: 0, in_progress: 0, blocked: 2, done: 0, cancelled: 0 } }),
  useWorkItem: () => ({ data: undefined, isLoading: false, isError: false }),
  useDeleteWorkItem: () => idle,
  useBulkDeleteWorkItems: () => idle,
  useRequeueWorkItem: () => idle,
  useCompleteWorkItem: () => idle,
  useCancelScheduledWorkItem: () => idle,
  useRescheduleScheduledWorkItem: () => idle,
  useAskWorkItem: () => idle,
}));

const { default: WorkItemsTab } = await import("./WorkItemsTab");

beforeEach(() => {
  localStorage.setItem("copilot-view:operations-work-items", "list");
});

afterEach(() => {
  localStorage.removeItem("copilot-view:operations-work-items");
});

describe("Work items list view", () => {
  it("marks the item waiting for the reader beside its title, and only that one", () => {
    renderWithProviders(
      <MemoryRouter initialEntries={["/fleets/f1?ops=work-items"]}>
        <WorkItemsTab fleet="f1" emptyDescription="Nothing yet." />
      </MemoryRouter>,
    );
    const badges = screen.getAllByTestId("work-item-list-waiting-for-you");
    expect(badges).toHaveLength(1);
    expect(badges[0].textContent).toContain("Waiting for you");
    const row = badges[0].closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("Approve the release")).toBeTruthy();
    const other = screen.getByText("Rebuild the index").closest("tr");
    expect(within(other as HTMLElement).queryByTestId("work-item-list-waiting-for-you")).toBeNull();
  });
});
