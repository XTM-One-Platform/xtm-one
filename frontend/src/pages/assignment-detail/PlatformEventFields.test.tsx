// The deadline of a platform event trigger: an editable field with a
// way out of a passed deadline, and the summary chips of each state.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { PlatformEventDraft } from "@/lib/platform-events";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "UTC" }, isAdmin: false }),
}));
vi.mock("@/hooks/api/useAssignmentApi", () => ({
  usePlatformEvents: () => ({ data: [], isLoading: false }),
}));

const { PlatformEventFields, PlatformEventSummary } = await import("./PlatformEventFields");

const draft = (overrides: Partial<PlatformEventDraft> = {}): PlatformEventDraft => ({
  events: ["work_item.completed"],
  rows: [],
  scope: "own",
  once: true,
  ...overrides,
});

describe("PlatformEventFields deadline", () => {
  it("offers Clear and a new date when the deadline has passed", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <PlatformEventFields draft={draft({ expiresAt: "2020-01-01T09:00:00Z" })} onChange={onChange} isAdmin={false} />,
    );
    expect(screen.getByRole("alert").textContent).toContain("This deadline has passed");
    const field = screen.getByLabelText("Deadline (UTC)") as HTMLInputElement;
    expect(field.value).toBe("2020-01-01T09:00");
    fireEvent.change(field, { target: { value: "2099-01-01T09:00" } });
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ expiresAt: "2099-01-01T09:00:00+00:00" }));
    fireEvent.click(screen.getByRole("button", { name: /Clear/ }));
    expect("expiresAt" in onChange.mock.lastCall![0]).toBe(false);
  });

  it("shows no error and no Clear without a deadline", () => {
    renderWithProviders(<PlatformEventFields draft={draft()} onChange={vi.fn()} isAdmin={false} />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: /Clear/ })).toBeNull();
  });
});

describe("PlatformEventSummary deadline", () => {
  it("reads an expired one-off watch as expired, never as waiting", () => {
    renderWithProviders(
      <PlatformEventSummary config={{ event: "work_item.completed", once: true, expires_at: "2020-01-01T09:00:00Z" }} />,
    );
    expect(screen.getByText(/One-off watch: expired on/)).toBeTruthy();
    expect(screen.queryByText(/waiting for its event/)).toBeNull();
  });

  it("reads a standing trigger past its deadline as ended", () => {
    renderWithProviders(<PlatformEventSummary config={{ event: "run.failed", expires_at: "2020-01-01T09:00:00Z" }} />);
    expect(screen.getByText(/Ended on .*: no longer starts runs/)).toBeTruthy();
  });

  it("shows a deadline that does not parse without a date", () => {
    renderWithProviders(<PlatformEventSummary config={{ event: "run.failed", once: true, expires_at: "not a date" }} />);
    expect(screen.getByText("One-off watch: expired, no event came")).toBeTruthy();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });
});
