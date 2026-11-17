// The expand state of the chat sidebar's workspace groups (#9179): every group
// collapsed on a first visit, the groups the person opens stored on their
// profile (and in this browser as the fallback), and restored on the next one.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { testQueryClient, withProviders } from "@/testing/providers";
import { EXPANDED_STORAGE_KEY, useSidebarWorkspaces } from "./useSidebarWorkspaces";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const auth = vi.hoisted(() => ({ user: { id: "me" } as Record<string, unknown> }));
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: auth.user, isAdmin: false }),
}));

vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAgenticAIEnabled: () => true,
}));

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);

function renderSidebar() {
  return renderHook(() => useSidebarWorkspaces(), { wrapper: withProviders(testQueryClient()) });
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  auth.user = { id: "me" };
  get.mockResolvedValue([]);
  put.mockResolvedValue({ status: "ok" });
});

describe("useSidebarWorkspaces expand state", () => {
  it("starts with every group collapsed on a first visit", () => {
    const { result } = renderSidebar();
    expect([...result.current.expanded]).toEqual([]);
  });

  it("stores an opened group on the profile and in the browser", async () => {
    const { result } = renderSidebar();
    act(() => result.current.toggle("ws-a"));
    act(() => result.current.toggle("ws-b"));

    expect([...result.current.expanded]).toEqual(["ws-a", "ws-b"]);
    expect(JSON.parse(window.localStorage.getItem(EXPANDED_STORAGE_KEY) ?? "null")).toEqual(["ws-a", "ws-b"]);
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith("/settings/preferences", {
        chat_sidebar_expanded_workspaces: ["ws-a", "ws-b"],
      }),
    );

    act(() => result.current.toggle("ws-a"));
    expect([...result.current.expanded]).toEqual(["ws-b"]);
  });

  it("restores the groups the profile holds, over the browser's copy", () => {
    window.localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify(["ws-old"]));
    auth.user = { id: "me", chat_sidebar_expanded_workspaces: ["ws-a", "ws-b"] };
    const { result } = renderSidebar();
    expect([...result.current.expanded]).toEqual(["ws-a", "ws-b"]);
  });

  it("falls back to the browser's copy when the profile holds none", () => {
    window.localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify(["ws-a"]));
    const { result } = renderSidebar();
    expect([...result.current.expanded]).toEqual(["ws-a"]);
  });

  it("opens a group once, and writes nothing when it already is", async () => {
    auth.user = { id: "me", chat_sidebar_expanded_workspaces: ["ws-a"] };
    const { result } = renderSidebar();
    act(() => result.current.setGroupOpen("ws-a", true));
    expect(put).not.toHaveBeenCalled();
    act(() => result.current.setGroupOpen("ws-new", true));
    expect([...result.current.expanded]).toEqual(["ws-a", "ws-new"]);
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  });
});
