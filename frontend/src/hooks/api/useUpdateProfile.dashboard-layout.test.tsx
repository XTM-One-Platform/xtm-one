// A dashboard layout write is one vertical's layout; the cached user carries
// them all. The optimistic patch merges it the way the server does, and never
// leaves the request's own shape on the user.

import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useUpdateProfile } from "@/hooks/api/useAuthApi";
import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";
import type { CtemDashboardLayout, User } from "@/types";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), put: vi.fn() },
}));

const CYBER: CtemDashboardLayout = { items: [{ i: "hero", x: 0, y: 0, w: 8, h: 12 }], hidden: ["briefing"] };
const FRAUD: CtemDashboardLayout = { items: [{ i: "digests", x: 0, y: 0, w: 12, h: 9 }], hidden: [] };

function setup(layouts: User["ctem_dashboard_layouts"]) {
  const client = testQueryClient();
  client.setQueryData<Partial<User>>(["me"], { id: "u-1", ctem_vertical: "cyber", ctem_dashboard_layouts: layouts });
  // Never answers: the cache shows the optimistic patch, not a refetch.
  vi.mocked(api.put).mockReturnValue(new Promise(() => {}));
  const { result } = renderHook(() => useUpdateProfile(), { wrapper: withProviders(client) });
  const cached = () => client.getQueryData<Record<string, unknown>>(["me"]);
  return { result, cached };
}

async function settleMutate() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("useUpdateProfile - dashboard layouts", () => {
  it("merges one vertical's layout and keeps the others", async () => {
    const { result, cached } = setup({ fraud: FRAUD });
    act(() => result.current.mutate({ ctem_dashboard_layout: { vertical: "cyber", layout: CYBER } }));
    await settleMutate();
    expect(cached()?.ctem_dashboard_layouts).toEqual({ fraud: FRAUD, cyber: CYBER });
    expect(cached()).not.toHaveProperty("ctem_dashboard_layout");
    expect(api.put).toHaveBeenCalledWith("/settings/preferences", {
      ctem_dashboard_layout: { vertical: "cyber", layout: CYBER },
    });
  });

  it("drops a reset vertical, and leaves null once none is left", async () => {
    const both = setup({ fraud: FRAUD, cyber: CYBER });
    act(() => both.result.current.mutate({ ctem_dashboard_layout: { vertical: "cyber", layout: null } }));
    await settleMutate();
    expect(both.cached()?.ctem_dashboard_layouts).toEqual({ fraud: FRAUD });

    const last = setup({ cyber: CYBER });
    act(() => last.result.current.mutate({ ctem_dashboard_layout: { vertical: "cyber", layout: null } }));
    await settleMutate();
    expect(last.cached()?.ctem_dashboard_layouts).toBeNull();
  });

  it("still patches the other preferences as sent", async () => {
    const { result, cached } = setup(null);
    act(() => result.current.mutate({ ctem_vertical: "fraud" }));
    await settleMutate();
    expect(cached()?.ctem_vertical).toBe("fraud");
    expect(cached()?.ctem_dashboard_layouts).toBeNull();
  });
});
