// What the host module sets on every surface's panel: the platform's names
// for its tools, a reply's working files behind one line, the agent's page
// behind the header's name, the upload route's limit on each file, and the
// waiting game's gamepad in the reasoning window, hidden with the game.

import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ChatPanelProps, ChatToolLabels } from "@filigran/chatbot";

import { XtmChatPanel } from "@/components/chat-panel/XtmChatPanel";
import { APP_LOCKED_EVENT } from "@/lib/app-lock";
import { MAX_CHAT_ATTACHMENTS_PER_MESSAGE, MAX_CHAT_UPLOAD_SIZE_BYTES } from "@/lib/constants";
import { testQueryClient, withProviders } from "@/testing/providers";

const seen = vi.hoisted(() => ({ props: null as ChatPanelProps | null }));

vi.mock("@filigran/chatbot", () => ({
  ChatPanel: (props: ChatPanelProps) => {
    seen.props = props;
    return null;
  },
}));
vi.mock("@filigran/chatbot/styles.css", () => ({}));
vi.mock("@/components/arcade/arcade-context", () => ({ useArcadeWaitSignal: () => "wait-1" }));
vi.mock("@/components/chat/ChatWaitingGame", () => ({ ChatWaitingGame: () => null }));
vi.mock("@/components/chat/ReasoningArcadeAction", () => ({
  ReasoningArcadeAction: ({ waitSource }: { waitSource?: string | null }) => <span>gamepad:{waitSource}</span>,
}));
vi.mock("@/components/chat-panel/MessageParts", () => ({ MessageExtras: () => null, MessageFooterActions: () => null }));
vi.mock("@/components/chat-panel/markers", () => ({
  agentSwitchMarker: () => null,
  useAgentSwitchRenderer: () => ({}),
  useMarkerRenderers: () => ({}),
  useTranscriptCards: () => ({ navigateToSlide: () => false, cardContext: {}, revisions: new Map() }),
}));
const { saveProfile } = vi.hoisted(() => ({ saveProfile: vi.fn() }));
vi.mock("@/hooks/api/useAuthApi", () => ({
  useMe: () => ({ data: { full_name: "Ada Lovelace", chat_sidebar_expanded_workspaces: ["w-1"] } }),
  useUpdateProfile: () => ({ mutate: saveProfile }),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ isAdmin: false }) }));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => false }));

function renderPanel(props: Partial<Parameters<typeof XtmChatPanel>[0]> = {}) {
  const Providers = withProviders(testQueryClient());
  render(
    <MemoryRouter>
      <Providers>
        <XtmChatPanel {...props} />
      </Providers>
    </MemoryRouter>,
  );
  if (!seen.props) throw new Error("the panel was not rendered");
  return seen.props;
}

const toolLabel = (labels: ChatToolLabels | undefined, name: string) =>
  typeof labels === "function" ? labels(name) : labels?.[name];

describe("XtmChatPanel", () => {
  it("opens the workspace groups the person opened, and keeps the ones they open on their profile", () => {
    const { expandedWorkspaceGroups, onExpandedWorkspaceGroupsChange } = renderPanel();
    expect(expandedWorkspaceGroups).toEqual(["w-1"]);
    onExpandedWorkspaceGroupsChange?.(["w-1", "w-2"]);
    expect(saveProfile).toHaveBeenCalledWith({ chat_sidebar_expanded_workspaces: ["w-1", "w-2"] });
  });

  it("shows the lock screen on a stream's app_locked frame, and hands every frame to the host still", () => {
    const host = vi.fn();
    const locked = vi.fn();
    window.addEventListener(APP_LOCKED_EVENT, locked);
    try {
      const { onStreamEvent } = renderPanel({ onStreamEvent: host });
      onStreamEvent?.({ type: "status", status: "thinking" }, { relayed: false });
      expect(locked).not.toHaveBeenCalled();
      onStreamEvent?.({ type: "app_locked", turn_id: "t-1" }, { relayed: false });
      expect(locked).toHaveBeenCalledTimes(1);
      expect(host).toHaveBeenCalledTimes(2);
      expect(host).toHaveBeenLastCalledWith({ type: "app_locked", turn_id: "t-1" }, { relayed: false });
    } finally {
      window.removeEventListener(APP_LOCKED_EVENT, locked);
    }
  });

  it("names the platform's tools as it does everywhere else", () => {
    const props = renderPanel();
    expect(toolLabel(props.toolLabels, "search_notion")).toBe("Searching Notion");
    expect(toolLabel(props.toolLabels, "a_tool_it_does_not_name")).toBeUndefined();
  });

  it("collapses a reply's working files, not its deliverables", () => {
    const { collapseUnnamedFiles } = renderPanel();
    expect(typeof collapseUnnamedFiles).toBe("function");
    const collapses = collapseUnnamedFiles as Exclude<typeof collapseUnnamedFiles, boolean | undefined>;
    const message = { id: "m-1", role: "assistant", content: "", timestamp: new Date() } as Parameters<typeof collapses>[1];
    expect(collapses({ fileId: "f-1", filename: "check.png", fileTag: "working_file" }, message)).toBe(true);
    expect(collapses({ fileId: "f-2", filename: "report.pdf", fileTag: "download_file" }, message)).toBe(false);
  });

  it("links the header's agent to its page, and nothing for the General Assistant", () => {
    const { agentHref } = renderPanel();
    const agent = (id: string) => ({ id, name: "Analyst", slug: null, icon: null, description: null });
    expect(agentHref?.(agent("a-1"))).toBe("/agents/a-1");
    expect(agentHref?.(agent("general-assistant:"))).toBeUndefined();
  });

  it("draws an agent's named icon as the platform does, and leaves an unknown name to the panel", () => {
    const { renderAgentIcon } = renderPanel();
    const drawn = render(<>{renderAgentIcon?.("Shield", { size: 14 })}</>).container.querySelector("svg");
    expect(drawn).not.toBeNull();
    expect(drawn?.getAttribute("width")).toBe("14");
    expect(drawn?.getAttribute("aria-hidden")).toBe("true");
    expect(renderAgentIcon?.("not-an-icon", { size: 14 })).toBeNull();
  });

  it("limits each file to the upload route's limit, with no total", () => {
    const props = renderPanel();
    expect(props.maxFileSize).toBe(MAX_CHAT_UPLOAD_SIZE_BYTES);
    expect(props.maxTotalSize).toBe(Number.POSITIVE_INFINITY);
  });

  it("takes a batch of screenshots on one message, not the panel's default of 10", () => {
    const props = renderPanel();
    expect(props.maxFileCount).toBe(MAX_CHAT_ATTACHMENTS_PER_MESSAGE);
    expect(MAX_CHAT_ATTACHMENTS_PER_MESSAGE).toBeGreaterThanOrEqual(20);
  });

  it("offers the game in the reasoning window on the surface's wait, unless the game is hidden", () => {
    const shown = renderPanel({ conversationId: "c-1" }).renderReasoningAction?.() as ReactNode;
    expect(render(<>{shown}</>).container.textContent).toBe("gamepad:wait-1");

    const hidden = renderPanel({ conversationId: "c-1", waitingGameHidden: true });
    expect(hidden.renderReasoningAction?.()).toBeNull();
  });
});
