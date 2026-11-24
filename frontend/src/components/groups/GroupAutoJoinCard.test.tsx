// The Auto join card of the group Overview (#1901): the switch shows the
// group's auto_join and sends the new value, and the result is toasted.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { Group } from "@/types";

import { GroupAutoJoinCard } from "./GroupAutoJoinCard";

const api = vi.hoisted(() => ({
  mutate: vi.fn(),
  outcome: "success" as "success" | "error",
}));
vi.mock("@/hooks/api/useGroupApi", () => ({
  useUpdateGroup: () => ({
    isPending: false,
    mutate: (vars: unknown, opts: { onSuccess?: () => void; onError?: (err: Error) => void }) => {
      api.mutate(vars);
      if (api.outcome === "success") opts.onSuccess?.();
      else opts.onError?.(new Error("Forbidden"));
    },
  }),
}));

const toasts = vi.hoisted(() => ({ calls: [] as { title?: string; variant?: string }[] }));
vi.mock("@/components/ui/toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/ui/toast")>()),
  useToast: () => ({ toast: (t: (typeof toasts.calls)[number]) => toasts.calls.push(t) }),
}));

const group = (autoJoin: boolean | undefined): Group =>
  ({
    id: "g1",
    name: "Filigran",
    member_count: 3,
    auto_join: autoJoin,
    created_at: "2026-11-24T10:00:00Z",
    updated_at: "2026-11-24T10:00:00Z",
  }) as Group;

describe("GroupAutoJoinCard", () => {
  beforeEach(() => {
    api.mutate.mockReset();
    api.outcome = "success";
    toasts.calls.length = 0;
  });
  afterEach(cleanup);

  it("is off for a group without auto join, and switching it on sends auto_join true", () => {
    renderWithProviders(<GroupAutoJoinCard group={group(undefined)} groupId="g1" />);
    const toggle = screen.getByRole("switch", { name: "Single sign-on users join this group" });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    fireEvent.click(toggle);
    expect(api.mutate).toHaveBeenCalledWith({ groupId: "g1", data: { auto_join: true } });
    expect(toasts.calls).toEqual([{ title: "Auto join updated", variant: "success" }]);
  });

  it("is on for a group with auto join, and switching it off sends auto_join false", () => {
    renderWithProviders(<GroupAutoJoinCard group={group(true)} groupId="g1" />);
    const toggle = screen.getByRole("switch", { name: "Single sign-on users join this group" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(toggle);
    expect(api.mutate).toHaveBeenCalledWith({ groupId: "g1", data: { auto_join: false } });
  });

  it("says so when the change is refused", () => {
    api.outcome = "error";
    renderWithProviders(<GroupAutoJoinCard group={group(false)} groupId="g1" />);
    fireEvent.click(screen.getByRole("switch", { name: "Single sign-on users join this group" }));
    expect(toasts.calls[0]).toMatchObject({ title: "Auto join could not be updated", variant: "error" });
  });
});
