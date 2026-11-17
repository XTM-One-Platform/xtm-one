// Bulk cancel of the runs that have not started: on the
// Executions list an administrator cancels every pending, delayed or queued
// run of the agent the list is filtered on. A dry run counts first, the
// confirmation names that count, and only a confirmed call changes anything;
// the list then shows the cancelled runs. Without an agent filter the action
// is disabled: the API refuses a filter on status alone.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import TaskHistory from "./TaskHistory";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
const auth = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ isAdmin: auth.isAdmin, user: { id: "u-1" } }),
}));
vi.mock("@/hooks/useFeatureFlag", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useFeatureFlag")>()),
  useFeatureFlagResolved: () => ({ enabled: false, isResolved: true, isError: false }),
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

function renderAt(entry: string) {
  renderWithProviders(
    <MemoryRouter initialEntries={[entry]}>
      <TaskHistory hideHeader />
      <LocationProbe />
    </MemoryRouter>,
  );
}

// The agent picker and its bulk cancel sit in the Filters pill (#10215).
async function findBulkCancel(): Promise<HTMLElement> {
  const pill = await screen.findByRole("button", { name: /^Filters/ });
  if (pill.getAttribute("aria-expanded") !== "true") fireEvent.click(pill);
  return screen.findByTestId("bulk-cancel-runs");
}

beforeEach(() => {
  auth.isAdmin = true;
  get.mockReset();
  post.mockReset();
  get.mockImplementation(async (url: string) => {
    if (url.includes("/segment-counts")) return { mine: 0, shared: 0, company: 0, all: 0 };
    if (url.includes("/count")) return { count: 0 };
    if (url.includes("/pending-by-tool")) return { tools: [], mixed_run_count: 0 };
    if (url.startsWith("/agents")) return [{ id: "agent-1", name: "Inbox agent" }];
    return [];
  });
});

describe("Executions filter row", () => {
  it("draws search, visibility, the Status pill and one Filters pill on one row, the rest in the pill", async () => {
    renderAt("/operations/executions");
    // The shared DataTable toolbar since #9378 slice 8f.
    const row = await screen.findByTestId("data-table-toolbar");
    expect(row).toContainElement(screen.getByRole("search", { name: "Search runs..." }));
    const pill = screen.getByRole("button", { name: /^Filters/ });
    expect(row).toContainElement(pill);
    // Status is a toolbar pill of its own (#9378 slice 8f), never inside Filters.
    expect(row).toContainElement(screen.getByRole("button", { name: /^Status/ }));
    expect(screen.queryByRole("group", { name: "Run type filter" })).toBeNull();
    fireEvent.click(pill);
    const panel = screen.getByRole("group", { name: "Filters" });
    expect(within(panel).queryByRole("group", { name: "Status filter" })).toBeNull();
    expect(within(panel).getByRole("group", { name: "View filter" })).toBeTruthy();
    expect(within(panel).getByRole("group", { name: "Run type filter" })).toBeTruthy();
    expect(within(panel).getByTestId("executions-picker-group")).toBeTruthy();
  });

  it("counts the filters that narrow the list on the pill, the status on its own pill", async () => {
    renderAt("/operations/executions?filter=failed&agent=agent-1");
    const pill = await screen.findByRole("button", { name: /^Filters/ });
    // The legacy ?filter=failed is a status now: counted by the Status pill, not by Filters.
    expect(pill.textContent).toBe("Filters1");
  });

  it("shows no count with no narrowing filter", async () => {
    renderAt("/operations/executions");
    const pill = await screen.findByRole("button", { name: /^Filters/ });
    expect(pill.textContent).toBe("Filters");
  });
});

describe("Executions: cancel runs not started", () => {
  it("is disabled without an agent filter, since the API refuses a status-only filter", async () => {
    renderAt("/operations/executions");
    const button = await findBulkCancel();
    expect(button).toBeDisabled();
    // The reason reaches keyboard and assistive tech: an
    // accessible description, and a focusable Tip wrapper, never a native title.
    expect(button).toHaveAccessibleDescription("Choose an agent to cancel its runs that have not started");
    expect(button).not.toHaveAttribute("title");
    const wrapper = button.parentElement as HTMLElement;
    expect(wrapper).toHaveAttribute("tabindex", "0");
    expect(wrapper).toHaveAttribute("aria-disabled", "true");
  });

  it("sits with the agent picker it is scoped to, not on the first toolbar row", async () => {
    renderAt("/operations/executions?agent=agent-1");
    const button = await findBulkCancel();
    expect(button).toBeEnabled();
    expect(button).not.toHaveAccessibleDescription();
    // Icon only (UX B1 round 2): the name comes from aria-label, not from text.
    expect(button).toHaveAccessibleName("Cancel runs not started");
    expect(button).toHaveTextContent("");
    expect(screen.getByTestId("executions-picker-group")).toContainElement(button);
  });

  it("renders Cleanup Stale Runs icon-only at every width, named by its aria-label", async () => {
    renderAt("/operations/executions?agent=agent-1");
    const cleanup = await screen.findByTestId("cleanup-stale-runs");
    // The text label pushed the 1440 toolbar to a third row in fr and es.
    expect(cleanup).toHaveAccessibleName("Cleanup Stale Runs");
    expect(cleanup).toHaveTextContent("");
    expect(cleanup).not.toHaveAttribute("title");
  });

  it("is not offered to a non-administrator", async () => {
    auth.isAdmin = false;
    renderAt("/operations/executions?agent=agent-1");
    await screen.findByRole("search", { name: "Search runs..." });
    fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
    expect(await screen.findByRole("combobox", { name: "Filter by agent" })).toBeInTheDocument();
    expect(screen.queryByTestId("bulk-cancel-runs")).not.toBeInTheDocument();
  });

  it("counts first, confirms with that count, cancels, then shows the cancelled runs", async () => {
    post.mockImplementation(async (_url: string, body: unknown) => {
      const { dry_run } = body as { dry_run?: boolean };
      return { count: 3, dry_run: !!dry_run };
    });
    renderAt("/operations/executions?agent=agent-1");

    fireEvent.click(await findBulkCancel());
    expect(await screen.findByText("Cancel 3 runs that have not started?")).toBeInTheDocument();
    // The dry run alone: nothing cancelled before the confirmation.
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenLastCalledWith("/assignments/runs/bulk-cancel", { agent_id: "agent-1", dry_run: true });

    fireEvent.click(screen.getByRole("button", { name: "Cancel runs" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post).toHaveBeenLastCalledWith("/assignments/runs/bulk-cancel", { agent_id: "agent-1" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toContain("status=cancelled"));
  });

  it("changes nothing when the confirmation is declined", async () => {
    post.mockResolvedValue({ count: 1, dry_run: true });
    renderAt("/operations/executions?agent=agent-1");

    fireEvent.click(await findBulkCancel());
    expect(await screen.findByText("Cancel 1 run that has not started?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Keep them" }));
    await waitFor(() => expect(screen.queryByText("Cancel 1 run that has not started?")).not.toBeInTheDocument());
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("asks nothing when no run of the agent is waiting", async () => {
    post.mockResolvedValue({ count: 0, dry_run: true });
    renderAt("/operations/executions?agent=agent-1");

    fireEvent.click(await findBulkCancel());
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: "Keep them" })).not.toBeInTheDocument();
  });
});
