// A workspace's own knowledge base: badged on the library pages, and
// never offered by a picker that binds knowledge bases.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { useBindableKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { api } from "@/lib/api";
import { renderWithProviders, testQueryClient, withProviders } from "@/testing/providers";
import { ManagedByWorkspaceBadge } from "./ManagedByWorkspaceBadge";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const get = vi.mocked(api.get);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("workspace-owned knowledge bases", () => {
  it("are left out of the bindable list", async () => {
    get.mockResolvedValue([
      { id: "kb-1", name: "Threat intel", managed_by_workspace: null },
      { id: "kb-2", name: "Launch — Sources", managed_by_workspace: { id: "w-1", name: "Launch" } },
    ]);
    const { result } = renderHook(() => useBindableKnowledgeBases(), { wrapper: withProviders(testQueryClient()) });
    await waitFor(() => expect(result.current.data?.map((kb) => kb.id)).toEqual(["kb-1"]));
  });

  it("carry a badge linking to their workspace", () => {
    renderWithProviders(
      <MemoryRouter>
        <ManagedByWorkspaceBadge workspace={{ id: "w-1", name: "Launch" }} link />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: "Managed by workspace Launch" });
    expect(link.getAttribute("href")).toBe("/tasks/workspaces/w-1");
  });
});
