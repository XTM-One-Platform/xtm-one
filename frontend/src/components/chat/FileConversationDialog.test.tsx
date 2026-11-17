// Filing a conversation asks first when the workspace reaches other people (FR-022).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Workspace } from "@/types";
import { FileConversationDialog } from "./FileConversationDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "me" }, isAdmin: false }),
}));

const get = vi.mocked(api.get);
const patch = vi.mocked(api.patch);

function workspace(id: string, name: string, over: Partial<Workspace> = {}): Workspace {
  return {
    id,
    user_id: "me",
    name,
    slug: id,
    description: "",
    icon: "",
    color: "",
    is_default: false,
    is_archived: false,
    is_company_managed: false,
    shared_group_ids: null,
    shared_group_permissions: null,
    can_manage: true,
    instructions: "",
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue([
    workspace("w-mine", "Mine"),
    workspace("w-team", "Team", { shared_group_ids: ["g-1"] }),
  ]);
  patch.mockResolvedValue({});
});

function renderDialog() {
  return renderWithProviders(
    <FileConversationDialog conversationId="c-1" currentWorkspaceId={null} open onClose={vi.fn()} />,
  );
}

describe("FileConversationDialog", () => {
  it("files into a workspace only the person sees at once", async () => {
    renderDialog();
    fireEvent.click(await screen.findByRole("radio", { name: "Mine" }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/chat/conversations/c-1", { workspace_id: "w-mine" }));
    expect(screen.queryByText("File into a shared workspace?")).toBeNull();
  });

  it("asks before filing into a shared workspace, and files nothing when declined", async () => {
    renderDialog();
    fireEvent.click(await screen.findByRole("radio", { name: "Team" }));
    expect(await screen.findByText("File into a shared workspace?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("File into a shared workspace?")).toBeNull());
    expect(patch).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("radio", { name: "Team" }));
    fireEvent.click(await screen.findByRole("button", { name: "File it" }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/chat/conversations/c-1", { workspace_id: "w-team" }));
  });
});
