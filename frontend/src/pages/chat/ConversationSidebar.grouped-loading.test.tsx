// The workspace-grouped sidebar while the conversations' first read is in
// flight: the group headers stay mounted and the skeleton sits under
// NOT IN A WORKSPACE, so the list does not jump when it lands.

import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { ConversationListItem } from "@/types";
import { ConversationSidebar } from "./ConversationSidebar";

const WORKSPACE = {
  id: "ws-general",
  name: "General",
  user_id: "u-1",
  is_default: true,
  is_archived: false,
  can_manage: true,
  // As `useSidebarWorkspaces` serves it, parsed for @filigran/chatbot's grouping too.
  isOwn: true,
  isDefault: true,
  canManage: true,
};

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ isAdmin: false, user: { id: "u-1" } }),
}));

vi.mock("@/pages/chat/useSidebarWorkspaces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/pages/chat/useSidebarWorkspaces")>()),
  useSidebarWorkspaces: () => ({
    enabled: true,
    workspaces: [WORKSPACE],
    fileable: [WORKSPACE],
    expanded: new Set<string>(["ws-general"]),
    toggle: () => {},
    setGroupOpen: () => {},
    createWorkspace: async () => null,
    renameWorkspace: async () => {},
    fileConversation: async () => {},
    filingConfirm: null,
  }),
}));

const noop = () => {};

function sidebar(props: { conversations: ConversationListItem[] | undefined; conversationsLoading?: boolean }) {
  return (
    <MemoryRouter>
    <ConversationSidebar
      {...props}
      activeConvId={null}
      selectedAgent={null}
      agents={[]}
      showSidebar
      onSelectConversation={noop}
      onNewConversation={noop}
      onArchive={noop}
      onRename={noop}
      onClearAll={async () => {}}
      onSelectAgent={noop}
    />
    </MemoryRouter>
  );
}

describe("ConversationSidebar grouped by workspace while the list loads", () => {
  it("keeps the group headers and puts the skeleton under Not in a workspace", () => {
    const { container } = renderWithProviders(sidebar({ conversations: undefined, conversationsLoading: true }));

    const general = container.querySelector<HTMLElement>('[data-workspace-group="ws-general"]');
    expect(general).not.toBeNull();
    expect(within(general as HTMLElement).getByRole("button", { expanded: true, name: /General/ })).toBeTruthy();
    expect(screen.getByText("Not in a workspace")).toBeTruthy();

    const unfiled = container.querySelector<HTMLElement>('[data-workspace-group="unfiled"]');
    expect(unfiled).not.toBeNull();
    const loading = within(unfiled as HTMLElement).getByTestId("conversation-list-loading");
    expect(loading.getAttribute("role")).toBe("status");
    expect(screen.getAllByTestId("conversation-list-loading")).toHaveLength(1);

    // No count and no empty line before the list is read.
    expect(screen.queryByText("No conversations yet")).toBeNull();
    expect(screen.queryByText("Drop a conversation here, or start one with +.")).toBeNull();
    expect(within(unfiled as HTMLElement).queryByText("0")).toBeNull();
  });

  it("drops the skeleton and draws the counts once the list is read", () => {
    renderWithProviders(sidebar({ conversations: [], conversationsLoading: false }));

    expect(screen.queryByTestId("conversation-list-loading")).toBeNull();
    expect(screen.getByText("No conversations yet")).toBeTruthy();
  });
});
