// The dashboard's work-item notice counts what waits for the reader only:
// an agent's own waits (blocked on a pull request, a deployment, another
// item) never turn the home page red.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import { DashboardNotices } from "./DashboardNotices";

const waiting = vi.hoisted(() => ({
  data: { count: 0, oldest: null } as {
    count: number;
    oldest: null | { id: string; title: string; reason: string | null; since: string | null };
  },
}));

vi.mock("@/hooks/api/useWorkItemApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useWorkItemApi")>()),
  useWorkItemsWaitingForMe: () => ({ data: waiting.data }),
}));
vi.mock("@/hooks/api/useIntegrationApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/api/useIntegrationApi")>()),
  useIntegrations: () => ({ data: [] }),
}));
vi.mock("@/components/ui/enterprise-upsell", () => ({ EnterpriseUpsellNotice: () => null }));
vi.mock("./ModelConfigWarnings", () => ({ ModelConfigWarnings: () => null }));
vi.mock("./KBHealthWarnings", () => ({ KBHealthWarnings: () => null }));

function Location() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function renderNotices() {
  return renderWithProviders(
    <MemoryRouter>
      <DashboardNotices />
      <Location />
    </MemoryRouter>,
  );
}

const OLDEST = { id: "item-1", title: "Pick a tier", reason: "pick a pricing tier", since: "2026-11-03T10:00:00Z" };

describe("DashboardNotices work-item notice", () => {
  it("renders nothing when nothing waits for the reader", () => {
    waiting.data = { count: 0, oldest: null };
    renderNotices();
    expect(screen.queryByText(/waiting for you/i)).toBeNull();
  });

  it("names the item waiting longest and why", () => {
    waiting.data = {
      count: 2,
      oldest: { id: "item-1", title: "Pick a tier", reason: "pick a pricing tier", since: "2026-11-03T10:00:00Z" },
    };
    renderNotices();
    expect(screen.getByText("2 work items are waiting for you")).toBeTruthy();
    expect(screen.getByText(/Waiting for you since .*: pick a pricing tier/)).toBeTruthy();
  });

  it("Review opens the Waiting for me list on All when several items wait, so every one it counted is listed", () => {
    // The count spans every segment (company-managed items included): Mine would hide them.
    waiting.data = { count: 4, oldest: OLDEST };
    renderNotices();
    fireEvent.click(screen.getByText("4 work items are waiting for you"));
    expect(screen.getByTestId("location").textContent).toBe("/tasks/work-items?view=waiting&vis=all");
  });

  it("Review opens the item itself when it is the only one waiting", () => {
    waiting.data = { count: 1, oldest: OLDEST };
    renderNotices();
    fireEvent.click(screen.getByText("1 work item is waiting for you"));
    expect(screen.getByTestId("location").textContent).toBe("/tasks/work-items/item-1");
  });

  it("Review is a button of its own beside the copy, never nested in the notice's button", () => {
    waiting.data = { count: 4, oldest: OLDEST };
    renderNotices();
    const review = screen.getByRole("button", { name: "Review" });
    expect(review.parentElement?.closest("button")).toBeNull();
    fireEvent.click(review);
    expect(screen.getByTestId("location").textContent).toBe("/tasks/work-items?view=waiting&vis=all");
  });

  it("draws a count of 1,000 or more compact, the exact count on hover (#10476)", () => {
    waiting.data = { count: 1234, oldest: OLDEST };
    renderNotices();
    const title = screen.getByText("1.2K work items are waiting for you");
    expect(title.getAttribute("title")).toBe("1,234");
  });
});
