// The /chat page's conversation menu: the workspace knowledge of a filed
// conversation sits beside its filing entry.

import { describe, expect, it, vi } from "vitest";

import { conversationMenuItems } from "@/pages/chat-panel/conversationMenu";

const actions = {
  showFiles: vi.fn(),
  share: vi.fn(),
  continueInChannel: vi.fn(),
  continueWithAgent: vi.fn(),
  exportAs: vi.fn(),
  fileInWorkspace: vi.fn(),
  showWorkspaceKnowledge: vi.fn(),
};
const state = {
  conversationId: "c-1" as string | null,
  workspaceId: null as string | null,
  isReadOnly: false,
  isChannelOnly: false,
  isWebConversation: true,
  turnRunning: false,
  isOrgReadable: false,
  agenticLicensed: true,
};
const ids = (overrides: Partial<typeof state>) =>
  conversationMenuItems({ ...state, ...overrides }, actions, (key) => key).map((item) => item.id);

describe("conversationMenuItems", () => {
  it("offers the workspace knowledge of a filed conversation only", () => {
    expect(ids({})).not.toContain("workspace-knowledge");
    expect(ids({ workspaceId: "ws-1" })).toEqual([
      "conversation-files",
      "share-conversation",
      "continue-in-channel",
      "continue-with-agent",
      "export-markdown",
      "export-pdf",
      "file-conversation",
      "workspace-knowledge",
    ]);
    expect(ids({ workspaceId: "ws-1", conversationId: null })).not.toContain("workspace-knowledge");
  });

  it("opens the drawer from its entry", () => {
    const item = conversationMenuItems({ ...state, workspaceId: "ws-1" }, actions, (key) => key).find(
      (entry) => entry.id === "workspace-knowledge",
    );
    item?.onSelect();
    expect(actions.showWorkspaceKnowledge).toHaveBeenCalledOnce();
  });

  it("continues only the owner's web conversation, between two turns", () => {
    const disabled = (overrides: Partial<typeof state>) =>
      conversationMenuItems({ ...state, ...overrides }, actions, (key) => key).find(
        (entry) => entry.id === "continue-with-agent",
      )?.disabled;
    expect(disabled({})).toBe(false);
    expect(disabled({ isWebConversation: false })).toBe(true);
    expect(disabled({ turnRunning: true })).toBe(true);
    expect(disabled({ isReadOnly: true })).toBe(true);
    expect(disabled({ conversationId: null })).toBe(true);
  });

  it("offers no workspace entry without the agentic licence", () => {
    expect(ids({ workspaceId: "ws-1", agenticLicensed: false })).not.toContain("file-conversation");
    expect(ids({ workspaceId: "ws-1", agenticLicensed: false })).not.toContain("workspace-knowledge");
  });
});
